"""The ledger: schema and queries, on Postgres.

Every statement here goes through ``proxy/pg.py`` — one pool, per-statement autocommit,
``?`` placeholders rewritten to ``%s`` on the way to the driver. That rewrite is why the
SQL below is byte-identical to the SQLite version this file started as: the port was a
change of execution layer, not fifty rewritten statements, and fifty hand-rewritten
statements is fifty chances to transpose a parameter.

Column names are copied verbatim from ARCHITECTURE.md §4. Deliberate divergences from
that schema, each flagged where it occurs:

* ``overhead_ms`` is an addition, not a divergence — see the note on the column. So are
  the four prediction columns, for the same reason: ARCHITECTURE.md §2 step 3 requires an
  estimate but §4 gives it nowhere to land, and predicted-vs-actual variance is
  unrecoverable after the fact.
* ``annotations`` carries a ``project_id`` that ARCHITECTURE.md §4 does not list. Without
  it any key could annotate any other project's traces, since a trace id is a
  caller-supplied string.
* Timestamps are ISO-8601 UTC strings rather than ``timestamptz``, and money is
  ``double precision`` rather than ``numeric``. Both are ported like for like from the
  SQLite schema rather than upgraded. The strings compare lexicographically in the
  correct order, which is what makes the rolling-window queries below work without a date
  function — and it is also what the dashboard's cutoff format depends on, so changing
  either type changes comparison semantics in two places at once. Open follow-ups, not
  oversights.

``reservation_id`` is no longer NULL: reservations are held in-process
(``proxy/budget.py``) rather than in the Redis that ARCHITECTURE.md §2 specifies, per the
decision recorded in PROPOSALS.md A5.

Concurrency: the pool hands each caller its own connection and MVCC handles concurrent
readers and writers, so there is nothing to serialise here. Callers from async code must
still go through ``asyncio.to_thread`` — more so than before, in fact. A round trip to a
hosted database is tens of milliseconds where a local SQLite read was microseconds, so
one unwrapped call blocks the event loop that is simultaneously pumping SSE bytes to a
client for far longer than it used to.

Multi-statement writes need ``pg.transaction()``. A bare ``BEGIN`` is silently useless:
every other statement borrows its own pooled connection, so the transaction opens on a
connection that goes straight back to the pool while the rest of the block runs outside
it. ``replace_budgets`` is the only caller that needs one.

Owner: Shubh (Proxy & Infra). Ported to Postgres by Shivam.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlsplit

from . import config, pg

log = logging.getLogger("meter.db")

# Was a `threading.Lock` around one shared SQLite connection. Postgres needs neither:
# the pool hands each caller its own connection and MVCC handles concurrent readers and
# writers, so the thing this lock existed to prevent cannot happen. Kept as a null
# context so the `with _lock:` blocks below stay untouched — see `pg._Connection` for
# why the call sites were left alone.
_lock = contextlib.nullcontext()


def now_iso() -> str:
    """Current UTC time in a fixed-width, lexicographically sortable format.

    Fixed width matters: the rolling-window queries compare timestamps as strings, and
    that is only correct if every timestamp has the same number of digits and the same
    trailing offset. ``isoformat()`` drops the microseconds field entirely when it
    happens to be zero, which would break the ordering roughly once every million
    requests — rare enough to survive testing and reappear on stage.
    """
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f+00:00")


def iso_seconds_ago(seconds: float) -> str:
    ts = datetime.now(timezone.utc) - timedelta(seconds=seconds)
    return ts.strftime("%Y-%m-%dT%H:%M:%S.%f+00:00")


def iso_at(ts: datetime) -> str:
    """Format an arbitrary instant the same fixed-width way. See :func:`now_iso`.

    Exists so callers storing a *future* timestamp — a judge session's `expires_at` — do
    not reach for `datetime.isoformat()`, which is variable width and therefore wrong for
    a column that gets compared as a string.
    """
    return ts.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f+00:00")


def hash_key(raw: str) -> str:
    """Meter keys are stored hashed, never in plaintext.

    ARCHITECTURE.md §4 names the column ``meter_keys.hash``. A leaked ledger should not
    hand the reader a working set of proxy credentials on top of the spend history.
    """
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
    id                TEXT PRIMARY KEY,
    name              TEXT NOT NULL,
    environment       TEXT NOT NULL DEFAULT 'dev',
    ceiling_usd_day   double precision,
    fail_mode         TEXT NOT NULL DEFAULT 'open'
);

CREATE TABLE IF NOT EXISTS meter_keys (
    id          TEXT PRIMARY KEY,
    project_id  TEXT NOT NULL REFERENCES projects(id),
    hash        TEXT NOT NULL UNIQUE,
    revoked_at  TEXT,
    -- Comma-separated scope names, or NULL for "unrestricted". NULL is what every key
    -- that predates this column has, and it has to keep meaning "everything" or a
    -- migration would lock every existing deployment out of its own treasury.
    -- See `key_allows`.
    scopes      TEXT
);

-- One row per "Try it yourself" judge session (PITCH.md §2). A judge gets their own
-- `projects` row and their own `meter_keys` row, exactly like any other tenant, so
-- `resolve_key` authenticates them through the unchanged path. This table holds only
-- what a judge session needs *beyond* being a tenant: how long it lives, how much it may
-- spend, and the demo-scale breaker floor that lets them actually trip it.
--
-- Deliberately a separate table rather than columns on `meter_keys`, for three reasons
-- (PITCH.md §5): `meter_keys` stores a hash precisely so a leaked ledger yields no
-- working credentials, it sits on the authentication hot path where a judge-console bug
-- must not reach, and judge rows are ephemeral where key rows are permanent. Drop this
-- table and the rest of the product is byte-identical.
--
-- NO SECRETS HERE. A judge's Prava merchant key, Linq key and phone live in an
-- in-process TTL vault (`judge/sessions.py`), never in Postgres — which is what the
-- console promises them, and what keeps encryption-at-rest and key rotation out of
-- scope. Safe because the backend is pinned to one instance anyway by budget.py's
-- in-process reservation lock.
CREATE TABLE IF NOT EXISTS judge_sessions (
    token              TEXT PRIMARY KEY,
    project_id         TEXT NOT NULL REFERENCES projects(id),
    key_id             TEXT NOT NULL,
    display_name       TEXT,
    email              TEXT,
    created_at         TEXT NOT NULL,
    expires_at         TEXT NOT NULL,
    breaker_floor_usd  double precision,
    ceiling_usd_day    double precision,
    call_cap           INTEGER NOT NULL DEFAULT 25,
    calls_used         INTEGER NOT NULL DEFAULT 0,
    -- SHA-256 of the money capability (PROPOSALS.md B20). The session token itself is
    -- deliberately script-readable, because the console sends it cross-origin in a header
    -- where cookies do not go. This second secret never reaches JavaScript: it rides an
    -- httpOnly cookie on the proxy's own origin and gates the routes that touch a card.
    capability_hash    TEXT
);

-- Per-feature daily ceilings, the `features:` block of meter.yaml. Not in
-- ARCHITECTURE.md §4, which gives ceilings only at project level — but README.md's own
-- meter.yaml example sets them per feature, and a project-only ceiling cannot express
-- "chat may spend 500 of the project's 800". Budgets are declared in meter.yaml and
-- upserted here at boot, so the file stays the source of truth and the request path
-- still gets to read a table (PROPOSALS.md A6).
CREATE TABLE IF NOT EXISTS feature_budgets (
    project_id       TEXT NOT NULL,
    feature          TEXT NOT NULL,
    ceiling_usd_day  double precision,
    PRIMARY KEY (project_id, feature)
);

-- One priced row per proxied call. This table is the moat described in
-- ARCHITECTURE.md §9: it is the only attributed, priced history of a company's
-- inference spend, and it does not port to a competitor.
CREATE TABLE IF NOT EXISTS requests (
    id                  TEXT PRIMARY KEY,
    ts                  TEXT NOT NULL,
    project_id          TEXT NOT NULL,
    environment         TEXT,
    actor               TEXT,
    feature             TEXT,
    trace_id            TEXT,
    provider            TEXT NOT NULL,
    model               TEXT,
    endpoint            TEXT NOT NULL,
    input_tokens        INTEGER NOT NULL DEFAULT 0,
    output_tokens       INTEGER NOT NULL DEFAULT 0,
    cache_read_tokens   INTEGER NOT NULL DEFAULT 0,
    cache_write_tokens  INTEGER NOT NULL DEFAULT 0,
    pricing_version     TEXT,
    cost_usd            double precision NOT NULL DEFAULT 0,
    latency_ms          double precision,
    ttft_ms             double precision,
    -- Milliseconds Meter itself added, measured as wall-clock minus upstream time.
    -- Not in ARCHITECTURE.md §4. It is here because ARCHITECTURE.md §8 says to publish
    -- the overhead number ("we add 3ms at p50") and there is otherwise nothing in the
    -- system that measures it. A claim you cannot compute from your own ledger is a
    -- claim you should not put on a slide.
    overhead_ms         double precision,
    status              INTEGER,
    is_stream           INTEGER NOT NULL DEFAULT 0,
    estimated           INTEGER NOT NULL DEFAULT 0,
    prompt_hash         TEXT,
    reservation_id      TEXT,
    -- What the predictor said BEFORE the call, alongside what actually happened.
    -- Storing both in the same row is what makes predictor accuracy a query rather
    -- than a separate pipeline: it feeds the learner (predictor/learner.py) and is the
    -- number that says whether the safety margin is set right. All nullable: a
    -- prediction is best-effort and must never be able to fail a request. NULL means
    -- "not predicted" (unsupported model — every Claude model — or predictor errored),
    -- which is different from and must not be confused with a prediction of zero.
    predicted_output_tokens INTEGER,
    predicted_cost_usd      double precision,
    bucket                  TEXT,
    prediction_method       TEXT,
    -- Raw heuristic output, before buffer/history/clamp. The learner fits its
    -- correction against THIS, never against predicted_output_tokens: a factor
    -- derived from an already-corrected number divides by the previous factor on
    -- every refresh and oscillates rather than converging.
    predicted_scope_tokens  INTEGER,
    -- Reservation. Separate from the forecast; uncapped bounds are statistical.
    bound_output_tokens     INTEGER,
    bound_cost_usd          double precision,
    bound_is_hard           INTEGER,
    history_factor          double precision
);

-- Attribution rung 3 (README.md), the requests × annotations join of ARCHITECTURE.md §4.
-- Outcomes attach to a trace, never to a request: one resolved ticket is a dozen calls,
-- and dividing its value across them is what turns cost-per-token into cost-per-outcome.
CREATE TABLE IF NOT EXISTS annotations (
    id          INTEGER GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
    ts          TEXT NOT NULL,
    -- Not in ARCHITECTURE.md §4. A trace id is a caller-supplied string, so without
    -- scoping, any key could annotate — or read the value of — another project's traces.
    project_id  TEXT NOT NULL,
    trace_id    TEXT NOT NULL,
    outcome     TEXT,
    value_usd   double precision
);

CREATE INDEX IF NOT EXISTS idx_annotations_trace ON annotations(trace_id);

-- (project_id, ts) backs the rolling-window breaker check that runs on every single
-- request, so it is the one index that is in the hot path.
CREATE INDEX IF NOT EXISTS idx_requests_project_ts ON requests(project_id, ts);
-- trace_id backs the requests x annotations join — cost per resolved outcome.
CREATE INDEX IF NOT EXISTS idx_requests_trace     ON requests(trace_id);
-- prompt_hash backs duplicate-call and cache-candidate detection for the Analyst.
CREATE INDEX IF NOT EXISTS idx_requests_prompt    ON requests(prompt_hash);
-- NOTE: the index on `bucket` is NOT here. It is created in _migrate(), after the
-- column exists. Creating it here fails outright on a ledger that predates the
-- column, because CREATE TABLE IF NOT EXISTS no-ops on the existing table and then
-- the index references a column that is not there yet.

-- Cross-model efficiency (PLAN.md Phase 3, PROPOSALS.md B11). One row per
-- (run, feature): the same task measured on a baseline model and a candidate, with the
-- token counts and costs of both sides kept separately.
--
-- Aggregates rather than per-call rows, because the question is "which model is cheaper
-- for this kind of work" and a per-call table would be a second `requests` with none of
-- its indexes earning their keep.
--
-- Written OFFLINE by `scripts/cross_model.py --push`, never from the request path. B11:
-- shadow-calling a second provider on live traffic doubles a customer's bill inside a
-- cost-control tool, and nothing about the comparison needs it — the prompts and the
-- baseline results are already in hand.
CREATE TABLE IF NOT EXISTS model_efficiency (
    id                      INTEGER GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
    run_id                  TEXT NOT NULL,
    ts                      TEXT NOT NULL,
    feature                 TEXT NOT NULL,
    model                   TEXT NOT NULL,
    baseline_model          TEXT NOT NULL,
    samples                 INTEGER NOT NULL,
    input_tokens            INTEGER NOT NULL,
    output_tokens           INTEGER NOT NULL,
    baseline_input_tokens   INTEGER NOT NULL,
    baseline_output_tokens  INTEGER NOT NULL,
    cost_usd                double precision NOT NULL,
    baseline_cost_usd       double precision NOT NULL,
    pricing_version         TEXT
);

-- The view reads the newest run and nothing else, so this is the whole access pattern.
CREATE INDEX IF NOT EXISTS idx_model_efficiency_run ON model_efficiency(run_id, feature);

CREATE TABLE IF NOT EXISTS breaker_events (
    id              INTEGER GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
    scope           TEXT NOT NULL,
    mode            TEXT NOT NULL,
    trigger_metric  TEXT,
    opened_at       TEXT NOT NULL,
    closed_at       TEXT,
    reset_by        TEXT
);

-- Partial index: only open breakers are ever looked up, and there is at most a handful
-- of those at any moment even after a long run.
CREATE INDEX IF NOT EXISTS idx_breaker_open ON breaker_events(scope) WHERE closed_at IS NULL;
"""


_schema_ready = False


def ledger_target() -> str:
    """Where the ledger actually is, safe to log — ``host:port/database`` with a schema.

    The boot line used to print `config.DB_PATH`, which since the Postgres port has been
    a path to a file that does not exist. "ledger ready at meter.db" on a deployed proxy
    sends whoever is debugging it looking for a local file, which is the opposite of
    useful at the moment they most need the truth.

    Credentials are stripped rather than trusted to be absent: `DATABASE_URL` carries the
    password, and a boot line is exactly the kind of thing that gets pasted into a chat.
    """
    raw = config.DATABASE_URL
    if not raw:
        return "<no DATABASE_URL>"
    try:
        parts = urlsplit(raw)
        where = parts.hostname or "?"
        if parts.port:
            where += f":{parts.port}"
        where += parts.path or ""
    except ValueError:
        return "<unparseable DATABASE_URL>"
    return f"{where} (schema {config.DB_SCHEMA})"


def connect() -> pg._Connection:
    """Ensure the schema exists, and hand back the pooled connection facade.

    The PRAGMAs that used to live here are gone with SQLite. `journal_mode=WAL`,
    `synchronous=NORMAL` and `busy_timeout` were all managing a single file with one
    writer; Postgres's MVCC removes the problem they were solving rather than solving it
    differently.
    """
    global _schema_ready
    if not _schema_ready:
        pg.executescript(SCHEMA)
        _migrate(pg.CONNECTION)
        _schema_ready = True
        log.info("ledger schema ready (postgres, schema=%s)", config.DB_SCHEMA)
    return pg.CONNECTION


# Columns added after the first ledgers were already created. CREATE TABLE IF NOT
# EXISTS silently does nothing on an existing table, so without this anyone holding a
# meter.db from before these columns landed gets "no such column" on every write.
# Dropping their ledger instead is not an option: it is the priced history, which
# ARCHITECTURE.md §9 calls the part that does not port.
_ADDED_COLUMNS = (
    ("requests", "predicted_output_tokens", "INTEGER"),
    ("requests", "predicted_cost_usd", "double precision"),
    ("requests", "bucket", "TEXT"),
    ("requests", "prediction_method", "TEXT"),
    # The raw heuristic output, BEFORE the safety buffer, the history factor and the
    # max_tokens clamp. Without it the learner cannot compute a stable correction:
    # deriving the factor from the already-corrected prediction divides by the
    # previous factor each refresh, which oscillates instead of converging.
    ("requests", "predicted_scope_tokens", "INTEGER"),
    # Reservation, not necessarily a hard upper bound without an explicit output cap.
    ("requests", "bound_output_tokens", "INTEGER"),
    ("requests", "bound_cost_usd", "double precision"),
    ("requests", "bound_is_hard", "INTEGER"),
    ("requests", "history_factor", "double precision"),
    # meter.yaml order, recorded explicitly. Under SQLite the dashboard read it off
    # `rowid` — `replace_budgets` clears both tables and re-inserts in file order, so
    # insertion order *was* file order. Postgres has no rowid, and `ctid` is a physical
    # address that VACUUM is free to move, so the ordering the budget cards depend on
    # has to be a column. Without it the cards reshuffle between reloads while someone
    # is watching them.
    ("projects", "sort_order", "INTEGER"),
    ("feature_budgets", "sort_order", "INTEGER"),
    # Per-project circuit-breaker floor, overriding config.BREAKER_WINDOW_USD. NULL means
    # "use the configured default", which is every project that predates this column.
    #
    # It is on `projects` rather than in `judge_sessions` on purpose: breaker sensitivity
    # per tenant is a general product feature, it belongs beside `fail_mode` which is
    # already per-project config, and `replace_budgets` does not clear it -- that function
    # nulls `ceiling_usd_day` and `sort_order` at every boot, so a judge's floor stored
    # there would silently vanish on the next Render cold start (PROPOSALS.md M8).
    ("projects", "breaker_floor_usd", "double precision"),
    # Per-key authorisation scopes (PROPOSALS.md B19). NULL means unrestricted, which is
    # every key issued before this existed -- including the one WALKTHROUGH.md publishes.
    # A default of "deny everything" would be the safer-looking choice and the wrong one:
    # it would take the treasury away from every deployment that upgrades, mid-demo.
    ("meter_keys", "scopes", "TEXT"),
    # PROPOSALS.md B20. NULL on any session minted before this existed, which `require`
    # treats as "no capability issued" -- those sessions can still read, and are asked to
    # start a new one before they can spend.
    ("judge_sessions", "capability_hash", "TEXT"),
)


def _migrate(conn: pg._Connection) -> None:
    """Add columns missing from an older ledger. Idempotent, safe on a fresh DB.

    Runs after the schema script, so every table exists by now. Any index that
    references a migrated column must be created here rather than in SCHEMA, for the
    reason noted there.
    """
    for table, column, decl in _ADDED_COLUMNS:
        if not pg.table_exists(table):
            continue  # table absent entirely; SCHEMA owns creating it
        if not pg.column_exists(table, column):
            # No DEFAULT and no NOT NULL: the column backfills NULL, which is the honest
            # value for "this row predates prediction". `IF NOT EXISTS` makes the ALTER
            # idempotent, so two processes booting at once cannot race each other.
            conn.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {column} {decl}")
            log.info("ledger migrated: added %s.%s", table, column)

    # bucket backs the learner's per-bucket refit and the accuracy-by-bucket report.
    conn.execute("CREATE INDEX IF NOT EXISTS idx_requests_bucket ON requests(bucket)")


def seed_keys(spec: str) -> int:
    """Seed ``projects`` and ``meter_keys`` from the ``METER_KEYS`` env format.

    Format is ``key:project:environment:scopes``, comma separated. ``environment``
    defaults to ``dev``; ``scopes`` is ``|``-separated (``:`` and ``,`` are both already
    taken by this format) and defaults to unrestricted. Idempotent, so restarting the
    proxy never duplicates a project or invalidates a key already in use.

        METER_KEYS=mk_live:acme:prod:proxy|read,mk_ops:acme:prod:money
    """
    conn = connect()
    seeded = 0
    with _lock:
        for entry in spec.split(","):
            entry = entry.strip()
            if not entry:
                continue
            parts = entry.split(":")
            if len(parts) < 2:
                log.warning("ignoring malformed METER_KEYS entry: %r", entry)
                continue
            raw_key, project_id = parts[0], parts[1]
            environment = parts[2] if len(parts) > 2 else "dev"
            scopes = None
            if len(parts) > 3 and parts[3].strip():
                named = [x.strip() for x in parts[3].split("|") if x.strip()]
                unknown = [x for x in named if x not in ALL_SCOPES]
                if unknown:
                    # Refuse rather than silently granting less than intended: a typo in a
                    # scope name is otherwise indistinguishable from a deliberate one, and
                    # it fails at the far end as a 403 nobody can explain.
                    log.warning("ignoring METER_KEYS entry %r: unknown scope(s) %s",
                                entry, ", ".join(unknown))
                    continue
                scopes = ",".join(named)
            conn.execute(
                # `ON CONFLICT DO NOTHING` with no conflict target, rather than
                # `INSERT OR IGNORE`: same "skip any constraint violation" semantics, but
                # it is standard SQL that Postgres also accepts. Part of moving the ledger
                # off SQLite without a dialect fork.
                "INSERT INTO projects (id, name, environment) VALUES (?, ?, ?)"
                " ON CONFLICT DO NOTHING",
                (project_id, project_id, environment),
            )
            conn.execute(
                # No conflict target on purpose: `meter_keys` can collide on either the
                # primary key or the unique hash, and re-seeding must skip both without
                # invalidating a key already in use.
                "INSERT INTO meter_keys (id, project_id, hash, scopes)"
                " VALUES (?, ?, ?, ?) ON CONFLICT DO NOTHING",
                (f"mk_{hash_key(raw_key)[:16]}", project_id, hash_key(raw_key), scopes),
            )
            seeded += 1
        conn.commit()
    return seeded


# ── Meter key cache ──────────────────────────────────────────────────────────
#
# `_lock` above is a nullcontext (Postgres MVCC made the SQLite serialisation pointless),
# so this needs a real lock: `resolve_key` runs on worker threads via `asyncio.to_thread`.
#
# The epoch is what makes revocation exact rather than nearly-exact. Without it there is a
# race: a request reads the key row, a revocation lands and clears the cache, and only
# then does the first request store what it read — reinstating a revoked key for a full
# TTL. Storing only if the epoch is unchanged closes that window. It is a cheap guard
# against a rare interleaving, and the thing it protects is a cut credential.
_key_cache: dict[str, tuple[float, dict[str, Any]]] = {}

# Sweep expired entries on insert once the map passes this size. A plain number rather
# than an LRU: entries expire on a timer anyway, so the only thing needed is that dead
# ones leave, and the threshold keeps the sweep off the path of a small deployment.
_KEY_CACHE_SWEEP_AT = 256
_key_cache_epoch = 0
_key_cache_lock = threading.Lock()


def invalidate_key_cache() -> None:
    """Drop every cached key. Called by anything that changes what `resolve_key` returns.

    Clearing all of it rather than one entry is deliberate: revocation is rare, the map is
    tiny, and a targeted eviction needs a `key_id`-to-hash reverse index that would be one
    more thing to keep correct for no measurable gain.
    """
    global _key_cache_epoch
    with _key_cache_lock:
        _key_cache.clear()
        _key_cache_epoch += 1


def resolve_key(raw_key: str) -> dict[str, Any] | None:
    """Resolve a presented Meter key to its project, or ``None`` if unknown.

    A revoked key still resolves — the caller needs to tell "unknown key" (401) apart
    from "key we cut on purpose" (403), and collapsing those two into one response
    makes a tripped breaker indistinguishable from a typo during the demo.
    """
    cache_key = hash_key(raw_key)
    ttl = config.KEY_CACHE_TTL_S
    epoch = None
    if ttl > 0:
        with _key_cache_lock:
            epoch = _key_cache_epoch
            hit = _key_cache.get(cache_key)
        if hit is not None and hit[0] > time.monotonic():
            # A copy, always. Callers treat the resolved row as their own — the tests
            # mutate it outright — and handing out the cached dict would let one request
            # edit what every later request authenticates against.
            return dict(hit[1])

    conn = connect()
    with _lock:
        row = conn.execute(
            # `breaker_floor_usd` rides along on a join the request path already makes.
            # It is per-project config exactly like `fail_mode` beside it, and reading it
            # here rather than in `breaker.check` is what keeps the floor free: another
            # query would be another sequential round trip, and overhead is round trips
            # times RTT (proxy/README.md).
            """SELECT k.id AS key_id, k.project_id, k.revoked_at, k.scopes,
                      p.environment, p.fail_mode, p.breaker_floor_usd
               FROM meter_keys k JOIN projects p ON p.id = k.project_id
               WHERE k.hash = ?""",
            (cache_key,),
        ).fetchone()
    if row is None:
        # Misses are deliberately NOT cached. A judge session mints a key and uses it
        # moments later; caching the "unknown" answer would reject it for a full TTL.
        # Unknown keys are refused at the door anyway, so the cheap path is the safe one.
        return None

    resolved = dict(row)
    if ttl > 0:
        now = time.monotonic()
        with _key_cache_lock:
            if _key_cache_epoch == epoch:
                # Expired entries are dropped rather than merely ignored on read. Judge
                # sessions mint a key each, so on a long-lived process the map would
                # otherwise keep every key ever presented — the same accumulation that
                # left 228 stale ceilings registered. Sweeping on insert keeps it
                # proportional to live keys and costs nothing until the map is large.
                if len(_key_cache) >= _KEY_CACHE_SWEEP_AT:
                    for k in [k for k, (exp, _) in _key_cache.items() if exp <= now]:
                        del _key_cache[k]
                _key_cache[cache_key] = (now + ttl, dict(resolved))
    return resolved


# Scope names. Deliberately three, not a permission per route: the question a key needs to
# answer is "may this move money", not "may this call /wallets/seed specifically", and a
# per-route matrix is a thing to keep in sync forever.
SCOPE_PROXY = "proxy"    # /v1/* -- metered inference
SCOPE_MONEY = "money"    # anything that charges, tops up, credits or drives the Treasurer
SCOPE_READ = "read"      # treasury reads: balances, mandates, event history
ALL_SCOPES = (SCOPE_PROXY, SCOPE_MONEY, SCOPE_READ)


def key_allows(key: dict[str, Any] | None, scope: str) -> bool:
    """Whether a resolved key carries ``scope``.

    ``NULL``/empty means unrestricted, and that asymmetry is the whole design. Scoping
    arrived after keys were already in circulation — including the working key published in
    `WALKTHROUGH.md` — so defaulting to deny would have revoked the treasury out from under
    every existing deployment the moment it upgraded. Existing keys keep everything; new
    ones are issued narrow, which is where the actual exposure is (a judge session acting
    with a real card).

    B19 is what this is for: before it, "authenticated" meant "may do everything", so the
    published walkthrough key could drive the autonomous charging loop.
    """
    if key is None:
        return False
    raw = key.get("scopes")
    if raw is None or not str(raw).strip():
        return True
    return scope in {s.strip() for s in str(raw).split(",") if s.strip()}


def set_breaker_floor(project_id: str, floor_usd: float | None) -> None:
    """Override the circuit-breaker floor for one project. ``None`` restores the default.

    The deployed floor is the production ``$20`` and a templated call costs ``$0.00004``,
    so a tenant who needs to *see* the breaker fire -- a judge session, a demo -- cannot
    get there by hand. Before this, the only lever was ``BREAKER_WINDOW_USD`` in the
    environment, which is process-global: lowering it for one judge lowered it for
    production traffic at the same time.
    """
    conn = connect()
    with _lock:
        conn.execute(
            "UPDATE projects SET breaker_floor_usd = ? WHERE id = ?",
            (floor_usd, project_id),
        )
        conn.commit()
    invalidate_key_cache()  # the floor rides along on the cached key row


def count_breaker_overrides() -> int:
    """How many projects run a floor other than the configured default.

    Surfaced by ``/healthz``. EXPERIENCE.md #32 is the reason: a restart that silently
    failed left the *old* process serving, ``/healthz`` answered ``"status": "ok"`` from
    it, and the only tell was that ``threshold_usd`` still read the production value. The
    lesson recorded there was that a health check should echo the settings that were
    overridden, not just prove something answers. Now that the floor is per project, the
    single number at ``/healthz`` is a default that may apply to nobody.
    """
    conn = connect()
    with _lock:
        row = conn.execute(
            "SELECT count(*) AS n FROM projects WHERE breaker_floor_usd IS NOT NULL"
        ).fetchone()
    return int(dict(row)["n"]) if row else 0


def revoke_key(key_id: str) -> None:
    conn = connect()
    with _lock:
        conn.execute(
            "UPDATE meter_keys SET revoked_at = ? WHERE id = ? AND revoked_at IS NULL",
            (now_iso(), key_id),
        )
        conn.commit()
    invalidate_key_cache()  # a cut key must not answer from cache


def unrevoke_key(key_id: str) -> None:
    """Manual un-revoke. ARCHITECTURE.md §6: manual reset is always available."""
    conn = connect()
    with _lock:
        conn.execute("UPDATE meter_keys SET revoked_at = NULL WHERE id = ?", (key_id,))
        conn.commit()
    invalidate_key_cache()  # and a restored one must work immediately


_REQUEST_COLUMNS = (
    "id", "ts", "project_id", "environment", "actor", "feature", "trace_id",
    "provider", "model", "endpoint",
    "input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens",
    "pricing_version", "cost_usd", "latency_ms", "ttft_ms", "overhead_ms",
    "status", "is_stream", "estimated", "prompt_hash", "reservation_id",
    "predicted_output_tokens", "predicted_cost_usd", "bucket", "prediction_method",
    "predicted_scope_tokens", "bound_output_tokens", "bound_cost_usd", "bound_is_hard",
    "history_factor",
)

# The `NOT NULL DEFAULT` columns of `requests`, and the defaults the schema declares.
#
# These must be applied in Python rather than left to the database. SQLite's
# `INSERT OR REPLACE` silently swaps a NULL for the column default on a NOT NULL
# constraint; a plain `INSERT ... ON CONFLICT` does not, and neither does Postgres — it
# raises instead. Since `record_request` writes partial rows on purpose (truncated
# streams, unknown providers, unpriced models all still get a row, flagged `estimated`),
# leaving that substitution implicit would turn "never drop a ledger row" into "drop the
# rows that matter most" the moment the ledger moved off SQLite.
_REQUEST_NOT_NULL_DEFAULTS = {
    "input_tokens": 0,
    "output_tokens": 0,
    "cache_read_tokens": 0,
    "cache_write_tokens": 0,
    "cost_usd": 0.0,
    "is_stream": 0,
    "estimated": 0,
}


def record_request(row: dict[str, Any]) -> None:
    """Write one priced ledger row.

    ``row["id"]`` is generated by the proxy before the upstream call, not by the
    database. That makes the write idempotent under replay: when Postgres has been
    unreachable and a buffer is drained on recovery, ``INSERT OR REPLACE`` on a
    caller-supplied id cannot double-count the same request as two.
    """
    conn = connect()

    # Substitute defaults for absent counters explicitly, rather than relying on a SQLite
    # quirk. `INSERT OR REPLACE` treats a NULL against a `NOT NULL DEFAULT` column as "use
    # the default"; a plain INSERT — and Postgres — raise `NOT NULL constraint failed`
    # instead. A partial row (a truncated stream, an unpriced model) is a case this table
    # must never drop, so the defaults are applied here where they are visible.
    values = [
        _REQUEST_NOT_NULL_DEFAULTS[col]
        if row.get(col) is None and col in _REQUEST_NOT_NULL_DEFAULTS
        else row.get(col)
        for col in _REQUEST_COLUMNS
    ]
    placeholders = ", ".join("?" * len(_REQUEST_COLUMNS))

    # `ON CONFLICT (id) DO UPDATE` rather than `INSERT OR REPLACE`, so the statement is
    # standard SQL Postgres accepts too. Every column is in the SET list, which is what
    # makes it equivalent: SQLite's REPLACE deletes the row and re-inserts it, so any
    # column left out of an upsert would silently revert to its default. Writing them all
    # keeps the two forms identical rather than subtly different.
    updates = ", ".join(f"{c} = excluded.{c}" for c in _REQUEST_COLUMNS if c != "id")

    with _lock:
        conn.execute(
            f"INSERT INTO requests ({', '.join(_REQUEST_COLUMNS)}) "
            f"VALUES ({placeholders}) "
            f"ON CONFLICT (id) DO UPDATE SET {updates}",
            values,
        )
        conn.commit()


def window_spend(project_id: str, feature: str | None, window_s: float) -> float:
    """Total USD spent by one *attribution tag* over a trailing window.

    ``feature is None`` means untagged traffic — rows where ``feature IS NULL`` — and
    NOT the project total. The distinction is the whole point of throttle mode: each tag
    has to be measured in isolation so one runaway feature gets ``429``d without taking
    every other feature down with it. Summing the project here would make the untagged
    scope trip on any tagged feature's burn, and untagged traffic is usually the traffic
    that has not been instrumented yet — i.e. production.

    For a project-wide total (the daily ceiling), use :func:`project_window_spend`.

    **No production caller remains** — `breaker_state`, `ceiling_spend` and
    `spend_by_scope` each fold this into a larger query. It is kept deliberately, as the
    obvious one-scope-at-a-time implementation the folded queries are asserted against in
    `test_proxy.py`. Deleting it as dead code would remove the only independent check that
    a `CASE`/`GROUP BY` fold still means what it replaced, which is exactly where those
    folds go wrong.
    """
    conn = connect()
    cutoff = iso_seconds_ago(window_s)
    with _lock:
        if feature is None:
            row = conn.execute(
                "SELECT COALESCE(SUM(cost_usd), 0) AS spend FROM requests "
                "WHERE project_id = ? AND feature IS NULL AND ts >= ?",
                (project_id, cutoff),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT COALESCE(SUM(cost_usd), 0) AS spend FROM requests "
                "WHERE project_id = ? AND feature = ? AND ts >= ?",
                (project_id, feature, cutoff),
            ).fetchone()
    return float(row["spend"])


def project_window_spend(project_id: str, window_s: float) -> float:
    """Total USD across every tag in a project over a trailing window.

    Not used by the breaker (see :func:`window_spend` for why). This is the query the
    per-project daily ceiling and the Treasurer's burn-rate projection both want, so it
    lives here rather than being rediscovered twice.
    """
    conn = connect()
    with _lock:
        row = conn.execute(
            "SELECT COALESCE(SUM(cost_usd), 0) AS spend FROM requests "
            "WHERE project_id = ? AND ts >= ?",
            (project_id, iso_seconds_ago(window_s)),
        ).fetchone()
    return float(row["spend"])


def spend_by_scope(
    project_ids: list[str], window_s: float
) -> tuple[dict[tuple[str, str | None], float], dict[str, float]]:
    """Settled spend for many projects at once, split by feature. **One** round trip.

    Returns ``({(project_id, feature): spend}, {project_id: project_total})``, where a
    ``feature`` of ``None`` is untagged traffic only — the same meaning `window_spend`
    gives it — and the project total is every tag plus untagged, matching
    `project_window_spend`.

    `budget.soft_breaches` used to call one of those two functions per configured ceiling,
    inside a loop. With ceilings declared only in `meter.yaml` that was a handful of
    queries a minute and nobody noticed. Judge sessions register their ceilings at runtime,
    so on the deployed instance the count reached **228** — 228 sequential round trips
    every poll, at an RTT measured at ~85 ms, which is more database work than the poll
    interval allows and grows with every judge who ever visits.

    One `GROUP BY` answers all of it. Empty input returns empty rather than scanning the
    whole table, which is what an unfiltered `IN ()` would degrade to.
    """
    if not project_ids:
        return {}, {}
    conn = connect()
    cutoff = iso_seconds_ago(window_s)
    with _lock:
        rows = conn.execute(
            "SELECT project_id, feature, COALESCE(SUM(cost_usd), 0) AS spend "
            "FROM requests WHERE project_id = ANY(?) AND ts >= ? "
            "GROUP BY project_id, feature",
            (list(project_ids), cutoff),
        ).fetchall()

    by_scope: dict[tuple[str, str | None], float] = {}
    totals: dict[str, float] = {}
    for row in rows:
        r = dict(row)
        spend = float(r["spend"])
        by_scope[(r["project_id"], r["feature"])] = spend
        totals[r["project_id"]] = totals.get(r["project_id"], 0.0) + spend
    return by_scope, totals


def ceiling_spend(project_id: str, feature: str | None, window_s: float) -> tuple[float, float]:
    """Feature spend and project spend over one window, in **one** round trip.

    Returns ``(feature_spend, project_spend)``.

    `budget.authorize` needs both numbers, and asking for them separately was two
    sequential queries against the same table, same project and same window — differing
    only by a `feature` filter. Under SQLite that cost microseconds and nobody noticed.
    With the ledger on a hosted Postgres it is a second **network round trip** on the
    request path, and the trips are sequential, so it is added latency on every single
    call rather than a rounding error.

    Measured on the deployed shape: the enforced path made 5 blocking round trips per
    request; this removes one of them. See `proxy/README.md` for the full count and why
    the count, not the query cost, is what matters once the database is 50ms away.

    ``feature is None`` keeps `window_spend`'s meaning exactly — untagged traffic only,
    rows where `feature IS NULL`, **not** the project total. Getting that wrong would make
    the untagged scope trip on any tagged feature's burn.
    """
    conn = connect()
    cutoff = iso_seconds_ago(window_s)
    # One scan, two aggregates. `FILTER` would read better but CASE keeps this in the
    # dialect-neutral form the rest of the file uses.
    if feature is None:
        sql = (
            "SELECT COALESCE(SUM(CASE WHEN feature IS NULL THEN cost_usd END), 0) AS feature_spend, "
            "COALESCE(SUM(cost_usd), 0) AS project_spend "
            "FROM requests WHERE project_id = ? AND ts >= ?"
        )
        params: tuple = (project_id, cutoff)
    else:
        sql = (
            "SELECT COALESCE(SUM(CASE WHEN feature = ? THEN cost_usd END), 0) AS feature_spend, "
            "COALESCE(SUM(cost_usd), 0) AS project_spend "
            "FROM requests WHERE project_id = ? AND ts >= ?"
        )
        params = (feature, project_id, cutoff)
    with _lock:
        row = conn.execute(sql, params).fetchone()
    return float(row["feature_spend"]), float(row["project_spend"])


# ── Budgets ──────────────────────────────────────────────────────────────────


def replace_budgets(budgets: dict[str, tuple[float | None, dict[str, float | None]]]) -> None:
    """Rebuild every ceiling from meter.yaml. ``{project: (ceiling, {feature: ceiling})}``.

    Replaces rather than upserts, because meter.yaml is the source of truth and these
    tables are a read cache of it (ARCHITECTURE.md §4). Upserting would make deletion
    impossible: removing a ceiling from the file would leave the old row enforcing a limit
    that no longer appears anywhere in the repo, which is the exact failure budget-as-code
    exists to prevent. Every prior ceiling is cleared first, so what the file says is what
    is enforced — including when the file says nothing.

    One transaction: a half-applied budget config is worse than either the old one or the
    new one, and it would be live for however long the rest of the loop took.

    Projects named only in meter.yaml are created. A ceiling on a project no Meter key has
    been seeded for is almost always a typo, and the row is what makes it visible.
    """
    connect()
    # `pg.transaction()` rather than `conn.execute("BEGIN")`. Every other statement in
    # this module borrows its own pooled connection, so a bare BEGIN would open a
    # transaction on whichever connection served that one statement and then hand it
    # straight back — the DELETE below would run on a different connection, outside it,
    # and commit on its own. Silently non-atomic, which is the failure this docstring
    # exists to rule out.
    with _lock, pg.transaction() as tx:
        # Runtime-owned projects are exempt from the wipe. meter.yaml is the source of
        # truth for ceilings *it declares*, and it cannot declare a ceiling for a tenant
        # that did not exist when the file was written — a judge session mints its project
        # at runtime, from someone who cannot open a pull request (PROPOSALS.md M8).
        #
        # Without this, a judge's ceilings survive exactly until the next boot, and on
        # Render's free tier a spin-down guarantees one within fifteen idle minutes: they
        # come back to an empty Team Spend card and unenforced limits, with nothing
        # anywhere reporting a problem.
        #
        # Keyed on `environment` rather than an id prefix, so the rule is a property of
        # the project rather than a naming convention someone can break by renaming.
        tx.execute(
            "DELETE FROM feature_budgets WHERE project_id NOT IN "
            "(SELECT id FROM projects WHERE environment = 'judge')"
        )
        tx.execute(
            "UPDATE projects SET ceiling_usd_day = NULL, sort_order = NULL "
            "WHERE environment IS DISTINCT FROM 'judge'"
        )
        for order, (project_id, (ceiling, features)) in enumerate(budgets.items()):
            tx.execute(
                "INSERT INTO projects (id, name, ceiling_usd_day, sort_order) "
                "VALUES (?, ?, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET ceiling_usd_day = excluded.ceiling_usd_day, "
                "sort_order = excluded.sort_order",
                (project_id, project_id, ceiling, order),
            )
            for feature_order, (feature, feature_ceiling) in enumerate(features.items()):
                tx.execute(
                    "INSERT INTO feature_budgets "
                    "(project_id, feature, ceiling_usd_day, sort_order) "
                    "VALUES (?, ?, ?, ?)",
                    (project_id, feature, feature_ceiling, feature_order),
                )


def load_ceilings() -> dict[tuple[str, str | None], float]:
    """Every configured ceiling, keyed ``(project_id, feature or None)``.

    Read once at boot and held in memory by ``budget.py``. Ceilings change when someone
    edits meter.yaml and restarts, so re-reading them per request would buy nothing and
    put a query in front of every call.
    """
    conn = connect()
    with _lock:
        ceilings: dict[tuple[str, str | None], float] = {
            (r["id"], None): float(r["ceiling_usd_day"])
            for r in conn.execute(
                "SELECT id, ceiling_usd_day FROM projects WHERE ceiling_usd_day IS NOT NULL"
            )
        }
        ceilings.update({
            (r["project_id"], r["feature"]): float(r["ceiling_usd_day"])
            for r in conn.execute(
                "SELECT project_id, feature, ceiling_usd_day FROM feature_budgets "
                "WHERE ceiling_usd_day IS NOT NULL"
            )
        })
    return ceilings


# ── Annotations ──────────────────────────────────────────────────────────────


def record_annotation(
    project_id: str, trace_id: str, outcome: str | None, value_usd: float | None
) -> int:
    """Attach an outcome to a trace. Append-only: a trace can be annotated twice."""
    conn = connect()
    with _lock:
        cur = conn.execute(
            # RETURNING id because Postgres has no `lastrowid` — the generated key comes
            # back with the statement or not at all.
            "INSERT INTO annotations (ts, project_id, trace_id, outcome, value_usd) "
            "VALUES (?, ?, ?, ?, ?) RETURNING id",
            (now_iso(), project_id, trace_id, outcome, value_usd),
        )
        conn.commit()
        return int(cur.lastrowid)


def trace_cost(project_id: str, trace_id: str) -> dict[str, Any]:
    """Total cost of one trace — the `requests` half of the cost-per-outcome join.

    Returned from the annotate endpoint so a caller sees the dollars-per-outcome number
    immediately instead of having to query the ledger to find out what it just annotated.
    Scoped to the project so a guessed trace id cannot read another project's spend.
    """
    conn = connect()
    with _lock:
        row = conn.execute(
            "SELECT COALESCE(SUM(cost_usd), 0) AS cost_usd, COUNT(*) AS request_count "
            "FROM requests WHERE project_id = ? AND trace_id = ?",
            (project_id, trace_id),
        ).fetchone()
    return {"cost_usd": round(float(row["cost_usd"]), 6), "request_count": int(row["request_count"])}


def record_model_efficiency(run_id: str, rows: list[dict[str, Any]]) -> int:
    """Store one cross-model comparison run. Returns rows written.

    Called by `scripts/cross_model.py --push`, offline. The dashboard reads this table
    directly; it has no other way to see the comparison, because the script writes JSONL
    on the proxy's host and the dashboard runs somewhere else entirely.

    Idempotent per `run_id`: re-pushing the same run replaces it rather than doubling
    every feature, which is what happens when a push is interrupted halfway and retried.
    """
    connect()
    written = 0
    with _lock, pg.transaction() as tx:
        tx.execute("DELETE FROM model_efficiency WHERE run_id = ?", (run_id,))
        for r in rows:
            tx.execute(
                "INSERT INTO model_efficiency (run_id, ts, feature, model, baseline_model,"
                " samples, input_tokens, output_tokens, baseline_input_tokens,"
                " baseline_output_tokens, cost_usd, baseline_cost_usd, pricing_version)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (run_id, r.get("ts") or now_iso(), r["feature"], r["model"],
                 r["baseline_model"], int(r["samples"]), int(r["input_tokens"]),
                 int(r["output_tokens"]), int(r["baseline_input_tokens"]),
                 int(r["baseline_output_tokens"]), float(r["cost_usd"]),
                 float(r["baseline_cost_usd"]), r.get("pricing_version")),
            )
            written += 1
    return written


def latest_model_efficiency() -> list[dict[str, Any]]:
    """Every feature from the most recent comparison run, or an empty list."""
    conn = connect()
    with _lock:
        rows = conn.execute(
            "SELECT * FROM model_efficiency WHERE run_id = "
            "(SELECT run_id FROM model_efficiency ORDER BY ts DESC, id DESC LIMIT 1)"
            " ORDER BY feature"
        ).fetchall()
    return [dict(r) for r in rows]


def active_breaker(scope: str) -> dict[str, Any] | None:
    """The open breaker event for a scope, or None.

    **No production caller remains**: `breaker_state` returns this alongside the spend
    windows in one round trip. Kept as the reference `breaker_state` is asserted to agree
    with — the two must not disagree about which row is "the" open one, and this is what
    pins that. See :func:`window_spend` for the same note.
    """
    conn = connect()
    with _lock:
        row = conn.execute(
            "SELECT * FROM breaker_events WHERE scope = ? AND closed_at IS NULL "
            "ORDER BY id DESC LIMIT 1",
            (scope,),
        ).fetchone()
    return dict(row) if row else None


def breaker_state(
    project_id: str,
    feature: str | None,
    scope: str,
    window_s: float,
    baseline_s: float,
) -> tuple[dict[str, Any] | None, float, float]:
    """The open breaker row and BOTH spend windows for one tag, in **one** round trip.

    Returns ``(open_row_or_None, short_spend, baseline_spend)``.

    `breaker.check` needed three facts before it could decide anything: is this scope
    already open, what has the tag spent in the short window, and what has it spent in the
    baseline window. That was `active_breaker` plus one or two `window_spend` calls — two
    sequential round trips in the quiet case, three when the floor was cleared. Same
    reasoning as :func:`ceiling_spend`: the trips are sequential, so overhead is roughly
    count x RTT, and with the ledger a network away each one is added latency on every
    request rather than the microseconds it cost under SQLite.

    The short window is contained by the baseline window (`breaker._evaluate` takes
    ``max`` of the two), so a single scan bounded by the baseline cutoff can produce both
    sums with a CASE. `ORDER BY id DESC LIMIT 1` matches `active_breaker` exactly rather
    than approximating it — the two must not disagree about which row is "the" open one.

    Both aggregates are computed even when the caller will short-circuit below the floor,
    deliberately and for the same reason `ceiling_spend` does it: the unused one rides
    along in a scan that has to happen anyway, and branching to avoid it reintroduces the
    second query on the path where the breaker is actually being exercised.
    """
    conn = connect()
    short_cutoff = iso_seconds_ago(window_s)
    baseline_cutoff = iso_seconds_ago(baseline_s)

    # `feature IS NULL` keeps `window_spend`'s meaning: untagged traffic is its own scope,
    # never the project total. Getting this wrong makes the untagged breaker trip on any
    # tagged feature's burn -- and untagged traffic is usually production.
    if feature is None:
        spend_where = "project_id = ? AND feature IS NULL AND ts >= ?"
        spend_params: tuple = (project_id, baseline_cutoff)
    else:
        spend_where = "project_id = ? AND feature = ? AND ts >= ?"
        spend_params = (project_id, feature, baseline_cutoff)

    sql = (
        "SELECT b.id AS breaker_id, b.mode AS breaker_mode, b.opened_at AS breaker_opened_at, "
        "       s.short_spend, s.baseline_spend "
        "  FROM (SELECT 1) AS anchor "
        "  LEFT JOIN (SELECT id, mode, opened_at FROM breaker_events "
        "              WHERE scope = ? AND closed_at IS NULL "
        "              ORDER BY id DESC LIMIT 1) b ON TRUE "
        "  LEFT JOIN (SELECT COALESCE(SUM(CASE WHEN ts >= ? THEN cost_usd END), 0) AS short_spend, "
        "                    COALESCE(SUM(cost_usd), 0) AS baseline_spend "
        f"               FROM requests WHERE {spend_where}) s ON TRUE"
    )
    with _lock:
        row = conn.execute(sql, (scope, short_cutoff) + spend_params).fetchone()

    open_row = None
    if row["breaker_id"] is not None:
        open_row = {
            "id": row["breaker_id"],
            "mode": row["breaker_mode"],
            "opened_at": row["breaker_opened_at"],
        }
    return open_row, float(row["short_spend"]), float(row["baseline_spend"])


def open_breaker(scope: str, mode: str, metric: dict[str, Any]) -> int:
    conn = connect()
    with _lock:
        cur = conn.execute(
            # RETURNING id: see record_annotation. The breaker's id is used to reopen it
            # on a half-open re-trip, so it has to come back from the insert.
            "INSERT INTO breaker_events (scope, mode, trigger_metric, opened_at) "
            "VALUES (?, ?, ?, ?) RETURNING id",
            (scope, mode, json.dumps(metric), now_iso()),
        )
        conn.commit()
        return int(cur.lastrowid)


def reopen_breaker(breaker_id: int, metric: dict[str, Any]) -> None:
    """Push a half-open breaker's clock forward after it re-tripped on re-evaluation."""
    conn = connect()
    with _lock:
        conn.execute(
            "UPDATE breaker_events SET opened_at = ?, trigger_metric = ? WHERE id = ?",
            (now_iso(), json.dumps(metric), breaker_id),
        )
        conn.commit()


def close_breaker(scope: str, reset_by: str) -> int:
    conn = connect()
    with _lock:
        cur = conn.execute(
            "UPDATE breaker_events SET closed_at = ?, reset_by = ? "
            "WHERE scope = ? AND closed_at IS NULL",
            (now_iso(), reset_by, scope),
        )
        conn.commit()
        return cur.rowcount
