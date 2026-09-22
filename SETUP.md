# Meter — set it up and prove every part works

> **This is the local setup guide — run the whole stack on your own machine.**
> It is written for teammates and for anyone who wants to stand Meter up from scratch.
>
> **Judges and anyone evaluating this project want [WALKTHROUGH.md](WALKTHROUGH.md)
> instead** — that one runs against the already-deployed instance and needs nothing
> installed but `curl`.
>
> Both files existed under the same name on two branches, doing different jobs: this one
> grew from actually running the setup on a fresh Windows machine (see
> [EXPERIENCE.md](EXPERIENCE.md)), while WALKTHROUGH.md was rewritten for asynchronous
> judging. Keeping both, because losing either would cost someone hours.



This is the hands-on guide. It takes you from a fresh clone to having personally
verified every claim the product makes: interception, cost prediction, the ledger,
budget ceilings, the circuit breaker, the iMessage alert, the Treasurer, and the
dashboard.

**Who this is for, in order:** Shivam (who owns the Prava leg and is the only one who
can complete §8), then Tanay and Shubh, then anyone we hand the repo to — including
judges. Every command is copy-pasteable and every step says what you should see, so a
failure is unambiguous rather than a shrug.

**Time:** ~15 minutes for §1–§7. §8 needs a Prava mandate approved in a browser, which
is another 2–3 minutes the first time.

**Cost:** under 5 cents of real OpenAI spend for the whole walkthrough.

> ### On the OpenAI free tier? Read this first.
>
> The core walkthrough (§4, §5, §6, §9) is **17 requests and about 1,000 input
> tokens** — comfortably under half of a free-tier day, whichever limit binds.
>
> **Two things will bite you, and neither is Meter's fault:**
>
> **1. Do not run `scripts/demo_live.py --n 2` (§5, "All 32 features at once").** It is
> 64 requests and **600,000 input tokens** — several times a free-tier daily token
> allowance, because it includes the high-consumption features whose prompts are 33k–44k
> tokens each. A free-tier-safe version is given in that section.
>
> **2. The free tier allows ~3 requests per minute. Pace yourself — roughly one command
> every 20 seconds.** If you fire them back to back, OpenAI returns a `429` of its own,
> and it looks almost exactly like the circuit breaker tripping in §6. Tell them apart
> like this:
>
> | | Meter's breaker | OpenAI's rate limit |
> | --- | --- | --- |
> | where it appears | `try.sh` prints an error; ledger row has status `429` | error mentions `rate_limit_exceeded` / `Rate limit reached` |
> | proxy log | `breaker TRIPPED scope=...` | no breaker line |
> | fix | wait 120s or reset the breaker | wait 60s |

---

## 1. Prerequisites

- Python 3.12+ and Node 20+
- **A bash-capable shell.** Every command here is bash. On macOS/Linux you already have
  one. **On Windows use Git Bash** (ships with Git for Windows) or WSL — PowerShell will
  not run `./scripts/try.sh`, and it fails *silently*, with no output and no error. See
  [Windows notes](#windows-notes) before starting.
- `DATABASE_URL` for the shared Supabase ledger — ask Shivam
- An OpenAI key. **A free-tier key is fine** — see the box above; the core walkthrough is
  17 requests. Pace yourself at roughly one command every 20 seconds and skip the
  `demo_live.py --n 2` sweep in §5.

Optional, needed only for the section that uses them:

| section | needs |
| --- | --- |
| §6 iMessage alert | `POKE_API_KEY`, `POKE_CTO_PHONE` — ask Tanay |
| §8 Prava top-up | `PRAVA_API_KEY` + an approved mandate — Shivam |

### Windows notes

Everything works on Windows, but four commands differ. Skip this section on macOS/Linux.

| The guide says | On Windows |
| --- | --- |
| `./scripts/try.sh <tag> "<prompt>"` | **Run it from Git Bash.** PowerShell does not execute `.sh` and gives **no output and no error** — it looks like the product silently did nothing. |
| `curl -s ...` | `curl.exe -s ...` — in PowerShell, `curl` is an alias for `Invoke-WebRequest`, so `-s` is rejected with a confusing *"missing mandatory parameters: Uri"*. |
| `curl ... \| python3 -m json.tool` | Use `Invoke-RestMethod <url> \| ConvertTo-Json`. Piping to a native command inserts a UTF-8 BOM and `json.tool` fails on it. |
| `VAR=value python -m uvicorn ...` | `$env:VAR = "value"; python -m uvicorn ...` — **in the same command**, since `$env:` does not survive into a new shell. |
| `source .venv/bin/activate` | `.venv\Scripts\Activate.ps1`, or skip the venv entirely if your system Python already has the dependencies. |

Also: `Ctrl-C` is not available if you started the proxy in the background. Kill it by port:

```powershell
$pids = (Get-NetTCPConnection -LocalPort 8080 -State Listen).OwningProcess
Stop-Process -Id $pids -Force
```

> ⚠ **After any "restart with this setting" step, check the value actually changed** —
> not just that `/healthz` answers. If the restart fails to bind the port, the *old*
> proxy keeps serving and `/healthz` still reports `"status": "ok"` with the old
> settings. In §6 that means the breaker never trips and it looks like the feature is
> broken.

---

## 2. Setup

```bash
git clone <repo> && cd meter
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Now edit `.env`. The four that matter:

```bash
OPENAI_API_KEY=sk-...
METER_KEYS=mk_dev_local:demo-project:dev
DATABASE_URL=postgresql://postgres.<project-ref>:<pw>@aws-1-ap-south-1.pooler.supabase.com:5432/postgres
PRICING_VERSION=2026-08-01
```

> **The `DATABASE_URL` trap — read this before you lose an hour.**
> Use the **pooler** host, not the direct one. `db.<ref>.supabase.co` publishes only an
> IPv6 (`AAAA`) record, so on any IPv4-only network it fails with *"failed to resolve
> host"*. Two further details: it is **`aws-1`**, not `aws-0` (that one resolves and
> then refuses the connection), and the username is **`postgres.<project-ref>`**, not
> `postgres`.

Check the connection before going further:

```bash
python -c "
import os; from dotenv import load_dotenv; load_dotenv('.env')
import psycopg
with psycopg.connect(os.environ['DATABASE_URL'], connect_timeout=15) as c:
    print('rows:', c.execute('select count(*) from requests').fetchone()[0])"
```

**Expect:** a row count in the thousands. If it hangs or fails to resolve, re-read the
box above.

---

## 3. Start the stack

Two processes, two terminals. Both stay running for the rest of this guide.

```bash
# terminal 1 — the proxy (this is the whole backend: proxy + treasury + mock provider)
source .venv/bin/activate          # EVERY new terminal needs this
python -m uvicorn proxy.app:app --port 8080
```

> Forgetting `source .venv/bin/activate` is the most common stumble here, because a
> restart or a new tab does not inherit it and the error names the system Python rather
> than the missing step: `No module named uvicorn`. If you would rather not think about
> it, call the interpreter directly instead — `.venv/bin/python -m uvicorn proxy.app:app
> --port 8080` works from a plain shell.

```bash
# terminal 2 — the dashboard
cd dashboard && npm install && npm run dev
```

Confirm the backend is healthy:

```bash
curl -s localhost:8080/healthz | python3 -m json.tool
```

**Expect** `"status": "ok"`, `predictor.available: true`, and a positive
`predictor.learned_factors` count. The count grows as the ledger learns; it is not a
fixed deployment value.

> `learned_factors: 0` means the predictor has no history and every estimate will be
> the raw heuristic — roughly 80% error instead of 10%. It is the single most common
> reason this walkthrough "does not work". Fix it with `python scripts/seed_demo.py`,
> which loads 1,224 real observations, then restart the proxy.

Open **http://localhost:3000/dashboard** — note the path. `/` is the marketing homepage
since the two-root-layout split; both return 200, so landing on the wrong one shows you a
product page rather than an error.

You should see **Team Spend**, **Live Requests**, **Cost per Outcome**, and the
**Treasurer** panel.

---

## 4. Prove the pipeline: one prompt, end to end

`scripts/try.sh` sends one prompt through the proxy and prints what the predictor
guessed against what actually happened.

```bash
./scripts/try.sh ticket-summary "Summarise this support ticket in two sentences for the on-call engineer: The notification fanout times out under load. Users report it started this morning. Logs point at the connection pool. We saw OperationalError: database is locked in the pod events."
```

**Expect** the model's answer, then:

```
  feature tag     ticket-summary
  input tokens    58
  max_tokens      1500
  predicted out   47
  actual out      41
  error           15%
  predicted cost  $0.000037
  actual cost     $0.000033
  history factor  0.67
```

That single block is the whole product working: authenticated, attributed to a feature
and an actor, **estimated before the call**, forwarded, captured, and priced.

`history factor 0.67` means the learned correction was applied. **`1.00` means no
history for that tag** — the prediction is uncorrected and will be much worse.

Watch **http://localhost:3000/dashboard** — the row appears in Live Requests within 3s, and
"total requests" increments.

---

## 5. Prove the accuracy claim across features

The claim is **~10% median error on templated traffic**. Verify it yourself rather than
taking the number on trust.

Run these. Each is `./scripts/try.sh <feature-tag> "<prompt>"`.

```bash
./scripts/try.sh sql-from-question "Write a SQL query answering: how many times did the notification fanout report 'OperationalError: database is locked' per day over the last week? Table events(ts, service, message)."

./scripts/try.sh changelog-entry "Write a one-line user-facing changelog entry for a fix to the connection pool in the notification fanout, which previously times out under load."

./scripts/try.sh pr-description "Write a pull request description for a change to the connection pool in our Python notification fanout. The bug was that it times out under load. Include what changed and how to test it."

./scripts/try.sh api-doc-paragraph "Write the reference documentation paragraph for an endpoint that reports the health of the connection pool in the notification fanout. Describe the response fields."

./scripts/try.sh postmortem-timeline "Draft the timeline section of a postmortem for an incident where the notification fanout times out under load, caused by the connection pool, first seen as OperationalError: database is locked. Include detection, escalation, mitigation, and resolution entries."

./scripts/try.sh regex-explain "Explain what this pattern matches and when it would fail, in the context of parsing logs from our Python notification fanout: ^\[(?P<ts>[^\]]+)\]\s+(?P<lvl>WARN|ERROR)\s+(?P<msg>.*)$"

./scripts/try.sh code-review-note "Write a short code review comment asking the author to reconsider the connection pool in this Python notification fanout, given that it currently times out under load."

./scripts/try.sh test-plan "Write a test plan for a fix to the connection pool in the notification fanout, which times out under load. Cover the happy path, the regression, and one edge case."

./scripts/try.sh commit-message "Write a conventional-commit message (subject line plus one body line) for a change that fixes the connection pool in the notification fanout, which previously times out under load."

./scripts/try.sh ticket-classify "Classify this issue by severity (P0-P3) and component. Answer with just the labels. The notification fanout times out under load. Signature: OperationalError: database is locked."
```

**Expected error, per feature** — from a real run of these exact prompts:

| feature | typical error | notes |
| --- | --- | --- |
| `sql-from-question` | 2–10% | |
| `changelog-entry` | 0–15% | |
| `pr-description` | 0–16% | |
| `api-doc-paragraph` | 4–13% | |
| `postmortem-timeline` | 17–19% | |
| `regex-explain` | 8% | |
| `code-review-note` | 11–18% | |
| `test-plan` | 2–20% | occasionally worse — see note below |
| `commit-message` | 2–24% | ~40-token answers, so % is harsh |
| `ticket-classify` | 50–88% | ~9-token answers; the % is meaningless, the cost error is $0.000001 |

**Median across all of them should land around 10–18%.** Do not expect every row to be
good — expect the *median* to be, and expect the two short-output features to look bad
in percentage terms while being irrelevant in cost terms.

### Three honest caveats

**Percentage error is brutal on tiny answers.** `ticket-classify` produces ~9 tokens.
Being 6 tokens off is 67% error and costs one millionth of a cent. Judge those two
features by cost, not by percentage.

**`incident-runbook` and `error-explainer` will read 39–47%.** Their training data was
collected at a 400-token cap and every row hit it, so their learned history says "400
tokens" — true only at `max_tokens=400`. `try.sh` prints a warning for both. This is a
known data-collection artifact, not a prediction failure.

**Writing the request in your own words roughly quadruples the error — ~40% instead of
~10%.** The correction keys on the *feature tag*, not on your text, so a prompt tagged
`ticket-summary` that implies a much longer answer than that feature usually produces
is predicted as though it were typical. This is the honest limit of the approach and
worth knowing before anyone demos an improvised prompt.

### All 32 features at once

**Paid tier only.** 64 requests and ~600,000 input tokens, because it includes the
high-consumption features whose prompts run 33k–44k tokens each:

```bash
python scripts/demo_live.py --n 2          # ~4 minutes, ~4 cents, blows a free-tier day
```

**Free-tier-safe version** — the same harness restricted to small-input features,
16 requests and under 1,000 input tokens:

```bash
python scripts/demo_live.py --n 2 --tags ticket-summary,commit-message,changelog-entry,\
sql-from-question,code-review-note,ticket-classify,pr-description,api-doc-paragraph
```

---

## 6. Prove the circuit breaker and the iMessage alert

The breaker cuts spend on a runaway feature. It needs **two** conditions, both true:

1. **Floor** — spend in the trailing 5 minutes clears a threshold (`$20` in production)
2. **Burst** — that 5-minute *rate* exceeds the trailing hour's average by 3×

The second condition is what stops a legitimately expensive feature from tripping every
five minutes forever. It asks *"is this feature behaving unusually for itself?"*

A templated call costs $0.00003, so $20 is unreachable by hand. Restart the proxy with a
demo-scale floor:

```bash
# terminal 1: Ctrl-C, then
BREAKER_WINDOW_USD=0.0001 python -m uvicorn proxy.app:app --port 8080
```

> This is a **runtime override**. `.env` still says `$20`, so restarting without it
> returns you to production settings. Write the command down before demo day.

Now run these four. **Spacing them ~15 seconds apart is fine** — the floor accumulates
across the whole 5-minute window, so "as fast as possible" is not required, and on a
free-tier key firing them back to back risks OpenAI's own `429` masquerading as the
breaker (see the box at the top of this guide for how to tell them apart).

```bash
./scripts/try.sh ticket-summary "Summarise this support ticket in two sentences for the on-call engineer: The auth gateway leaks file descriptors. Users report it started this morning. Logs point at the connection pool. We saw OOMKilled at 512Mi in the pod events."

./scripts/try.sh ticket-summary "Summarise this support ticket in two sentences for the on-call engineer: The billing worker drops the last page of results. Users report it started this morning. Logs point at the cache key. We saw ECONNRESET during a keep-alive request in the pod events."

./scripts/try.sh ticket-summary "Summarise this support ticket in two sentences for the on-call engineer: The search indexer double-charges on retry. Users report it started this morning. Logs point at the idempotency check. We saw context deadline exceeded after 30s in the pod events."

./scripts/try.sh ticket-summary "Summarise this support ticket in two sentences for the on-call engineer: The CDN purge job deadlocks on concurrent writes. Users report it started this morning. Logs point at the pagination cursor. We saw OperationalError: database is locked in the pod events."
```

**Expect** the first 2–3 to succeed and the next to come back **429**.

Immediately run a **different** feature:

```bash
./scripts/try.sh commit-message "Write a conventional-commit message (subject line plus one body line) for a change that fixes the retry policy in the auth gateway, which previously leaks file descriptors."
```

**Expect 200.** This is the important one: the runaway feature is cut off while
everything else keeps serving. That is throttle mode, and it is a stronger claim than
"we turned the key off".

The proxy log should show:

```
breaker TRIPPED scope=demo-project:ticket-summary mode=throttle
        spend=$0.0001/300s floor=$0.00 burst=4.54x (need 3.00x, ceiling 12x)
ALERT circuit breaker tripped
poke alert sent (HTTP 202)
```

`poke alert sent (HTTP 202)` means the iMessage went out. **Without `POKE_API_KEY` and
`POKE_CTO_PHONE` set you will see nothing** — the alert package returns early and, by
design, swallows its own errors so a third-party outage can never fail a request. The
log is your only evidence either way.

> **Linq sandbox rule:** the recipient must have messaged the sending line **first**, or
> delivery fails silently with error `2008`. If the log says sent and no message
> arrives, that is why.

### Recovering

The breaker is never permanently open. Three ways back:

1. **Wait 120 seconds**, then send another request to that feature. It re-measures
   ("half-open") and closes itself if spend has decayed. Recovery is **lazy** — no
   background job. The database row stays marked open until a request triggers the
   check, which looks alarming and is not.
2. **Manual reset**, any time:
   ```bash
   curl -s -X POST localhost:8080/v1/breaker/reset \
     -H "Authorization: Bearer mk_dev_local" -H "Content-Type: application/json" \
     -d '{"project_id":"demo-project","feature":"ticket-summary"}'
   ```
3. **Fix the cause.** If the burst is still happening it re-trips on the next check,
   which is correct.

---

## 7. Prove budget ceilings

Ceilings live in `meter.yaml` at the repo root — in the repo, not the database, so a
change to a spend limit gets reviewed like any other change. Confirm what loaded:

```bash
curl -s localhost:8080/healthz | python3 -c "import sys,json; print(json.load(sys.stdin)['budget'])"
```

**Expect** `meter_yaml_found: true` and 19 ceilings.

The dashboard's Team Budget card shows `demo-project` against its $3.00/day ceiling,
tracking the ledger to six decimals.

> **The bar will not visibly move.** One call is $0.0003 against a $3.00 ceiling —
> about 0.01%. The number updates correctly; the percentage does not budge. To make it
> climb visibly, drop one feature's ceiling to ~$0.01 in `meter.yaml` and restart.

Syntax that will silently bite you — a feature ceiling is a **mapping**, not a number:

```yaml
features:
  ticket-summary: { ceiling_usd_per_day: 0.50 }   # correct
  ticket-summary: 0.50                            # parses as YAML, then ignored
```

The bare-number form loads the project ceiling and drops every feature ceiling without
an error.

---

## 8. Prove the Treasurer and Prava — **Shivam**

This is the only section that needs a real Prava wallet, and it is the Prava track's
core requirement: *the agent takes a meaningful action*.

### What the Treasurer actually watches

**Not the budget ceiling.** Those are unrelated:

| | budget ceiling (`meter.yaml`) | Treasurer |
| --- | --- | --- |
| watches | project spend in the ledger | **wallet balance vs burn rate** |
| fires when | spend + estimate > ceiling | runway < 0.75h, or balance < $10 |
| does what | refuses the request with `429` | calls Prava, tops up the wallet |

Running out of budget headroom gets you a 429. It never triggers a top-up.

### Step 1 — create a wallet

```bash
curl -s -X POST "localhost:8080/wallets/seed?project_id=demo-project&provider=openai&balance_usd=0.05&reset=true" \
  -H "Authorization: Bearer mk_dev_local"
```

> **These are query parameters, not a JSON body.** Passing `-d '{"balance_usd":0.05}'`
> is silently ignored — the endpoint accepts it, uses its defaults, and returns 200, so
> the mistake looks like success. Same for `/mandates/create`.
>
> `reset=true` matters too: the balance applies **on creation only**, so without it a
> re-run returns the existing wallet untouched. That is deliberate — it stops a re-run
> wiping a top-up the Treasurer already made — but it means `reset=true` is how you put
> the demo back to its starting state between run-throughs.

**Expect** `"balance_usd": 0.05`. Use `balance_usd=4.00` for the demo's "getting low"
state, or `0.05` to make the Treasurer fire immediately.

### Step 2 — check the decision

```bash
curl -s localhost:8080/treasury/assess | python3 -m json.tool
```

**Expect** `"should_topup": true` with `"trigger": "runway"`, because §4–§6 generated
real burn against a near-empty wallet.

### Step 3 — a mandate

```bash
curl -s localhost:8080/mandates/chargeable | python3 -m json.tool
```

If that is empty, the Treasurer will refuse with `no_chargeable_mandate` — correctly,
because it checks it has permission to spend **before** reaching for the network. No
Prava call is made at all.

To create one:

```bash
# query parameters again, not a body
curl -s -X POST "localhost:8080/mandates/create?project_id=demo-project&amount_usd=50&recurring_frequency=monthly&user_email=you@example.com" \
  -H "Authorization: Bearer mk_dev_local" | python3 -m json.tool
```

Open the returned `approval_url`, add a card, clear the device-binding OTP (`456789` in
sandbox), and register a passkey. **Budget 2–3 minutes the first time**, well under a
minute for repeats on the same browser — which is exactly why a live demo should approve
beforehand.

Then claim it locally:

```bash
curl -s -X POST localhost:8080/mandates/sync -H "Authorization: Bearer mk_dev_local"
curl -s localhost:8080/mandates/chargeable | python3 -m json.tool
```

### Step 4 — run the loop

```bash
curl -s -X POST localhost:8080/treasury/tick -H "Authorization: Bearer mk_dev_local" | python3 -m json.tool
curl -s localhost:8080/treasury/events | python3 -m json.tool
```

**`TREASURER_DRY_RUN=true` is the default and should stay that way until you mean it.**
In dry run the agent decides, records the event, and does not spend. The Treasurer Agent
panel on the dashboard shows both lines either way.

To do it for real, set `TREASURER_DRY_RUN=false`, restart, and re-tick. Expect a
`treasury_events` row settling to `succeeded` with a `prava_txn_id`, and the wallet
balance rising.

### Known state as of this writing

The loop has been verified up to the mandate check: it detects the wallet, computes
runway, decides to request $25.00, checks its rails, refuses without a mandate, writes
the audit row, and the dashboard renders it. **The Prava call itself is unverified** —
the key on the author's machine returns `401 AUTH_1001`. Shivam's wallet is the one that
closes this.

---

## 9. Prove cost-per-outcome

Spend attributed to a *result*, not just a request. Tag a call with a trace, then
annotate that trace with what it achieved:

```bash
curl -s localhost:8080/v1/chat/completions \
  -H "Authorization: Bearer mk_dev_local" \
  -H "X-Meter-Feature: ticket-summary" -H "X-Meter-Actor: shivam" \
  -H "X-Meter-Trace: ticket-4471" -H "Content-Type: application/json" \
  -d '{"model":"gpt-4o-mini","max_tokens":400,"messages":[{"role":"user","content":"Summarise: the auth gateway is returning 502s for EU customers since the 14:00 deploy."}]}' > /dev/null

curl -s -X POST localhost:8080/v1/annotate \
  -H "Authorization: Bearer mk_dev_local" -H "Content-Type: application/json" \
  -d '{"trace_id":"ticket-4471","outcome":"resolved","value_usd":12.50}'
```

The Cost per Outcome table on the dashboard joins `requests × annotations` on
`trace_id`. One resolved ticket is usually a dozen calls, which is why the join is on
the trace and not the request.

---

## Budget for the whole guide

| section | requests | input tokens |
| --- | --- | --- |
| §4 one prompt | 1 | ~60 |
| §5 ten features | 10 | ~500 |
| §5 free-tier-safe sweep (optional) | 16 | ~700 |
| §6 breaker | 5 | ~300 |
| §9 cost per outcome | 1 | ~40 |
| **total** | **33** | **~1,600** |

Against a free tier's ~200 requests/day that is **17%**. §10 costs nothing — the test
suites never call a provider. The only thing that breaks the budget is the paid-tier
sweep in §5, which is labelled.

---

## 10. Run the automated checks

```bash
python tests/test_predictor.py     # estimator
python tests/test_proxy.py         # proxy, ledger, breaker, budget
python tests/test_treasury.py      # wallets, mandates, Treasurer
python tests/test_alerts.py        # Poke/Linq
python tests/test_judge.py         # session-scoped judge flow
python scripts/e2e_journey.py --offline   # drives the running app
```

All checks should pass without calling a provider.

`e2e_journey.py` is the one to run before any demo: it boots the real ASGI app and walks
the whole journey. It exists because every other test exercised modules in isolation,
and `GET /mandates` was answering a **500** with all 601 of them passing.

---

## Troubleshooting

| symptom | cause |
| --- | --- |
| `failed to resolve host db.<ref>.supabase.co` | Direct host is IPv6-only. Use the **pooler** — see §2. |
| `learned_factors: 0` | Ledger has no history. `python scripts/seed_demo.py`, restart the proxy. |
| Every prediction has `history factor 1.00` | No learned factor for that tag. Either it is a new feature (needs ~20 calls) or the ledger is unseeded. |
| `Unknown Meter key` | The key in your request is not in `METER_KEYS`. It is `mk_dev_local` by default, **not** `mk_demo`. |
| Prediction is wildly wrong, tiny input tokens | You sent an empty prompt. `try.sh` refuses these; a hand-written `curl` will not. |
| `No module named uvicorn` / `No module named psycopg` | The virtualenv is not active. Every new terminal needs `source .venv/bin/activate` first — a restart or a fresh tab does not inherit it. Or skip activation and call `.venv/bin/python -m uvicorn ...` directly. |
| Breaker will not trip | Threshold is $20 by default. Restart with `BREAKER_WINDOW_USD=0.0001`. |
| Breaker will not clear | 120s cooldown, and recovery is lazy — send a request to that feature to trigger the re-check. |
| iMessage never arrives | `POKE_API_KEY`/`POKE_CTO_PHONE` unset, or the Linq sandbox rule (§6). |
| Feature ceilings missing from `/healthz` | Bare-number YAML instead of a mapping — see §7. |
| `no_chargeable_mandate` | Expected without a mandate. Not a bug: the rails check permission before touching the network. |

---

## What is NOT proven yet

Stated plainly so nobody demos a claim we have not earned:

- **The Prava charge itself.** Everything up to it is verified; the transaction is not.
  §8 is Shivam's to close.
- **Cross-model analysis.** Cut from scope (`PROPOSALS.md` B11). The offline script
  exists but was never run. The proxy deliberately does **not** shadow-call a second
  provider on live traffic — doubling a customer's bill inside a cost-control tool is
  indefensible.
- **Open-ended prompt accuracy.** ~49% median, and close to a hard ceiling: output
  length is only weakly predictable from prompt text (R² = 0.28). The product's claim is
  about *tagged, repeated* feature traffic, and should always be stated that way.
- **Deployment.** Everything here runs locally. The dashboard is not deployed.

---

## The one-paragraph version

Meter is a drop-in proxy: point any OpenAI SDK at it and every call is metered,
attributed to a feature and a person, cost-predicted *before* it runs, and written to a
shared ledger. Features you tag get ~10% cost prediction after about 20 calls. A runaway
feature is throttled and an engineer gets an iMessage. When the provider balance runs
short, an agent holding a Prava mandate tops it up without waking anyone.
