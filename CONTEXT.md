# Meter: The Autonomous Inference Treasurer

## 0. Prompting Instructions for AI Assistants  
You are assisting a team of 4 developers in a 48-hour hackathon (Agentic Commerce Hackathon by Prava). This document is your primary context. The team is building "Meter." Your goal is to help them write production-ready code for a FastAPI proxy, a predictive token engine, a Prava payment integration, and a Next.js dashboard. Prioritize speed, simplicity, and flawless execution of the core "Predictive Top-up" loop. Do not suggest over-engineered solutions that cannot be built in 48 hours.

---

## 1. Project Overview & Hackathon Context  
**What we are building:** Meter is a drop-in LLM proxy that meters inference spend, attributes it by employee/tool, enforces per-project budget ceilings, and uses an autonomous Treasurer agent holding Prava mandates to top up provider credits *before* production dies at 3am. It also features a Circuit Breaker that cuts spend on anomaly spikes and alerts engineers via iMessage.

**Hackathon Track:** Prava (Agentic Commerce) + Visa Intelligent Commerce (VIC) + Localhost (Startup Readiness) + Linq/Poke (iMessage Alerts).  
**Core Requirement:** The agent must take a meaningful action (complete a transaction using Prava).

---

## 2. The User, Problem, and Solution  
**The User:** CTOs, Engineering Leads, and Founders at AI-native startups.   
**The Problem:** Inference is the #2 cost for AI companies, yet it is completely unobservable. Founders can't attribute spend to specific employees or tools (Cursor, CI/CD). Furthermore, when the provider balance hits zero at 3am, production breaks because no human is awake to authorize a credit top-up. Existing observability tools (Helicone) just show you the graph; they don't buy more gas.  
**The Solution:** Meter is an active treasurer. It predicts upcoming token costs, enforces employee budgets, and uses an autonomous AI agent to procure more credits via Prava *before* production fails. 

**The Core Differentiator:** Predictive Scaling + Autonomous Procurement. We don't react when the balance hits zero. We use `tiktoken` and task classification to predict the cost of an incoming prompt *before* execution. If a batch job comes in and the balance is too low, the Treasurer Agent calculates the shortfall, calls Prava to generate a one-time scoped virtual card, and pays the provider automatically.

---

## 3. MVP Scope (The 48-Hour Build)  
To win, we must ruthlessly prioritize the **Predictive Prava Top-up** and the **Circuit Breaker**.   
*   **MUST BUILD:** FastAPI Proxy, `tiktoken` prediction, Postgres Ledger, Prava Sandbox integration, Mock Provider Billing endpoint, Treasurer Agent loop, Circuit Breaker, Next.js Dashboard, Poke iMessage alerts.  
*   **FAKE / SIMULATE:** We cannot actually top up a real OpenAI account with a Prava test card. Therefore, we will build a **Mock Provider Billing Endpoint** (`/mock-openai/billing`). This simulates OpenAI's billing system, accepting the Prava sandbox card and updating the balance in our DB.  
*   **REAL LLM CALLS:** We WILL forward requests to real OpenAI/Anthropic APIs using a master company key. We need real token usage data to train our predictive engine and prove it works.

---

## 4. Architecture & Tech Stack  
*   **Backend:** Python + FastAPI (Handles async proxy routing and background agent loops).  
*   **Database:** Postgres for the ledger — users, wallets, transactions, cross-model efficiency metrics. Hosted on Supabase (ap-south-1) and shared by the proxy, the treasury, the predictor's learning loop and the dashboard; SQLite in Phase 1, ported 2026-08-02. **Redis is post-hackathon.** It is not what makes reservations correct — serialization is, and with a single proxy process an in-process lock is an identical guarantee for none of the operational cost. Redis becomes load-bearing at proxy replica #2. See ARCHITECTURE.md §2 and `PROPOSALS.md` A5.  
*   **Frontend:** Next.js + Tailwind CSS (Dashboard for spend, balances, and live agent logs).  
*   **Token Counting:** `tiktoken` library.  
*   **Payments:** Prava Sandbox API (Using fake credit card info provided by organizers).  
*   **Alerts:** Poke / Linq API (for iMessage alerts when the circuit breaker trips).

### System Flow  
1. **Intercept:** Employee request hits Meter Proxy (`/v1/chat/completions`).  
2. **Predict:** Meter counts input tokens and predicts output tokens/cost.  
3. **Check:** Verifies if the team has enough budget.  
4. **Procure (The Magic):** If provider balance is too low, the Treasurer Agent calls Prava for a scoped card and hits the Mock Provider Billing endpoint to top up.  
5. **Execute:** Forwards request to real OpenAI/Anthropic.  
6. **Learn:** Logs actual vs. predicted token usage (Cross-Model Analysis).  
7. **Protect:** If spend spikes abnormally (> $20 in 5 mins **and** ≥ 3x the trailing hour's rate), the Circuit Breaker throttles the offending tag with `429` — or cuts the key entirely with `403` in revoke mode — and sends an iMessage via Poke.

---

## 5. Core Features to Implement

### A. Predictive Engine & Cross-Model Analysis  
The estimator is **one design with three parts**, not competing options (ARCHITECTURE.md §2 says the same thing in the same words):
*   Use `tiktoken` for exact input token counting. The prompt is in hand — there is no reason to predict a number that can be counted.
*   Use heuristics to predict output tokens (e.g., coding task = input * 2.0). The response does not exist yet, so this is irreducibly a prediction and is where the predictor earns its place.
*   Use trailing p95 cost for `(project, endpoint, model)` as the **cold-start fallback**, for the first calls of a new feature before there is per-feature history to calibrate the heuristic against.
*   Implement Cross-Model Routing: Allow the proxy to send the same prompt to OpenAI and Anthropic to log efficiency differences.  
*   Feedback Loop: Compare `predicted_output_tokens` vs `actual_output_tokens`. Save variance to DB to calculate predictor accuracy.

### B. The Treasurer Agent (Prava Integration)  
*   An `asyncio` background loop that checks the `wallets` table every few seconds.  
*   If `provider_balance < threshold`, calculate top-up amount (shortfall + buffer).  
*   Call Prava Sandbox API to generate a one-time virtual card.  
*   Call Mock Provider Billing endpoint to process the card and update the balance.

### C. Circuit Breaker & Poke Alerts  
**Detection — two conditions, both must hold** (full reasoning in ARCHITECTURE.md §6):
*   **Floor:** trailing 5-minute spend clears `> $20`. This is what makes detection fast.
*   **Burst:** that 5-minute window's spend *rate* exceeds the trailing 1-hour average rate by 3x. Without this second condition, a feature that legitimately costs more than $20 per 5 minutes trips the breaker every five minutes forever, and the only fix is raising the threshold until the breaker is useless for that project.
*   A leaked key with no prior history still trips *immediately* — all of its hour's spend is in the last five minutes, so the ratio is at its 12x ceiling. Setting the ratio to `0` reverts to the flat detector as an escape hatch.

**Two response modes, because a retry storm and a leaked key are different emergencies:**
*   **Throttle (default):** the offending attribution tag gets `429 Too Many Requests` + `Retry-After`; every other tag on the same key keeps flowing. This is the right answer to a runaway loop in one feature — and the better demo, since "one feature got cut off and everything else kept serving" is a stronger claim than "we turned it off". `429` also matters because provider SDKs already back off on it, whereas a `403` makes them treat a temporary condition as permanent.
*   **Revoke:** the Meter key is cut entirely with `403 Forbidden`. This is the right answer to a leaked credential, where every request under that key is suspect.
*   Breakers auto half-open after a cooldown and can always be reset manually at `POST /v1/breaker/reset` — without that, the demo trips the breaker once and strands us on stage.

*   Trigger Poke API to send an iMessage to a hardcoded "CTO" phone number: *"🚨 Circuit Breaker Tripped! Spend threshold exceeded. API key revoked."*

### D. The Dashboard  
*   **The Bill:** Total spend, spend by user/tool.  
*   **The Wallets:** Current OpenAI/Anthropic balance, Current Team Budget.  
*   **Live Logs:** `User | Model | Predicted Cost | Actual Cost | Status`.  
*   **Agent Activity:** Streaming logs of the 3AM save (`Shortfall detected. Requesting $50 scoped card from Prava... Top-up successful.`).  
*   **Model Efficiency:** View showing which model is more token-efficient for specific tasks.

---

## 6. Team Roles & Execution Plan

*   **Shubh (Proxy & Infra):** FastAPI setup, real LLM routing, latency management, Circuit Breaker logic.  
*   **Shivam (Payments & Agent):** Prava Sandbox SDK integration, Mock Provider Billing endpoint, Treasurer Agent background loop.  
*   **Ammar (Predictive AI):** `tiktoken` integration, prompt classification, output token prediction, cross-model analysis engine.  
*   **Tanay (Frontend & DX):** Next.js dashboard, live data streaming, Poke/Linq alerts, demo script/pitch.

---

## 6a. Current Status
*(Keep this current — see `AGENTS.md` for the update policy. Update in the same turn as any scope or architecture decision, don't batch it for later.)*

*   **Marketing + technical docs refresh, 2026-09-23 (Shubh) — live.** The
    homepage is now an editorial, evidence-led inference-systems surface rather than an
    animation-first landing page: it removes the simulated spend motif, carousel, forced intro,
    and unsupported marketing claims; it adds direct paths to the Control Room, guided trial,
    and technical documentation. New static **`/docs`** covers quickstart, request lifecycle,
    configuration, API routes, and the split Vercel/Render deployment model. Dashboard navigation
    now includes a Docs link. No dependency was added. `npm run lint` and `npm run build` pass;
    desktop visual QA passed locally. Vercel production deployed commit `f2e47cc`; the public
    homepage and **https://meter-five-nu.vercel.app/docs** were verified after launch.

*   **Dependency/UI audit, 2026-09-22 (Shubh) — dashboard patched and clean.** Next.js
    moved from `16.2.12` to `16.3.5`, closing the audit's critical RCE findings and
    its transitive `postcss`/`sharp` findings; the lockfile also resolves the remaining
    vulnerable `nanoid`. `npm run lint`, `npm run build`, `npm audit --omit=dev`,
    `ruff check .`, `pip-audit -r requirements.txt`, and Python byte-compilation pass.
    All backend suites now pass against a fresh temporary Postgres 16 cluster — **985 checks**
    (proxy 306, predictor 140, treasury 253, alerts 52, judge 234). The local dashboard
    visual pass also found that the shared `.glass-pill` display rule overrode Tailwind's
    mobile `hidden` utility, clipping the breaker pill; the predictor explainer link now
    hides below 640px and the nav has no horizontal overflow at 390px. A balance older than the
    shared 30-minute threshold now also renders as a warning in the summary card, matching the
    detailed balance panel. The judge-session form now uses native required/email validation
    before it calls the proxy and announces returned errors to assistive technology. The two
    polling panels now change their live indicator to `Paused` when `usePoll` deliberately
    stops after inactivity, rather than claiming they are still streaming; the shared hook
    also now reports endpoint failures as `Offline` while retaining the last good rows and
    retrying with backoff. Dashboard and homepage section navigation now exposes its current
    location to assistive technology, and the dashboard stylesheet now gives keyboard focus a
    visible shared ring (including the judge inputs that intentionally suppress the browser default).
    `dashboard/README.md` is now a real operator guide instead of the stock Next.js template:
    it records the read-only boundary, local setup, checks, and Vercel pooler choice.
    Dashboard CI now runs `npm audit --omit=dev` rather than the old critical-only gate,
    because the production tree is clean after the Next.js upgrade.
    The dashboard's judge-scoped Treasurer feed now fails closed when a half-built schema
    has events but no wallets: events lack a project id, so returning the public feed there
    could disclose another session's activity.
    Dashboard schema guards now cache positive table/column checks but recheck a missing one
    after five seconds, so opening the dashboard before proxy bootstrap no longer leaves its
    panels permanently empty until the dashboard process restarts.
    `WALKTHROUGH.md` and `SETUP.md` no longer pin dynamic learned-factor or test counts;
    their verification now checks the stable health invariants and includes the judge suite.
    `PROPOSALS.md` C7 now requires the deployment owner to verify the public walkthrough's
    Meter key is intentionally dedicated, constrained, and scheduled for rotation.
    **Deployment handoff:** the local Vercel CLI token is invalid and the repository has no
    `.vercel/project.json`, so this branch cannot safely deploy to the existing dashboard until
    an authenticated team owner links the intended Vercel project.
    The deployed Render
    `/healthz` probe initially timed out while the free instance slept, then returned healthy
    after waking with budget, breaker and predictor-refresh state. It also reports OpenAI
    available but Anthropic unavailable; `PROPOSALS.md` C6 records the deployment verification
    needed before claiming the public demo forwards both providers.
    **Open documentation decision:** `PROPOSALS.md` A7 records that README's Compose quickstart
    promises Postgres, Redis and dashboard services that the one-service Compose file does not
    provide; do not silently pick a documentation story until the team decides it.

*   **Security audit, 2026-08-03 (Shubh) — nine fixes shipped, three questions raised.**
    Full-codebase pass, every finding runtime-verified against a running proxy rather than
    read off the source. **Fixed:** the CORS preview regex was `https://.*\.vercel\.app`,
    which matched *any* Vercel deployment — anyone could publish a page that drives the
    judge API, the exact wildcard the comment beside it claimed to avoid; it is now built
    from the configured origins and has its own test (`test_cors_preview_origins`).
    `POST /treasury/tick` was **unauthenticated** — the whole autonomous charging loop, on
    demand, for anyone who could reach the port (`PROPOSALS.md` B19). The Dockerfile now
    passes `--proxy-headers`, without which every request on Render looked like it came
    from the edge IP and the judge per-IP session limit was one shared bucket of 8/hour for
    the entire internet; it also runs as uid 10001 instead of root. Plus: `allow_headers`
    enumerated rather than `*`, `/treasury/events?limit=` bounded, security headers on the
    dashboard, an IP-bookkeeping leak swept, `openai` moved off the shipped dependency set,
    and CI gained `pip-audit`, `npm audit`, **and `test_judge.py`, which it had never run**.
    Suites: **867 checks** (proxy 256, treasury 213, judge 206, predictor 140, alerts 52).
    **Raised, deliberately not actioned** — all three reverse or reconsider a human
    decision: the treasury *read* routes are still open and now disclose judge tenants'
    finances (B19); the judge cookie is script-readable for a sound reason whose stated cost
    understates what the token authorises (B20); and `WALKTHROUGH.md` publishes a working
    Meter key, which means the `/treasury/tick` fix moved that route from "anyone" to
    "anyone who read the walkthrough" — closing it properly needs per-key scopes, which do
    not exist. **`TREASURER_DRY_RUN` is still the real control on that endpoint.**

*   **Judge experience: BUILT, NOT YET DEPLOYED** — [PITCH.md](PITCH.md) (Ammar, 2026-08-03) is
    the agreed design for asynchronous judging: a public dashboard anyone can read, plus an
    opt-in **"Try it yourself"** session scoped to `judge-<nonce>` with the judge's own
    Prava merchant key, Linq key and phone. Prompts are **templated and not editable** —
    accuracy is keyed on `(project, feature)`, so free text falls to the raw heuristic
    (~65–80% error against ~10%) and would make a working product look broken.
    **Two organizer confirmations it rests on (2026-08-03): every judge has their own Prava
    *merchant* key** — which closes the gap where a judge could not see their own charge in
    their own wallet (EXPERIENCE #44), because they become the merchant — **and a judge's
    Linq key can message their own phone.**
    All four backend blockers are now built (`judge/`, 165 checks): per-project breaker
    floor on `projects.breaker_floor_usd`, session tenants in `judge_sessions`,
    per-project ceilings exempt from the `replace_budgets` wipe via
    `environment = 'judge'`, and per-call Prava auth on a `ContextVar`. Plus the console
    itself at **`/try`**, `X-Meter-Provider-Key` for bring-your-own-key, CORS for the
    dashboard origin, and per-call Linq recipients.
    **Two deployment changes are still required and neither has been made.**
    `TREASURER_ENABLED` is `true` on Render and must become **`false`** — a background
    loop was caught charging a shared wallet every 30s (EXPERIENCE #43), and judges bring
    real cards. And Act 4 only settles a real charge if `TREASURER_DRY_RUN` is flipped,
    which is a deliberate, irreversible decision: Prava allows **one purchase per mandate
    per monthly cycle**. Vercel also needs `NEXT_PUBLIC_METER_PROXY_URL`, and the proxy
    needs `CORS_ALLOW_ORIGINS` to name the dashboard if its URL changes.

*   **Deployment: LIVE** (Shubh, 2026-09-23). Backend **https://meter-proxy.onrender.com**
    (Render free, Singapore, Docker), dashboard **https://meter-five-nu.vercel.app**,
    ledger on **Supabase** ap-south-1. **Render, not Fly.io** — Fly now requires a card and
    the goal was zero spend; Render's free web service takes the existing Dockerfile as-is.
    Three hosts, not two, for the reason DEPLOY.md gives: **Vercel cannot run the
    Treasurer/refresh/soft-budget loops**, which are lifespan `asyncio` tasks, and serverless
    functions do not outlive a request. Render runs them.
    The Vercel project now correctly uses `dashboard/` as its root with the Next.js preset,
    and `NEXT_PUBLIC_METER_PROXY_URL=https://meter-proxy.onrender.com` is set for Production.
    Its homepage and `/dashboard` route are live. The dashboard currently reports no reachable
    ledger, while Render `/healthz` is healthy, so verify Vercel's `DATABASE_URL` is the
    Supabase **transaction** pooler for the same database before presenting live ledger data.
    *   **Cold start is handled, not eliminated.** A free service sleeps after ~15 min idle
        and pays 30–60s waking. **UptimeRobot** pings `/healthz` every 5 min, which keeps it
        warm — measured 300 ms. Judges must not meet a 60-second first request.
    *   **Poolers:** transaction (6543) for the Vercel dashboard, **session (5432) for the
        Render backend**. Both need `?options=-c%20search_path%3Dpublic` — the poolers reuse
        backends without resetting session state, so without it a connection inherits a stale
        `search_path` and fails `relation "requests" does not exist`, *intermittently*.
        Measured 0/6 connections working without it, 6/6 with.
    *   **The dashboard opens one connection per serverless instance, not four.** Vercel
        gives every instance its own module scope, so the `globalThis` pool cache de-dups
        *within* an instance and not across them — `max: 4` became 4 × N connections and hit
        Supabase's `EMAXCONN: max client connections reached, limit: 200`, 500ing every DB
        route while the static page still served. `dashboard/src/lib/db.ts` now detects
        serverless via `process.env.VERCEL` and drops to `max: 1` with a 5s idle timeout.
        A local `npm run dev` polling the same pooler counts against the same 200 — leaving
        one running overnight is enough to take the deployed dashboard down.
    *   ⚠ **One backend instance, on any plan.** `proxy/budget.py` serialises reservations
        with an in-process `asyncio.Lock` (A5), so two instances mean two locks reading the
        same headroom and a daily ceiling that silently stops holding. Redis fixes it and is
        not built. `render.yaml` carries this warning for whoever upgrades the plan.
    *   **RLS is on** (`scripts/secure_ledger.py`). Supabase's `anon` role shipped with full
        read/write **and TRUNCATE** on all nine tables; that key is designed to go in
        browsers and is only safe because RLS is meant to be the gate. Re-run the script
        after any schema change — `CREATE TABLE IF NOT EXISTS` at boot creates new tables
        with RLS *off*.

*   **Last updated:** 2026-08-02 — **Full post-Postgres audit (Shubh).** Boot, every route,
    both images, the dashboard, all suites and soaks, and config-vs-docs drift. Everything
    passes: 249/131/182/46, e2e 21/21, both soaks, negative control still fails as designed,
    both Docker images build and serve.
    *   ✅ **Fixed: the boot log lied.** It printed `ledger ready at meter.db` — a local file
        that does not exist — because it still read `config.DB_PATH` after the port. On a
        deployed proxy that sends whoever is debugging it looking for a file. Now
        `db.ledger_target()`, which prints `host:port/database (schema X)` with credentials
        stripped, since a boot line is exactly what gets pasted into a chat.
    *   ✅ **Fixed: `.env.example` was missing 14 config vars**, including the entire Treasurer
        tuning set (`TREASURER_ENABLED`, `TOPUP_WHEN_HOURS`, `MIN_BALANCE_USD`,
        `BURN_WINDOW_S`, `TARGET_HOURS`, `MIN_TOPUP_USD`), all five `MANDATE_*` and both
        `PREDICT_REFRESH_*`. Undiscoverable config on a deployment nobody can tune.
    *   ✅ **Fixed: two stale docstrings** — `treasury/db.py` still claimed `busy_timeout` was
        set (contradicting its own `connect()` note), and the marketing page still explained
        itself by a `meter.db`-on-local-disk constraint that no longer exists. Both now say
        what is true *and* what stayed true for a different reason.
*   **Model Efficiency shipped, and the cross-model lane is no longer blocked (Shubh,
    2026-08-09).** This was the one genuinely open lane. `scripts/cross_model.py` already
    settled `PROPOSALS.md` B11 the right way — replay offline, never shadow-call a second
    provider on live traffic, because doubling a customer's bill inside a cost-control tool
    is indefensible. What was missing was the last hop: the script writes JSONL on the
    proxy's host, and the dashboard reads only Postgres from another host, so the
    comparison could not reach the view meant to render it. Added a `model_efficiency`
    table, `--push` / `--push-only` on the script, and a **Model Efficiency** card that
    decomposes the cost gap into **rate** (a vendor decision) and **verbosity** (usually a
    prompt one) instead of one useless multiplier — and says on its face that it measures
    no quality, so "cheaper" is half a trade-off. ⚠ **The live replay has not been run**: it
    needs a funded Anthropic key and spends real money, so the card renders its empty state
    until someone decides to. Verified by running the dashboard's literal SQL against a
    seeded table, so the view is proven queryable rather than only typechecked.

*   **Judge money routes need a second, `httpOnly` capability (Shubh, 2026-08-09) —
    `PROPOSALS.md` B20 closed.** The session token stays script-readable, which was always
    the right call: the console calls `/judge/*` cross-origin in `X-Judge-Session`, where a
    cookie is not sent. What B20 found is that the token reaches more than its comment
    admitted — it keys the credential vault and `/judge/mandate` acts with the judge's own
    Prava **merchant** key, so a readable token was a readable licence to charge a real
    card. Now `/judge/mandate` and `/judge/topup` also require `meter_judge_capability`:
    minted independently, stored only as a SHA-256, set by the **proxy on its own origin**
    as `httpOnly; Secure; SameSite=None; Path=/judge`, compared with `compare_digest`.
    Reads are untouched. CORS gained `allow_credentials`, **off whenever origins are
    wildcarded** — `*` plus credentials is rejected by browsers, and silently getting no
    cookie would present as a backend bug. Sessions predating the column fail closed.
    `JUDGE_CAPABILITY_INSECURE` exists for plain-http local dev only and must never be set
    in a deployment.

*   **Per-key scopes shipped (Shubh, 2026-08-09) — `PROPOSALS.md` B19's wider question.**
    A Meter key was all-or-nothing, so "authenticated" meant "may do everything" — the same
    key that reads a balance could drive the autonomous charging loop, and `WALKTHROUGH.md`
    publishes a working one. `meter_keys.scopes` now carries `proxy` / `money` / `read`;
    all eight money-moving treasury routes require `money`, asserted as a **set** so the
    next route added is covered by default rather than enumerated. **`NULL` means
    unrestricted**, which is deliberate: defaulting to deny would have locked every existing
    deployment out of its own treasury on upgrade. The demo key is therefore still
    unrestricted until someone narrows it alongside `WALKTHROUGH.md`. **Judge keys are
    narrowed already** — issued `proxy` only, so even via B20's script-readable token a leak
    reaches metered inference inside a capped session, never a charge. `METER_KEYS` learned
    an optional fourth field (`key:project:env:proxy|read`) and refuses unknown scope names
    rather than silently granting less than intended. Migration verified against a
    pre-scopes `meter_keys`.
*   **`PROPOSALS.md` M8 was already fixed and recorded as open.** `replace_budgets` exempts
    `environment = 'judge'` projects from the boot wipe — keyed on the environment rather
    than an id prefix, so it is a property of the project and not a naming convention.
    `test_a_boot_does_not_wipe_judge_ceilings` covers it, including that a ceiling deleted
    from `meter.yaml` still stops being enforced.

*   **`/healthz` now reports the Treasurer's posture (Shubh, 2026-08-09).** It reported
    only whether the loop had already tripped. Whether it was *running*, and whether it was
    allowed to move real money, were invisible on a deployment: `render.yaml` sets
    `TREASURER_ENABLED` and `TREASURER_DRY_RUN`, the Render dashboard can override either,
    and nothing anywhere told you which won. This section carried a standing warning that
    `TREASURER_ENABLED` must be `false` before judges arrive with real cards — and the only
    way to check was to open the Render console. `treasurer.enabled` and
    `treasurer.dry_run` are on `/healthz` now, so the safe state is verifiable from outside
    by anyone. **`render.yaml` sets both safely; confirm against the live service before a
    demo, because the blueprint is not the running configuration.**

*   **CORS preview origins are opt-in now — `PROPOSALS.md` B22 (Shubh, 2026-08-09).** B19's
    fix stopped the preview regex matching *unrelated* projects, but it still has to match
    `<project>-<anything>.vercel.app`, and `vercel.app` subdomains are first-come-first-served
    — `meter-three-beta-evil.vercel.app` is registrable by anyone. Survivable until B20 turned
    on credentialed CORS: the money capability is a cookie, so a matching origin now receives
    it **automatically**, and the second factor guarding a leaked session token would land on
    an origin an attacker owns. `CORS_ALLOW_VERCEL_PREVIEWS` defaults to **off**.
    ⚠ Testing the console from a branch preview now needs the flag, or that preview URL named
    in `CORS_ALLOW_ORIGINS`.

*   **Filed `PROPOSALS.md` B21: ARCHITECTURE.md's `<5ms p50` is unreachable as deployed.**
    The measured 255 ms is distance, not code — 3 round trips at a Singapore↔Mumbai RTT.
    Three readings lead different places (colocate; or the budget always meant the proxy's
    own work; or restate the budget in *round trips*, which is the thing code controls), so
    it is filed rather than edited: ARCHITECTURE.md is a source-of-truth doc and the reading
    chosen changes what "done" means for the request path. Recommendation is colocate, then
    restate §8 in round trips.

*   **The deployed overhead number exists now: p50 255 ms (Shubh, 2026-08-09).** The
    measurement §6a has been withholding permission to quote since the Postgres port.
    `scripts/bench_deployed.py` provisions a **judge session** and drives that, so every row
    it writes is tenanted to a throwaway `judge-<nonce>` project and **demo-project is
    untouched** — it also needs no Meter key, because a judge's key never leaves the server.
    n=12 across three sessions, two independent runs agreeing within 4 ms, spread 254–263 ms.
    **18% of a 1,428 ms request**; the rest is the provider.
    **The cause is that the deployment is not colocated** — proxy on Render in **Singapore**,
    ledger on Supabase in **ap-south-1 (Mumbai)**. Three sequential round trips at that RTT
    lands where it lands, so the count model predicted the measurement rather than being
    fitted to it. **Colocating is now the highest-value change available anywhere in the
    system**: the same three trips in-region are 3–6 ms, at or just over ARCHITECTURE.md §8's
    5 ms budget. It also prices the trip reduction above at **~170 ms per request** here.
    ⚠ The old ~53 ms laptop figure is **not** comparable — different path, measured from
    outside both regions. Quote 255 ms or quote nothing.
    ⚠ One trap this measurement walked into first, recorded so nobody repeats it: a judge's
    breaker floor is deliberately tiny so judges can *watch* it trip, so a run of any length
    is mostly **refusals**. A refused request never calls a provider — it writes
    `latency_ms = 0` and an `overhead_ms` that is the entire refusal — and averaging those in
    produced a confident **129 ms** that describes neither path. The script now excludes any
    row that did not forward, and says how many it dropped.

*   **Round trips on the request path: 5 -> 3 (Shubh, 2026-08-09).** Measured end to end by
    counting `pg._Connection.execute` across a real request through a running proxy, not by
    reading the code. The shipping configuration (breaker on + ceilings) now makes **3**
    blocking round trips with a warm key cache and 4 cold; the minimal path makes 1 warm,
    2 cold. Two reductions: `db.breaker_state` folds the open-breaker lookup and both spend
    windows into one statement (3 -> 1 over the floor, 2 -> 1 quiet), and `resolve_key` is
    cached in-process for `KEY_CACHE_TTL_S` (default 5 s). **Revocation stays immediate** —
    `revoke_key`, `unrevoke_key` and `set_breaker_floor` purge synchronously, an epoch
    counter stops an in-flight read from re-caching a pre-purge row, and misses are never
    cached so a freshly minted judge key works at once. Pinned by 22 checks that count
    statements at the connection facade, because this is the kind of win that regresses
    with nothing failing. `proxy/README.md` has the counted table. **Still do not quote a
    latency number until it comes from the deployed proxy** — this changes the count, not
    the RTT, and distance is still what dominates.

    *   ✅ **Fixed: `PRAVA_LIVE_MODE=false` now stops every call to Prava** — `PROPOSALS.md`
        **M6**, closed 2026-08-09 (Shubh). It gated `charge_mandate`, `report_charge` and
        `verify_credentials` but **not** `list_mandates` or `create_mandate_session`, so the
        flag that reads as off still sent traffic — including to `POST /v1/sessions`, the one
        endpoint Prava documents a `429 TRIES_EXHAUSTED` throttle on. **So §6a's own "demo runs
        on `PRAVA_LIVE_MODE=False`" did not achieve what it said.** Both are gated now. The
        decision M6 was waiting on: the simulated mandate list is **empty**, never the locally
        stored rows — a list populated from `mandates` would show rows Prava has never
        confirmed, presented identically to ones it has. The `mandates` key is still present,
        so `/mandates` renders `[]` rather than the 503 a missing key produces: simulation is
        not an outage. `create_mandate_session` returns no `session_id` and no `iframe_url`
        (a fake approval URL is a link a human clicks), and `/mandates/create` reports
        `reason: "simulated"` rather than `session_create_failed` — and opens no pending row,
        which was the other half of M6. `_sync_project` bails on a simulated list too;
        without that the empty result fell through to the 15-minute expiry sweep and marked
        genuine pending rows `expired`. 14 checks in `test_treasury.py`, each with a live-mode
        control so a gate that stops working for another reason still fails.
    *   ✅ **Fixed earlier: `POST /mandates/create` and `/mandates/sync` are authenticated** —
        `PROPOSALS.md` **M7**. Both carry `Depends(_authed_key)`
        ([treasury/routes.py](treasury/routes.py) lines 326 and 418). Recorded here because
        §6a and M7 both described them as open long after the fix landed.
    *   **Not a bug, recorded so nobody re-chases it:** `app.routes` reports 11 entries for a
        21-route app because this FastAPI version keeps each `include_router` as one opaque
        `_IncludedRouter`. The treasury surface is fine. Use `/openapi.json`.

*   Prior entry — **Post-Postgres overhead investigated; one round trip removed
    (Shubh, answering Shivam's handoff).** The 52.7 ms figure is real but it is the **best case**,
    and the framing "essentially all of it is one network round trip" holds only for the config it
    was measured in. `bench_overhead.py` hardcoded `BREAKER_ENABLED=false` and defaults to no
    `meter.yaml`, so it measured the minimal path. **Round trips are sequential, so overhead ≈
    count × RTT**, and the count was measured exactly by instrumenting `pg._Connection.execute`
    (loopback timings cannot separate three trips from five):
    *   **2** round trips minimal · **4** breaker on · **5** breaker on + ceilings enforced — the
        last being what actually ships. At ~50 ms RTT that is ~200 ms, not 53 ms.
    *   ⚠ **Colocation alone does not settle this.** Five sequential trips at an in-region 1–2 ms
        is still 5–10 ms, at or over ARCHITECTURE.md §8's 5 ms budget. Region is necessary, not
        sufficient.
    *   ✅ **One trip removed: `db.ceiling_spend`.** `budget.authorize` was issuing two queries for
        feature spend and project spend — same table, same project, same window, differing only by
        a feature filter. Now one scan returning both. Under SQLite this saved microseconds and
        would not have been worth writing; at 50 ms it is 50 ms off every enforced request.
        `test_proxy.py` asserts `authorize` makes **exactly one** call, because this regresses
        silently. Production path 6 → 5 round trips.
    *   `bench_overhead.py` gained **`--breaker`**, so the shipping configuration is measurable at
        all — it was not before. The next reductions, if the deployed number still disappoints, are
        folding the breaker's two queries into one and briefly caching `resolve_key`. **Measure
        first.** Suites re-verified against a local Postgres 16: **249/182/46/131**.
    *   Still true, and still the rule: **no latency number goes on a slide until it comes from the
        deployed proxy.**

*   **2026-08-02 (Ammar, predictor):** shrinkage was the biggest error in the engine — `SHRINK_K` 20 → 1 and the blend made geometric took templated accuracy from ~30% to **~7% held-out / ~10% live**, 24 of 32 features at ≤15%. Corpus grown to 32 feature tags / 1,224 real calls. Verified against the hosted Supabase ledger; `learned_factors: 31`. Open: cross-model analysis still unbuilt (`ANTHROPIC_API_KEY` is empty), `PROPOSALS.md` B11 still undecided.

*   Prior entry — **Treasury hardening (Shubh, in Shivam's module, on
    instruction while that lane was idle).** Three things, all in `treasury/`:
    *   ✅ **M5 fixed.** `GET /treasury/assess` no longer creates a wallet — it uses a new
        `db.wallet_id_for()` instead of `ensure_wallet`. The demo trap is gone: on a fresh
        database, seeding $4.00 after an `assess` now yields **$4.00** rather than the $0.00 it
        silently produced before. Verified on a running app.
    *   ✅ **C3 implemented** — the recommendation from Shubh's own research, which nothing had
        built. Reads retry a 429 with exponential backoff (`Retry-After` honoured *and*
        clamped); **writes are never retried in the transport helper**, because resuming a
        charge safely means `topup`'s pending-event path with the original idempotency key, not
        a second POST hidden inside `_request`; and **`TRIES_EXHAUSTED` is a trip, not a blip**
        — the allowance is spent, so the Treasurer backs off 300s and says so on
        `/healthz .treasurer`. A demo box ticks every 3s, which is 1,200 calls an hour into a
        throttled rail without this. ⚠ **The rate limit itself is still undocumented by Prava,
        so A3's interval question remains open** — this makes either interval survivable, it
        does not answer it.
    *   ✅ **The Treasurer no longer does blocking SQLite on the proxy's event loop.**
        `tick()`, `assess()`, `execute_topup()` and the mock-billing credit all now run their
        database work in threads. There is one process and one event loop, so a background
        top-up decision was previously able to stall **every in-flight request** — bounded at
        five seconds by `busy_timeout` under write contention. Soak-measured worst-case
        event-loop stall fell from 44ms to **19ms**, and is now structurally bounded rather
        than merely observed to be small.
    *   `test_treasury.py` is now **182 checks** (was 159).

*   Prior entry — **Shubh's lane closed out completely (`shubh/final`).** The
    streaming soak landed (see the soak entry below), and the three remaining Shubh-owned
    proposals shipped: **D1** (a caller may supply `X-Meter-Request-Id`, so a retry overwrites
    its own ledger row instead of double-counting — `INSERT OR REPLACE` on a caller-supplied
    id was already the semantics, nothing had ever exposed it), **D2** (soft-budget iMessage at
    80% of any ceiling, as a background poll rather than a request-path check, reading settled
    spend only), and **D3** (documented that Meter's `429` means budget-exhausted, not rate
    limited; `402` stays reserved for a real paywall). Verified against a **running** app, not
    just the modules: the warning fires at exactly 80% and the eventual 429 names the same
    scope string. Refusals now also carry `X-Meter-Request-Id` — found because the live check
    caught a 429 with no id on it, and a breaker trip and a budget refusal both write ledger
    rows the id is supposed to point at. `test_proxy.py` is now **249 checks**.

*   Prior entry — **Sustained-load soak built and passing; overhead numbers corrected (Shubh).** `tests/load_soak.py` closes the last open item in the proxy lane. It
    found three real bugs in the test harnesses. The benchmark's fake upstream had been
    answering **422 to every call**, so the committed +0.26/+0.35ms overhead pair was measured
    on a path that skipped usage parsing and pricing — but re-measuring three times on the
    fixed path **reproduced the same numbers**, so the figures stand and the finding is that
    parsing and pricing a small usage block is nearly free. Also fixed: the harness raced app
    startup (fixed sleep → wait on
    `server.started`), and pointed the proxy at **api.openai.com instead of the fake upstream**
    because the env override landed after `proxy.config` had already been imported — it only
    failed safe because the key was fake. Prior entry — **`PLAN.md` reconciled against the code
    (Shubh).** Every phase
    item is now marked ✅/🟡/⬜ with evidence. The result: **one lane is genuinely open — Ammar's
    cross-model routing, the efficiency data it feeds, and Tanay's Model Efficiency view on top of
    it (four plan items, one dependency, `PROPOSALS.md` B11)** — plus a sustained-load run (Shubh)
    and the demo video/pitch rewrite (Tanay). The pitch script is flagged stale in three places:
    it says "one-time card" where we ship a mandate, its repeat-top-up beat is blocked by the
    one-charge-per-cycle limit, and its cross-model beat has no measurement behind it. Prior
    entry — **full documentation review + external research (Shubh, same day as phase3 merge):** Prava docs (`docs.prava.space`), Linq docs
    (`docs.linqapp.com`), and the major open-source LLM gateways (LiteLLM, Helicone, Portkey,
    OpenRouter, one-api, Langfuse) were read for drift and gaps. Findings: Prava rate limits
    are **undocumented anywhere** (C3 researched, still open); recurring mandates are
    documented as **one charge per cycle** with no over-count error — the Treasurer loop must
    self-gate on `renewsAt` + status (see Treasurer entry); Linq sandbox requires the
    recipient to message first (C5, verify before demo); "Poke is not Linq's former name" —
    Poke is Linq's flagship customer; gateway review added `PROPOSALS.md` D1–D4 (request-id
    echo, soft-budget alert, 402-vs-429 note, and a "things we already do right" list) and
    fixed stale `proxy/README.md` text (check counts, the overhead-harness contradiction,
    and the "Not implemented" table). Prior entries — **`shubh/phase2` merged into main**, then
    **`shubh/phase3` + full-codebase audit (Shubh, same day).** Phase 3: the **Treasurer agent
    loop** (`treasury/loop.py`, registered in `proxy/app.py` lifespan) watches burn rate every
    `TREASURER_INTERVAL_S` and autonomously tops up when projected runway drops under
    `TOPUP_WHEN_HOURS_REMAINING = 0.75h`; the full decision path was runtime-verified end to
    end (runway 0.56h → top-up $0.79 computed → dry-run refusal → loop continues). Top-up
    iMessages wired (`send_topup_alert`, cooldown-scoped like the breaker alerts).
    **Overhead benchmark committed** (`tests/bench_overhead.py`): p50 **+0.26ms** (minimal)
    / **+0.35ms** (enforced path) self-reported, consistent with the documented 0.29ms.
    **Audit fixes (all verified):** treasury money routes now require a Meter key (B18,
    recommendation 1 — 401 verified live in the container); `/mandates` + `/mandates/sync`
    return a 503 envelope instead of a bare 500 when Prava is down (M3); `/report` validates
    `transaction_id` shape (M4); every `execute_topup` refusal now leaves a
    `treasury_events` row (was documented, wasn't true); `tests/test_treasury.py` added
    (**18 checks** — suites now total **387**); `Dockerfile` + `compose.yaml` built and the
    container smoke-tested (B10 closed); GitHub Actions CI (ruff + all four suites + dashboard
    build/lint); ruff debt fixed and `ruff.toml` added; `.editorconfig` added. Remaining,
    Shubh: `features.<name>.models` allowlist, Redis at replica #2 (never in the 48h build).
    Prior entry — **`shubh/phase2` merged into main:** the merge reconciled two
    independent predictor integrations: Ammar's estimator v2 (scope stacking, history correction —
    `predictor/DESIGN.md`) wired ESTIMATE/CAPTURE on main while Shubh's branch wired
    ESTIMATE/RESERVE/CAPTURE. **Ammar's `_predict` + estimator v2 won the ESTIMATE step; Shubh's
    RESERVE (in-process authorize/capture, `proxy/budget.py`) now holds v2's
    `predicted_cost_usd`.** Both sides had picked identical ledger column names, so the schema
    merged cleanly. Shubh's branch also brought: daily ceilings from `meter.yaml`,
    `POST /v1/annotate`, and three review decisions recorded below (B17 validation rule, B9
    ratified, overhead number qualified). Also on main from this window: dashboard restyled onto a
    dark control-room visual system (Tanay) — Phase 4's "finalize UI" effectively done ahead of
    schedule; Poke/Linq breaker alerts wired and verified live (Tanay) — a real iMessage delivered
    end to end; treasury `/topup` + mandate-selection fixes (Shivam). **Since that merge: the
    dashboard's "Team Budget" card (Tanay) reads Shubh's new ceilings, closing the §5D gap that
    had been specified but unbuildable — spend against a limit, per project and per feature.
    "Cost per Outcome" (Tanay) followed, putting the `requests × annotations` margin metric on
    screen and documenting the fan-out trap that makes the obvious query overstate cost.**

*   **Setup: DONE.** `/docs/prava` and `/docs/linq` have reference docs (API reference, SDKs, sandbox
    test cards, error codes) pulled from the sponsor doc sites, scoped to what Meter's Prava
    top-up flow and Poke/Linq iMessage alert need. Visa VIC test-card requirements are covered by
    `docs/prava/api-reference/test-cards.md` (no separate Visa doc set needed). `.env.example`
    at repo root has placeholders for all provider/datastore/agent config keys.

*   **Proxy: WORKING.** `proxy/` — FastAPI, run with `uvicorn proxy.app:app --port 8080`. Full detail in `proxy/README.md`.
    *   `POST /v1/chat/completions` (OpenAI-shaped) and `POST /v1/messages` (Anthropic-native); provider chosen by model prefix, overridable with `X-Meter-Provider`.
    *   Caller sends a **Meter** key; the proxy substitutes the master provider key upstream.
    *   SSE streaming with real usage extraction for both provider shapes (pulled forward from Phase 2). Non-streamed calls too.
    *   Attribution rungs 0–2 (`X-Meter-Feature` / `-Actor` / `-Trace`) recorded on every row.
    *   **Verified against the real Anthropic API** (2026-08-01): real completions, real SSE streams, usage parsed from the actual wire format, costs matching the published haiku-4-5 rates to 8 decimal places, on both `/v1/messages` and the OpenAI-compat path. That run found two bugs no fake upstream could (`PROPOSALS.md` B15, B16) — most seriously, streamed responses arrive **gzipped**, and reading them raw left every streamed row byte-estimated *and* handed clients unreadable compressed SSE.
    *   Measured overhead: quote it as **"p50 +1.49 ms, measured Phase 1 on loopback"** — never without the qualifier. Two caveats: it predates ESTIMATE and RESERVE (the enforced path, with a `meter.yaml` present, is untested), and ⚠ **the harness was never committed, so nobody can reproduce the number on demand.** Re-measuring plus committing the script is Phase 4 work (Shubh); `overhead_ms` is already a ledger column, so it is a loop plus one `SELECT`. **Re-run it before it goes on a judge-facing slide.**
    *   **Phase 2 done (2026-08-01):** ESTIMATE, RESERVE and `POST /v1/annotate` all landed. `python tests/test_proxy.py` is now **215 checks**.
    *   **Predictive engine wired in.** Every row now carries `predicted_output_tokens`, `predicted_cost_usd`, `bucket`, `prediction_method` alongside the actuals, so predicted-vs-actual variance is a subtraction. **This closes Ammar's feedback loop** — the rows `load_fits()` needs now exist; nothing calls it on a schedule yet. Prediction degrades to NULL rather than erroring on anything unsupported, **which includes every Claude model** (no tiktoken vocabulary; the predictor refuses to guess).
    *   **Daily ceilings enforced** from `meter.yaml` at the repo root (`meter.yaml.example` is the template), project-level and per-feature. Refusal is `429` with `X-Meter-Budget-Scope` / `-Ceiling-Usd` / `-Spend-Usd` naming the ceiling hit — whichever ceiling is actually exhausted, since that is what tells an operator which line of the file to edit. No `meter.yaml` = no ceilings and no added latency, which is the Phase 1 behaviour exactly.
    *   **Validation rule changed from what `ARCHITECTURE.md` §4 originally specified** (decided 2026-08-01, `PROPOSALS.md` B17, §4 updated). The loader rejects a config where a *single* feature ceiling exceeds its project's, and only **warns** when the siblings *sum* past it. The old sum-rule answered an over-restrictive config by enforcing nothing at all for that project. Over-allocated features are safe because both ceilings are checked independently at authorize time — asserted in the self-check, not assumed.
    *   **Reservations are real** (`proxy/budget.py`), in-process per the A5 decision. Holds are counted alongside settled spend inside one `asyncio.Lock`, so concurrent requests cannot all pass the same ceiling — the self-check fires 40 at a ceiling admitting 4. Released *inside* the capture task so the hold never disappears before the row lands, and **heartbeat-extended during streams**, which ARCHITECTURE.md §2 flags as a silent failure if skipped. `reservation_id` is no longer written NULL.
    *   **`POST /v1/annotate`** (attribution rung 3, was PROPOSALS.md B9 and owned by nobody; ratified 2026-08-01). Returns the trace's total cost, request count and margin, scoped to the calling key's project — a `trace_id` is caller-supplied, so without that scope any key could read another project's spend.
    *   **Ledger migration:** `proxy/db.py` ALTERs missing columns onto an existing `requests` table at boot (`_ADDED_COLUMNS`). Pull and restart — no manual step, no dropped database. The database is shared now, so a column added without an entry there fails for everyone at once rather than on one machine.
    *   **`features.<name>.models` allowlist built 2026-08-01** (was listed as "not yet done"). A request whose model is not on its feature's list is refused with **403 `model_not_allowed`** and an `X-Meter-Allowed-Models` header between ATTRIBUTE and ESTIMATE — before any prediction or reservation — and the rejection is ledgered like the breaker's. Malformed lists are ignored with a warning, same posture as a bad ceiling. Verified in the self-check.
    *   Not yet done, Shubh: Redis-backed reservations (only needed at proxy replica #2).
    *   ⚠ **Overhead is distance-bound since the Postgres port: p50 +52.7ms**, measured from a laptop against Supabase in ap-south-1 (`tests/bench_overhead.py`, 2026-08-02). Essentially all of it is network — a warm round trip to that database measures ~50ms from here, and the request path makes a small number of them. **It should return to single digits with the proxy colocated with the database on Fly.io, but nobody has measured that yet — do not quote a latency number until it comes from the deployed proxy.** Running the pool in autocommit halved it on its own (115ms → 53ms): without it psycopg opened a transaction on the first statement and the pool ended it on the way out, so every one-statement helper paid for a query *and* a COMMIT.
    *   The pre-Postgres figures below are kept because they are the measurement of the *proxy's own work*, which has not changed — only the storage round trip has.
    *   **Overhead: p50 +0.26ms minimal / +0.35ms enforced** on the SQLite ledger (`tests/bench_overhead.py`) — **re-validated against a working upstream.** A bug was found in the harness on 2026-08-01: the fake upstream had been answering **422 to every call** (`from __future__ import annotations` plus a function-local `Request` import made FastAPI treat the handler's `request` argument as a required query parameter), so every benchmarked call skipped usage parsing and pricing. Fixed — and re-measuring three times each reproduced the same numbers (0.26/0.27/0.30 minimal, 0.35/0.36/0.37 enforced). **The honest conclusion is that parsing a small non-streamed usage block and pricing it is nearly free**, not that the old figure was wrong. ⚠ One single run during that work read 0.40ms and did not reproduce — **take any single reading of this number with suspicion; run it three times.**
    *   **Sustained-load soak: DONE, and it passes** (`tests/load_soak.py`, 2026-08-01 — the Phase 4 "stress test the proxy / fix race conditions in concurrent DB writes" item). N clients drive the enforced path while the Treasurer writes `treasury_events` to the same ledger. Measured at 16 clients / 15s **on SQLite**: **~5,000 requests at ~400 req/s, every one ledgered, zero `database is locked`, zero failed ledger writes, worst event-loop stall 44ms.** CLAUDE.md's two-writer claim was an argument until then; this was the evidence for it.
        *   **Re-run on Postgres (2026-08-02): all 9 checks pass** at 8 clients / 12s — every 2xx ledgered, zero lock or pool errors, event-loop p99 16ms, the Treasurer wrote throughout. Throughput is not comparable to the SQLite figure and should not be quoted against it: the database is now a WAN hop away, so this run is bound by the same ~50ms round trip as the overhead number. The harness counts `PoolTimeout` and `deadlock detected` alongside `database is locked` — the new engine's version of the same failure — and sizes its pool above `--concurrency` so it measures the proxy rather than psycopg queueing.
        *   **Throughput stopped scaling past ~16 clients on SQLite** — at 64 the proxy sustained ~122 req/s against a ~247 req/s no-proxy baseline the harness measures itself, so the ceiling was attributable rather than guessed. About half was the single-process harness saturating its own event loop; the rest was the A5 design (one SQLite connection behind a lock, shared `to_thread` pool). The one-connection-behind-a-lock half is gone — Postgres serves concurrent writers from a pool — and the shared `to_thread` pool half remains.
        *   **Deliberately not in CI.** Timing-sensitive thresholds on a shared runner is how a load test becomes flaky and then muted.
        *   **Streaming now covered too** (`--stream`, added 2026-08-02). An SSE fake upstream emits deltas for 4s against a **2s reservation TTL**, so every response deliberately outlives its own hold — the condition `budget.extend()` exists for. Asserts usage came off the wire (not a byte estimate, the B15 failure), the stream was readable SSE, the injected usage chunk was stripped, and **live holds never hit zero while streams were in flight**.
        *   ⚠ **The reservation check is verified sensitive, not just passing.** `--break-heartbeat` pushes the heartbeat past the stream duration and the check must fail — it is run that way deliberately. The first version of it *passed* under that control and was therefore worthless: it counted `len(_holds)`, but holds are reaped lazily (only when the next `authorize()` runs `_expire()`), so an expired hold lingers in the dict. It now counts holds whose `expires_at` is still in the future. **Any future edit to that check must be re-validated with `--break-heartbeat`.**

*   **Ledger: WORKING, ON POSTGRES** (Shivam, 2026-08-02 — `PROPOSALS.md` A5's SQLite decision is superseded). The proxy writes a priced row per call to Supabase (ap-south-1). Everything goes through `proxy/pg.py`: one `psycopg_pool` pool, per-statement autocommit, `?` placeholders rewritten to `%s` so the SQL in `proxy/db.py` and `treasury/db.py` is byte-identical to the SQLite version — the diff is a change of execution layer, not fifty rewritten statements. Indexes on `(project_id, ts)`, `(trace_id)`, `(prompt_hash)` carried over.
    *   **Why hosted, beyond deployment:** the predictor needs ~20 rows for a `(project, feature)` key before its correction beats the raw heuristic. A judge or teammate running against an empty local file got the *worse* number (65% median error against 31%). One shared database means everyone inherits the accumulated history.
    *   **Isolation is by schema, not by file.** `DB_SCHEMA` (default `public`). Every test suite and scratch harness mints a throwaway schema and drops it in a `finally`, which is what replaced pointing `METER_DB_PATH` at a tempfile — without it a test run would delete rows from the database the demo and the judges are using.
    *   **Types are ported like for like, deliberately.** `ts` stays TEXT rather than `timestamptz`, money stays `double precision` rather than `numeric(14,6)`. Both upgrades are correct and both are in `ARCHITECTURE.md` §4, but each changes comparison semantics the rolling-window queries and the dashboard's `isoSecondsAgo` cutoff depend on. Doing them in the same commit as the engine swap would mean a failure could be either. **Open follow-ups.**
    *   **`sort_order` added to `projects` and `feature_budgets`.** Postgres has no `rowid`, and the budget cards read meter.yaml order — which under SQLite came free from insertion order. `replace_budgets` stamps the position explicitly now.
    *   **Two things the port broke and fixed:** `replace_budgets` ran an explicit `BEGIN` through the connection facade, but each statement borrows a *different* pooled connection, so the rebuild was silently non-atomic against its own docstring — `pg.transaction()` now yields one connection for a whole block. And `pg._args` passes `None` rather than `()` for parameterless statements, because psycopg client-side-binds whenever it is handed a sequence and then rejects any literal `%` in the SQL.
    *   Not started: Redis-backed reservations (only load-bearing at proxy replica #2).

*   **Circuit Breaker: WORKING** (pulled forward from Phase 3). `proxy/breaker.py`. Rolling-window detection, `throttle` (429, tag-scoped) and `revoke` (403, key-scoped) modes, auto half-open recovery, manual reset at `POST /v1/breaker/reset`. **Poke alerts are wired** — see the Alerts entry below.

*   **Predictive Engine: WORKING (v4), WIRED INTO THE PROXY, LEARNING FROM THE SHARED LEDGER.** `predictor/` — method in `predictor/DESIGN.md`, contract in `predictor/README.md`, self-check `python tests/test_predictor.py` (131 checks, mints its own Postgres schema).
    *   **Verified end to end against the hosted Supabase ledger (2026-08-02):** 601 checks green, 1,224 rows / 32 features seeded, `/healthz` reports `learned_factors: 31`, and a live `scripts/try.sh` call predicted 47 against an actual 48. **Two things to run it yourself:** `scripts/try.sh <feature-tag> "<prompt>"` for one prompt with predicted-vs-actual, `scripts/demo_live.py --n 2` for every tag at once.
    *   **Use the Supabase POOLER host, not the direct one.** `db.<ref>.supabase.co` publishes only an AAAA record and will not resolve on an IPv4-only network. Working form: `postgresql://postgres.<project-ref>:<pw>@aws-1-ap-south-1.pooler.supabase.com:5432/postgres` — note `aws-1` (aws-0 resolves but refuses) and the user is `postgres.<project-ref>`.
    *   `predict(payload, model, max_tokens, response_format=, project=, feature=, actor=) -> PredictionResult`. Deterministic, no I/O. Called from `proxy/app.py` at ESTIMATE; prediction stored beside the actual at CAPTURE.
    *   **Two numbers, not one.** `predicted_*` is the forecast (dashboard, treasurer runway). `bound_*` is what the call *cannot* exceed and is what a ceiling check must use. With `max_tokens` set the bound is exact, so the safety guarantee is structural, not statistical. **The forecast carries no safety buffer** — that would double-correct against the history factor.
    *   **CURRENT ACCURACY (2026-08-02). Quote these, not the historical figures further down.**

        | traffic | median APE | within 2x | p90 | n |
        | --- | --- | --- | --- | --- |
        | **templated, held-out slots** | **6.8%** | **96.1%** | 21.3% | 1,224 rows / 32 features |
        | **templated, live through the proxy** | **9.7%** | **97%** | 41% | 64 fresh prompts |
        | templated, no history (cold start) | 79.5% | — | — | same rows |
        | open-ended (WildChat locked test) | 49.2% | 54.7% | 829% | 75 |

        *   **24 of 32 features are at or under 15%**; 27 of 32 under 30%. Best: `entity-tag` 0.9%, `json-extract` 1.9%, `rfc-draft` 3%, `security-audit` 5%.
        *   **The one real failure is `severity-triage` at ~69%, and it is not fixable by tuning.** Its untruncated rows still spread 5.1×, so the model itself is inconsistent on that task. Its answers are 34 tokens, so the cost impact is nil — but do not present it as a win.
        *   **`incident-runbook` and `error-explainer` read artificially well at their own cap and badly above it.** All 80 of their training rows hit a 400-token ceiling, so their history says "400 tokens" — true only at `max_tokens=400`. `scripts/try.sh` warns on both. Re-collecting them uncapped (~3 cents) would fix it.
        *   **What produced the jump from ~30% to ~7%:** two constants, not new modelling. `SHRINK_K` 20 → 1, and shrinkage made geometric rather than arithmetic. See the shrinkage entry below.
    *   **Do not quote median APE alone — `scripts/accuracy_report.py` prints the honest picture.** Median hides direction, money, and the tail, all three of which a budget tool cares about. *(Table below is the pre-tuning v1/v2 measurement, kept because the reasoning it supports is still correct; the headline numbers are superseded by the table above.)*

        | | median | p90 | within 2x | token-weighted | portfolio bias |
        | --- | --- | --- | --- | --- | --- |
        | open-ended (WildChat test, n=75) | 49.2% | **829%** | 54.7% | 83.9% | +18.7% |
        | templated, no history (n=200) | 82.6% | 903% | 40.0% | 103.6% | +36.3% |
        | templated + history (n=200) | 28.8% | 108% | 89.0% | 32.5% | −18.6% |

        *   The history correction's real win is the **tail**, not the median: p90 falls 903% → 108% and within-2x goes 40% → 89%. That is the difference between "usually about right" and "reliably about right".
        *   **⚠ It also flips the portfolio bias negative (−18.6%).** In aggregate the corrected forecast now runs ~19% *light*. This does not leak ceilings — those hold `bound_cost_usd`, not the forecast — but the Treasurer's runway projection is ~19% optimistic and should carry a margin. Untested against a real top-up decision.
        *   Token-weighted error (error across the whole bill, so big requests dominate) is the metric closest to what a treasurer feels: **32.5%** templated, 83.9% open-ended.
    *   **REPLICATION on a second, independent set (`scripts/consistency_check.py`).** Set v2 is 264 real calls over **8 different templates**, `max_tokens=1500`, **0% truncated**, never used to fit or tune anything. It exists because v1's headline could have been a property of five templates the author wrote, plus 80 truncated rows.
        *   **The method replicates.** Base 65.0% → **31.6%** median with history (v1: 82.6% → 28.8%). p90 990% → 116%. within-2x 62% → **87.1%**. Token-weighted 54.2% → 33.2%. The loop roughly halves error on data it has never seen.
        *   **The ≤30% target is MISSED at 31.6%** (95% CI [29.5%, 34.1%]; P(true median ≤30%) ≈ 7%). Excluding `ticket-classify` — a feature whose median answer is **9 tokens**, where APE is nearly meaningless — it is 29.4%. **Do not tune against v2 to close this gap**: it is now a held-out set, and fitting to it would repeat exactly the adaptive overfitting the three-way split exists to prevent.
        *   **The live gated loop installed 7 of 8 features**, every one an improvement (changelog-entry 35%→18%, sql-from-question 53%→25%, api-doc-paragraph 40%→18%, postmortem-timeline 77%→36%, pr-description 72%→33%, regex-explain 82%→41%, ticket-classify 300%→136%). `test-plan` was held back as "unproven" — the conservative default. So the shipped code and the k-fold analysis agree, which they need not have.
        *   **Under-prediction got worse, not better: portfolio bias −32.4%, under-rate 84.8%.** The mechanism: the factor is fitted as `median(actual/scope)`, but output length is right-skewed, so a median-fitted factor systematically under-forecasts the **sum**. Correct for per-request accuracy, wrong for aggregate spend. Fix, when it matters, is a mean-fitted (or high-quantile) factor for forecasting alongside the median-fitted one for per-request estimates.
        *   **Severity of that bias is LOW, contrary to a claim first recorded here — nothing decides on the forecast.** Traced every consumer: budget ceilings hold `bound_cost_usd` (not the forecast); the Treasurer's runway divides balance by trailing **actual** spend, `SUM(cost_usd)` from the ledger (`treasury/loop.py` → `proxy/db.py:project_window_spend`), so it never reads a prediction; the dashboard only *displays* `predicted_cost_usd`. The forecast is currently informational. Treat this as a reporting-accuracy issue, not a safety one — and re-check this paragraph before wiring the forecast into any decision.
    *   **Shrinkage was the single biggest error in the engine, and it was a constant nobody had revisited.** `load_history` blends a fitted factor toward 1.0 by `(n·raw + k)/(n + k)` with `k = 20`. Measured on 1,224 held-out slot fillings across 19 features, **k=20 cost 25 points of median error — 32.1% against 6.7% at k=1 — and was worse for every feature tested**, including at fit sets subsampled down to 10 rows (9.5% vs 58.6%). The guard protects against noisy estimates, but output length inside one template varies only 1.0–1.4×, so there was almost no noise to suppress and the blend contributed bias alone. Reproduce with `scripts/shrinkage_sweep.py`.
    *   **The blend is also now geometric, not arithmetic.** These factors are multiplicative, so an arithmetic blend toward 1.0 inflates factors below 1 far more than it deflates factors above 1: `commit-message` needs 0.092 and linear shrinkage returned 0.114 (+24%), visible live as a 51-token prediction for a feature whose best possible constant is 42. Geometric returns 0.099 and takes median error 8.2% → 7.1% overall — it removes a systematic bias against exactly the short-output features where percentage error is harshest.
    *   **`FACTOR_MAX` has been widened twice, both times because it clamped real signal.** `[0.5, 3.0]` → `[0.05, 20]` → `[0.02, 100]`. Real features want factors from 0.09 to 51.7. The clamp was never the noise guard — `MIN_ROWS_FOR_KEY` and shrinkage are — so it only has to stop the absurd.
    *   **Targets: the original <30% is met on templated traffic and remains unreachable on open-ended.** On templated traffic the engine is at ~7% held-out / ~10% live, comfortably inside the original target. On open-ended prompts it sits at 49.2% and that is close to a hard ceiling: `std(log(output)) = 1.16` and our features explain R² = 0.28, where 30% would need R² ≈ 0.88. **State the traffic shape whenever quoting a number.** Do not quote MAPE — a handful of 20-token answers dominate it permanently.
    *   **What the optimizer proved about our own design:** it drove nearly every hand-written cue to neutral. On real traffic, task keywords fire on 17.4% of prompts and CoT cues on 1.7%. Predicting a constant per bucket and ignoring the prompt scores 51.4% against 44.2% for the full machinery — the scope extraction is worth ~7 points, not the bulk of the estimate. Strongest real signals found by search: `is_very_short` (-0.40), `start_generate` (+0.35), `start_question` (-0.29).
    *   **Offline self-tuning works and is applied.** `python -m predictor.optimize --apply` (coordinate search over `ScopeConfig`) and `python -m predictor.discover --apply` (greedy feature selection over a log-linear model) write `data/fitted.json` and `data/model.json`, loaded automatically at `Predictor()` construction. Delete either file to revert.
    *   **Online loop is built, gated, and now actually RUNNING in the proxy.** `predictor/refresh.py` reads the ledger, fits on the older 75%, and scores against the newest 25%. Spawned from `proxy/app.py`'s lifespan behind `PREDICT_REFRESH_ENABLED` (default on, `PREDICT_REFRESH_INTERVAL_S=120`); `/healthz` reports `learned_factors`. Never touches the request path — it refits into an in-memory dict that `predict()` only reads.
    *   **Gating is PER-KEY, not all-or-nothing.** Each candidate factor is accepted only on held-out rows it owns. All-or-nothing was measurably wrong: on the templated ledger, four features improved 2–3× (code-review-note 1417% → 633%, commit-message 718% → 279%, incident-runbook 22% → 8%, ticket-summary 8% → 4%) while a fifth got worse — and because the pooled median happened to sit inside that fifth feature's rows, the entire candidate was discarded. One bad key vetoed four good ones. The factors are independent by construction, so per-key is also the honest unit.
    *   **Three bugs found by booting the real app against a real ledger, all fixed — none of which the unit tests could see.** The loop ran on its timer, logged a verdict every pass, and installed nothing, ever: (1) the correction factor was clamped to `[0.5, 3.0]`, but real templated features need **0.27 to 18.2** — the clamp destroyed the win and the gate then correctly rejected what was left (now `FACTOR_MIN/FACTOR_MAX`, with `MIN_ROWS_FOR_KEY` + shrinkage as the actual noise guards); (2) the gate scored a *shrunk* candidate and installed the *raw* one, so it validated an object that was never installed (now one `shrink_history()` used by both); (3) `load_history` re-shrank already-shrunk values, pulling every factor toward 1.0 twice (now `set_history()` for pre-shrunk input). Pinned by `test_refresh_gate`.
    *   **The loop's value depends entirely on whether traffic is templated, and this is now measured, not assumed.**
        *   On **WildChat** (unrelated strangers' prompts, synthetic keys) it is **FLAT**: first third 55.1%, last third 63.9%, and a permutation test puts a swing that size at 18% under random reordering — batch noise, not learning.
        *   On **templated traffic** (`scripts/templated_probe.py`, 200 real gpt-4o-mini calls, 5 templates × 40 slot fillings, $0.025 spent) it is a **large real win**. K-fold, out-of-sample: base engine **82.6%** median APE → **28.8%** with the per-`(project, feature)` factor. On the 120 untruncated rows: **332% → 28.1%**. Control (same machinery, feature labels shuffled, 20 repeats) lands at 71.1% and never below 70.2%, so the gain is per-feature signal, not five free parameters.
        *   Within a template, observed output length is extremely regular — p90/p10 spread of **1.0–1.5×**, against a 10× spread across templates. That regularity is the whole mechanism.
        *   **Use `scripts/history_value.py`, not the prequential curve, to judge this on small data.** With 200 rows over five features whose true outputs differ 10×, each batch of 20 is a different mixture: batch 7 scored 18% and batch 10 scored 347% with identical factors installed. The curve measures batch composition, the k-fold measures the factor.
        *   Caveat worth keeping: 80 of the 200 responses stopped at `max_tokens=400`. Those are the truth for *billing* (the caller is charged 400 and no more) but a lower bound on natural length. `--exclude-truncated` on `prequential.py` measures the other reading; the conclusion holds either way.
    *   **Two real bugs the prequential test caught, both fixed:** (1) the buffer and history factor were both fitted as `actual/scope` and both multiplied the same base, computing `scope × (actual/scope)²` — median error rose 77% → 204% as the loop "learned"; (2) fitting a factor against `predicted_output_tokens` rather than `predicted_scope_tokens` divides by the previous factor each refresh and oscillates between 1.91 and 1.00 forever.
    *   **The corpus is 32 feature tags, 1,224 real gpt-4o-mini calls, ~$2.20 spent.** Built by `scripts/corpus_probe.py` (19 tags, TPM-paced, held-out slots built in) and `scripts/templated_probe.py` (13 tags). Spans 36 to 44,680 input tokens — a ~600× range — and within-template output regularity is p90/p10 of 1.0–1.4× for 30 of 32. That regularity is the entire mechanism the per-feature factor exploits.
    *   **Coverage does not generalise between features — measured, not assumed.** Leave-one-feature-out with bucket-level history made a *new* feature WORSE (71% → 74% median, and 39% → 625% at worst), because one bucket averages features whose outputs differ 10×. A new feature tag therefore starts at ~80% error and needs ~20 calls of its own. That is the honest onboarding claim: **tag your features, and after ~20 calls each you get sub-15% cost prediction.**
    *   **Data:** `data/wildchat/{train,validation,test}.jsonl` (522/150/75, committed — regenerating means re-paging a rate-limited API). `data/calibration/*.jsonl` — 45 real API observations. `data/templated/gpt-4o-mini.jsonl` — the 200 probe calls above, committed because they cost real money and cannot be regenerated for free. **`test.jsonl` is the locked set: score it once, at the end.**
    *   Deferred: the `translation` bucket is knowingly mis-calibrated; see `predictor/buckets.py`.
    *   **Merged with Shubh's RESERVE (2026-08-01):** `proxy/budget.py` holds the estimate against the daily ceiling before forwarding, so the prediction is not informational — it is what a request reserves. It correctly holds **`bound_cost_usd`, not the forecast**: reserving the forecast would leak the ceiling on every under-prediction (~half of requests, by design, since the forecast carries no safety buffer). Over-holding the bound is transient, released at CAPTURE. This is the two-numbers split in DESIGN.md §1 being used exactly as intended.
    *   **Known gap vs §5A:** §5A specifies a trailing-p95 fallback per `(project, endpoint, model)`. The history correction is the closest equivalent but keys on `(project, feature, actor)` and corrects residuals rather than replacing the estimate. Raised in `PROPOSALS.md` rather than treating §5A as satisfied.

*   **Treasurer Agent / Prava: WORKING END TO END AGAINST SIMULATED PRAVA; ONE LIVE RUN OUTSTANDING.**
    `treasury/` — **mounted on Shubh's proxy app**, so there is one backend process on one port:
    `uvicorn proxy.app:app --port 8080`. Treasury routes are kept off
    the `/v1` prefix, which stays the surface a caller's provider SDK targets.
    *   **We use a standing mandate, not a one-time virtual card.** §4/§5B and the pitch script still
        say "one-time scoped card"; the mandate is strictly better for the Treasurer — the human
        approves once with a passkey, the agent charges repeatedly with none. Wording needs updating
        before the demo.
    *   **Verified against the real Prava sandbox** (2026-08-01): charge against an active mandate with
        no passkey; repeating a `reference` returns `deduplicated: true`; an over-cap charge is refused
        with `THRESHOLD_EXCEEDED`. That refusal is the safety beat — `POST /charge-refusal`.
    *   `report_charge()` settles a charge via `POST /v1/mandates/{id}/charges/{txnId}/report`.
        Without it charges sit at `awaiting_result` forever. **Written from the docs, not yet run
        live** — the one piece of this lane that is documented rather than verified.
    *   `treasury/db.py` adds `wallets`, `mandates`, `treasury_events` to the **same database** the
        proxy writes, column names verbatim from `ARCHITECTURE.md` §4. The Treasurer is a second
        writer alongside the proxy; Postgres makes the lock contention a non-event, and the rule
        that made it safe still holds in its new form — writes are single statements and no
        transaction is held open across a Prava call, which would otherwise pin a pooled
        connection for the length of a network round trip. `treasury_events` is written *before*
        Prava is called and its row id is the `reference`, so a retry after a timeout dedupes
        instead of double-charging (§5).
    *   `POST /mock-openai/billing` accepts minted credentials and credits the wallet
        (`treasury/mock_provider.py`). This is the one simulated component — everything on the Prava
        side of it is real. Say so in the demo.
    *   **Treasurer loop: WORKING** (`treasury/treasurer.py`). `assess()` reads balance and burn
        (from `proxy/db.py:project_window_spend`, which Shubh wrote for exactly this) and projects
        runway; `tick()` acts. **Two triggers, and the second is not redundant:** runway below
        `TREASURER_TOPUP_WHEN_HOURS`, *or* balance below an absolute floor — at zero traffic burn is
        0, runway is infinite, and a wallet at $0.00 would never trip the runway check.
        Amount is shortfall + buffer toward `TREASURER_TARGET_HOURS` of runway.
        `GET /treasury/assess` shows the decision without spending; `POST /treasury/tick` runs one
        pass on demand, so the demo does not depend on a timer firing at the right moment.
        Guarded by **two** switches: `TREASURER_ENABLED` (does the loop wake up) and
        `TREASURER_DRY_RUN` (can it move money). `notify()` is a log-only seam mirroring
        `breaker.notify` — **Tanay wires Poke to it.**
    *   **Mandates are scoped per project** via `externalUserId` (`meter_{project_id}`), which Prava
        echoes on every mandate. Selection also excludes `one_time` and checks remaining headroom.
        This matters because judges create their own mandates on the *same* merchant account —
        unscoped selection would let the Treasurer charge a stranger's card.
    *   **Self-serve onboarding:** `POST /mandates/create` returns an approval URL (the session's
        `iframe_url`); `GET /mandates/status` polls until `ready`. **Tanay: that is the two-call
        "Connect your card" flow.** A mandate does not exist on Prava's side until approved, so the
        pending row is held locally.
    *   **Failure handling (Phase 4):** every Prava call goes through one helper and none of them
        raise. A **timeout leaves the event `pending`** — it is not a refusal, the charge may have
        landed — so a retry resumes the same row and the same idempotency key and Prava dedupes it.
        A definite refusal settles `failed`. `X-Response-ID` is captured into the error column,
        because that is what Prava support traces on.
    *   ⚠ **Two settings before any live run**, both read at import so uvicorn must restart:
        `TREASURER_DRY_RUN=false`, and `PRAVA_MANDATE_ID` still points at the `one_time` mandate
        (only affects the bare `/charge` and `/report` endpoints — `/topup` reads the table).
    *   🛑 **ONE PURCHASE PER PAYMENT CYCLE — confirmed live, 2026-08-01.** A second charge against
        the monthly mandate was declined by Visa:
        *"Purchase already made in the current payment cycle for transaction: tli_…"*.
        `remaining` and `renewsAt` make a recurring mandate *look* like a renewing pool; it is not.
        The evidence was visible all along — the monthly mandate reads `45.00/50.00`, meaning
        exactly one $5 charge ever landed despite several attempts.
        **This breaks repeat top-ups against a single mandate**, which the demo narrative assumes.
        Options: mint a fresh mandate per top-up (which is what §4/§5B's original "one-time scoped
        card" wording actually described), keep a pool of pre-approved mandates and consume one per
        save, or demo a single save. **Unresolved — needs a decision before the demo.**
    *   ⚠ **`remaining`, not `approvedAmount`, is the enforced cap.** `chargeable_mandate` now
        skips any mandate whose cycle is spent, detected as `remaining < approvedAmount` —
        **not** `lastCharge`, which reports the most recent *attempt* and can read `declined`
        while an earlier charge already consumed the cycle.
    *   ✅ **`PRAVA_LIVE_MODE` parsing fixed 2026-08-01 (Shubh, repo audit) — adopted here.**
        `prava.py` was reading all four `PRAVA_*` variables itself, duplicating
        `treasury/config.py` and comparing with an exact `== "True"`, so `true` / `1` / `yes`
        silently **simulated** while looking live. It now reads `config`, parsed leniently like
        every other boolean. **If your `.env` says `PRAVA_LIVE_MODE=true` and you were relying on
        it quietly simulating, it will now transact.**
    *   **Rate limits — researched 2026-08-01 (Shubh): not documented anywhere.** No RPM/RPS
        figures in any Prava doc; the only documented throttle is `429 TRIES_EXHAUSTED` on
        `POST /v1/sessions`, with no `Retry-After`. Prava's own docs use a **3s poll cadence** for
        session credentials — good precedent for a demo-box `TREASURER_INTERVAL_S=3`.
    *   🛑 **PRAVA SANDBOX FAILURE, 2026-08-01 ~12:40 UTC onward — blocks the live demo.**
        Credential minting stopped working: mandates approve fine and show `active` with full
        headroom, but every charge returns `Visa 400 — Fetching cryptogram failed` and sessions
        stall at `processing` with `token: null`. Reproduced on 6 mandates, two customers, $50 and
        $500, with and without `max_charges`, in normal and incognito browsers, and by re-running
        `create_mandate.py` **unmodified** — the exact request that succeeded at 05:35 UTC. It also
        fails inside Prava's own hosted checkout ("identity verified, but we couldn't complete the
        payment"), so it is not our integration. Reported to the organizers with response-ids.
        `GET /v1/mandates` separately returned `500 DB_INFRASTRUCTURE_ERROR` for ~30 minutes and
        recovered. **Demo runs on `PRAVA_LIVE_MODE=False` until this clears.**

*   **Dashboard: LAYOUT + LIVE LOGS WORKING, RESTYLED.** `dashboard/` — Next.js (App Router) +
    Tailwind, `npm run dev` from `dashboard/`. Reads the ledger directly and read-only via `pg`
    (`dashboard/src/lib/db.ts`); under MVCC readers never block the proxy's writer, which is the
    same guarantee WAL used to buy. **Needs its own `DATABASE_URL`** in `dashboard/.env.local`
    (template at `dashboard/.env.example`) — it needed no configuration at all when it opened a
    file, and without one every card renders its empty state.
    *   **Ported to Postgres 2026-08-02 (Shivam).** Every exported function in `lib/db.ts` is now
        async — node-postgres has no synchronous API — and `page.tsx` awaits its ten reads through
        one `Promise.all` rather than serially, because each is a network round trip now. Two
        things needed solving rather than translating: `ORDER BY rowid` (no Postgres equivalent;
        `replace_budgets` stamps an explicit `sort_order`, read behind a `columnExists` guard), and
        aggregates arriving as **strings** — Postgres returns `SUM()` as `numeric` and `COUNT()` as
        `bigint`, and the driver hands both back as strings because neither fits a JS number
        exactly, so `.toFixed()` throws and `>` compares lexically. Every aggregate goes through
        `num()` on the way out.
    *   **Visual system (2026-08-01):** a "mission-control darkroom" — pitch-black canvas, a
        five-level surface stack (obsidian → carbon → graphite → iron → steel), elevation by inset
        white hairline rather than shadow, and one accent blue reserved for the active nav
        indicator and the live-polling dot. Type is Inter with a negative tracking ladder
        (-0.064em at 86px down to -0.005em at 12px). Tokens and the type ladder live in
        `dashboard/src/app/globals.css`; shared pieces in `dashboard/src/components/ui/`.
    *   **Restyled again 2026-08-01 (Tanay) onto a supplied "mission control" design — the visual
        system below this entry describes the superseded darkroom.** Canvas `#05050a`, glass panels
        (`rgba(12,12,24,0.75)` + `blur(16px)`) so the background reads through them, indigo `#6366f1`
        accent, Inter with weight-based hierarchy. Tokens in `dashboard/src/app/globals.css`.
        *   **The background is four fixed layers**, not a colour: drifting mesh orbs (22/25/28/30s
            so they never come back into phase), a line grid **masked by a radial ellipse**, SVG
            noise at 0.03, and a vignette. The mask is the load-bearing part — without it the grid
            reads as graph paper. All four are `pointer-events: none` and mount once in the root
            layout, so navigation does not reset the orbs' drift. Honours
            `prefers-reduced-motion`.
        *   **Team Budget is now a card grid, not a table** — one colour-gradient card per enforced
            scope, with a progress fill, a pulsing badge above 70%, and an avatar stack. The
            avatars are **real attribution**: distinct `actor`s who spent against that scope,
            ordered by spend, from the ledger.
        *   **The nav's breaker pill reads `breaker_events`**, not a hardcoded "Normal". A status
            pill that can only ever say "fine" is worse than no pill, because it reads as a check
            that ran and passed. Revoke outranks throttle when both are open.
        *   ⚠ **Two contrast failures in the supplied spec were corrected, and both should stay
            corrected.** (1) Its status green `#4ade80` sits at **ΔE 7.3** from the amber under
            protanopia — inside the 6–8 band that needs a secondary encoding. Green-vs-amber here
            is "safe" against "nearly out of budget", the one distinction a spend dashboard cannot
            lose, so the ramp keeps `#34d399` (ΔE 10.6). (2) Its `--text-tertiary: #555570`
            measures **2.74:1** on the glass panel, and that token carries the second line of every
            two-line row, the column headers and the footnotes — raised to `#7a7a9a` (4.76:1).
            Budget-card alpha levels were raised the same way (white/35 measured 3.1:1). Every
            badge fill/text pair now runs 5.43–10.34:1.
    *   **Tables were restyled 2026-08-01 (Tanay) to a supplied reference, and they deliberately
        break two of the rules above.** The rest of the system takes authority from size and
        negative tracking at weight 400; a data table has neither room for size nor a single
        focal point, so its hierarchy comes from **weight** — a 620 primary over a 400 muted
        secondary, on a two-line row. And tables are **sans, not the IBM Plex Mono readout
        register**: `tabular-nums` holds a money column in alignment, which is the only job the
        mono was actually doing there, without making every cell look like an instrument label.
        Mono is no longer used anywhere in the dashboard. Section labels went from uppercase mono
        micro-labels to sentence-case sans for the same reason.
        *   **The two-line row retires columns rather than adding decoration.** Model rides under
            the actor, `project · feature` under the member, trace/request counts under the
            outcome, staleness under the provider, `spend of ceiling` under the budget scope —
            each was a column of its own and each reads as a qualifier of the thing above it.
        *   **All five tables render through one `DataTable` primitive**
            (`components/ui/DataTable.tsx`) so they cannot drift apart, with `IdentityCell` for
            the two-line pattern.
        *   **Rows collapse above a threshold of 8.** The reference row is tall, and Live Logs
            fetches 50, which would otherwise be most of the page. Below the threshold no control
            renders at all — a "Show all" button over three rows is furniture. The control names
            what is hidden ("Show all 28 · 20 more"), and **footnote counts span every fetched
            row, not the visible ones**: a number that changed when you expanded the table would
            look unreliable at the exact moment someone is reading it.
        *   **The nav floats.** A translucent rounded bar inset from every edge, pinned to the
            viewport with content scrolling beneath — 10% *white* over a 6px blur, not a dark
            scrim, because a dark scrim on a near-black page reads as a hole cut in the canvas
            rather than glass lying on it. ⚠ It is `fixed`, **not `sticky`**: the bar was already
            `sticky top-0`, but the layout nests it inside `body.flex` > `div.flex-1` and it had
            quietly stopped pinning there. A sticky element that has stopped sticking looks
            identical to a working one until you scroll, which is how it went unnoticed — if you
            move the nav, re-check it by actually scrolling. A spacer replaces the height it no
            longer occupies. Of the two translucency knobs, raise alpha rather than blur: blur is
            what destroys the legibility of whatever is passing underneath.
        *   **Badges became filled tints** (plus outline and inverted variants) instead of colored
            text on a flat surface. Every text/fill pair is **measured, not judged** — the earlier
            tinted attempt put a hue on its own 20% tint and landed at 3.49:1, under the 4.5:1
            floor for small text. The shipped pairs run 5.81:1 (bad) to 18.42:1 (outline).
    *   **Status colors are a deliberate exception to that monochrome system** and should survive
        any future restyle. Green/amber/red for 2xx / 429 / 5xx, because catching a failure by
        color beats reading a number on a dashboard watched during a live demo. The specific hex
        values were chosen against the colorblind case, not by eye: the obvious green/amber pair
        collapses to ΔE 5.1 under protanopia. The shipped ramp clears ΔE 10.6, stays separable
        from the accent blue, and all three clear AA on the badge surface. Reasoning is kept in a
        comment beside the tokens.
    *   "Team Spend" table (grouped by project/actor/feature from `requests`), with a neutral
        proportion bar per row — share-of-total is what that table answers and length reads faster
        than a column of figures.
    *   "Provider Balances" card — **now reading the real `wallets` table** (`treasury/db.py`,
        same database). Ordering mirrors `treasury.db.list_wallets()` so the card and
        `GET /wallets` cannot disagree. Each row shows how stale the balance is, because
        "$4.00" and "$4.00, three hours ago" call for different reactions. The project name is
        shown only when more than one project has wallets, so it stays out of the way in the
        single-project demo. Seed with `POST /wallets/seed` (defaults to `$4.00`, the
        demo's "too low" state). The old `dashboard/src/lib/wallets.ts` placeholder is deleted.
    *   Hero: total metered spend at display size, with floating readout pills carrying live
        ledger counts. It is the one number the product exists to answer, and a single figure is
        a stat rather than a chart.
    *   **"Team Budget" card — the §5D gap, now closed** (2026-08-01, unblocked by Shubh's
        Phase 2 ceilings). One meter per enforced scope: the project ceiling from
        `projects.ceiling_usd_day`, then its features from `feature_budgets`, showing spend,
        ceiling, percent used and headroom. It exists at feature granularity because a project
        can sit at 31% while one feature is at 98% — and it is the feature that will 429, which
        is why the refusal names a scope in `X-Meter-Budget-Scope` at all.
        *   **Labelled "rolling 24h", not "today".** The column is `ceiling_usd_day` but
            `proxy/budget.py` compares against `now - BUDGET_WINDOW_S`, so a calendar-day label
            would disagree with the 429 a developer just read. The window is read from
            `BUDGET_WINDOW_S` for the same reason, rather than hard-coded.
        *   **Scopes render in `meter.yaml` order and are never re-sorted**, including by
            utilisation — rows must not move under someone watching them during the demo. That
            order is `ORDER BY rowid`, which is file order because `replace_budgets()` clears
            and re-inserts both tables in file order at every boot.
        *   **Settled spend only.** The proxy authorises against settled + in-flight holds, but
            holds live in its process memory and never reach the database by design, so the card can
            read a shade under what is being enforced during a burst. Footnoted on the card
            rather than hidden.
        *   Both spend queries mirror `db.project_window_spend()` / `db.window_spend()`
            verbatim, including the `iso_seconds_ago` cutoff *format* — `ts` is TEXT compared
            lexicographically, and JS `toISOString()` emits 3 fractional digits and a `Z`, so a
            stored `...123456+00:00` sorts below a `...123Z` cutoff and rows inside the window
            silently vanish.
        *   No ceilings configured returns `null`, not an empty list, so the card says "no
            ceilings configured" instead of rendering `$0.00 of $0.00` — which reads as
            catastrophically over budget when it means the opposite.
        *   **Verified against a running proxy** (2026-08-01): with a four-ceiling `meter.yaml`,
            the card's figures matched `/healthz .budget` exactly, and a request tagged with the
            98%-full feature was refused `429 X-Meter-Budget-Scope: feature:demo-project/batch-eval`,
            `ceiling 0.50`, `spend 0.490000` — the same numbers the card showed. Requests on the
            two scopes with headroom passed the budget gate and reached the provider.
    *   "Live Logs" table (`User | Model | Predicted Cost | Actual Cost | Status`), polling
        `GET /api/live-logs` every 3s. **Predicted Cost now reads the real column** (wired
        2026-08-01 once Shubh's predictor integration landed). It stays blank for Claude
        models, which have no local tokenizer — that is a real state to render, not a missing
        feature, and the table's footnote says so. The query degrades to `NULL` when the column
        is absent, so a database whose proxy has not been restarted since the migration still
        renders instead of throwing. Verified end-to-end against a seeded schema: a row
        inserted mid-session shows up on the next poll with no restart.
    *   **"Treasurer Agent" panel — the autonomous loop, on screen** (2026-08-01, unblocked by
        Shivam's Treasurer landing). A monospace terminal reading `treasury_events` joined to
        `wallets`, polling `GET /api/treasury-events` every 3s, oldest-first and pinned to the
        newest line.
        *   **`treasury_events` is a lifecycle row, not a log stream** — one row per top-up
            attempt, moving `pending` → `settled`/`dry_run`/`refused`/`failed`. Each row is
            rendered as two or three lines (detection from `decision_inputs`, the request, the
            outcome), **every one derived from a recorded field**. The design reference drove
            this panel from a `setInterval` inventing messages like "Scanning burn rates…";
            nothing in the ledger corresponds to those, because a tick that decides not to act
            writes no row at all. Inventing them would make a real autonomous agent look like an
            animation, so the panel stays quiet instead and its empty state says why.
        *   ⚠ **`dry_run` must never render as success, and this is the default.**
            `TREASURER_DRY_RUN` ships `true`, so a rehearsal writes `status='dry_run'` — the
            reference's wording ("✓ Top-up successful") would claim a payment that never
            happened, in front of the people judging whether the payment works. It reads
            "Dry run — rehearsed $X, no money moved" in the accent colour, never the success
            green. **Keep that distinction if this panel is ever restyled.**
        *   Timestamps are **local wall-clock**, not the ledger's UTC. Slicing the ISO string is
            the obvious implementation and puts a clock on screen hours off the viewer's own,
            which on a live panel reads as stale data. Server and client therefore disagree by
            timezone, which is what the `suppressHydrationWarning` on that span is for.
        *   **Verified against a real loop** (2026-08-01): `TREASURER_ENABLED=true`,
            `TREASURER_INTERVAL_S=5`, a wallet seeded at $4.00 under the $10 floor and a local
            monthly mandate. The loop fired on the **floor** trigger, wrote real `dry_run`
            events, and the panel rendered them with the clock matching the wall clock to five
            seconds. `TREASURER_DRY_RUN` was left at its default throughout — the settled path
            has not been exercised from the dashboard side.
    *   **"Cost per Outcome" table — the margin metric, now on screen** (2026-08-01, unblocked
        by Shubh's `POST /v1/annotate`). Grouped by `outcome`:
        `Outcome | Traces | Requests | Cost | Per trace | Value | Margin`. This is the
        `requests × annotations` join ARCHITECTURE.md §4 calls the difference between a cost
        tool and a margin tool.
        *   ⚠ **The obvious query for this is wrong, and wrong in the dangerous direction.**
            `annotations` is append-only — `record_annotation` says outright that a trace can be
            annotated twice — so `requests JOIN annotations ON trace_id` **fans out**: a trace
            with 12 requests and 2 annotations yields 24 rows and reports double the cost.
            Measured on the verification fixture, the naive join overstated `resolved` cost by
            **1.75x** ($0.70 against a true $0.40). On a margin metric that turns a loss into a
            profit on screen. `dashboard/src/lib/db.ts` therefore rolls `requests` up to one row
            per trace **before** anything joins to it, and collapses annotations to one row per
            trace, so the join is strictly 1:1. **Anyone writing this query elsewhere — a
            Treasurer report, a pitch number — has to do the same.**
        *   **A trace annotated twice takes its latest annotation, not the sum** (`MAX(id)`).
            A re-annotation is usually a correction — a ticket reopened, then resolved again —
            and summing double-counts it. Isolated to one CTE if that call is ever reversed.
        *   **Margin is measured against only the traces that carry a `value_usd`.** Comparing
            annotated revenue with the group's whole cost would report a loss that is an
            artefact of incomplete annotation. Rows where only some traces are valued are
            marked; a trace with no value shows margin `—`, never `0`, because "broke even" and
            "unknown" are different facts.
        *   **Coverage is stated, not assumed.** The card reports annotated traces against all
            traced traces and the share of traced spend they represent. "$0.20 per resolved
            ticket" from 3% of traffic looks identical to one from 95% unless coverage sits
            beside it. Annotations naming a trace with no metered requests (a mistyped
            `trace_id`) are excluded from the join and counted separately.
        *   **Verified against a running proxy**, annotating through the real endpoint: a trace
            annotated twice did not double its cost, `resolved` totalled $0.4000 across 2 traces
            / 4 requests exactly matching `SUM(cost_usd)`, and the orphan annotation was
            excluded and reported.
    *   **Every query guards on the table existing, not just the connection.** The schema can be
        created by either side — `treasury/db.py` makes only the treasury tables, so running any
        treasury script before the proxy left a database that connected fine but had no
        `requests` table, and the whole page 500'd. Each read checks `information_schema` first
        and degrades to an empty state per card, so a half-built database shows what it has
        instead of nothing.
        *   **The column guard is the same rule one level down, and it is not theoretical.**
            Pointing the page at a schema seeded before the `sort_order` migration 500'd it —
            `ORDER BY` a missing column throws rather than being ignored. The prediction columns
            and `sort_order` are all read behind `columnExists`.
    *   Not yet done: **Model Efficiency view only** (Phase 3, needs Ammar's cross-model data —
        B11, and that data does not exist: nothing routes one prompt to two providers and there is
        no `model_efficiency` table). The Agent Activity panel that used to sit on this line
        **shipped** — see the "Treasurer Agent" panel entry above.
    *   **PLANNED, NOT STARTED — a marketing homepage in front of the dashboard** (Tanay,
        decided 2026-08-01). A flashy hero, a concise product overview, a "how to use it"
        section, and an entry point into the dashboard. **Deliberately a different character
        from the dashboard**: the homepage is creative and expressive, the dashboard stays
        simple, productive and professional — an operations screen someone watches while
        production is live is not the place to be impressive. Awaiting design direction; no
        code written and nothing designed yet. Three consequences worth settling before it
        starts, recorded here so they are decided rather than discovered:
        *   **The dashboard currently owns `/`.** A homepage there moves it — `/dashboard` is
            the obvious target. Small change, but it touches the nav and any shared link.
        *   **Tokens should stay shared even though the characters differ.** Same canvas,
            accent and type, varying only density and motion budget, or the two halves stop
            reading as one product. A genuinely separate look for the homepage is a legitimate
            choice, but it is a bigger one and needs saying explicitly.
        *   **The animated background is mounted in the root layout**, so today it renders on
            both. It likely belongs at full strength on the homepage and dialled down or absent
            on the dashboard — drifting orbs behind live financial numbers work against the
            "professional" half of this split.
        *   The "how to use it" section can be **accurate rather than illustrative**: Meter
            keys, `POST /v1/chat/completions`, `meter.yaml` ceilings and `POST /v1/annotate` all
            work and are verified, which is unusual for a hackathon landing page.

    *   **`/how-it-works` — the predictor explainer: SHIPPED** (Shubh, 2026-08-03; rebuilt against
        the real engine 2026-08-03).
        `dashboard/src/app/(predictor)/` + `dashboard/src/components/predictor/PredictorPage.tsx`,
        reached from a pill in the homepage nav **and from the dashboard `TopNav`** (added so a judge
        already in the console does not have to go back to the homepage to find the argument).
        A scroll-driven walkthrough of *why* pre-flight
        cost prediction is the novel part — the argument the homepage does not have room for.
        *   **Two bugs fixed on 2026-08-03, both of which had shipped to `main`.**
        *   **(1) Everything below the hero was invisible.** `gsap.from` stamped `opacity: 0` onto
            all 48 `.reveal` elements and relied on GSAP **ScrollTrigger** to bring them back;
            ScrollTrigger never fired — verified dead by dispatching both `resize` and `scroll` at it
            and watching nothing change — so seven of eight sections rendered blank at every scroll
            position. Replaced with `IntersectionObserver`, which has no measurement cache to
            invalidate and so cannot be desynchronised by a webfont reflow. **The hidden state now
            lives in CSS behind `.px-js`, a class the component adds only as it wires the observer**,
            so any future script failure renders the page fully visible rather than blank. ScrollTrigger
            is gone; `gsap` is still used for the readout count-up and `lenis` for smooth scroll.
        *   **(2) Every number on the page was wrong**, because it was written from
            `predictor/DESIGN.md` rather than from the engine. **`data/fitted.json` overrides
            `ScopeConfig` at `Predictor()` construction**, so the shipped constants are the fitted
            ones, not the hand-written defaults: `base_scope` **80** not 150, `task_code` **0** not
            500, `tokens_per_sentence` **35** not 25, `high_intensity` **1.2** not 1.5, `cot` **1.0**
            (the optimizer drove it to neutral). The page also still showed a **1.30 safety buffer on
            the forecast**, which was removed precisely because it double-corrects against the history
            factor, and **`SHRINK_K` 20** when it is 1 and geometric. It was missing the bucket-factor
            stage and the `imperative` multiplier entirely.
        *   **Rule for anyone editing this page: read the numbers out of the engine, not out of
            DESIGN.md.** The worked example was generated by running `predict()` on that exact prompt
            (`"Summarize this incident and write the SQL query that finds every affected account."`
            → bucket `code`, scope 330 × 1.2 × 1.5 × 1.2 = 712.8, × 4.031 bucket factor = **2,873**
            predicted, bound 4,096, $0.028770). Accuracy figures come from §6a above, with the traffic
            shape stated on every row as §6a requires.
        *   Added since the first version: a **request-lifecycle diagram** placing the engine at
            ARCHITECTURE.md §2 step 3 and showing the refit loop running off the request path, the
            **whole output-token formula** as one annotated block, and the **measured accuracy table**
            including the open-ended 49.2% row we are worst at.
        *   **`predictor/DESIGN.md` is now stale in the places listed above.** Not edited here: it is
            Ammar's spec and the divergence is a real decision record, not a typo. Worth reconciling.
        **Its own route group with its own root layout and `predictor.css`**, so no stylesheet
        crosses into the homepage or the dashboard; the nav link is a full page load for the
        same reason the `/dashboard` link is. Adds two dependencies used nowhere else, `gsap`
        and `lenis`; both animation paths are skipped under `prefers-reduced-motion`. The page
        is static-prerendered and reads no data — nothing here touches the ledger.

*   **Alerts (Poke / Linq): WORKING, VERIFIED LIVE.** A real iMessage was delivered end to end
    through the shipping code path on 2026-08-01 (Linq returned `202 Accepted`). `alerts/` — a sibling
    package to `treasury` and `predictor`. `proxy.breaker.notify()` still logs unconditionally
    (that line is the record of record) and then hands off to `alerts.send_breaker_alert`.
    *   `POST /v3/messages` on the Linq Partner API, which resolves the sending line and chat
        itself — we own no provisioned number, and picking one would be guessing.
    *   **Dispatched on a daemon thread, never awaited.** `notify()` runs inside the request path,
        so an inline HTTP call would put a third party's latency in front of production traffic.
        It also swallows every exception: an alerting failure must not become a request failure.
    *   **Per-scope cooldown, default 300s** (`POKE_COOLDOWN_S`). A breaker half-opens and
        re-trips while a burst continues; without a floor, one runaway feature texts somebody
        every few seconds, which is how an alerting channel gets muted for good.
    *   Silent without credentials, so every teammate's machine stays quiet. `POKE_ENABLED` is a
        separate kill switch. Destination must be E.164 — validated at config time, because Linq
        rejects anything else with error 1002 and it would otherwise surface as an unsent alert
        mid-incident.
    *   `python tests/test_alerts.py` — 46 checks covering payload shape, the configuration gate,
        cooldown, failure isolation, and that a 1.5s send returns to the caller in under 0.25s.
        **Needs Python 3.10+** (`proxy/breaker.py` uses `dataclass(slots=True)`); macOS system
        python is 3.9 and will fail on the import, not on the logic.
    *   **Destination validation is NANP-aware, not just E.164.** A US number one digit short
        (`+1217213007`) still sits inside E.164's generic 8-15 range, so the generic rule passed a
        real typo through to a live send attempt. `+1` now requires exactly 10 national digits;
        other country codes keep the generic rule.
    *   **Wording: Linq is not "Poke's former name".** Poke (The Interaction Company of
        California) is Linq's flagship *customer*; Linq is the iMessage infrastructure provider
        (`linqapp.com`). Our alert calls `POST /v3/messages` on the Linq Partner API, which
        resolves the sending line itself — the docs' recommended pattern. The API is current
        (V3; V2 is legacy), and error `1002` is confirmed as an E.164 *format* check — our
        NANP-aware validation is a strict subset of what Linq validates.
    *   ⚠ **Sandbox gotcha, verify before demo day (error `2008`): in sandbox, recipients
        must message the sending line first.** If `.env` uses a sandbox token, the CTO's phone
        may need to text the line once before breaker alerts deliver — otherwise the demo's
        iMessage beat silently fails. Confirmed against `docs.linqapp.com` (error reference).
    *   **Rate limits we are nowhere near:** 30 msgs/60s per sender–recipient pair, and a
        sandbox cap of 100 msgs/day — our 300s `POKE_COOLDOWN_S` caps us at ~12/hour. Nothing
        to change; known so nobody "optimises" the cooldown down.
    *   **One-line improvement, not yet done:** log the `X-Trace-ID` Linq returns on success —
        currently only logged inside failure bodies.
    *   **Verified end to end against a running proxy** (2026-08-01), not just from a direct call.
        Seeded $25.20 into a 5-minute window against a $20 floor at a 12x burst, sent one tagged
        request, and got: `429` with `X-Meter-Breaker-Scope`/`-Mode` and `Retry-After`, the trip
        logged with the numbers it compared, and `poke alert sent (HTTP 202)`. Three properties
        confirmed in the same run — a second request to the open scope returned `429` **without**
        a duplicate text, and `chat` plus untagged traffic kept flowing while only `batch-eval`
        was throttled, which is the isolation claim the demo makes out loud.
    *   **The trip costs nothing to rehearse.** The breaker rejects before FORWARD, so a tripped
        request never reaches the provider — seed the ledger, send one request, and the whole path
        exercises with no upstream call and no provider key.
    *   `GET /v3/phone_numbers` is a zero-side-effect way to check a token — use it before
        sending anything, so credential problems never get confused with integration problems.

*   **Resolved since kickoff:**
    1.  ✅ **Pricing is verified** against Anthropic's and OpenAI's published rate cards (2026-08-01; Sonnet 5 rechecked 2026-09-22). The first draft was written from memory and was wrong in both directions. Anthropic cancelled the scheduled Sonnet 5 increase: its $2/$10 per-MTok rate is now standard, so `pricing/2026-08-01.yaml` remains the active version and must not be edited. A new dated pricing file is still required for an actual rate change. (`PROPOSALS.md` C1)
    2.  ✅ **Redis: not in the 48-hour build — Shubh, Phase 2. SHIPPED.** Reservations are built and in-process (`proxy/budget.py`). Redis is not what makes authorize/capture correct; serialization is, and with one proxy process an `asyncio.Lock` is an identical guarantee for none of the operational cost. Redis becomes load-bearing at proxy replica #2. (`PROPOSALS.md` A5)
    3.  ✅ **Budget enforcement is now owned — Shubh, Phase 2. SHIPPED.** `meter.yaml` loader plus a pre-flight ceiling check in the request path, project-level and per-feature. (`PROPOSALS.md` B7)
    4.  ✅ **`meter.yaml` vs. the database as source of truth — resolved by the loader's direction of travel.** The file is authoritative; it is projected into `projects`/`feature_budgets` at boot and nothing at runtime writes back, so the request path still reads a table without the file ever being second-hand. (`PROPOSALS.md` A6)
    5.  ✅ **`POST /v1/annotate` shipped and ratified — Shubh, 2026-08-01.** Was owned by nobody despite being documented in both `README.md` and `ARCHITECTURE.md`; built, then kept on review because what was missing was an owner rather than a decision. Its one schema deviation — a `project_id` on `annotations`, which §4 does not list — was **explicitly approved on security grounds**: without it any key could annotate, or read the cost of, another project's traces. (`PROPOSALS.md` B9)
    6.  ✅ **Feature-ceiling validation rule corrected — Shubh, 2026-08-01.** `ARCHITECTURE.md` §4 required rejecting a project whose feature ceilings *sum* past its own; that rule was a mis-restatement of the prior art it cited and inverted the failure mode. §4 now carries a per-feature rule plus a warn-only sum check, and a third rule the original omitted: the loader must replace rather than upsert, or a ceiling deleted from `meter.yaml` keeps being enforced. (`PROPOSALS.md` B17)

*   **Open blockers/decisions:**
    1.  ✅ **`docker compose up` shipped — Shubh, 2026-08-01.** Single-service `Dockerfile` (python:3.12-slim, uvicorn) + `compose.yaml` (port 8080, `env_file: .env`, read-only mounts for `meter.yaml` and `pricing/`). **The named volume is gone as of the Postgres port (2026-08-02)** — the image holds no state at all now and `DATABASE_URL` comes in from the environment, which is also what makes it deployable to Fly.io, where a container has no durable local disk. The service will not start without it, by design. (`PROPOSALS.md` B10)
    2.  **The Visa VIC track has no architectural surface.** Tanay's Phase 0 confirmed the test-card requirements are covered by `docs/prava/api-reference/test-cards.md`, so the *docs* gap is closed — but nothing in `ARCHITECTURE.md` or the build actually targets VIC. We are still entered in a track no component is designed for. (`PROPOSALS.md` B14)
    3.  ✅ **Both providers are funded and verified live end to end** (moved here from blockers, 2026-08-01). Real completions and real streams through the proxy on OpenAI *and* Anthropic, all rows priced to the published rates exactly, cross-provider routing landing both in one ledger. The REAL LLM CALLS item in §3 is fully unblocked — `predictor/calibrate.py` and the Phase 3 cross-model comparison have both providers to run against. **Rotate all three keys** (2 Anthropic, 1 OpenAI) — they were shared over chat; the live ones exist only in the gitignored `.env`. (`PROPOSALS.md` C4)
    4.  **Cross-model routing is specified two ways** — §5A says the *proxy* sends the same prompt to both providers; PLAN.md Phase 3 has it as an offline script. The script is right: shadow-calling a second provider on live traffic doubles the customer's bill inside a cost-control tool. Left as a proposal pending Ammar. (`PROPOSALS.md` B11)
    5.  **Prava rate limits are undocumented (researched 2026-08-01)** — no RPM/RPS anywhere in `docs.prava.space`; only `429 TRIES_EXHAUSTED` on `POST /v1/sessions` exists. C3 stays open: ask `support@prava.space` or measure empirically. Prava's own docs recommend a 3s poll cadence, which supports the demo-box `TREASURER_INTERVAL_S=3`. (`PROPOSALS.md` C3)
    6.  **Linq sandbox rule to verify before demo day** — in sandbox, recipients must message the sending line first (error `2008`), or breaker alerts silently fail to deliver. (`PROPOSALS.md` C5)

*   **`PROPOSALS.md`** collects 33 items from full architecture and external research reads — contradictions between the three source-of-truth docs, gaps they leave undefined, and verification tasks. Most are now closed; the rest still need decisions. **`README.md` and `ARCHITECTURE.md` remain unedited** — proposals get approved there, not applied silently.

---

## 6b. Decision Record

### 2026-08-01 — Evaluated and rejected PreflightLLMCost as a dependency (Ammar)

We evaluated [PreflightLLMCost](https://github.com/aatakansalar/PreflightLLMCost) (MIT) as a
ready-made predictive engine, to avoid rebuilding heuristics from scratch. **Decision: do not
depend on it. Adopt its bucket taxonomy and starting ratios as cold-start priors; write the engine
ourselves.**

Found by cloning, instrumenting, and running it — not by reading the README:

*   Predictions were **non-deterministic** — Gaussian noise multiplied into every estimate, giving a ~70% spread on an identical prompt. Disqualifying for a reservation, which must be reproducible.
*   Its **"Tier 3 hidden-state analysis"** performs no LLM call. It is a formula over prompt *string length*, and scores *"write a 10,000 word novel"* **below** *"reply with exactly one word"*. Off by default, and labelled `# Simulate more sophisticated LLM call` in the source.
*   Its **learning loop was never connected**: `store_actual_result()` is defined but called from nowhere, so its history table stays empty and its regression tier is unreachable in practice.
*   Its regression **does work** when given data (recovers a known law at 0.6% MAPE) — real code, just unreachable. Credit where due.
*   The README's *"≤15% MAPE ✅ Achieved"* has **no benchmark, dataset, or evaluation script** anywhere in the repository.

Why we still didn't take the code: only **~35 of its 1,726 lines** survive the fixes, and that
surviving part is a lookup table rather than logic. Its public API is a batch template/CI
forecaster, not a per-request in-path predictor, so the entry point needed rewriting regardless.
It also pulls **scipy (99MB) + pandas (72MB)**, neither of which we need — `numpy.linalg.lstsq`
replaces its entire use of scipy, exactly, faster, and deterministically. And shipping code
containing fabricated academic citations into a judged repository is a credibility risk we do not
need to take.

**Not wasted effort:** the evaluation independently validated our architecture — they converged on
the same priors-then-learned design. We skipped their execution, not their reasoning. Their priors
are credited in `predictor/buckets.py`.

### 2026-08-01 — Prediction is OpenAI-only; cross-model analysis is *not* blocked by it (Ammar)

`tiktoken` is exact for OpenAI and **wrong for Anthropic** (different tokenizer). `predictor/
tokenizer.py` therefore **raises** on Claude rather than approximating with `cl100k_base`, which is
what the reference did — that returns a confident-looking number roughly 10-20% off with nothing to
signal it. A visibly wrong answer beats a quietly wrong one in a component that gates spend. For
exact Claude counts, Anthropic's `/v1/messages/count_tokens` endpoint is free.

**Consequence worth being explicit about: cross-model analysis does not depend on the predictor.**
Comparing model efficiency uses *actual* usage returned by each provider, which needs no local
tokenizer — the proxy already captures this for both shapes. So log actuals for both providers from
day one; only *prediction* is OpenAI-first. This decouples Phase 2's cross-model work from the
tokenizer question entirely.

### 2026-08-01 — Prediction is deliberately biased high (Ammar)

Accuracy here is **asymmetric**. Under-predicting lets a request through that should have been
blocked, so the ceiling silently fails; over-predicting holds back budget that is released seconds
later at CAPTURE. So `SAFETY_MARGIN = 1.15` aims high rather than accurate-on-average, and
`learner.accuracy_report()` tracks `under_prediction_rate` as a first-class metric alongside MAPE.

**The predictor never affects billing** — billing prices the provider's actual usage. It only
answers "do we have room for this request?".

---

## 7. The 90-Second Demo Narrative  
1.  **The Problem (15s):** "Inference is the #2 cost for AI companies. Existing tools just show you a graph. When the balance hits zero at 3am, production dies."  
2.  **The Solution (15s):** "Meet Meter. A predictive proxy that doesn't just watch your bill—it pays it. And it analyzes token efficiency across models in real-time."  
3.  **The Predictive Prava Save (30s):** *[Live UI]* "We trigger a 3 AM batch job. Meter predicts it will cost $14.50, but the OpenAI balance is $4.00. The Treasurer Agent wakes up, calls Prava to generate a one-time sandbox card, and autonomously tops up the provider. The batch job runs flawlessly. Zero dropped requests."  
4.  **Cross-Model Analysis (15s):** *[Show UI]* "Because Meter logs actual usage, it tells you exactly which model is more token-efficient for your specific tasks—saving you money on routing decisions."  
5.  **The Safety & Poke Alert (10s):** *[Simulate leaked key]* "If a key leaks, the Circuit Breaker trips. We integrate with Poke to send an immediate iMessage to the engineering lead, and the key is killed instantly."  
6.  **The Close (5s):** "Meter is your autonomous AI treasurer. Keeping production alive, optimizing spend, and alerting you when it matters."  
