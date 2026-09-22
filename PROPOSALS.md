# PROPOSALS

Open questions and suggested changes from a full read of `README.md`, `CONTEXT.md`,
`ARCHITECTURE.md`, and `PLAN.md`, plus everything that surfaced while building the Phase 1
proxy.

Each item states what is wrong or missing, why it matters, and a recommendation. Items are
applied to `README.md` / `CONTEXT.md` / `ARCHITECTURE.md` **only after a human approves
them** — this file is the staging area, not the record.

**Applied so far:** A1–A6, B1–B7, B15–B18, C1, C2, C4. Everything else is still a proposal; if you find a
new contradiction, add it here and raise it rather than editing one source doc to match
another (the one you "fixed" may have been the correct one).

Status key: **OPEN** — needs a decision · **RESOLVED / DONE / SHIPPED** — closed, kept for the record.

40 items: A1–A7 (contradictions), B1–B22 (gaps), C1–C7 (verification), D1–D4 (research findings).

---

## A. Contradictions between the source-of-truth documents

These are places where two documents state different things, so no implementation can
satisfy both. They matter more than the gaps in §B: a judge who reads two of our documents
and finds them inconsistent stops trusting the third.

### A1 — Circuit breaker detection: flat threshold or baseline ratio? · **RESOLVED & SHIPPED**

| Document | Specifies |
| --- | --- |
| `CONTEXT.md` §5C | flat `> $20 in 5 minutes`, response `403` |
| `PLAN.md` Phase 3 | flat `> $20 in 5 minutes`, blocks the key |
| `ARCHITECTURE.md` §6 | trailing 5-min spend **ratio** vs the same tag's 7-day baseline, plus an absolute floor; throttle → `429`, revoke → key cut |

These fire on completely different traffic. The flat threshold cannot distinguish a
legitimately expensive batch job from a runaway retry loop — both spend $20 fast. The ratio
detector can, but it needs 7 days of baseline data that a 48-hour-old system does not have.

**Prior art.** Google's SRE Workbook chapter on
[alerting on SLOs](https://sre.google/workbook/alerting-on-slos/) documents exactly this
tension: a single window either detects fast and produces false positives, or is precise
and detects too late. The established fix is a **multi-window, multi-burn-rate** alert —
require a short window *and* a long window to both breach before firing, with the short
window roughly **1/12 the duration** of the long one (reference config: 1h at 14.4x burn
paired with 6h at 6x).

**Where the textbook version breaks for us, and what shipped instead.** The first draft of
this proposal said to port that literally: add a 1-hour window with its own absolute dollar
threshold and require both. That is wrong here, and the reason is latency. MWMBR alerts page
a *human* about SLO burn, where an hour of detection delay is acceptable. A 1-hour window
carrying an equivalent dollar threshold cannot trip until a full hour of sustained burn has
accumulated, because the opening minutes of an incident are diluted by the quiet period in
front of them. An hour is a fine delay for a pager and a catastrophic one for a leaked API
key — and it would have broken the demo, where the leaked-key moment has no prior history at
all.

**Shipped:** the long window as a **rate baseline** rather than a second absolute threshold.
Both conditions must hold:

1. **Floor** — trailing 5-minute spend clears `BREAKER_WINDOW_USD` (default `$20`, the
   number from `CONTEXT.md` §5C and `PLAN.md` §3). Detection stays as fast as this allows.
2. **Burst** — that window's spend *rate* is at least `BREAKER_BURST_RATIO` (default 3x) the
   trailing `BREAKER_BASELINE_WINDOW_S` (default 1h) average rate.

| Traffic | Floor | Ratio | Result |
| --- | --- | --- | --- |
| Leaked key, no history | cleared | 12x (the ceiling) | **trips on the first check** |
| Legitimately expensive but steady | cleared | ~1x | does not trip |
| Burst on top of steady traffic | cleared | >3x | **trips** |

The middle row is the entire point, and it is the failure mode a flat floor has: a feature
that simply costs more than the threshold trips every five minutes forever, and the
operator's only remedy is raising the threshold until the breaker is useless for that
project. The suite has a dedicated assertion for it (`test_burst_detection`).

This keeps both source documents true rather than picking a winner: the floor is
`CONTEXT.md`'s flat threshold, and the baseline comparison is `ARCHITECTURE.md`'s ratio with
a 1-hour lookback instead of 7 days — needing no accumulated training data and working from
the first hour. The ratio is bounded by the window sizes (3600/300 = 12x max), so a
threshold above the ceiling makes the breaker unfirable; `BREAKER_BURST_RATIO=0` reverts to
the flat detector as a live escape hatch.

**Applied to:** `ARCHITECTURE.md` §6, `CONTEXT.md` §5C and §4 system flow, `README.md`
circuit-breaker section and config table, `.env.example`, `proxy/breaker.py`,
`proxy/config.py`, `tests/test_proxy.py`.

### A2 — Breaker response code: `403` or `429`? · **RESOLVED — both adopted, docs updated**

`CONTEXT.md` §5C says `403 Forbidden` and only describes revocation. `ARCHITECTURE.md` §6
describes two modes where throttle returns `429`.

`429` is the correct code for a throttle: it is retryable, and every provider SDK already
understands it and will back off on its own. `403` on a rate limit makes well-behaved
clients treat a temporary condition as permanent.

**What Phase 1 shipped:** `429` + `Retry-After` for throttle, `403` for revoke.

**Recommendation:** adopt both, and add the missing throttle mode to `CONTEXT.md` §5C.
Throttle is also the better demo — "one feature got cut off and everything else kept
serving" is a more interesting claim than "we turned it off".

**Applied to:** `CONTEXT.md` §5C (throttle mode documented alongside revoke) and §4 system
flow, `README.md` config table. The proxy already behaved this way; the docs now match.

### A3 — Treasurer loop interval: 30s or 3s? · **RESOLVED in docs — 30s default, 3s demo box** (number still pending C3)

`ARCHITECTURE.md` §5 and `README.md` say every 30 seconds. `PLAN.md` Phase 3 says every 3
seconds. `CONTEXT.md` §5B says "every few seconds".

3s is 1,200 Prava sandbox calls an hour if the loop probes on every tick. `AGENTS.md`
already anticipates sandbox rate limits as a plan-changing blocker.

**Recommendation:** 30s for the documented default; let the demo box run at 3s via
`TREASURER_INTERVAL_S` so the on-stage top-up is not a 30-second silence. Shivam should
confirm the sandbox rate limit before this is settled. Owner: Shivam.

**Applied to:** `ARCHITECTURE.md` §5 and `.env.example`, both stating 30s as the documented
default with `TREASURER_INTERVAL_S` as the demo-box override, and both flagging that the
number is unconfirmed until C3 lands. Nothing in the Treasurer is built yet, so this is a
constraint recorded ahead of the code rather than a change to it.

### A4 — Cost estimation: `tiktoken` or historical p95? · **RESOLVED — documented as one design**

`ARCHITECTURE.md` §2 step 3 estimates from "p95 cost for (project, endpoint, model) over
trailing 7d, from Redis cache". `CONTEXT.md` §5A estimates with `tiktoken` for input plus a
task-classification heuristic for output.

These are not alternatives — they are the two halves of one estimator, and the documents
present them as competing. `tiktoken` counts input *exactly* and cannot know output length;
history predicts output well and is redundant for input.

**Recommendation:** state it as one design — exact input via `tiktoken`, predicted output
via heuristic, with trailing p95 as the cold-start fallback before per-feature history
exists. This is a documentation fix, not a code change. Owner: Ammar + Shubh.

**Applied to:** `ARCHITECTURE.md` §2 (new "The estimator is one design, not two" subsection,
and step 3 of the lifecycle rewritten) and `CONTEXT.md` §5A. Zero code — the predictor is
Ammar's Phase 2 work and now has an unambiguous spec to build against.

### A5 — Datastore: Postgres, SQLite, or Redis? · **DECIDED & SHIPPED — Shubh, 2026-08-01**

`CONTEXT.md` §4 says Postgres **and** Redis. `PLAN.md` Phase 1 says "Postgres/SQLite". The
Redis Lua reservations in `ARCHITECTURE.md` §2 are meaningless against SQLite, and **no
phase in `PLAN.md` assigned standing up Redis to anyone.**

**What Phase 1 shipped:** SQLite, no Redis, `reservation_id` written NULL. This is the
honest version — reservations without Redis would be theatre.

**Decision: no Redis in the 48-hour build. Reservations get implemented anyway, in-process.
Owner: Shubh, Phase 2.**

The reasoning: Redis is not what makes authorize/capture correct — *serialization* is.
`ARCHITECTURE.md` §2 reaches for a Lua script because Lua runs atomically inside Redis, and
Redis is the only shared point between multiple proxy replicas. **With a single proxy
process — which is the entire demo and most self-hosted installs — an `asyncio.Lock` around
the same read-modify-write gives an identical guarantee for none of the operational cost.**
Redis becomes load-bearing at replica #2, not before.

So Phase 2 adds real reservations against the existing SQLite ledger: reserve the estimate
before forwarding, release the difference on capture, TTL-expire abandoned holds. The
thousand-simultaneous-requests failure mode §2 exists to prevent is genuinely fixed, the
demo can show a ceiling actually holding, and the upgrade path is one function moving to a
Lua script. That is ~40 lines instead of a new container, a new dependency, a new failure
mode in the request path, and a `docker-compose` service nobody owns.

**Applied to:** `CONTEXT.md` §4 (now reads "Postgres for the ledger; Redis is
post-hackathon") and `ARCHITECTURE.md` §2, which now states that what makes authorize/capture
correct is serialization rather than Redis, and that the primitive is process-local until
replica #2.

**Prior art:** [LiteLLM](https://docs.litellm.ai/docs/proxy/multi_tenant_architecture) —
the closest open-source equivalent to Meter — does run FastAPI + Redis + Postgres. Worth
knowing that the reference architecture agrees with `ARCHITECTURE.md` at scale; it just
isn't what a 48-hour demo needs to prove the concept.

**Shipped 2026-08-01 (Shubh):** `proxy/budget.py`. Holds live in a process-local dict
behind an `asyncio.Lock`; `authorize()` counts outstanding holds alongside settled ledger
spend inside one critical section, and `reservation_id` is now written to every row
instead of NULL. Two implementation notes that were not obvious from the design:

* **Holds are deliberately not persisted.** A restart drops them, which is correct — a
  restart also drops every in-flight request they were covering. Settled spend is in the
  ledger; only the in-flight delta lives in memory.
* **The release must happen after the ledger row lands, not alongside it.** Capture is
  scheduled, not awaited, so releasing next to `_schedule_capture` leaves a window where
  the cost is counted by neither the hold nor the ledger. The release is therefore done
  *inside* the capture task.

The self-check fires 40 concurrent authorizes at a ceiling admitting four and asserts
exactly four pass — without serialisation all 40 do.

### A6 — Budget source of truth: `meter.yaml` or the `projects` table? · **RESOLVED & SHIPPED — YAML wins**

`README.md` and `ARCHITECTURE.md` §9 make budget-as-code a *lock-in mechanism* — limits live
in the customer's repo and change by pull request. `ARCHITECTURE.md` §4 also has
`projects.ceiling_usd_day` as a column. Nothing says which wins, and nothing loads the YAML.

**Recommendation:** `meter.yaml` is the source of truth; a loader syncs it into the table at
boot; the table is a read cache the hot path uses. Say so in `ARCHITECTURE.md` §4. Roughly
40 lines of loader — see B7 for the enforcement half.

**Applied to:** `ARCHITECTURE.md` §4, which now states the precedence explicitly and carries
the two rules that follow from it: the loader must be idempotent, and it must reject a config
whose feature ceilings exceed their project's.

**Shipped 2026-08-01 (Shubh):** `budget.load_meter_yaml()` at boot, feeding
`db.replace_budgets()`. Both rules are implemented and asserted. One thing the design did
not anticipate: **the loader has to REPLACE, not upsert.** Upserting makes deletion
impossible — a ceiling removed from `meter.yaml` would keep being enforced from a stale
row, which is precisely the failure budget-as-code exists to prevent, and it would be
invisible because the file no longer mentions it. `replace_budgets` clears every ceiling
and rebuilds from the file in one transaction, so what the file says is what is enforced,
including when the file says nothing.

### A7 — Quickstart promises a local stack that Compose does not run · **OPEN — found 2026-09-22**

| Source | Says |
| --- | --- |
| `README.md` Deployment | `docker compose up` starts proxy, worker, Postgres, Redis, and dashboard. |
| `compose.yaml` / `CONTEXT.md` §6a | One `meter` backend service only; `DATABASE_URL` must name an external Postgres, Redis is intentionally absent, and the dashboard runs separately. |

The quickstart cannot succeed as written on a fresh checkout: the service refuses to boot without
`DATABASE_URL`, and a reader expecting a dashboard on a Compose port will not get one. This is
especially damaging because it is the first command the README gives a prospective user.

**Recommendation:** choose one explicit product story: either add owned local Postgres and dashboard
services to Compose, or change the README quickstart to require an external Postgres and document
the separate dashboard command. The current single-service implementation matches the latter with
the least code, but a human should approve it before source-of-truth docs are changed.

---

## B. Design gaps

Things no document specifies that an implementation is forced to decide anyway.

### B1 — Nothing defines how a request picks its provider · **RESOLVED — compat path verified live**

`README.md` promises a one-line base-URL swap for "every model provider", but an Anthropic
SDK calls `/v1/messages` with an `x-api-key` header and will never touch
`/v1/chat/completions`. No document says how the proxy chooses a provider.

**What Phase 1 shipped:** both routes mounted; routing by model prefix (`claude-*` →
Anthropic), overridable with `X-Meter-Provider`.

**Verified live 2026-08-01.** `https://api.anthropic.com/v1/chat/completions` **exists**.
Three independent signals, from a real key:

| Probe | Result | Reading |
| --- | --- | --- |
| `POST /v1/chat/completions` | request-level error, **not** `404` | the route is real |
| `POST /v1/definitely-not-a-route` (control) | `404 not_found_error` | absent routes do 404, so the above is meaningful |
| error envelope on the compat path | `{"error":{"code":…,"param":null,…}}` | **OpenAI's** shape, not Anthropic's `{"type":"error",…}` — this is a genuine compatibility layer |

It also accepts **both** `x-api-key` and `Authorization: Bearer`, so the header substitution
works whichever style the caller's SDK uses. Model-prefix routing is confirmed end to end:
a `claude-*` model posted to `/v1/chat/completions` reached Anthropic (not OpenAI), the
provider's status and body passed through unmodified, and every attempt produced a ledger row
attributed to `anthropic` at zero cost — which also confirms the `ARCHITECTURE.md` §7
provider-error behaviour against a real provider rather than a fake one.

The alternative — writing a bidirectional OpenAI↔Anthropic translator including for streams —
remains a multi-day job and is now definitively unnecessary.

### B2 — Nothing defines what the client puts in `Authorization` · **RESOLVED — README quickstart updated**

The entire trust story is that provider keys never leave the customer's VPC and Meter is
"the control plane, not a key custodian" (`ARCHITECTURE.md` §8). That only works if the
caller sends a **Meter** key and the proxy substitutes the provider key. No document says
this, and it is the first thing anyone integrating will ask.

**What Phase 1 shipped:** Meter key accepted in either `Authorization: Bearer` or
`x-api-key`; substitution on the way out; outbound headers built from a whitelist.

**Recommendation:** add three sentences to the `README.md` quickstart. It is the single
most-asked integration question and currently has no written answer.

**Applied to:** `README.md` quickstart — a new "What goes in the Authorization header"
subsection with the diff a caller actually makes, the `x-api-key` note for Anthropic SDKs,
and why the whitelist means a client credential cannot reach the provider.

### B3 — Reservation TTL contradicts streaming duration · **DONE — shipped with reservations, 2026-08-01**

`ARCHITECTURE.md` §2 sets a 120s reservation TTL "so a crashed worker releases its holds".
`README.md` says the proxy holds SSE streams open "for minutes at a time".

Any stream longer than two minutes has its reservation silently expire mid-flight. The
ceiling then stops holding during exactly the longest, most expensive calls — and the
failure is invisible, because nothing errors.

**Recommendation:** heartbeat-extend the reservation while the stream is alive (re-`EXPIRE`
every 30s from the streaming loop), and keep the TTL short so a genuine crash still
releases quickly. Roughly 15 lines. Owner: Shubh.

**Applied to:** `ARCHITECTURE.md` §2, which now spells out that the naive fixed TTL expires
mid-flight on the longest calls *and fails silently* — nothing errors when a reservation
disappears, the ceiling just stops holding. The requirement is recorded ahead of the
reservations themselves (A5, Phase 2) so it lands with them rather than being discovered
after.

**Shipped 2026-08-01 (Shubh).** `budget.extend()` plus a heartbeat in `_forward_stream`'s
chunk loop: `RESERVATION_TTL_S=120` kept short, pushed forward every
`RESERVATION_HEARTBEAT_S=30` for as long as bytes keep arriving. The stream is its own
clock, so no background task is needed. `extend()` is deliberately lock-free — a single
float write on the single-threaded event loop — because taking the async lock per chunk
would serialise every stream in the process against every authorize. Self-check asserts
both halves: that `extend()` rescues a hold whose expiry has passed, and that the
rescued hold still counts against the ceiling.

### B4 — Client disconnect is harder than the doc implies · DONE

`ARCHITECTURE.md` §2 says a mid-stream disconnect should write a row with
`estimated = true`. It does not mention that Starlette cancels the response generator on
disconnect, which cancels the capture path too — so the promised row is exactly what gets
lost.

**Resolved in Phase 1:** capture is scheduled before any `await` in the generator's
`finally` block, so it survives the cancellation. Verified end to end: a client that hangs
up after three chunks still produces a priced, `estimated`, status-499 ledger row.

**Recommendation:** add a sentence to `ARCHITECTURE.md` §2 so the next person to touch the
streaming path does not "clean up" the ordering.

**Applied to:** `ARCHITECTURE.md` §2 and `proxy/README.md`, both stating that scheduling is
synchronous and must precede the first `await` in teardown. Worth the words because the
tidied-up version passes every test — the failure only appears when a real client hangs up.

### B5 — Breaker and fail-open contradict each other during a Redis outage · **RESOLVED — documented, and revocation already fails closed**

`ARCHITECTURE.md` §7: Redis unreachable → fail-open, no enforcement, loud alert. But the
breaker is the leaked-credential defence. Fail-open means a leaked key burns freely during
precisely the incident the breaker exists for.

This is a defensible trade — availability over enforcement — but it is currently an
undocumented one, and a security-minded judge will find it.

**Recommendation:** state it explicitly in §7 as an accepted risk, and consider making
revocation the one check that fails *closed*.

**Finding while applying this: revocation already fails closed, by construction.** The
`revoked_at` flag is read during authentication, and authentication is deliberately not
subject to `FAIL_MODE` (serving a request nobody can be billed for is worse than a `503`).
The revocation check then consults that already-resolved record instead of issuing its own
query — so there is no datastore call in the revocation path that *could* fail open. The
half of the breaker that matters most during an incident is the half that survives one.

**Applied to:** `ARCHITECTURE.md` §7 (new "Where fail-open and the circuit breaker disagree"
subsection) and `proxy/README.md`. Also pinned down by `test_revocation_fails_closed`, which
monkeypatches the ledger to raise and asserts a revoked key is still blocked with `403` —
including with the breaker disabled entirely.

### B6 — `prompt_hash` has no defined normalization · **RESOLVED — written into ARCHITECTURE §4**

`ARCHITECTURE.md` §4 calls `prompt_hash` what makes duplicate-call and cache-candidate
detection "a single query rather than a research project" — but never says what goes into
the hash, and the answer changes the results completely.

**What Phase 1 shipped, and why:** model **in** (same prompt to two models is two cache
entries); system prompt **in** (it is sent and paid for); whitespace collapsed (reindented
templates are the same prompt); sampling params **out**.

Excluding `temperature`/`top_p`/`seed` is the load-bearing choice: a retry storm re-sends an
identical prompt, often with jittered sampling. Including them would make every retry look
like a distinct prompt and would break retry-loop detection — the headline optimization
feature — in the one scenario it exists for.

**Recommendation:** write this into `ARCHITECTURE.md` §4 so the Analyst agent is built
against a defined hash rather than reverse-engineering one.

**Applied to:** `ARCHITECTURE.md` §4 as a field-by-field table with the reasoning per row,
framed as part of the schema rather than an implementation detail — because changing it later
silently changes every dedupe result computed before the change.

### B7 — Per-project ceilings are specified but nothing enforces them · **DONE — shipped 2026-08-01 (Shubh)**

`projects.ceiling_usd_day` is in the schema, `meter.yaml` is in the README, `CONTEXT.md`
§3 "Check: verifies if the team has enough budget" is step 3 of the system flow — and no
phase in `PLAN.md` assigned anyone to build it. Every phase task was about the Treasurer,
the breaker, or the predictor.

Budget enforcement is one of Meter's three pillars ("Budget" in the README's own table).
It was the only one with no owner.

**Owner: Shubh, Phase 2.** Scope, in dependency order:

1. **`meter.yaml` loader** (resolves A6): parse the file at boot, upsert into `projects`
   and a new `feature_budgets` table, treat the YAML as source of truth and the tables as
   a read cache the hot path can hit. ~40 lines.
2. **Pre-flight ceiling check** in `_proxy()`, between the breaker check and forwarding:
   `db.project_window_spend(project_id, 86400)` against `ceiling_usd_day`, plus the
   per-feature ceilings from `meter.yaml`. Reject with `429` and a header naming which
   ceiling was hit. ~20 lines — the query half already exists.
3. **Fold into the reservation** once A5's reservations land, so the check and the spend
   are atomic rather than a read-then-call race.

**Design note worth stealing:** [LiteLLM enforces budgets
hierarchically](https://docs.litellm.ai/docs/proxy/multi_tenant_architecture) — key → user
→ team → org, where a request is blocked if *any* level on its path is over, and a child
budget cannot exceed its parent's. Meter's `project → feature` nesting in `meter.yaml` is
the same shape, so the same rule applies: validate at load time that the feature ceilings
sum to no more than the project ceiling, and reject the config if they don't. Catching that
in a pull request is the entire pitch of budget-as-code, and it is a 5-line check.

> ⚠ **The last sentence above is wrong, and B17 corrects it.** The rule described in the
> sentence before it — "a child budget cannot exceed its parent's" — is LiteLLM's actual
> rule and is a *per-child* check. Restating it as a check on the *sum across siblings* is
> a different rule that the cited prior art does not support, and it inverts the failure
> mode. Left in place rather than edited away, because the drift is the useful part of the
> record. See B17 for what shipped.

**Applied to:** `ARCHITECTURE.md` §4, which now records both constraints on the loader —
idempotent, and rejects a config whose feature ceilings exceed their project's — alongside
the `meter.yaml`-wins precedence from A6. The enforcement code is Phase 2; the spec it has to
satisfy is now written down rather than living only in this file.

**Shipped 2026-08-01 (Shubh).** All three numbered steps, folded into the reservation as
step 3 anticipated — the ceiling check and the hold happen in one critical section, so it
is never a read-then-call race. Both loader constraints are implemented and asserted.
Refusal is `429` with `X-Meter-Budget-Scope` / `-Ceiling-Usd` / `-Spend-Usd`, because a
project can have several ceilings and the caller cannot see `meter.yaml`.

Two behaviours chosen where the spec was silent, both erring toward availability, since
Meter is in the critical path and a cost tool that takes down production is not a cost
tool:

* **A malformed `meter.yaml` boots with no ceilings** rather than refusing to boot.
* **Ceilings of `0` or less are ignored,** not enforced literally. A `0` enforced as
  written blocks every request in the project, and nobody commits that on purpose.

Both are the same fail-open posture `FAIL_MODE` already takes, and both are loud in the
log. B17 later applied the same reasoning to the third case — feature ceilings summing past
their project's total now warn and keep enforcing, rather than rejecting the project's
budgets outright.

### B8 — Reconciliation after a ledger outage can double-count · DONE

`ARCHITECTURE.md` §7 says to buffer writes durably and reconcile on recovery, but does not
say what makes a replayed write idempotent. Replaying a buffer without a stable key inflates
spend on recovery — and inflated spend is what triggers the Treasurer to buy credits.

**Resolved in Phase 1:** the request id is generated by the proxy before the upstream call,
not by the database, and the ledger write is `INSERT OR REPLACE` on it. A replay overwrites
rather than duplicates.

**Recommendation:** note the constraint in §7 so the Postgres port keeps it.

### B9 — `POST /v1/annotate` is documented but assigned to nobody · **DONE — built 2026-08-01 (Shubh)**

`README.md` documents it as attribution rung 3 with a working `curl` example.
`ARCHITECTURE.md` §4 calls the `requests × annotations` join on `trace_id` "the interesting
join" and cost-per-outcome "a margin metric... roughly forty lines of code away". It does
not appear in any `PLAN.md` phase for any of the four of us.

The proxy already records `trace_id` on every row, so the join's expensive half is done.

**Recommendation:** build it — it is genuinely ~40 lines (one table, one endpoint, one
query) and it is the difference between "another cost dashboard" and a margin tool. If it
is being cut, cut it from `README.md` too; documenting an endpoint that 404s is worse than
not having it. Suggested owner: Shubh or Tanay, Phase 3.

**Built 2026-08-01 (Shubh), alongside the Phase 2 proxy work. Ratified the same day** —
raised for a decision because it was taken without a formal assignment, and kept: the
endpoint was already documented in `README.md` with a working `curl` and called "the margin
metric" in `ARCHITECTURE.md` §4, so what was missing was an owner, not a decision.

Two deliberate deviations from `ARCHITECTURE.md` §4's `annotations
(id, trace_id, outcome, value_usd, ts)`:

* **`project_id` added — explicitly approved 2026-08-01, on security grounds.** A
  `trace_id` is a caller-supplied string. Without scoping, any key could annotate — or,
  via the returned totals, *read the cost of* — another project's traces. The self-check
  plants an identical trace id under a second project and asserts it is not counted.
* **Response returns the trace's cost, request count and margin**, not just an ack. The
  number the endpoint exists to produce is dollars-per-outcome, and making the caller run
  a second query to see what they just annotated is a worse API for no saving.

`value_usd` is optional; when it is absent `margin_usd` comes back `null` rather than `0`,
because "broke even" and "we don't know the value" are different facts.

### B10 — `docker compose up` does not exist · **DONE — shipped 2026-08-01 (Shubh)**

It is the first command in the `README.md` quickstart and the whole of
`ARCHITECTURE.md` §8. No phase assigns a `Dockerfile` or a `compose.yaml`.

**Shipped:** single-service `Dockerfile` (python:3.12-slim, `uvicorn proxy.app:app`,
`METER_DB_PATH=/data/meter.db` on a named volume) + `compose.yaml` (port 8080,
`env_file: .env`, read-only mounts for `meter.yaml` and `pricing/` so budget-as-code
and pricing change by PR, not by exec). Verified: `docker compose config` valid, image
builds, container boots, healthz 200, treasurer loop starts. The full five-service
version is still future work once the other components exist; the dashboard remains a
host-side dev process reading `meter.db` read-only.

### B11 — Cross-model routing doubles spend if it sits in the request path · **RESOLVED — offline, and now rendered**

`CONTEXT.md` §5A says "Allow the proxy to send the same prompt to OpenAI and Anthropic to
log efficiency differences". `PLAN.md` Phase 3 has Ammar building it as an offline *script*
over a fixed prompt suite.

The script version is right. Shadow-calling a second provider on live traffic doubles the
customer's bill — inside a tool whose entire pitch is cost control. A judge will notice.

**Recommendation:** correct `CONTEXT.md` §5A to describe the offline script, and keep the
proxy single-provider per request. Owner: Ammar.

**RESOLUTION (2026-08-09, Shubh).** Settled the way this entry implies: the proxy does not
shadow-call a second provider on live traffic. `scripts/cross_model.py` replays prompts
already measured on the baseline model and reports the delta offline.

What was missing was the last hop. The script writes JSONL on the proxy's host; the
dashboard runs somewhere else entirely and reads only Postgres, so the comparison was
unreachable by the thing meant to display it — which is why the Model Efficiency view sat
blocked on "there is no data to render". There is now a `model_efficiency` table,
`--push` / `--push-only` on the script, and a card that reads it.

The card decomposes the gap into **rate** and **verbosity** rather than printing one
multiplier, and states on its face that it measures no quality at all, so "cheaper" is half
a trade-off rather than a recommendation.

⚠ **The live replay still has not been run.** It needs a funded Anthropic key and spends
real money against a cap, so it is a deliberate decision rather than something to trigger
in passing. Until then the card renders its empty state, which names the command.

### B12 — Nothing measured Meter's own overhead · DONE

`ARCHITECTURE.md` §8 says "publish the overhead number" and sets a p50 under 5ms target, but
nothing in the design computes it, and a number you cannot derive from your own ledger is
not a number to put on a slide.

**Resolved in Phase 1:** an `overhead_ms` column on `requests` (an addition to the
`ARCHITECTURE.md` §4 schema), plus `X-Meter-Overhead-Ms` on every response so a caller can
verify the claim without access to our dashboard.

**Measured:** p50 **+1.49 ms** wall-clock, **0.29 ms** self-reported in-process, over 300
requests against a local fake upstream. Caveat for the pitch: loopback, no TLS to a real
provider, single client. Treat it as a floor. A real measurement needs a provider round trip.

### B13 — Missing indexes on the hot-path table · DONE

`ARCHITECTURE.md` §4 defines `requests` but no indexes. The breaker's rolling-window query
runs on **every single request**, so an unindexed scan of that table degrades the whole
proxy as the ledger grows — worst on the busiest day.

**Resolved in Phase 1:** `(project_id, ts)` for the breaker window, `(trace_id)` for the
cost-per-outcome join, `(prompt_hash)` for duplicate detection. Should be carried into the
Postgres schema. Owner to carry over: Shivam.

### B14 — The Visa Intelligent Commerce track has no architectural surface · OPEN

`CONTEXT.md` §1 lists VIC as one of four tracks. Nothing in `ARCHITECTURE.md` mentions Visa,
and no `PLAN.md` task targets it. We are currently entered in a track we have not built for.

**Recommendation:** either find the VIC hook (the scoped single-use virtual card the
Treasurer already generates via Prava is plausibly the story) and write it into
`ARCHITECTURE.md` §5, or drop the track from `CONTEXT.md`. Needs a decision from whoever
read the track rules. Owner: Tanay (owns the sponsor docs per `PLAN.md` Phase 0).

---

### B15 — Streaming responses arrive gzipped; reading them raw breaks the proxy · **DONE**

Found by running the SSE parser against the real Anthropic API instead of a fixture.

httpx puts `Accept-Encoding: gzip, deflate` on every outbound request by default, and real
providers honour it on SSE. The streaming path read the body with `aiter_raw()`, which yields
the still-compressed bytes. That failed **silently, in both directions at once**:

1. The usage tap parsed gzip as SSE, found no `data:` lines, and downgraded every streamed
   row to a byte estimate — so streamed spend was quietly wrong rather than visibly missing.
2. The proxy forwarded compressed bytes while stripping `content-encoding` as hop-by-hop, so
   the client received gzip labelled `text/event-stream` — **no SSE client could read the
   stream at all.**

Every existing test passed throughout, because a fake uvicorn upstream does not compress. This
is the class of bug that only a real provider produces.

**Fixed:** `aiter_bytes()` instead of `aiter_raw()`. Compression stays on the proxy↔provider
hop, which is the one that crosses the internet; the client is served what the headers
actually promise. Guarded by `test_gzipped_upstream_stream`, which stands up a real gzipping
socket server — the only test in the suite that does, and deliberately so.

### B16 — Anthropic's compat endpoint merges usage into the final content chunk · **DONE**

The proxy injects `stream_options: {include_usage: true}` on OpenAI-shaped streams and strips
the resulting usage chunk when the caller did not ask for one. Safe against OpenAI, which
emits usage as a separate trailing chunk with `choices: []`. **Not** safe in general:
Anthropic's compatibility endpoint merges usage into the final *content* chunk — the one
carrying `finish_reason: "stop"`.

The shipped rule was already correct (drop only when `choices` is empty), so nothing needed
fixing in the request path — but the reasoning was undocumented and a future "simplification"
to "drop the chunk with usage in it" would silently truncate every compat stream. Now recorded
in `ARCHITECTURE.md` §2 and `proxy/providers.py`, and pinned by a test using the real observed
chunk shape.

The consequence we accept: on that endpoint the caller sees a `usage` field it did not request.
Forwarding an extra field is a much smaller sin than deleting the end-of-stream signal.

### B17 — Rejecting an over-allocated project removes all its enforcement · **RESOLVED & SHIPPED — rule changed, 2026-08-01**

`ARCHITECTURE.md` §4 (via A6/B7) **used to require** the loader to "reject a config whose
feature ceilings sum to more than their project's ceiling". It was first implemented
exactly as specified, then raised here rather than quietly reinterpreted, because building
it surfaced two problems the rule's one-line statement hides. The decision below changed
the rule; §4 and the loader now match the recommendation, not the original text.

**1. "Reject" is the most dangerous possible outcome here.** A rejected project gets *no*
ceilings at all — not the project one, not the feature ones. So the response to "you
allocated slightly too much" is to switch budget enforcement **off** for that project. The
failure mode is inverted: a config that is too restrictive on paper results in nothing
being restricted. An operator who fat-fingers `500` to `5000` on one feature loses the
project ceiling that would have caught it. The alternative — apply the project ceiling,
warn about the features — keeps the outer limit intact, which is the one that actually
bounds spend.

**2. Over-allocation is a legitimate pattern, not only a typo.** The rule assumes feature
ceilings are a partition of the project budget. In practice they are usually *independent
caps on unrelated things*: "no single feature may exceed \$200/day, and the project may not
exceed \$800/day" is a coherent policy with six features at \$200 each. Every feature
maxing out simultaneously is exactly what the project ceiling exists to stop, and it stops
it correctly — the two limits are checked independently, so nothing can actually breach the
project total no matter what the features sum to. The validation forbids a configuration
the runtime handles correctly.

Worth noting the cited prior art does not do this either: LiteLLM's rule is that a *child*
budget cannot exceed its *parent's* — a per-feature check (`feature <= project`), not a sum
across siblings. That check has neither problem above.

**Recommendation:** replace the sum rule with LiteLLM's actual one — reject a config where
any single feature ceiling exceeds its project's, and *warn* (do not reject) when the
siblings sum to more. That keeps the review-time catch for the mistake that matters,
preserves the outer ceiling when the config is merely generous, and stops the loader from
disabling enforcement as a response to over-restriction. ~3 lines changed in
`budget.load_meter_yaml`, plus one line in `ARCHITECTURE.md` §4.

**Decided 2026-08-01 (Shubh): recommendation accepted, rule changed.** The sum rule was
never in the prior art it cited — the drift is visible inside B7's own paragraph, which
correctly describes LiteLLM's rule as "a child budget cannot exceed its parent's" (a
per-child check) and then restates it one sentence later as a check on the sum.

**Applied to:** `ARCHITECTURE.md` §4, which now carries three loader rules instead of two —
replace-don't-upsert, reject a single feature above its project, warn on the sibling sum.
`proxy/budget.py` implements all three; `meter.yaml.example` documents them.

**The claim the decision rests on is now asserted, not argued.** The self-check builds a
project with a \$10 ceiling and two \$8 features, fires 16 authorizes, and asserts exactly
10 pass — over-allocated features cannot breach the project total, because both ceilings
are evaluated independently in `authorize`. It also pins which scope reports the refusal in
each direction: a feature at its own limit is refused in the *feature's* name, a feature
with headroom stopped by the project total is refused in the *project's*. That second pair
came out of a failing assertion — the first version of the test assumed the project always
wins, which is wrong, and the 429 would have sent an operator to the wrong line of
`meter.yaml`.

### B18 — The treasury control plane is unauthenticated · **RESOLVED & SHIPPED — applied 2026-08-01, recommendation 1**

Found during the full-codebase audit (2026-08-01, runtime-verified). Every `/v1` route
authenticates the Meter key and scopes to its project (`_proxy`, `/v1/annotate`,
`/v1/breaker/reset` — the annotate scoping was explicitly approved on security grounds in
B9), but **none of the treasury surface does**: `/wallets`, `/wallets/seed`, `/topup`,
`/charge`, `/report`, `/charge-refusal`, `/mock-openai/billing`, `/mandates*`,
`/treasury/events`.

What that means in practice, verified live against a running proxy: an unauthenticated
`POST /charge` reached the Prava sandbox (the `AUTH_1001` returned was **Prava's** rejection,
not Meter's — the local `.env` keys are placeholders, so no money moved). With real
credentials, any process that can reach the port can top up, drain, or settle — the
money-moving half of the whole product, with no key and no project scoping.

No source-of-truth document says the treasury routes are public. `CONTEXT.md` §4 and
`proxy/README.md` describe them as demo surface, but demo does not have to mean
unauthenticated — the B9 decision shows the team already cares about this class of hole.

**Shipped (Shubh, 2026-08-01): recommendation 1 — auth on the money moves only.**
`treasury/routes.py` gains a `_authed_key` FastAPI dependency (same header handling and
fail-closed behavior as `proxy/app.py`, which cannot be imported there without a cycle)
applied to `/wallets/seed`, `/topup`, `/charge`, `/report`, `/charge-refusal`.
Read-only and demo routes stay open. Verified live inside the built container: all five
return 401 without a key, all five pass with one, reads still open.

Two extras landed in the same pass, both audit findings:

* **M3 — `/mandates` and `/mandates/sync` return a 503 envelope instead of a bare 500**
  when Prava is unreachable.
* **M4 — `/report` validates `transaction_id`** (`^[A-Za-z0-9_\-]{8,64}$`, matching both
  Prava's `txn_…` and simulated `sim_…` shapes) and returns 400 otherwise.

**Applied to:** `proxy/README.md` (auth note + the demo curls' header), `CONTEXT.md` §6a.

---

### B19 — B18's rule was applied to a list, not to a definition, and `/treasury/tick` fell outside it · **FIXED 2026-08-03; per-key scopes shipped 2026-08-09**

Found in the full-codebase audit (2026-08-03, runtime-verified against a running proxy).

**The bug, fixed without asking, because it is B18's own rule and not a new one.** B18
decided "authenticate the money moves, leave the reads open" and then enumerated the
routes: `/wallets/seed`, `/topup`, `/charge`, `/report`, `/charge-refusal`.
`POST /treasury/tick` is not on that list and was never authenticated — verified live,
returns 200 with no credential. It runs `treasurer.tick()` across **every wallet**: the
entire autonomous charging loop, on demand, for anyone who can reach the port. The only
thing standing between an anonymous caller and a real Prava charge was
`TREASURER_DRY_RUN` defaulting to true, which is a safety default, not an access control.
Now carries `Depends(_authed_key)`.

The lesson is the one worth writing down: B18 enumerated instead of defining, so a route
added later inherited "read" by omission. If a route can move money, it is a write —
regardless of which list it is on.

**⚠ This fix is partial, and the gap is worth stating plainly.** `WALKTHROUGH.md` publishes
a working Meter key (`mk_74e8…`, lines 14/67/135/153/159/227) on purpose, so a judge can
drive the deployed proxy from a browser without being provisioned. That key now also
satisfies `_authed_key` on `/treasury/tick`. So the route went from "anyone on the
internet" to "anyone who read the walkthrough" — a real reduction in exposure, and not the
one the word "authenticated" implies. Closing it properly needs something Meter does not
have: **scopes on a key**, so the public demo key can reach `/v1` and nothing on the
control plane. That is a design change, not a patch, so it is proposed rather than done.
Until it exists, `TREASURER_DRY_RUN=true` remains the actual control on that endpoint, and
it should be treated as such rather than as a default.

**The open question, NOT actioned, because it reverses a decision a human made.** B18 says
read-only treasury routes stay open. Verified still true: `/wallets`,
`/treasury/assess`, `/treasury/events`, `/mandates/stored`, `/mandates/chargeable` all
return 200 unauthenticated, and between them they disclose every project's balance, burn
rate, mandate ids and remaining headroom — the complete financial position of every tenant,
to anyone. That was defensible when the only tenant was `demo-project`. It is a different
proposition now that `judge/` provisions tenants for strangers who bring their own cards,
which did not exist when B18 was decided.

Not changed unilaterally. Three options, in the order I would pick them:

1. **Scope the reads to the caller's key** — same `_authed_key` dependency, filtered to its
   project. Costs the unauthenticated demo curls in `SETUP.md` §440–499 and
   `WALKTHROUGH.md` §127, which would each need a header.
2. **Keep them open but redact cross-tenant rows** — public view shows `demo-project` only,
   judge projects require the session. Preserves every existing curl.
3. **Leave as-is** and accept it, with the judge-tenant exposure written down somewhere a
   judge can see it.

Owner: Shivam. Blocking nothing, but it should not ship to real customers unresolved.

**THE WIDER QUESTION IS NOW ANSWERED (2026-08-09, Shubh).** B19 closed one route and left
the real problem stated: a key was all-or-nothing, so "authenticated" meant "may do
everything", and `WALKTHROUGH.md` publishes a working key. `meter_keys.scopes` now exists —
`proxy`, `money`, `read`, comma-separated, with `NULL` meaning unrestricted. All eight
money-moving treasury routes require `money`, asserted as a *set* so the next one added is
covered by default rather than enumerated.

`NULL` means unrestricted rather than deny-all, deliberately. Scoping arrived after keys
were in circulation; defaulting to deny would have taken the treasury away from every
deployment at the moment it upgraded, which is a worse failure than the one being fixed.
The consequence is that the published walkthrough key is still unrestricted until someone
narrows it — the mechanism is shipped, that specific decision is a one-line config change
and belongs with whoever updates `WALKTHROUGH.md`.

**Judge keys are already narrowed**, which is where the actual exposure was: a judge's key
is issued `proxy` and nothing else, so even with B20's script-readable token the worst a
leak reaches is metered inference inside a capped session, not a charge against a real
card. 28 checks across `test_treasury.py` and `test_judge.py`, plus a migration probe
confirming an existing `meter_keys` table gains the column at boot and keeps working.

### B20 — The judge cookie is not `httpOnly`, and the comment justifying that understates what the token can do · **FIXED 2026-08-09 (Shubh)**

`dashboard/src/lib/session.ts` sets `meter_judge_session` deliberately script-readable, and
says why: the console also calls `/judge/*` cross-origin where cookies are not sent, so the
browser needs to read the same value. That reasoning is sound and the alternative (storing
it twice, keeping the copies in sync) is genuinely worse.

**The stated cost is wrong, though.** The comment describes the risk as "a throwaway sandbox
session with a call cap and a four-hour life". But `judge/sessions.py` keys the in-process
credential vault by that same token, and `/judge/mandate` uses it to act with the judge's
own Prava **merchant** key. So the token does not only read a sandbox — it authorises
charges against a real card. That is a materially more expensive thing to leave readable by
any script on the page, and the decision should be re-made knowing it rather than inherited
from a comment that undersells it.

Not changed unilaterally — the cross-origin constraint that drove the original decision is
real, and splitting it is a design call. The cheapest fix if the team wants one: keep the
readable cookie for reads, and require a second, `httpOnly` capability for the routes that
touch money.

Owner: Ammar.

---

**RESOLUTION.** Shipped, and not by any of the three options as written. `replace_budgets`
exempts runtime-owned projects from the wipe, keyed on `environment = 'judge'` rather than
option 1's `id NOT LIKE 'judge-%'` — the rule is then a property of the project rather than
a naming convention someone can break by renaming a tenant. `projects.breaker_floor_usd`
was already outside the wipe for the same reason, recorded in `_ADDED_COLUMNS`. Pinned by
`test_a_boot_does_not_wipe_judge_ceilings`, which also asserts the property that made the
wipe correct in the first place: a ceiling deleted from `meter.yaml` still stops being
enforced. Recorded here 2026-08-09 because this section described it as open long after the
fix landed.

**RESOLUTION.** Built as this entry proposed — keep the readable cookie for reads, require
a second `httpOnly` capability for the routes that touch money — because the cross-origin
constraint that drove the original decision is real and does not go away.

`meter_judge_session` stays script-readable and stays sufficient for every read. A second
secret, `meter_judge_capability`, is minted independently at session creation, stored only
as a SHA-256 in `judge_sessions.capability_hash`, and returned exactly once — straight into
an `httpOnly; Secure; SameSite=None; Path=/judge` cookie set by the **proxy on the proxy's
own origin**, which is what makes it reachable cross-site while never being visible to
JavaScript. `/judge/mandate` and `/judge/topup` require it; comparison is
`secrets.compare_digest`.

Consequences worth knowing:

* `allow_credentials` is now on for CORS, but **only when origins are not wildcarded**.
  `Access-Control-Allow-Origin: *` with credentials is rejected by every browser, so a
  wildcard deployment that turned it on would get no cookie and money routes that 403 while
  reads work — a failure that reads as a backend bug. Refusing the combination keeps it
  legible.
* A session minted before this column existed has `capability_hash` NULL and **cannot
  spend**. Failing closed costs a judge one click to start a new session; the alternative
  is a grandfather clause on exactly the routes that move money.
* `JUDGE_CAPABILITY_INSECURE` relaxes the cookie for plain-http local development, where
  `Secure` makes the browser drop it. It must never be set in a deployment, and
  `test_capability_cookie_attrs` asserts the shipping attributes with it off.

15 checks in `test_judge.py`, including that reads still work on the token alone, that a
missing *and* a wrong capability are both refused, that one judge's capability cannot spend
another judge's session, and that the capability never appears in the session payload.
`dashboard/src/lib/session.ts`'s comment — the one this entry said undersold the risk — now
states what the token actually reaches and what carries the authority instead.

### B21 — ARCHITECTURE.md's `<5ms p50` is unreachable on the deployment as configured · OPEN — found 2026-08-09

ARCHITECTURE.md line 14 draws the hot path as `PROXY (hot path, <5ms p50)`, and §8 treats
that as the budget. The first measurement from the **deployed** proxy puts it at
**p50 255 ms** (n=12, two runs agreeing within 4 ms, `scripts/bench_deployed.py`).

This is not the proxy being slow. It makes **3** sequential database round trips on the
shipping path — already reduced from 5 — and the deployment puts Render in **Singapore**
and Supabase in **ap-south-1 (Mumbai)**. Three trips at that RTT is 255 ms, so the budget
is missed by distance, not by code.

Why this needs a human rather than an edit. Three readings, and they lead different places:

1. **The budget is right and the deployment is wrong.** Colocate — proxy in `bom`, or the
   database in Singapore — and the same three trips cost 3–6 ms, which lands at or just
   over 5 ms. This is the cheapest fix by a wide margin and needs no code.
2. **The budget was always about the proxy's own work, not wall time.** On that reading
   the number to compare is the in-process figure (p50 +0.26 ms, loopback), the budget is
   already met, and §8 should say *which* it means — because as written it reads as wall
   time and every reader has taken it that way.
3. **The budget is wrong.** A hosted ledger a network away cannot be single-digit
   milliseconds at any trip count above zero, so a proxy that must read before forwarding
   has a floor set by geography. If that is accepted, §8 should state a budget in *round
   trips* rather than milliseconds, which is the thing the code can actually control.

Not edited unilaterally, per this file's own rule: ARCHITECTURE.md is a source-of-truth
document and the reading chosen changes what "done" means for the request path.

**Recommendation: 1, then restate §8 as 3.** Colocation is a deployment change with no code
risk and it is the only option that makes the current number defensible. Restating the
budget in round trips afterwards records what was actually learned — that the count is the
engineering lever and the RTT is a hosting decision.

Owner: Shubh (proxy), with whoever owns the hosting call.

### B22 — The CORS preview regex still admits an attacker-registrable origin, and credentials made that matter · **FIXED 2026-08-09 (Shubh) — default changed**

B19's fix replaced `https://.*\.vercel\.app` with a pattern built from the configured
origins, so it can no longer match an unrelated project. It still matches
`<project>-<anything>.vercel.app`, and **it has to**: that is the shape of a Vercel preview
URL and the suffix is not predictable.

`vercel.app` subdomains are first-come-first-served, so `meter-three-beta-evil.vercel.app`
is registrable by anyone in about a minute. Verified against the actual pattern, with
Starlette's `fullmatch` semantics:

    MATCH   https://meter-three-beta-abc123-team.vercel.app   (a real preview)
    MATCH   https://meter-three-beta-evil.vercel.app          (anyone can register this)
    reject  https://evil-meter-three-beta.vercel.app
    reject  https://meter-three-beta.vercel.app.evil.com

That was survivable while CORS carried no credentials — a matching origin could send a
session token it had somehow obtained, and nothing else. **B20 changed the stakes**: the
judge money capability is a cookie, `allow_credentials` is now on, so a browser on any
matching origin attaches it *automatically*. The second factor that exists specifically to
stop a leaked session token from spending would be delivered to an origin an attacker owns.

**Fix: `CORS_ALLOW_VERCEL_PREVIEWS`, default off.** Previews are a developer convenience;
a credential-bearing wildcard is not a reasonable price for it. Naming a specific preview
URL in `CORS_ALLOW_ORIGINS` still works and is the recommended route.

⚠ **Behaviour change:** testing the judge console from a branch preview now requires either
setting the flag or listing that preview URL explicitly. Worth knowing before someone spends
an afternoon on a CORS error that is doing its job.

## C. Verification tasks
Not design questions — things that are written down and might simply be wrong.

### C1 — Pricing rates · **DONE — verified 2026-08-01**

Every rate in `pricing/2026-08-01.yaml` has been checked against the providers' own
published rate cards
([Anthropic](https://platform.claude.com/docs/en/about-claude/pricing),
[OpenAI](https://developers.openai.com/api/docs/pricing)). The first draft was written from
memory and was wrong in both directions — Haiku 4.5 was under-priced 20% and the generic
"claude-opus" entry was priced 3x high against Opus 5. Both would have been checkable from
a phone in the audience.

**One dated item now carries a deadline.** Claude Sonnet 5 is on introductory pricing of
**$2/$10 per MTok through 2026-08-31**; standard pricing of **$3/$15** takes effect
2026-09-01 — a 50% jump, 30 days out. The file prices at the current intro rate.

**On 2026-09-01, do not edit `2026-08-01.yaml`.** Copy it to `2026-09-01.yaml`, apply the
standard Sonnet 5 rates, and bump `PRICING_VERSION`. Editing in place would silently
reprice every historical row and destroy the reproducibility that versioned pricing exists
to provide — which is the one property `ARCHITECTURE.md` §3 is explicit about.

**Known, deliberate gap:** Anthropic charges 2x input for a 1-hour-TTL cache write versus
1.25x for the 5-minute default. The proxy only sees the aggregate
`cache_creation_input_tokens` and prices everything at the 5-minute rate, so a
1-hour-TTL workload is under-billed by 0.75x input on its cache writes only. The 1-hour
rates are already in the YAML as `cache_write_1h`, unused. Upgrade path is in the file's
header comment; not worth building until someone actually runs 1-hour TTLs.

### C2 — Anthropic OpenAI-compatibility path · **DONE — verified 2026-08-01**

See B1. Confirmed to exist, with OpenAI-shaped errors and dual auth-header support.

### C3 — Prava sandbox rate limits · **HANDLED IN CODE 2026-08-02 (Shubh)** — the limit itself is still undocumented by Prava

Blocks A3. Owner: Shivam.

**Research result (2026-08-01, full read of `docs.prava.space` — errors.md, OpenAPI spec,
create-session.md, developer FAQ, go-live checklist):** no RPM/RPS figures exist for any
endpoint. The only documented throttle is `429 TRIES_EXHAUSTED` on `POST /v1/sessions`,
described inconsistently as either a session-allowance depletion or a sandbox
test-transaction limit. No `Retry-After` / `X-RateLimit-*` headers are documented anywhere.
The mandate charge/report paths declare no 429 at all (charge: 400/401/403/409/500; report:
400/401/404/500).

**Implication for the loop-interval decision:** no published number constrains either 30s or
3s. The real limiters are (a) the undocumented sandbox test-transaction allowance (ask
`support@prava.space`, quoting an `X-Response-ID`, or measure empirically) and (b) the
mandate's per-cycle cap (see the mandate finding in CONTEXT.md §6a). Prava's own docs
recommend a **3s poll cadence** for session credentials — a precedent that supports the
demo-box `TREASURER_INTERVAL_S=3`. Implement backoff on 429 and treat `TRIES_EXHAUSTED` as
a loop trip rather than a retryable blip.

**Both implemented 2026-08-02 (Shubh).** The number is still unknown — Prava has not
published one — but an undocumented limit is precisely the kind you meet on stage, and
nothing handled it at all before this.

* **Reads retry with exponential backoff** (2 retries, 1s→2s, capped at 8s), because a GET
  has no side effect and the worst case of a retry is a wasted second. `Retry-After` is
  honoured if it ever appears and **clamped**, so a bad value cannot wedge the loop for
  hours.
* **Writes are never retried in the transport helper.** A 429 on a charge is a definite
  refusal, but the safe way to resume a charge is `topup`'s pending-event path with its
  original idempotency key — a second POST from inside `_request` would be a second charge
  attempt wearing the same clothes.
* **`TRIES_EXHAUSTED` is a trip, not a blip.** It means an allowance is *spent*, so
  retrying sooner cannot refill it. It is flagged separately from an ordinary 429 and is
  never retried, on any method.
* **The Treasurer backs off for 300s** on either signal, mirroring the circuit breaker, and
  reports it on `/healthz .treasurer`. Without this a demo box ticking every 3s hammers a
  throttled rail 1,200 times an hour and turns a recoverable throttle into a dead one.

15 checks across `test_treasury.py`. **A3 is still not settled** — this makes either
interval survivable, but it does not tell us what the real limit is. Measuring it, or
asking `support@prava.space`, is still open.

### C4 — Provider credit balances · **RESOLVED — both providers funded and verified live**

Found while verifying B1. The key authenticates correctly — it is a real, working
credential — but **every** call returns:

```json
{"type":"error","error":{"type":"invalid_request_error",
 "message":"Your credit balance is too low to access the Anthropic API..."}}
```

This is not a proxy problem and nothing in the code needs to change; it is an account
problem, and it blocks work that `CONTEXT.md` §3 lists under MUST BUILD:

> **REAL LLM CALLS:** We WILL forward requests to real OpenAI/Anthropic APIs using a master
> company key. We need real token usage data to train our predictive engine and prove it works.

Concretely, with no credits:

- **Ammar's predictive engine has nothing to calibrate against.** The output-token heuristic
  and the predicted-vs-actual variance column both need real completions. Fake upstreams can
  exercise the plumbing but cannot produce a defensible accuracy number.
- **Cross-model efficiency analysis (PLAN.md Phase 3) cannot run.** It is defined as a real
  prompt suite across GPT-4o and Claude; half of it has no provider.
- **The pitch line "Across 50 test prompts, Claude was 15% more token-efficient for coding
  tasks" has no data behind it.**

**Resolved 2026-08-01 for Anthropic.** A second key was supplied and it is funded — real
completions, real streams, real usage, priced correctly end to end (see B15/B16 below, both
of which were only findable because of it). Total spend proving it: **$0.00028**.

**Resolved 2026-08-01 for OpenAI too.** A funded key was supplied and verified live through
the proxy, 18/18: unary and streamed completions on gpt-4o-mini, ledger costs matching the
published rates exactly, and — the path no fixture ever exercised — the full
inject-capture-strip cycle against real OpenAI: the proxy injects
`stream_options.include_usage`, real OpenAI honours it and emits its separate `choices: []`
usage chunk, the tap captures it, and the client never sees it. When the caller *does*
request usage, the chunk passes through untouched. Both halves of B16's drop rule are now
proven against real providers: OpenAI's separate chunk is dropped, Anthropic-compat's merged
chunk is forwarded. A mixed run also confirmed cross-provider routing lands both providers'
rows in one ledger, correctly attributed. Total spend proving it: **$0.000054**.

**Everything CONTEXT.md §3 lists under REAL LLM CALLS is now unblocked** — Ammar's
`calibrate.py` has both providers to measure against, and the cross-model efficiency
comparison in PLAN.md Phase 3 can run for real.

⚠ All three keys (two Anthropic, one OpenAI) were shared over chat and should be
**rotated**. The live ones sit only in `.env`, which is gitignored and has never been
committed; verified by scan before every commit.

*Silver lining for the demo narrative:* an account at zero balance returning errors on every
request is, verbatim, the failure Meter exists to prevent — "when the balance hits zero at
3am, production returns errors until a human wakes up." We have an unusually authentic
screenshot of the problem statement.

### C5 — Linq sandbox requires the recipient to message first · OPEN — verify before demo day

Found in a full read of `docs.linqapp.com` (2026-08-01). The sandbox error reference lists
`2008 Recipient not allowed` with the rule *"in sandbox, recipients must message you first"*.
If the demo `.env` uses a sandbox Linq token, breaker alerts to the CTO's number may silently
fail with 2008 until that number has messaged the sending line once.

**Recommendation:** before demo rehearsal, send one iMessage from the line to the demo
number (or confirm with the Linq token whether the account is sandbox). One sentence in
CONTEXT.md §6a records the gotcha; this item closes when someone has verified which mode
the token is in. Owner: Tanay.

### C6 — Hosted health reports Anthropic unavailable · OPEN — found 2026-09-22

`GET https://meter-proxy.onrender.com/healthz` returned healthy after the free Render service
woke, with `providers.openai: true` and `providers.anthropic: false`. This contradicts the
README's promise to forward both providers and CONTEXT's record of funded, verified Anthropic
traffic. It may be an intentionally absent deployment secret, but the public demo currently
cannot substantiate the two-provider claim.

**Recommendation:** inspect Render's non-secret configuration and either set a valid
`ANTHROPIC_API_KEY` then run one safe smoke request, or qualify the hosted demo as OpenAI-only.
Do not put a key in this repository to resolve it. Owner: deployment operator.

### C7 — Public walkthrough prints an active Meter credential · OPEN — found 2026-09-22

`WALKTHROUGH.md` prints `mk_74e…` and uses it in its deployed curl commands. A Meter key
is deliberately a proxy credential, so publishing one can be an intentional hackathon demo
choice; it also lets anyone consume the deployment's provider credits until its project
ceiling is reached or the key is rotated. The repository cannot establish the deployed key's
scope, quota, rate limit, or revocation plan because `METER_KEYS` is a Render secret.

**Recommendation:** the deployment owner should verify that this is a dedicated demo key,
has only `proxy` scope, maps to a tightly capped project, and has a post-demo rotation plan.
If it is not, replace the public curl path with the session-scoped `/try` flow and rotate the
credential. Do not copy the production key into local configuration or git to verify it.

---

## D. Research findings (2026-08-01)

Patterns from a review of the major open-source LLM gateways (LiteLLM, Helicone, Portkey,
OpenRouter, one-api/new-api, Langfuse) — things Meter deliberately does not yet have, or does
have and was validated on. These are recorded for the team to pick from; none block the demo.

### D1 — Client-visible request id / idempotency key · **DONE — shipped 2026-08-02 (Shubh)**

Every major gateway exposes a request id — Helicone accepts a client-supplied
`Helicone-Request-Id` header (idempotent retries, links retries to one logical request),
OpenRouter returns `request_id`, LiteLLM has a `request_id` column. Meter's request id is
generated internally (B8) but never echoed to the caller, and nothing accepts a client
idempotency key.

**Recommendation (post-hackathon unless a judge asks):** echo an `X-Meter-Request-Id`
response header and accept an optional client-supplied `X-Meter-Request-Id`, `INSERT OR
REPLACE` semantics already make a replay idempotent (B8). ~5 lines, answers "what happens
when the client retries?". Owner: Shubh.

**Shipped 2026-08-02 (Shubh).** Half of this already existed — `X-Meter-Request-Id` was
echoed on every response, including refusals. What was missing was the inbound half:
`_request_id()` in `proxy/app.py` now uses a caller-supplied `X-Meter-Request-Id` when one
is sent, so `record_request`'s `INSERT OR REPLACE` on that id makes a retry overwrite its
own row rather than double-count spend (B8). Ids must match `[A-Za-z0-9._:-]{8,128}` —
long enough not to collide, restricted enough to be safe as a primary key and in a log
line, and **anchored at both ends** so a value carrying a newline cannot inject a header.
A malformed id is *ignored*, not rejected: refusing would make adopting the header riskier
than never sending it. 10 checks in `test_proxy.py`.

### D2 — Soft-budget alert below the hard ceiling · **DONE — shipped 2026-08-02 (Shubh)**

LiteLLM distinguishes `soft_budget` (warn only) from `max_budget` (block). Meter has the
block side (`meter.yaml` ceilings → 429) and the alert rail (`alerts/` → iMessage) but no
"approaching the ceiling" warning. An iMessage at ~80% of a ceiling turns a binary block
into a preventable incident — and is a demo beat the current 429-only flow cannot show.

**Recommendation:** a per-ceiling threshold check in the Treasurer loop (it already scans
burn rate) that fires `alerts/` when spend crosses a configurable fraction of a ceiling.
Cheap; the pieces all exist. Owner: Shubh or Tanay, post-hackathon.

**Shipped 2026-08-02 (Shubh).** `budget.soft_breaches(ratio)` plus a background poll in
`proxy/app.py`'s lifespan, firing `alerts.send_budget_alert`. Four decisions worth keeping:

* **A background poll, not a request-path check** — and not in the Treasurer loop as
  suggested above, which would have put a proxy concern in `treasury/`. A ceiling being
  80% full is a *level*, not an edge, so an inline test re-evaluates the same condition
  thousands of times to send at most one message, in front of production traffic.
* **Settled spend only.** Including in-flight holds would let a burst trip the warning and
  then un-trip it as the holds released. An alert that retracts itself is worse than none.
* **The same scope string a 429 would name**, so the warning and the refusal cannot
  disagree about which line of `meter.yaml` is the problem. Asserted in the self-check.
* **The message leads with headroom** ("$0.15 left"), not a percentage — "83% of ceiling"
  needs arithmetic before anyone can act on it.

Per-scope cooldown comes free from `alerts._within_cooldown`, and it matters more here
than for the breaker: the condition stays true for the rest of the day, so without it this
is one text per poll forever. 12 checks in `test_proxy.py`.

### D3 — `402` vs `429` for budget refusals · **DECIDED & DOCUMENTED — 2026-08-02 (Shubh)**

OpenRouter splits the two: credit/balance exhaustion is `402 Payment Required`, rate limits
are `429` with `X-RateLimit-*` headers. Meter refuses budget-exhausted requests with `429`
(no rate-limit headers) — which matches LiteLLM's budget-block behaviour, but conflates
"out of money" with "too fast" on the wire.

**Recommendation:** keep `429` (it is what SDKs already back off on, and LiteLLM does the
same), but add one sentence to `proxy/README.md` stating that Meter's `429` means "budget
exhausted", not "rate limited", and reserve `402` for a future paywall. Owner: Shubh, if
anyone asks.

### D4 — Things Meter already does that the gateways validate (recorded, no action)

- **Reserve/reconcile/release** authorize-capture matches LiteLLM's budget reservation and
  one-api's pre/post consumption exactly (A5, B7).
- **Throttle-vs-block split** (429 tag-scoped vs 403 key-scoped) mirrors LiteLLM's
  budget-throttle escape hatch and Portkey's breaker-on-429 handling.
- **Keys stored as SHA-256 hashes** (`proxy/db.py` `hash_key`) matches LiteLLM's
  `hash_token`.
- **Versioned pricing files** match Langfuse's cost-at-ingestion rule: never reprice
  history (C1).
- **`stream_options.include_usage` injection** is Helicone's canonical streaming-cost fix
  and LiteLLM's `always_include_stream_usage` (B15/B16).
- **Floor+burst detection** is the SRE multi-window pattern (A1).

**Deliberately not copied (post-hackathon candidates):** provider retries with backoff and
fallback routing (Portkey), per-key RPM/TPM token buckets (LiteLLM), response caching with
cache-hit accounting (Helicone), pre-aggregated daily spend rollups (LiteLLM), mid-stream
error encoding as SSE `finish_reason: "error"` (OpenRouter). All are real industry features;
none are needed for the 48-hour demo, and a few (retries) would obscure the ledger's
one-row-per-request contract.

---

### M5 — `GET /treasury/assess` creates a wallet, which silently defeats `/wallets/seed` · **DONE — fixed 2026-08-02 (Shubh, with Shivam's lane idle)**

`treasurer.assess()` documents itself as *"Would we top up right now, and why? Reads only —
never spends."* The second half is true. The first is not: its first statement is
`db.ensure_wallet(project_id, provider)`, which **inserts a wallet row at $0.00** when the
project has none. `GET /treasury/assess` is an unauthenticated read endpoint, so anyone —
a judge poking the API, the dashboard, a demo script — can create wallet rows.

On its own that is a docstring inaccuracy. It becomes a **demo trap** in combination with
`POST /wallets/seed`, which is idempotent by design and applies its balance *on creation
only* (`treasury/routes.py`, and correctly so — re-running it must not wipe a top-up the
Treasurer already made). The sequence:

1. Fresh `meter.db`. Anything hits `GET /treasury/assess`.
2. A wallet is created at **$0.00**.
3. `POST /wallets/seed` (default `balance_usd=4.00`, "the demo's too-low state") **no-ops**
   and the wallet stays at $0.00. No error, and the response body looks successful — it
   returns the wallet, just with the wrong balance.

Reproduced on a fresh database:

```
wallets on fresh db:            []
after GET /treasury/assess:     [('wal_demo-project_openai', 0.0)]
after POST /wallets/seed 4.00:  0.0     <-- expected 4.00
```

Not fatal — the Treasurer still fires, because the floor trigger exists precisely for a
balance that cannot pay for the next request. But the pitch's "$4.00, and OpenAI is about
to run dry" beat is a number on screen during the demo, and it silently reads $0.00.

**Recommendation:** make `assess()` match its own docstring and not create anything — look
the wallet up and report `balance_usd: 0.0` when it is absent. `tick()` iterates
`list_wallets()` and only ever assesses wallets that already exist, so nothing depends on
the creation side effect. Roughly three lines.

**The workaround until then is `POST /wallets/seed?reset=true`**, which is already
implemented and documented. Anyone rehearsing the demo should use it rather than trusting
a fresh-looking database.

Owner: Shivam (`treasury/`). Raised rather than patched because it is another lane's
module and the fix is a behaviour change to a documented endpoint.

**Fixed 2026-08-02 (Shubh), on instruction, with Shivam's lane idle.** `assess()` now calls
a new `db.wallet_id_for()` — pure string work — instead of `ensure_wallet`, so the read
endpoint reads. A project with no wallet reports `balance_usd: 0.0`, which is what having
no wallet means, and the floor trigger still fires. `tick()` only ever assesses wallets
`list_wallets()` returned, so nothing depended on the creation side effect. Verified on a
running app against a fresh database: `/wallets` stays `[]` across an `assess`, and seeding
$4.00 afterwards now yields $4.00 rather than the $0.00 it silently produced before.
5 checks in `test_treasury.py`.

---

### M6 — `PRAVA_LIVE_MODE=false` does not stop calls to Prava · **FIXED 2026-08-09 (Shubh)**

The flag reads as "simulate instead of transacting", and `CONTEXT.md` §6a relies on it in
exactly that sense: *"Demo runs on `PRAVA_LIVE_MODE=False` until this clears"*, written
during the sandbox outage. It does not do that.

Gated by the flag (return a simulated response, no network):

* `charge_mandate`
* `report_charge`
* `verify_credentials`

**Not gated — these always call the live sandbox, whatever the flag says:**

* `list_mandates` → `GET /v1/mandates`
* `create_mandate_session` → **`POST /v1/sessions`**

Observed on a booted proxy with `PRAVA_LIVE_MODE=false` set explicitly: both endpoints
went out and came back `401 AUTH_1001` against the placeholder key, and `GET /mandates`
answered **503**.

Two consequences, and the second is the one that matters:

1. **The outage workaround does not work.** With the sandbox down, `/mandates` still fails
   and the dashboard's mandate panel still breaks, despite the flag being set precisely to
   avoid that.
2. **`POST /v1/sessions` is the one endpoint Prava documents a throttle on** — `429
   TRIES_EXHAUSTED` (C3). So the call that can exhaust the allowance is also the one the
   flag does not restrain. C3's trip logic catches the exhaustion afterwards; it would be
   better not to spend the allowance in the first place.

**Recommendation:** make the flag mean what it says — gate all five, returning simulated
mandate lists and sessions the way `charge_mandate` already returns `simulated: True`.
⚠ **This is a behaviour change with a real edge:** a simulated `/mandates` would show
mandates that do not exist, which could mislead someone mid-demo. So the simulated
response must be *visibly* simulated, exactly as the charge path is. Not applied
unilaterally for that reason.

Owner: Shivam (`treasury/`).

**RESOLUTION (2026-08-09, Shubh).** Both calls are gated. The open question was what a
simulated `list_mandates` should return; the answer is an **empty list**, never the rows in
the local `mandates` table — a populated simulation would show mandates Prava has never
confirmed, mid-demo, indistinguishable from ones it has. The `mandates` key stays present so
`/mandates` renders `[]` rather than the 503 a missing key triggers, because simulation is
not an outage. `create_mandate_session` returns no `session_id` and no `iframe_url`: a fake
approval URL is worse than none, being a link a human will click. `/mandates/create` reports
`reason: "simulated"` instead of `session_create_failed` — the old envelope read as "Prava
refused us" when Prava was never asked — and opens no pending row, which was M6's second
complaint. `_sync_project` returns early on a simulated list; without that the empty result
fell through to the 15-minute expiry sweep and marked genuine `pending_approval` rows
`expired`. Pinned by 14 checks in `test_treasury.py`, every one paired with a live-mode
control, plus a mutation check confirming the guard's removal fails the suite.

### M7 — `POST /mandates/create` and `/mandates/sync` are unauthenticated writes · **FIXED — recommendation applied; both carry `Depends(_authed_key)`**

B18 shipped "auth on the money moves only", and `/mandates/create` genuinely moves no
money — it returns an approval URL and authorizes nothing until a human completes a
passkey. By that reading it is correctly open.

But it is a **write that spends someone else's quota**: it calls `POST /v1/sessions`,
which is the single endpoint Prava documents a `429 TRIES_EXHAUSTED` throttle on (C3), and
it inserts a pending row in `mandates`. Verified unauthenticated on a running proxy: both
return `200` with no key.

So an unauthenticated caller can exhaust the sandbox allowance the Treasurer depends on,
and fill the mandates table with pending rows. On a public URL that is a denial of the
money rail, not a leak of it — which B18's "read-only routes stay open" framing did not
consider, because at the time these routes did not consume a metered third-party resource.

**Recommendation:** put `Depends(_authed_key)` on both. ⚠ **It breaks the self-serve
onboarding flow** if the dashboard calls `/mandates/create` from the browser without a key
— `CONTEXT.md` §6a describes it as "the two-call *Connect your card* flow" for Tanay. So
the fix is one line plus a decision about who calls it. Not applied unilaterally.

Owner: Shubh (B18's author) + Tanay (the flow). Cheap either way; needs the decision first.

### M8 — judge ceilings cannot live in `projects`, because every boot wipes them · **FIXED — exemption shipped in `replace_budgets`**

`replace_budgets` ([proxy/db.py](proxy/db.py)) begins each boot with

```sql
DELETE FROM feature_budgets;
UPDATE projects SET ceiling_usd_day = NULL, sort_order = NULL;
```

then re-inserts only what `meter.yaml` names. That is correct and deliberate — its
docstring is explicit that upserting would make deletion impossible, so a ceiling removed
from the file must stop being enforced.

It also means **a per-judge ceiling written to `projects.ceiling_usd_day` is silently
erased at the next restart** (PITCH.md §2.3, build item #3). On Render's free tier that is
not rare: a spin-down kills the process, so the next cold start re-runs `lifespan` →
`replace_budgets` → the judge's ceilings are gone. A judge returning after fifteen idle
minutes finds an empty Team Spend card and no ceilings enforced — and nothing anywhere
reports a problem.

The contradiction is real rather than cosmetic: `meter.yaml` is the source of truth for
ceilings **reviewed by pull request**, and a judge session is a ceiling created at runtime
by someone who cannot open a pull request. Both are legitimate; the current model has room
for only one.

**Three ways out, none applied unilaterally:**

1. **Exempt judge projects from the wipe** — scope the `UPDATE`/`DELETE` with
   `WHERE id NOT LIKE 'judge-%'`. Smallest diff, and it keeps one storage location. It
   does put a magic prefix in a budget function, and it quietly means "the file is the
   source of truth, except for these".
2. **Hold judge ceilings in `judge_sessions`** (the column already exists, unused for
   this) and merge at read time in `db.ceiling_spend`. Keeps `replace_budgets` honest and
   keeps judge data in the judge table, at the cost of two places to look when a ceiling
   misbehaves.
3. **Give `projects` an `owner` discriminator** (`file` vs `runtime`) and wipe only
   `file`. The most correct model and the largest change — it is the general answer to
   "budgets declared somewhere other than the file", which is also what per-tenant
   ceilings would need later.

**Recommendation: 2 for now, 3 if runtime budgets outlive the hackathon.** 2 touches no
existing behaviour at all, which is the property the judge work is being built to
(PITCH.md §5: drop the judge table and the product is byte-identical). 1 is tempting and
is the one that will look obvious to whoever hits this next — worth recording that it was
considered and passed over, because it makes `replace_budgets` mean something subtly
different from what it says.

Owner: Shubh (`replace_budgets` and the budget model) + Ammar (the judge flow).

**DECIDED 2026-08-03 (Ammar): option 1, keyed on `environment`.** Option 2 was chosen
first and then reversed on evidence, which is worth recording because the reasoning above
is incomplete rather than wrong.

`dashboard/src/lib/db.ts` reads `projects.ceiling_usd_day` and `feature_budgets`
**directly**, in both `getBudgets` and `getHeadlineMetrics`. So holding judge ceilings
anywhere else means changing two dashboard queries as well as the proxy, and the Team
Spend card renders empty until all three agree. Option 2 was picked as the smaller change
and is in fact the larger one — the recommendation above was written without checking what
the dashboard actually queries.

So judge ceilings live in the ordinary tables, and `replace_budgets` exempts them:

```sql
DELETE FROM feature_budgets WHERE project_id NOT IN
    (SELECT id FROM projects WHERE environment = 'judge');
UPDATE projects SET ceiling_usd_day = NULL, sort_order = NULL
    WHERE environment IS DISTINCT FROM 'judge';
```

Keyed on `environment = 'judge'`, **not** the `judge-` id prefix. The objection to option 1
was that it puts a magic string in a budget function; `environment` is an existing column
that already describes what a project *is*, so the exemption is a property of the project
rather than a naming convention a rename would silently break. That is option 3's
discriminator without option 3's new column.

The rule this establishes, stated so the next person does not have to infer it:
**meter.yaml is the source of truth for the ceilings it declares, and it cannot declare
one for a tenant that did not exist when it was written.** `budget.register_ceilings` only
ever adds, so a runtime caller can never widen or remove a ceiling that was reviewed in a
pull request.

**RESOLVED 2026-08-03 (Ammar): both routes now require a Meter key.**

The objection that stopped this being applied when it was filed — that it would break the
self-serve onboarding flow if a browser called `/mandates/create` without a key — no longer
holds. The judge console does not call these over HTTP at all: `judge/routes.py` imports
`create_mandate` and `sync_mandates` and calls them as Python functions, inside
`prava.use_api_key(the judge's own merchant key)`. FastAPI dependencies do not apply to a
direct call, so the console path is unaffected and the HTTP surface is closed.

Nothing documented breaks either: SETUP.md §8 already sends
`Authorization: Bearer mk_dev_local` on both commands.

What this actually shuts is the reason M7 was filed — an unauthenticated caller on a public
URL exhausting the sandbox `/v1/sessions` allowance the Treasurer depends on, and filling
`mandates` with pending rows. That matters more now than when it was written, because the
console drives this route and the deployment is public.
