import { Pool } from "pg";

// The ledger is Postgres (Supabase). proxy/db.py and treasury/db.py are the only
// writers; the dashboard only ever reads.
//
// Every exported function here is async, which the better-sqlite3 version was not.
// That is not a style choice — node-postgres has no synchronous API, and there is no
// version of "read a hosted database" that does not go over the network.
//
// On Vercel, point DATABASE_URL at Supabase's *transaction* pooler (port 6543), not the
// direct connection. A serverless function gets frozen and thawed rather than shut down,
// so direct connections accumulate until the database refuses new ones. The transaction
// pooler exists for exactly that shape, and `pg` needs no special configuration for it
// beyond not using prepared statements — which this file does not.
const CONNECTION_STRING = process.env.DATABASE_URL ?? "";

// Same schema the proxy writes into. Defaults to `public`; the test suites and the
// scratch harnesses override it, and pointing the dashboard at one of those is the
// easiest way to look at a harness run.
const DB_SCHEMA = process.env.DB_SCHEMA ?? "public";

// Next reloads modules on every edit in dev, and a fresh Pool per reload leaks
// connections until Postgres starts refusing them. Stashing it on globalThis is the
// documented way to survive that.
const globalForPg = globalThis as unknown as { meterPool?: Pool | null };

function getPool(): Pool | null {
  if (globalForPg.meterPool !== undefined) return globalForPg.meterPool;
  if (!CONNECTION_STRING) {
    globalForPg.meterPool = null;
    return null;
  }
  // On a serverless platform every concurrent function instance gets its own module
  // scope, so `globalThis` de-duplicates the pool within an instance and not across
  // them. A pool of 4 therefore means 4 x (number of live instances), and Supabase's
  // transaction pooler caps CLIENTS at 200 across everything — this dashboard, the
  // proxy, and anyone's local dev server. We hit exactly that: every database-backed
  // route started returning 500 with `EMAXCONN: max client connections reached,
  // limit: 200`, while the static route kept serving, which reads like a broken app
  // rather than an exhausted connection budget.
  //
  // One connection per instance is the right number where a function handles one
  // request at a time; a larger pool buys nothing and multiplies across instances.
  const serverless = Boolean(process.env.VERCEL || process.env.AWS_LAMBDA_FUNCTION_NAME);
  globalForPg.meterPool = new Pool({
    connectionString: CONNECTION_STRING,
    // Small on purpose. The dashboard polls; it does not need to hold connections the
    // proxy could be using, and Supabase's connection budget is shared between them.
    max: Number(process.env.DASHBOARD_DB_POOL_MAX ?? (serverless ? 1 : 4)),
    // Serverless instances are frozen rather than shut down, so an idle connection can
    // be held long after the request that opened it. Release quickly there; a long-lived
    // local server can afford to keep them warm.
    idleTimeoutMillis: serverless ? 5_000 : 30_000,
    connectionTimeoutMillis: 10_000,
    // Pins search_path for every connection this pool opens, so unqualified table names
    // resolve to the same schema the proxy wrote into.
    options: `-c search_path=${DB_SCHEMA}`,
  });
  // A pool that emits 'error' with no listener takes the process down. A dead idle
  // connection is not a reason to kill the dashboard.
  globalForPg.meterPool.on("error", () => {});
  return globalForPg.meterPool;
}

export async function query<T>(sql: string, params: unknown[] = []): Promise<T[]> {
  const pool = getPool();
  if (!pool) return [];
  const result = await pool.query(sql, params);
  return result.rows as T[];
}

// The treasury tables (wallets/mandates/treasury_events) are created at boot by
// proxy/app.py's lifespan, but a database the proxy has never connected to will not have
// them — and neither will one where only the treasury scripts have run. Checking beats
// catching, since the card wants to distinguish "not set up yet" from "set up and empty".
//
// Cached per process: this sits under a 3-second poll and the answer only changes when
// someone restarts the proxy.
const tableCache = new Map<string, boolean>();

async function tableExists(name: string): Promise<boolean> {
  const cached = tableCache.get(name);
  if (cached !== undefined) return cached;
  const rows = await query<{ one: number }>(
    `SELECT 1 AS one
       FROM information_schema.tables
      WHERE table_schema = current_schema() AND table_name = $1`,
    [name],
  );
  const found = rows.length > 0;
  tableCache.set(name, found);
  return found;
}

// Same reasoning one level down. The prediction columns are added to `requests` by an
// ALTER in proxy/db.py's connect(), so a database whose proxy has not been restarted
// since that shipped has the table but not the columns — and SELECTing a missing column
// throws rather than returning null.
const columnCache = new Map<string, boolean>();

async function columnExists(table: string, column: string): Promise<boolean> {
  const cacheKey = `${table}.${column}`;
  const cached = columnCache.get(cacheKey);
  if (cached !== undefined) return cached;
  const rows = await query<{ one: number }>(
    `SELECT 1 AS one
       FROM information_schema.columns
      WHERE table_schema = current_schema()
        AND table_name = $1 AND column_name = $2`,
    [table, column],
  );
  const found = rows.length > 0;
  columnCache.set(cacheKey, found);
  return found;
}

/**
 * Postgres returns `numeric` and `bigint` as strings, because both can hold values a
 * JavaScript number cannot represent exactly. Every money and count column here is
 * `double precision` or `integer`, which the driver does give back as numbers — but
 * `SUM()` and `COUNT()` promote to `numeric` and `bigint`, so aggregates come back as
 * strings anyway. Formatting a string with `.toFixed()` throws; comparing one with `>`
 * compares lexically and gets the wrong answer.
 *
 * Every aggregate below is passed through this on the way out.
 */
function num(value: unknown): number {
  if (value === null || value === undefined) return 0;
  return typeof value === "number" ? value : Number(value);
}

/** As `num`, but preserves null — some callers distinguish "absent" from zero. */
function numOrNull(value: unknown): number | null {
  if (value === null || value === undefined) return null;
  return typeof value === "number" ? value : Number(value);
}

/**
 * A view of the ledger: the team's, or one judge's.
 *
 * `null` is the public Control Room — every project except judge sessions, because a
 * stranger's six-call demo must not distort the numbers that page shows in public. A
 * project id is one judge's private view of *the same page*: identical components,
 * identical queries, their rows.
 *
 * That symmetry is the point. The judge view is not a second dashboard to keep in sync
 * with this one; it is this one, with a WHERE clause.
 */
export type Scope = string | null;

/**
 * The scope predicate, plus the parameter it needs.
 *
 * The parameter is appended at the END of each query's list rather than the front, so
 * every existing `$1`, `$2` … keeps its number and no call site has to be renumbered —
 * which is the kind of edit that transposes a parameter and is not caught by types.
 */
/**
 * As `scoped`, but the public view includes judge sessions rather than excluding them.
 *
 * Used by the two live panels — the Request Ledger and the Treasurer Agent — because
 * there the whole point is that something is happening *now*. A judge running the console
 * against their own Prava merchant key produces a real settled charge on real rails, and
 * that is the single most convincing thing this dashboard can show. Their call costs
 * $0.000033 against $2.38 of history, so nothing it touches is distorted.
 *
 * The aggregate panels still use `scoped`: Team Budget would gain a card per session, and
 * Team Spend and Cost per Outcome group by project, so judges would dilute the 1,300-call
 * story those panels exist to tell.
 */
function scopedIncludingJudges(scope: Scope, nextParam: number, column = "project_id") {
  return scope === null
    ? { sql: "TRUE", params: [] as unknown[] }
    : { sql: `${column} = $${nextParam}`, params: [scope] as unknown[] };
}

function scoped(scope: Scope, nextParam: number, column = "project_id") {
  return scope === null
    ? { sql: `${column} NOT LIKE 'judge-%'`, params: [] as unknown[] }
    : { sql: `${column} = $${nextParam}`, params: [scope] as unknown[] };
}

export type SpendRow = {
  project_id: string;
  actor: string | null;
  feature: string | null;
  total_cost_usd: number;
  request_count: number;
};

export async function getTeamSpend(scope: Scope = null): Promise<SpendRow[]> {
  if (!(await tableExists("requests"))) return [];
  const s = scoped(scope, 1);
  const rows = await query<SpendRow>(
    `SELECT project_id,
            actor,
            feature,
            SUM(cost_usd) AS total_cost_usd,
            COUNT(*)      AS request_count
       FROM requests
      WHERE ${s.sql}
      GROUP BY project_id, actor, feature
      ORDER BY total_cost_usd DESC`,
    s.params,
  );
  return rows.map((r) => ({
    ...r,
    total_cost_usd: num(r.total_cost_usd),
    request_count: num(r.request_count),
  }));
}

/**
 * Whether the request ledger is usable — we can reach the database *and* it has the
 * `requests` table. Both halves matter: `treasury/db.py` can create the treasury tables
 * in a database the proxy has never touched, so running any treasury script first leaves
 * a database that connects fine but has nothing to meter.
 */
export async function isLedgerAvailable(): Promise<boolean> {
  if (!getPool()) return false;
  try {
    return await tableExists("requests");
  } catch {
    // An unreachable database is "no ledger", not a 500. The page renders its
    // "start the proxy" banner instead of failing to render at all.
    return false;
  }
}

export type SpendSummary = {
  total_cost_usd: number;
  request_count: number;
  actor_count: number;
  provider_count: number;
  token_count: number;
  last_ts: string | null;
};

/**
 * Circuit-breaker state for the nav pill, read from the same `breaker_events` rows
 * `db.active_breaker()` uses — an open breaker is one with `closed_at IS NULL`.
 *
 * Real state rather than a decorative "Normal": a status pill that cannot say
 * anything except "fine" is worse than no pill, because it reads as a check that
 * ran and passed.
 */
export type BreakerState = {
  open: boolean;
  mode: string | null;
  scope: string | null;
  count: number;
};

export async function getBreakerState(scope: Scope = null): Promise<BreakerState> {
  const idle: BreakerState = { open: false, mode: null, scope: null, count: 0 };
  if (!(await tableExists("breaker_events"))) return idle;
  const rows = await query<{ scope: string; mode: string }>(
    // `breaker_events.scope` is "project:feature", so the project test is a prefix
    // match on that column rather than an equality on `project_id`.
    scope === null
      ? `SELECT scope, mode FROM breaker_events
          WHERE closed_at IS NULL AND scope NOT LIKE 'judge-%'
          ORDER BY id DESC`
      : `SELECT scope, mode FROM breaker_events
          WHERE closed_at IS NULL AND scope LIKE $1
          ORDER BY id DESC`,
    scope === null ? [] : [`${scope}:%`],
  );
  if (rows.length === 0) return idle;
  // Revoke outranks throttle: it is the more severe of the two, and if any key is
  // cut that is the fact the pill should be reporting.
  const revoked = rows.find((r) => r.mode === "revoke");
  const lead = revoked ?? rows[0];
  return {
    open: true,
    mode: lead.mode,
    scope: lead.scope,
    count: rows.length,
  };
}

/** The headline figure. A single number is a stat, not a chart. */
export async function getSpendSummary(scope: Scope = null): Promise<SpendSummary> {
  const s = scoped(scope, 1);
  const empty: SpendSummary = {
    total_cost_usd: 0,
    request_count: 0,
    actor_count: 0,
    provider_count: 0,
    token_count: 0,
    last_ts: null,
  };
  if (!(await tableExists("requests"))) return empty;
  const rows = await query<SpendSummary>(
    `SELECT COALESCE(SUM(cost_usd), 0) AS total_cost_usd,
            COUNT(*)                   AS request_count,
            COUNT(DISTINCT actor)      AS actor_count,
            COUNT(DISTINCT provider)   AS provider_count,
            COALESCE(SUM(input_tokens + output_tokens), 0) AS token_count,
            MAX(ts)                    AS last_ts
       FROM requests
      WHERE ${s.sql}`,
    s.params,
  );
  const row = rows[0];
  if (!row) return empty;
  return {
    total_cost_usd: num(row.total_cost_usd),
    request_count: num(row.request_count),
    actor_count: num(row.actor_count),
    provider_count: num(row.provider_count),
    token_count: num(row.token_count),
    last_ts: row.last_ts ?? null,
  };
}

export type WalletRow = {
  id: string;
  project_id: string;
  provider: string;
  balance_usd: number;
  updated_at: string;
};

/**
 * Provider balances, mirroring treasury.db.list_wallets() — same ordering, so the
 * card and the /wallets route can never disagree about row order.
 *
 * Returns null (rather than []) when the treasury tables have not been created yet,
 * so the card can say "run the proxy" instead of "you have no money".
 */
export async function getProviderBalances(scope: Scope = null): Promise<WalletRow[] | null> {
  if (!(await tableExists("wallets"))) return null;
  const s = scoped(scope, 1);
  const rows = await query<WalletRow>(
    `SELECT id, project_id, provider, balance_usd, updated_at
       FROM wallets
      WHERE ${s.sql}
      ORDER BY project_id, provider`,
    s.params,
  );
  return rows.map((r) => ({ ...r, balance_usd: num(r.balance_usd) }));
}

// ── Budgets ──────────────────────────────────────────────────────────────────

/**
 * The ceiling window, mirroring proxy/config.py's BUDGET_WINDOW_S (default 86400).
 * Read from the environment for the same reason the proxy does: if someone shortens
 * the window there, a dashboard hard-coding 24h would quietly show a different number
 * than the one being enforced.
 *
 * Note this is a *rolling* window, not a calendar day — the column is named
 * `ceiling_usd_day` but proxy/budget.py compares against `now - BUDGET_WINDOW_S`, and
 * the 429 it returns says "in the last 24h". The card has to say the same thing.
 */
const BUDGET_WINDOW_S = Number(process.env.BUDGET_WINDOW_S ?? 86_400);

/**
 * The proxy writes `ts` with strftime("%Y-%m-%dT%H:%M:%S.%f+00:00") and stores it as
 * TEXT — that did not change with the move to Postgres, and the comparison is still
 * textual, so the cutoff has to be byte-identical in shape or it is wrong.
 * `Date.toISOString()` is not: it emits 3 fractional digits and a "Z", so a stored
 * "...123456+00:00" sorts *below* a cutoff of "...123Z" ("4" < "Z") and rows inside the
 * window get dropped. Same format in, same rows out.
 */
export function isoNow(): string {
  return isoSecondsAgo(0);
}

function isoSecondsAgo(seconds: number): string {
  const then = new Date(Date.now() - seconds * 1000);
  const micros = String(then.getUTCMilliseconds()).padStart(3, "0") + "000";
  return then.toISOString().replace(/\.\d+Z$/, `.${micros}+00:00`);
}

export type BudgetScope = {
  project_id: string;
  /** null is the project-wide ceiling — every feature plus untagged traffic. */
  feature: string | null;
  ceiling_usd: number;
  spend_usd: number;
  /**
   * Distinct actors who spent against this scope in the window, most-spend first.
   * Drives the avatar stack. Real ledger attribution, not decoration — the faces
   * on a budget card are the people who consumed it.
   */
  members: string[];
};

/**
 * Daily ceilings and the spend measured against them.
 *
 * Returns null when no ceilings are configured at all, which is the normal state for a
 * deployment with no meter.yaml — distinct from "configured and at zero spend". The
 * card must not render an absent budget as $0.00 of $0.00, which reads as catastrophically
 * over budget when it means the opposite.
 *
 * Two things this deliberately does NOT do:
 *
 * 1. It counts *settled* spend only. proxy/budget.py authorizes against settled + held,
 *    but holds live in that process's memory and never reach the database (by design — a
 *    restart is supposed to drop them). So this can read a shade under what is actually
 *    being enforced, for the seconds a request is in flight. The card footnotes it.
 * 2. It does not re-sort. Scopes come back in meter.yaml order (see the sort_order note
 *    below) so rows never move while someone is watching them.
 */
export async function getBudgets(scope: Scope = null): Promise<BudgetScope[] | null> {
  const [hasProjects, hasFeatureBudgets] = await Promise.all([
    tableExists("projects"),
    tableExists("feature_budgets"),
  ]);
  if (!hasProjects && !hasFeatureBudgets) return null;

  // ORDER BY sort_order is meter.yaml order. proxy/db.py's replace_budgets() clears both
  // tables and re-inserts every ceiling in file order on each boot, stamping its
  // position as it goes. Under SQLite this read `rowid` and relied on insertion order;
  // Postgres has no rowid and `ctid` is a physical address VACUUM may move, so the
  // ordering is an explicit column now.
  //
  // Guarded on the column existing, for the same reason the prediction columns are: a
  // database whose proxy has not restarted since that migration has the table but not
  // the column, and ORDER BY a missing column throws rather than being ignored. That is
  // not hypothetical — it 500'd this page the first time it was pointed at a schema
  // seeded before the migration. `id`/`feature` is the fallback ordering: arbitrary, but
  // stable across reloads, which is the property the cards actually need.
  //
  // NULLS LAST covers the other half — migrated but not yet re-seeded, where the column
  // exists and is still NULL. Those rows sort to the end instead of ahead of everything.
  const [hasProjectOrder, hasFeatureOrder] = await Promise.all([
    hasProjects ? columnExists("projects", "sort_order") : false,
    hasFeatureBudgets ? columnExists("feature_budgets", "sort_order") : false,
  ]);

  const projectCeilings = hasProjects
    ? await query<{ project_id: string; ceiling_usd_day: number }>(
        // Scoping the two ceiling queries scopes the whole function: every spend query
        // below filters on `project_id = ANY(...)` built from these lists, so they
        // follow automatically.
        `SELECT id AS project_id, ceiling_usd_day
           FROM projects
          WHERE ceiling_usd_day IS NOT NULL
            AND ${scoped(scope, 1, "id").sql}
          ORDER BY ${hasProjectOrder ? "sort_order NULLS LAST, " : ""}id`,
        scoped(scope, 1, "id").params,
      )
    : [];

  const featureCeilings = hasFeatureBudgets
    ? await query<{
        project_id: string;
        feature: string;
        ceiling_usd_day: number;
      }>(
        `SELECT project_id, feature, ceiling_usd_day
           FROM feature_budgets
          WHERE ceiling_usd_day IS NOT NULL
            AND ${scoped(scope, 1).sql}
          ORDER BY ${hasFeatureOrder ? "sort_order NULLS LAST, " : ""}feature`,
        scoped(scope, 1).params,
      )
    : [];

  if (projectCeilings.length === 0 && featureCeilings.length === 0) return null;
  // Ceilings exist but there is nothing to measure them against yet.
  if (!(await tableExists("requests"))) return [];

  const cutoff = isoSecondsAgo(BUDGET_WINDOW_S);

  /*
   * Four grouped statements for the whole board, not two per scope.
   *
   * This used to run projectSpend + projectMembers per project and featureSpend +
   * featureMembers per feature, awaited inside a `for` loop — so the round trips
   * were serial across scopes. With the 19 scopes meter.yaml currently declares
   * that is 38 sequential trips to a hosted database at ~50ms each, on a page that
   * also holds one of only four pooled connections for the duration. It was by far
   * the heaviest thing the dashboard did, and it got worse every time someone added
   * a ceiling.
   *
   * The arithmetic per scope is unchanged and still mirrors proxy/db.py's
   * project_window_spend() and window_spend() — the card and the 429 a developer
   * just got have to agree about who is over budget. Only the grouping moved into
   * the database.
   */
  const projectIds = projectCeilings.map((p) => p.project_id);
  const featureProjectIds = [...new Set(featureCeilings.map((f) => f.project_id))];
  const featureNames = [...new Set(featureCeilings.map((f) => f.feature))];

  const [projectSpendRows, projectMemberRows, featureSpendRows, featureMemberRows] =
    await Promise.all([
      projectIds.length
        ? query<{ project_id: string; spend: number }>(
            `SELECT project_id, COALESCE(SUM(cost_usd), 0) AS spend
               FROM requests
              WHERE project_id = ANY($1) AND ts >= $2
              GROUP BY project_id`,
            [projectIds, cutoff],
          )
        : Promise.resolve([]),
      // Actors ordered by their spend against the scope, so the avatar stack shows
      // who is actually consuming the budget rather than whoever sorts first.
      projectIds.length
        ? query<{ project_id: string; actor: string }>(
            `SELECT project_id, actor
               FROM requests
              WHERE project_id = ANY($1) AND ts >= $2 AND actor IS NOT NULL
              GROUP BY project_id, actor
              ORDER BY project_id, SUM(cost_usd) DESC`,
            [projectIds, cutoff],
          )
        : Promise.resolve([]),
      // Filtering on both lists can match (project, feature) pairs that no ceiling
      // declares. Harmless — results are read back by exact key below, so an extra
      // group is simply never looked up.
      featureCeilings.length
        ? query<{ project_id: string; feature: string; spend: number }>(
            `SELECT project_id, feature, COALESCE(SUM(cost_usd), 0) AS spend
               FROM requests
              WHERE project_id = ANY($1) AND feature = ANY($2) AND ts >= $3
              GROUP BY project_id, feature`,
            [featureProjectIds, featureNames, cutoff],
          )
        : Promise.resolve([]),
      featureCeilings.length
        ? query<{ project_id: string; feature: string; actor: string }>(
            `SELECT project_id, feature, actor
               FROM requests
              WHERE project_id = ANY($1) AND feature = ANY($2) AND ts >= $3
                AND actor IS NOT NULL
              GROUP BY project_id, feature, actor
              ORDER BY project_id, feature, SUM(cost_usd) DESC`,
            [featureProjectIds, featureNames, cutoff],
          )
        : Promise.resolve([]),
    ]);

  // ORDER BY above fixes the actor order; these preserve it, because Map keeps
  // insertion order and the rows arrive already sorted by spend.
  const key = (p: string, f: string) => `${p}\0${f}`;

  const projectSpendBy = new Map(
    projectSpendRows.map((r) => [r.project_id, num(r.spend)]),
  );
  const featureSpendBy = new Map(
    featureSpendRows.map((r) => [key(r.project_id, r.feature), num(r.spend)]),
  );

  const projectMembersBy = new Map<string, string[]>();
  for (const r of projectMemberRows) {
    const list = projectMembersBy.get(r.project_id);
    if (list) list.push(r.actor);
    else projectMembersBy.set(r.project_id, [r.actor]);
  }

  const featureMembersBy = new Map<string, string[]>();
  for (const r of featureMemberRows) {
    const k = key(r.project_id, r.feature);
    const list = featureMembersBy.get(k);
    if (list) list.push(r.actor);
    else featureMembersBy.set(k, [r.actor]);
  }

  // Project order comes from `projects`; a project that only has feature ceilings still
  // has to appear, so it is appended in the order its features were declared.
  const order: string[] = projectCeilings.map((p) => p.project_id);
  for (const f of featureCeilings) {
    if (!order.includes(f.project_id)) order.push(f.project_id);
  }

  // Assembly is pure bookkeeping now — no awaits in this loop. Order still comes
  // from meter.yaml (see the sort_order note above) and is never re-sorted.
  const scopes: BudgetScope[] = [];
  for (const projectId of order) {
    const ceiling = projectCeilings.find((p) => p.project_id === projectId);
    if (ceiling) {
      scopes.push({
        project_id: projectId,
        feature: null,
        ceiling_usd: num(ceiling.ceiling_usd_day),
        // A scope with no requests in the window has no row in the grouped result;
        // that is zero spend, not missing data.
        spend_usd: projectSpendBy.get(projectId) ?? 0,
        members: projectMembersBy.get(projectId) ?? [],
      });
    }
    for (const f of featureCeilings.filter((x) => x.project_id === projectId)) {
      const k = key(projectId, f.feature);
      scopes.push({
        project_id: projectId,
        feature: f.feature,
        ceiling_usd: num(f.ceiling_usd_day),
        spend_usd: featureSpendBy.get(k) ?? 0,
        members: featureMembersBy.get(k) ?? [],
      });
    }
  }
  return scopes;
}

// ── Headline metrics ─────────────────────────────────────────────────────────

/**
 * The Treasurer's own burn window and trigger, mirrored from treasury/config.py
 * (TREASURER_BURN_WINDOW_S, TREASURER_TOPUP_WHEN_HOURS). Read from the environment
 * for the same reason BUDGET_WINDOW_S is: if someone retunes the Treasurer, a
 * dashboard hard-coding the old figures would show a runway the agent disagrees
 * with, and the panel below it would be topping up for reasons the card denies.
 */
const BURN_WINDOW_S = Number(process.env.TREASURER_BURN_WINDOW_S ?? 3_600);
const RUNWAY_TRIGGER_HOURS = Number(
  process.env.TREASURER_TOPUP_WHEN_HOURS ?? 0.75,
);

export type HeadlineMetrics = {
  /** Spend in the trailing ceiling window, and the window before it. */
  spend_window_usd: number;
  spend_prev_window_usd: number;
  /** Fractional change vs the previous window. null when there is no prior spend
   *  to compare against — a first-ever window has no percentage, and rendering
   *  "+100%" for it would invent a trend out of an empty baseline. */
  spend_delta: number | null;
  request_count_window: number;

  /** Project-level ceilings only. See the note in getHeadlineMetrics. */
  budget_ceiling_usd: number | null;
  budget_spend_usd: number;

  /** Hours until the first wallet reaches zero at the current burn rate.
   *  null means no measurable burn, which is infinite runway, not zero. */
  runway_hours: number | null;
  runway_provider: string | null;
  runway_trigger_hours: number;
  burn_usd_per_hour: number;
  /** Which trailing window the burn rate was measured over, so the card can say so. */
  burn_basis_s: number | null;
};

/**
 * The four numbers across the top of the page.
 *
 * Two things here are easy to get wrong and are done deliberately:
 *
 * 1. **The budget total sums project ceilings only.** Feature ceilings sit *inside*
 *    their project's ceiling — meter.yaml declares a $3.00 project cap and then
 *    caps individual features within it — so adding all 19 scopes together would
 *    report a budget several times the real one, and a headroom figure nobody is
 *    entitled to. Only rows with no feature are counted.
 * 2. **Runway mirrors treasury/treasurer.py::assess verbatim**: burn is project
 *    spend over TREASURER_BURN_WINDOW_S divided by that window in hours, and
 *    runway is balance / burn. The card reports the *minimum* across wallets —
 *    the one that dies first is the one that needs a decision. A second formula
 *    here would mean the card and the agent disagree about when to act.
 */
export async function getHeadlineMetrics(scope: Scope = null): Promise<HeadlineMetrics> {
  const empty: HeadlineMetrics = {
    spend_window_usd: 0,
    spend_prev_window_usd: 0,
    spend_delta: null,
    request_count_window: 0,
    budget_ceiling_usd: null,
    budget_spend_usd: 0,
    runway_hours: null,
    runway_provider: null,
    runway_trigger_hours: RUNWAY_TRIGGER_HOURS,
    burn_usd_per_hour: 0,
    burn_basis_s: null,
  };
  if (!(await tableExists("requests"))) return empty;

  const cutoff = isoSecondsAgo(BUDGET_WINDOW_S);

  const [hasProjects, hasWallets] = await Promise.all([
    tableExists("projects"),
    tableExists("wallets"),
  ]);

  const [windowRows, prevRows, ceilingRows, walletRows] = await Promise.all([
    // All time, not a rolling window.
    //
    // The hero number used to be "last 24h", which is the right frame for an operations
    // screen watched during a working day and the wrong one for a page a stranger opens
    // whenever they happen to. Judging here runs for two weeks: any rolling window empties
    // overnight and the first number on the page becomes $0.00, which reads as a product
    // nobody uses rather than as a quiet Tuesday.
    //
    // The budget bar below still uses the real 24h window, because that one IS the ceiling
    // and "0% of today" is a true thing to say. Only the summary widened.
    query<{ spend: number; requests: number }>(
      `SELECT COALESCE(SUM(cost_usd), 0) AS spend, COUNT(*) AS requests
         FROM requests WHERE ${scoped(scope, 1).sql}`,
      scoped(scope, 1).params,
    ),
    // Kept as the trailing 24h figure the budget bar reports against, not as a delta:
    // there is no "previous window" to compare an all-time total to.
    query<{ spend: number }>(
      `SELECT COALESCE(SUM(cost_usd), 0) AS spend
         FROM requests WHERE ts >= $1 AND ${scoped(scope, 2).sql}`,
      [cutoff, ...scoped(scope, 2).params],
    ),
    hasProjects
      ? query<{ project_id: string; ceiling_usd_day: number }>(
          `SELECT id AS project_id, ceiling_usd_day
             FROM projects WHERE ceiling_usd_day IS NOT NULL
               AND ${scoped(scope, 1, "id").sql}`,
          scoped(scope, 1, "id").params,
        )
      : Promise.resolve([]),
    hasWallets
      ? query<{ project_id: string; provider: string; balance_usd: number }>(
          `SELECT project_id, provider, balance_usd FROM wallets
        WHERE ${scoped(scope, 1).sql}`,
      scoped(scope, 1).params,
        )
      : Promise.resolve([]),
  ]);

  const spend = num(windowRows[0]?.spend);
  const prevSpend = num(prevRows[0]?.spend);

  // Spend against the capped projects specifically, not all traffic — an uncapped
  // project's spend is real but is not measured against this ceiling.
  let budgetSpend = 0;
  let budgetCeiling: number | null = null;
  if (ceilingRows.length > 0) {
    budgetCeiling = ceilingRows.reduce(
      (sum, r) => sum + num(r.ceiling_usd_day),
      0,
    );
    const ids = ceilingRows.map((r) => r.project_id);
    const spendRows = await query<{ spend: number }>(
      `SELECT COALESCE(SUM(cost_usd), 0) AS spend
         FROM requests WHERE project_id = ANY($1) AND ts >= $2`,
      [ids, cutoff],
    );
    budgetSpend = num(spendRows[0]?.spend);
  }

  // Runway, per wallet, minimum wins.
  //
  // Burn is grouped in one statement rather than queried per wallet. Two wallets in
  // the same project were asking the same question twice, and the loop awaited
  // serially — cheap today at one wallet, but it is the shape that turns into a
  // stall the moment the treasury has a few.
  // Burn is measured over the shortest of these that has any spend in it.
  //
  // A one-hour window is the right basis for the Treasurer, which has to react quickly,
  // and the wrong one for a card someone reads days later: an hour with no traffic makes
  // burn zero, and zero burn is infinite runway, so the card goes blank within sixty
  // minutes of the last call. That is honest and useless — a dashboard that reads "—"
  // for most of its life is not reporting anything.
  //
  // So it widens until it finds spend, and reports which window it used so the card can
  // say "at the 3-day average" rather than implying it measured the last hour. The
  // Treasurer's own trigger is untouched and still uses TREASURER_BURN_WINDOW_S: this is
  // a reading, not a decision about money.
  const BURN_FALLBACKS = [BURN_WINDOW_S, 86_400, 3 * 86_400, 7 * 86_400];

  let runwayHours: number | null = null;
  let runwayProvider: string | null = null;
  let burnRate = 0;
  let burnBasisS: number | null = null;

  if (walletRows.length > 0) {
    const projectIds = [...new Set(walletRows.map((w) => w.project_id))];
    for (const windowS of BURN_FALLBACKS) {
      const burnRows = await query<{ project_id: string; spend: number }>(
        `SELECT project_id, COALESCE(SUM(cost_usd), 0) AS spend
           FROM requests WHERE project_id = ANY($1) AND ts >= $2
          GROUP BY project_id`,
        [projectIds, isoSecondsAgo(windowS)],
      );
      const burnBy = new Map(burnRows.map((r) => [r.project_id, num(r.spend)]));
      const windowHours = windowS / 3600;

      for (const w of walletRows) {
        const perHour = (burnBy.get(w.project_id) ?? 0) / windowHours;
        if (perHour <= 0) continue; // No burn is infinite runway, not zero.
        const hours = num(w.balance_usd) / perHour;
        if (runwayHours === null || hours < runwayHours) {
          runwayHours = hours;
          runwayProvider = w.provider;
          burnRate = perHour;
          burnBasisS = windowS;
        }
      }
      // The first window with spend wins. Widening further would average real burn
      // against quiet time and understate it.
      if (runwayHours !== null) break;
    }
  }

  return {
    spend_window_usd: spend,
    spend_prev_window_usd: prevSpend,
    // No delta against an all-time total. The card shows the request count instead,
    // which is a fact rather than a comparison that cannot be made.
    spend_delta: null,
    request_count_window: num(windowRows[0]?.requests),
    budget_ceiling_usd: budgetCeiling,
    budget_spend_usd: budgetSpend,
    runway_hours: runwayHours,
    runway_provider: runwayProvider,
    runway_trigger_hours: RUNWAY_TRIGGER_HOURS,
    burn_usd_per_hour: burnRate,
    burn_basis_s: burnBasisS,
  };
}

// ── Cost per outcome ─────────────────────────────────────────────────────────

/**
 * The two CTEs every outcome query is built on. They exist to defuse one specific bug.
 *
 * `annotations` is append-only — proxy/db.py says so outright: "a trace can be annotated
 * twice". So the obvious `requests JOIN annotations ON trace_id` fans out. A trace with
 * 12 requests and 2 annotations yields 24 rows and reports **double** the cost actually
 * spent. On a margin metric that is the error direction that turns a loss into a profit
 * on screen, which is worse than no metric at all.
 *
 * `trace_rollup` therefore collapses requests to one row per trace *before* anything
 * joins to it, and `latest` collapses annotations to one row per trace, so the join is
 * strictly 1:1 and cannot multiply.
 *
 * `latest` keeps the newest annotation per trace rather than summing: a trace annotated
 * twice is usually a correction (a ticket reopened, then resolved again), and summing
 * would double-count the correction. Both halves of that choice live here — swapping to
 * additive value is a change to this CTE alone.
 */
const outcomeCtes = (scope: Scope) => `
  WITH latest AS (
    SELECT a.project_id, a.trace_id, a.outcome, a.value_usd
      FROM annotations a
     WHERE ${scoped(scope, 1, "a.project_id").sql}
       AND a.id = (SELECT MAX(b.id)
                     FROM annotations b
                    WHERE b.project_id = a.project_id
                      AND b.trace_id  = a.trace_id)
  ),
  trace_rollup AS (
    SELECT project_id,
           trace_id,
           SUM(cost_usd) AS cost_usd,
           COUNT(*)      AS request_count
      FROM requests
     WHERE trace_id IS NOT NULL
       AND ${scoped(scope, 1).sql}
     GROUP BY project_id, trace_id
  )
`;

export type OutcomeRow = {
  outcome: string | null;
  trace_count: number;
  request_count: number;
  cost_usd: number;
  /** Null when no annotation in this group carried a `value_usd`. */
  value_usd: number | null;
  /** Cost of only the traces that carried a value — the denominator margin can use. */
  valued_cost_usd: number | null;
  valued_trace_count: number;
};

export async function getOutcomeCosts(scope: Scope = null): Promise<OutcomeRow[]> {
  const [hasRequests, hasAnnotations] = await Promise.all([
    tableExists("requests"),
    tableExists("annotations"),
  ]);
  if (!hasRequests || !hasAnnotations) return [];
  // INNER JOIN, deliberately. `trace_id` is a caller-supplied string, so an annotation
  // can name a trace that was never metered (a typo, or a ticket handled outside the
  // proxy). Meter knows no cost for those, and letting them in would add traces to the
  // count while adding nothing to cost, quietly deflating every cost-per-trace figure.
  // They are surfaced separately as `orphan_annotations` in the coverage row instead.
  const rows = await query<OutcomeRow>(
    `${outcomeCtes(scope)}
     SELECT l.outcome                        AS outcome,
            COUNT(*)                         AS trace_count,
            SUM(t.request_count)             AS request_count,
            SUM(t.cost_usd)                  AS cost_usd,
            SUM(l.value_usd)                 AS value_usd,
            SUM(CASE WHEN l.value_usd IS NOT NULL THEN t.cost_usd END)
                                             AS valued_cost_usd,
            COUNT(l.value_usd)               AS valued_trace_count
       FROM latest l
       JOIN trace_rollup t
         ON t.project_id = l.project_id AND t.trace_id = l.trace_id
      GROUP BY l.outcome
      ORDER BY cost_usd DESC`,
    scoped(scope, 1).params,
  );
  return rows.map((r) => ({
    outcome: r.outcome,
    trace_count: num(r.trace_count),
    request_count: num(r.request_count),
    cost_usd: num(r.cost_usd),
    value_usd: numOrNull(r.value_usd),
    valued_cost_usd: numOrNull(r.valued_cost_usd),
    valued_trace_count: num(r.valued_trace_count),
  }));
}

export type OutcomeCoverage = {
  traced_traces: number;
  traced_cost: number;
  annotated_traces: number;
  annotated_cost: number;
  /** Annotations naming a trace that has no metered requests — almost always a typo. */
  orphan_annotations: number;
  total_cost: number;
};

/**
 * How much of the ledger the outcome numbers actually speak for.
 *
 * This is not decoration. "$0.004 per resolved ticket" computed from 3% of traffic is a
 * number nobody should quote on a stage, and it looks identical to one computed from 95%
 * unless the coverage is stated beside it.
 */
export async function getOutcomeCoverage(scope: Scope = null): Promise<OutcomeCoverage | null> {
  const [hasRequests, hasAnnotations] = await Promise.all([
    tableExists("requests"),
    tableExists("annotations"),
  ]);
  if (!hasRequests || !hasAnnotations) return null;
  const rows = await query<OutcomeCoverage>(
    `${outcomeCtes(scope)}
     SELECT (SELECT COUNT(*)                       FROM trace_rollup) AS traced_traces,
            (SELECT COALESCE(SUM(cost_usd), 0)     FROM trace_rollup) AS traced_cost,
            (SELECT COUNT(*)
               FROM latest l JOIN trace_rollup t
                 ON t.project_id = l.project_id AND t.trace_id = l.trace_id)
                                                                      AS annotated_traces,
            (SELECT COALESCE(SUM(t.cost_usd), 0)
               FROM latest l JOIN trace_rollup t
                 ON t.project_id = l.project_id AND t.trace_id = l.trace_id)
                                                                      AS annotated_cost,
            (SELECT COUNT(*)
               FROM latest l LEFT JOIN trace_rollup t
                 ON t.project_id = l.project_id AND t.trace_id = l.trace_id
              WHERE t.trace_id IS NULL)                               AS orphan_annotations,
            (SELECT COALESCE(SUM(cost_usd), 0)     FROM requests
              WHERE ${scoped(scope, 1).sql})                          AS total_cost`,
    scoped(scope, 1).params,
  );
  const row = rows[0];
  if (!row) return null;
  return {
    traced_traces: num(row.traced_traces),
    traced_cost: num(row.traced_cost),
    annotated_traces: num(row.annotated_traces),
    annotated_cost: num(row.annotated_cost),
    orphan_annotations: num(row.orphan_annotations),
    total_cost: num(row.total_cost),
  };
}

// ── Treasurer activity ───────────────────────────────────────────────────────

/**
 * What the Treasurer recorded about one top-up attempt.
 *
 * `treasury_events` is a lifecycle row, NOT a log stream: one row per attempt, moving
 * from `pending` to a terminal status. There is no "scanning" event — a tick that
 * decides nothing writes nothing — so the panel is quiet whenever the Treasurer is
 * healthy, and says so rather than inventing heartbeat lines.
 */
// ── Model efficiency ─────────────────────────────────────────────────────────

export type ModelEfficiencyRow = {
  feature: string;
  model: string;
  baseline_model: string;
  samples: number;
  output_tokens: number;
  baseline_output_tokens: number;
  cost_usd: number;
  baseline_cost_usd: number;
};

/**
 * The most recent cross-model comparison, or `null` if none has been run.
 *
 * `null` and `[]` mean different things here and the card renders them differently: no
 * table at all means the comparison has never been pushed, which is the normal state of a
 * fresh database, while an empty run would mean it was pushed and produced nothing.
 *
 * Guarded on the table existing, like every other query in this file — either side can
 * create the schema with only its own half of the tables, and this one is written by an
 * offline script that may never have been run.
 */
export async function getModelEfficiency(): Promise<ModelEfficiencyRow[] | null> {
  if (!(await tableExists("model_efficiency"))) return null;
  const rows = await query<ModelEfficiencyRow>(
    `SELECT feature, model, baseline_model, samples,
            output_tokens, baseline_output_tokens, cost_usd, baseline_cost_usd
       FROM model_efficiency
      WHERE run_id = (SELECT run_id FROM model_efficiency
                       ORDER BY ts DESC, id DESC LIMIT 1)
      ORDER BY feature`,
  );
  return rows.map((r) => ({
    ...r,
    samples: num(r.samples),
    output_tokens: num(r.output_tokens),
    baseline_output_tokens: num(r.baseline_output_tokens),
    cost_usd: num(r.cost_usd),
    baseline_cost_usd: num(r.baseline_cost_usd),
  }));
}

export type TreasuryEvent = {
  id: number;
  wallet_id: string;
  project_id: string | null;
  provider: string | null;
  mandate_id: string | null;
  amount_usd: number;
  /** pending | settled | dry_run | refused | failed */
  status: string;
  prava_txn_id: string | null;
  error: string | null;
  created_at: string;
  settled_at: string | null;
  /** Parsed `decision_inputs`; null when absent or unparseable. */
  decision: TreasuryDecision | null;
};

export type TreasuryDecision = {
  balance_usd?: number;
  burn_usd_per_hour?: number;
  runway_hours?: number | null;
  threshold_hours?: number;
  floor_usd?: number;
  trigger?: string | null;
  recommended_topup_usd?: number;
};

/**
 * Rows where the agent declined to act for a reason that was never about the money.
 *
 * `no_chargeable_mandate` means nobody has connected a card yet, and `cooldown` is the
 * agent backing off after a recent attempt. Both are the rails working, and neither is a
 * decision about spending — but they are written on every poll, so they outnumber real
 * charges 130 to 5 and would bury the settled transactions this panel exists to show.
 *
 * Deliberately narrow. `failed`, `pending` and `dry_run` all stay: a charge that reached
 * the network and did not settle, or one the agent decided on and stopped short of, is
 * exactly what someone auditing an autonomous spender needs to see. Only the two
 * "nothing happened" reasons are hidden.
 */
const NOT_A_MONEY_DECISION =
  "NOT (e.status = 'refused' AND e.error IN ('cooldown', 'no_chargeable_mandate'))";

export async function getTreasuryEvents(limit = 40, scope: Scope = null): Promise<TreasuryEvent[]> {
  if (!(await tableExists("treasury_events"))) return [];

  // The wallet join supplies project and provider, which the event row does not carry.
  // LEFT JOIN, and guarded: a database can have treasury_events without wallets if the
  // schema was created piecemeal, and losing the whole panel over a missing label
  // would be the wrong trade.
  const joined = await tableExists("wallets");
  // The event itself does not carry a project id. A public dashboard can still render
  // an unlabelled event when a half-built schema lacks wallets; a judge dashboard cannot
  // prove the row belongs to its session in that state. Empty is the only safe answer.
  if (scope !== null && !joined) return [];
  const rows = await query<
    Omit<TreasuryEvent, "decision"> & { decision_inputs: string | null }
  >(
    `SELECT e.id, e.wallet_id, e.mandate_id, e.amount_usd, e.status,
            e.prava_txn_id, e.error, e.created_at, e.settled_at,
            e.decision_inputs,
            ${joined ? "w.project_id, w.provider" : "NULL AS project_id, NULL AS provider"}
       FROM treasury_events e
       ${joined ? "LEFT JOIN wallets w ON w.id = e.wallet_id" : ""}
      WHERE ${NOT_A_MONEY_DECISION}
        ${joined && scope !== null ? "AND w.project_id = $2" : ""}
      ORDER BY e.id DESC
      LIMIT $1`,
    joined && scope !== null ? [limit, scope] : [limit],
  );

  return rows.map(({ decision_inputs, ...row }) => ({
    ...row,
    id: num(row.id),
    amount_usd: num(row.amount_usd),
    // decision_inputs is free-form JSON written by the treasurer. A shape change
    // there must not take the dashboard down, so parsing failure degrades to null
    // and the line that needs it is simply omitted.
    decision: parseDecision(decision_inputs),
  }));
}

function parseDecision(raw: string | null): TreasuryDecision | null {
  if (!raw) return null;
  // A `jsonb` column would already be parsed by the driver; this one is TEXT, but
  // accepting both means the column type can change without breaking the panel.
  if (typeof raw === "object") return raw as TreasuryDecision;
  try {
    const parsed: unknown = JSON.parse(raw);
    if (parsed && typeof parsed === "object") return parsed as TreasuryDecision;
    return null;
  } catch {
    return null;
  }
}

export type LiveLogRow = {
  id: string;
  ts: string;
  actor: string | null;
  feature: string | null;
  provider: string | null;
  model: string | null;
  // Written at CAPTURE by the proxy, from the prediction made before the call.
  // Null is meaningful and distinct from zero: it means the request was not
  // predicted at all — Claude models raise rather than approximate with the wrong
  // tokenizer, so those rows carry no forecast. `prediction_method` says which.
  predicted_cost_usd: number | null;
  prediction_method: string | null;
  cost_usd: number;
  status: number | null;
};

export async function getLiveLogs(limit = 50, scope: Scope = null): Promise<LiveLogRow[]> {
  if (!(await tableExists("requests"))) return [];

  // The prediction columns arrived with the predictor integration. A database can
  // predate that, and selecting a missing column throws — same class of failure as the
  // missing-table case, so it gets the same treatment.
  const [hasPredicted, hasMethod] = await Promise.all([
    columnExists("requests", "predicted_cost_usd"),
    columnExists("requests", "prediction_method"),
  ]);
  const predicted = hasPredicted
    ? "predicted_cost_usd"
    : "NULL AS predicted_cost_usd";
  const method = hasMethod ? "prediction_method" : "NULL AS prediction_method";

  const rows = await query<LiveLogRow>(
    `SELECT id,
            ts,
            actor,
            feature,
            provider,
            model,
            ${predicted},
            ${method},
            cost_usd,
            status
       FROM requests
      WHERE ${scopedIncludingJudges(scope, 2).sql}
      ORDER BY ts DESC
      LIMIT $1`,
    [limit, ...scopedIncludingJudges(scope, 2).params],
  );
  return rows.map((r) => ({
    ...r,
    predicted_cost_usd: numOrNull(r.predicted_cost_usd),
    cost_usd: num(r.cost_usd),
    status: numOrNull(r.status),
  }));
}
