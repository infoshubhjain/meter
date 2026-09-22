# Meter — see it working

Meter is a drop-in LLM proxy. Point any OpenAI SDK at it and every call is metered,
attributed to a feature and a person, **cost-predicted before it runs**, and written to a
shared ledger. A runaway feature gets throttled and an engineer gets an iMessage. When the
provider balance runs short, an agent holding a Prava mandate tops it up.

It is deployed and running. **You do not need to clone or install anything.**

| | |
| --- | --- |
| **Dashboard** | https://meter-three-beta.vercel.app |
| **Proxy API** | https://meter-proxy.onrender.com |
| **Meter key** | `mk_74e8201b8eb1cf6e98a1c29ab7b12bd0` |

## The fastest path is the console

**https://meter-three-beta.vercel.app/try** — click *Try it yourself*, give a name and an email,
and you get **your own Control Room**: the same dashboard the team uses, showing only your session.
Three templated prompts, a runaway feature throttled, an agent paying its own bill. Ten minutes,
nothing installed.

Bring your own keys and each one unlocks a step — an **OpenAI key** spends your credit instead of
ours, a **Prava merchant key** settles a real charge on *your* account so it appears in *your*
Prava dashboard, and a **Linq key plus your phone** puts the circuit-breaker alert on your device.
All three are optional; skip them and everything still runs on ours.

Everything below does the same thing from a terminal, for anyone who would rather see the wire.

---

Three paths, shortest first. **Path A takes two minutes and needs nothing but a browser.**

> ### ⏳ First request may be slow — this is expected
> The backend is on a free tier that sleeps when idle. The first request after a quiet
> spell takes **up to a minute**; everything after is ~1–2 seconds. If a command seems to
> hang, it is waking up, not broken. Wake it first and wait for the reply:
> ```bash
> curl https://meter-proxy.onrender.com/healthz
> ```

---

## Path A — look at it (2 minutes, browser only)

Open **https://meter-three-beta.vercel.app**

You are looking at a live ledger of **~1,300 real API calls**. Everything on the page is
queried from Postgres; nothing is mocked.

| panel | what it is showing |
| --- | --- |
| **Control Room** | Every proxied call — predicted cost beside actual cost, per person, per feature |
| **Team Budget** | Spend against the ceilings declared in [`meter.yaml`](meter.yaml), rolling 24h |
| **Cost per Outcome** | `requests × annotations` joined on trace id — spend per *resolved ticket*, not per call |
| **Treasurer Agent** | The autonomous top-up loop's own log, including the runs it refused |
| **Provider Balances** | What the wallet holds now |

**The claim to check:** in Control Room, compare *Predicted* against *Actual*. Those
predictions were made **before** each call, by a model that had never seen that prompt.
Median error on tagged traffic is **~7% held-out, ~10% live**.

---

## Path B — run it yourself against the deployed proxy (10 minutes, curl only)

Still no cloning. You are calling our proxy, which uses **our** OpenAI credits — your own
API keys and rate limits are irrelevant.

### B1. Prove it is up

```bash
curl -s https://meter-proxy.onrender.com/healthz
```

Look for `"status":"ok"`, `"predictor":{"available":true`, and a positive
`"learned_factors"` count. The count grows as the shared ledger gains enough evidence for
another correction factor; it is not a deployment constant.

### B2. Send one prompt

```bash
curl -s https://meter-proxy.onrender.com/v1/chat/completions \
  -H "Authorization: Bearer mk_74e8201b8eb1cf6e98a1c29ab7b12bd0" \
  -H "X-Meter-Feature: ticket-summary" \
  -H "X-Meter-Actor: judge" \
  -H "Content-Type: application/json" \
  -d '{"model":"gpt-4o-mini","max_tokens":400,"messages":[{"role":"user","content":"Summarise this support ticket in two sentences for the on-call engineer: The notification fanout times out under load. Users report it started this morning. Logs point at the connection pool. We saw OperationalError: database is locked in the pod events."}]}'
```

An ordinary OpenAI response comes back. **Now refresh the dashboard** — your call is at the
top of Control Room within three seconds, with a predicted cost recorded *before* it ran and
the actual cost beside it.

That one observation proves authentication, attribution, pre-flight estimation, the provider
key substitution, pricing, and the ledger write.

### B3. Check the prediction across features

`X-Meter-Feature` is the only thing that changes. Swap the tag and the prompt together:

| feature tag | prompt |
| --- | --- |
| `sql-from-question` | `Write a SQL query answering: how many times did the notification fanout report 'OperationalError: database is locked' per day over the last week? Table events(ts, service, message).` |
| `changelog-entry` | `Write a one-line user-facing changelog entry for a fix to the connection pool in the notification fanout, which previously times out under load.` |
| `pr-description` | `Write a pull request description for a change to the connection pool in our Python notification fanout. The bug was that it times out under load. Include what changed and how to test it.` |
| `postmortem-timeline` | `Draft the timeline section of a postmortem for an incident where the notification fanout times out under load, caused by the connection pool, first seen as OperationalError: database is locked. Include detection, escalation, mitigation, and resolution entries.` |
| `code-review-note` | `Write a short code review comment asking the author to reconsider the connection pool in this Python notification fanout, given that it currently times out under load.` |
| `commit-message` | `Write a conventional-commit message (subject line plus one body line) for a change that fixes the connection pool in the notification fanout, which previously times out under load.` |

Expect **0–20% error** for most, and the dashboard shows each one as it lands.

**Two will look bad, and here is why before you conclude the model is broken.**
`commit-message` and `ticket-classify` produce 9–40 token answers, where being six tokens
out is a 60% error and costs **one millionth of a dollar**. Percentage error is a poor
metric at that scale; judge those two by cost. `incident-runbook` and `error-explainer`
read high for a different reason — their training data was collected under a 400-token cap,
so their learned history only holds at that cap. Both limits are documented in
[CONTEXT.md](CONTEXT.md) rather than hidden.

### B4. Confirm the circuit breaker is armed

The breaker needs **two** conditions: trailing 5-minute spend over a floor **and** that
spend rate at 3× the trailing hour's average. The second condition is what stops a
legitimately expensive feature tripping every five minutes forever.

The deployed floor is the production `$20`, which you cannot reach by hand — a call costs
$0.00004. **To watch it actually fire, run it locally** (Path C3), where the floor can be
lowered.

What you can check here is that it is armed:

```bash
curl -s https://meter-proxy.onrender.com/healthz | grep -o '"breaker":{[^}]*}'
```

`"can_fire":true` means both conditions are satisfiable with the configured windows. The
proxy shouts at boot if they are not, because a breaker that cannot fire is worse than no
breaker — everyone believes they are protected.

### B5. Watch the Treasurer decide

```bash
curl -s https://meter-proxy.onrender.com/treasury/assess
```

It reports the wallet balance, the burn rate computed from real ledger spend, projected
runway, and whether it would top up. Then run one pass:

```bash
curl -s -X POST https://meter-proxy.onrender.com/treasury/tick \
  -H "Authorization: Bearer mk_74e8201b8eb1cf6e98a1c29ab7b12bd0"
```

You will see it decide to charge **$25.00** against a real Prava mandate and stop at
`"reason":"dry_run"`. That is deliberate: `TREASURER_DRY_RUN=true` is the shipped default,
because a process that charges a card on a timer should take two explicit switches to turn
on. The attempt is recorded in `treasury_events` and appears in the dashboard's Treasurer
Agent panel.

The mandate is real — `curl https://meter-proxy.onrender.com/mandates` shows it with its
remaining headroom.

### B6. Cost per outcome

Attribute spend to a *result* rather than a request:

```bash
curl -s https://meter-proxy.onrender.com/v1/chat/completions \
  -H "Authorization: Bearer mk_74e8201b8eb1cf6e98a1c29ab7b12bd0" \
  -H "X-Meter-Feature: ticket-summary" -H "X-Meter-Actor: judge" \
  -H "X-Meter-Trace: ticket-9001" -H "Content-Type: application/json" \
  -d '{"model":"gpt-4o-mini","max_tokens":400,"messages":[{"role":"user","content":"Summarise: the auth gateway is returning 502s for EU customers since the 14:00 deploy."}]}' > /dev/null

curl -s -X POST https://meter-proxy.onrender.com/v1/annotate \
  -H "Authorization: Bearer mk_74e8201b8eb1cf6e98a1c29ab7b12bd0" \
  -H "Content-Type: application/json" \
  -d '{"trace_id":"ticket-9001","outcome":"resolved","value_usd":12.50}'
```

The response gives cost, request count and margin for that outcome. One resolved ticket is
usually a dozen calls, which is why the join is on the trace and not the request.

---

## Path C — run the whole thing locally (20 minutes)

Only needed to watch the circuit breaker fire, receive the iMessage alert, or read the code
while it runs.

### C1. Setup

```bash
git clone <repo> && cd meter
python -m venv .venv && source .venv/bin/activate      # EVERY new terminal needs the activate
pip install -r requirements.txt
cp .env.example .env
```

Edit `.env`:

```bash
OPENAI_API_KEY=sk-...                 # your own; local runs use your credits
METER_KEYS=mk_dev_local:demo-project:dev
DATABASE_URL=postgresql://postgres.<ref>:<pw>@aws-1-ap-south-1.pooler.supabase.com:5432/postgres?options=-c%20search_path%3Dpublic
```

> **Two things about `DATABASE_URL` that each cost an hour to rediscover.**
> Use the **pooler** host: `db.<ref>.supabase.co` publishes only an IPv6 record and will not
> resolve on an IPv4-only network. It is `aws-1`, not `aws-0`, and the user is
> `postgres.<project-ref>`.
> Keep the **`?options=-c%20search_path%3Dpublic`** suffix. The poolers reuse backend
> connections without resetting session state, so without it a connection can inherit a
> stale `search_path` and fail with `relation "requests" does not exist` — intermittently,
> which is worse than consistently. Measured: 0/6 connections worked without it, 6/6 with.

```bash
python -m uvicorn proxy.app:app --port 8080      # terminal 1
cd dashboard && npm install && npm run dev       # terminal 2 (needs its own DATABASE_URL on :6543)
```

**Check:** `curl localhost:8080/healthz` reports a positive `learned_factors` count. If it
reports `0`, run `python scripts/seed_demo.py` and restart — the predictor has no history
and every estimate will be the raw heuristic, roughly 80% error instead of 10%.

> `No module named uvicorn` means the virtualenv is not active. A new terminal does not
> inherit it. Either `source .venv/bin/activate` first, or call
> `.venv/bin/python -m uvicorn ...` directly.

### C2. One prompt, with the prediction shown

```bash
./scripts/try.sh ticket-summary "Summarise this support ticket in two sentences for the on-call engineer: The notification fanout times out under load. Users report it started this morning. Logs point at the connection pool. We saw OperationalError: database is locked in the pod events."
```

Prints predicted vs actual tokens, predicted vs actual cost, and the learned correction
factor. **`history factor 1.00` means no learned history for that tag** — the prediction is
uncorrected and will be much worse.

The same script works against the deployed proxy:

```bash
METER_URL=https://meter-proxy.onrender.com \
METER_KEY=mk_74e8201b8eb1cf6e98a1c29ab7b12bd0 \
./scripts/try.sh ticket-summary "…"
```

### C3. The circuit breaker, and the iMessage

Restart the proxy with a demo-scale floor — a real call costs $0.00004, so the production
`$20` is unreachable by hand:

```bash
BREAKER_WINDOW_USD=0.0001 python -m uvicorn proxy.app:app --port 8080
```

> A **runtime override**, not a file edit. `.env` still says `$20`, so a plain restart
> returns you to production settings.

Now run four `ticket-summary` prompts in quick succession (those in B3 work). Expect the
first two or three to succeed and the next to return **429**.

Then immediately run a **different** feature:

```bash
./scripts/try.sh commit-message "Write a conventional-commit message (subject line plus one body line) for a change that fixes the retry policy in the auth gateway, which previously leaks file descriptors."
```

**It succeeds.** That is the point: the runaway feature is cut off while everything else
keeps serving. The log shows both conditions being satisfied, not just the floor:

```
breaker TRIPPED scope=demo-project:ticket-summary mode=throttle
        spend=$0.0001/300s floor=$0.00 burst=4.54x (need 3.00x, ceiling 12x)
```

**For the iMessage** you need `POKE_API_KEY` and `POKE_CTO_PHONE` in `.env`.

> **Linq sandbox rule:** the recipient must have messaged the sending line **first**, or
> delivery fails silently with error `2008`. Text the line once before testing. Alert
> dispatch runs on a daemon thread and swallows its own errors by design — a third-party
> outage must never fail a request — so `poke alert sent (HTTP 202)` in the log is your
> only confirmation.

**Recovering:** the breaker is never permanently open. Wait 120 seconds and send another
request to that feature — it re-measures and closes itself if spend has decayed. Recovery
is **lazy**: no background job, so the database row stays marked open until a request
triggers the check. `POST /v1/breaker/reset` clears it immediately.

### C4. The automated checks

```bash
python tests/test_predictor.py     # estimator
python tests/test_proxy.py         # proxy, ledger, breaker, budget
python tests/test_treasury.py      # wallets, mandates, Treasurer
python tests/test_alerts.py        # Poke/Linq
python tests/test_judge.py         # session-scoped judge flow
python scripts/e2e_journey.py --offline   # drives the running app
ruff check .
```

All checks run without calling a provider.

`e2e_journey.py` exists because everything else tested modules in isolation, and
`GET /mandates` was returning a **500** with all 608 of them passing.

---

## Deploying your own

[`DEPLOY.md`](DEPLOY.md) is the runbook: Render for the backend, Vercel for the dashboard,
Supabase for the ledger, all free. Two things it will save you:

- **`python scripts/secure_ledger.py`** — Supabase's `anon` role arrives with full read and
  write on every table, including `TRUNCATE`. That key is designed to ship in browsers; it
  is only safe because RLS is meant to be the gate, and RLS is off by default.
- **`POST /mandates/sync`** — a mandate created at Prava is not visible to the Treasurer
  until it is claimed into the local table. Without it the agent refuses with
  `no_chargeable_mandate`, which looks like a broken integration rather than a missing step.

---

## What is *not* proven

Stated plainly, because a walkthrough that lists only successes is how a demo claims
something it has not earned.

- **A real Prava charge.** Everything up to it works and is visible in
  `/treasury/events` — mandate selection, the rails, the audit row. `TREASURER_DRY_RUN=true`
  stops it at the last step, deliberately.
- **Open-ended prompt accuracy: ~49% median.** The ~10% figure is for *tagged, repeated*
  feature traffic, which is what real product traffic looks like. Arbitrary one-off prompts
  are close to a hard ceiling — output length is only weakly predictable from prompt text
  (R² = 0.28 across 1,224 measured calls).
- **A brand-new feature tag starts at ~80% error** and needs ~20 calls of its own. Coverage
  does not transfer between features: bucket-level history made a held-out feature *worse*
  (71% → 74% median, 39% → 625% at worst). The honest claim is **"tag your features, and
  after ~20 calls each you get sub-15% cost prediction."**
- **`severity-triage` sits at ~69%** and no amount of tuning fixes it. Its untruncated
  outputs still spread 5.1×, so the model itself is inconsistent on that task.
- **One backend instance only.** `proxy/budget.py` serialises reservations with an
  in-process lock, so a second instance would mean two locks seeing the same headroom and a
  ceiling that silently stops holding. Redis would fix it; it is not built.

Every number above is reproducible from this repository —
`scripts/accuracy_report.py`, `scripts/consistency_check.py`, `scripts/shrinkage_sweep.py`.
