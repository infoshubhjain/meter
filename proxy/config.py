"""Environment-backed configuration for the Meter proxy.

Every knob is read once at import time. Nothing here reaches out to the network or the
database, so importing this module is safe from tests and from the test-suite's own
``__main__`` self-check.

Owner: Shubh (Proxy & Infra).
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

# Load `.env` from the repo root regardless of the working directory uvicorn was started
# from. `override=False` means a variable already exported in the shell (or injected by
# docker compose) wins over the file, which is what you want in every deployment.
REPO_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(REPO_ROOT / ".env", override=False)


def _str(name: str, default: str) -> str:
    value = os.getenv(name)
    return default if value is None or value == "" else value


def _float(name: str, default: float) -> float:
    try:
        return float(_str(name, str(default)))
    except ValueError:
        return default


def _int(name: str, default: int) -> int:
    try:
        return int(float(_str(name, str(default))))
    except ValueError:
        return default


def _bool(name: str, default: bool) -> bool:
    return _str(name, "true" if default else "false").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


# ── Providers ────────────────────────────────────────────────────────────────
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
OPENAI_BASE_URL = _str("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
ANTHROPIC_BASE_URL = _str("ANTHROPIC_BASE_URL", "https://api.anthropic.com/v1").rstrip("/")
ANTHROPIC_VERSION = _str("ANTHROPIC_VERSION", "2023-06-01")

# Streams stay open for minutes, so the read timeout has to be generous. It is applied
# per socket read, not to the request as a whole — a healthy stream that emits a token
# every few seconds never trips it, while a genuinely hung upstream still does.
UPSTREAM_TIMEOUT_S = _float("UPSTREAM_TIMEOUT_S", 600.0)
UPSTREAM_CONNECT_TIMEOUT_S = _float("UPSTREAM_CONNECT_TIMEOUT_S", 10.0)

# ── Ledger ───────────────────────────────────────────────────────────────────
# The ledger is Postgres. There is no local-file fallback on purpose: a silent fallback
# means half the team develops against a database nobody else can see, and the predictor
# quietly gets the cold-start numbers (65% median error against 31%) without anyone
# noticing why. Missing DATABASE_URL is an error you can read, not a degraded mode.
DATABASE_URL = _str("DATABASE_URL", "")

# Pool bounds. Small: one proxy process makes a handful of queries per request, and
# Supabase's free tier is not generous with connections.
DB_POOL_MIN = _int("DB_POOL_MIN", 1)
DB_POOL_MAX = _int("DB_POOL_MAX", 10)

# Which Postgres schema the ledger lives in. `public` in normal use; the test suites set
# a throwaway one per run so they cannot write into the tables the demo and the judges
# are using — the isolation a tempfile gave them for free under SQLite.
DB_SCHEMA = _str("DB_SCHEMA", "public")

# Dead since the Postgres port, and kept only because `predictor/refresh.py` still takes a
# `db_path` argument it explicitly ignores. Nothing reads the file it points at — there is
# no file. It is NOT a fallback: `DATABASE_URL` above is the only ledger, and its absence
# raises rather than degrading.
#
# It used to feed the boot log, which then announced "ledger ready at meter.db" on a
# deployed proxy and sent whoever was debugging it looking for a local file. That line now
# calls `db.ledger_target()`. Delete this once the predictor drops the argument.
DB_PATH = Path(_str("METER_DB_PATH", str(REPO_ROOT / "meter.db")))
PRICING_VERSION = _str("PRICING_VERSION", "2026-08-01")
PRICING_DIR = REPO_ROOT / "pricing"

# `open` serves traffic when the ledger is unreachable; `closed` drops it. Meter sits in
# the critical path, and a cost tool that takes down production is not a cost tool — so
# open is the default and closed is the deliberate opt-in.
FAIL_MODE = _str("FAIL_MODE", "open").strip().lower()

# ── Budgets and reservations ─────────────────────────────────────────────────
# Budgets are declared in `meter.yaml` at the repo root so spend limits are reviewed by
# pull request (README.md). No file means no ceilings, which is the Phase 1 behaviour.
METER_YAML_PATH = Path(_str("METER_YAML_PATH", str(REPO_ROOT / "meter.yaml")))

# The window a `ceiling_usd_per_day` is measured over. Rolling, not calendar-aligned: a
# midnight reset would let a runaway loop spend a full ceiling at 23:59 and another at
# 00:01, which is the failure the ceiling exists to prevent.
BUDGET_WINDOW_S = _int("BUDGET_WINDOW_S", 86_400)

# How long an unreleased reservation survives. Short on purpose (ARCHITECTURE.md §2): a
# crashed worker must release its holds rather than deadlock the ceiling. Streams outlive
# this routinely, which is why `budget.extend()` heartbeats it rather than raising it.
RESERVATION_TTL_S = _float("RESERVATION_TTL_S", 120.0)

# How often a live stream pushes its reservation's expiry forward. Must stay comfortably
# under RESERVATION_TTL_S or a slow stream reaps its own hold mid-flight.
RESERVATION_HEARTBEAT_S = _float("RESERVATION_HEARTBEAT_S", 30.0)

# How long a resolved Meter key stays cached in-process. Authentication is the one query
# every single request makes, so at a hosted ledger's RTT it is the largest fixed cost on
# the path — and the answer almost never changes.
#
# The tradeoff is revocation latency, and it is smaller than it looks. Every path that can
# revoke or re-scope a key in this process (`revoke_key`, `unrevoke_key`,
# `set_breaker_floor`) purges the cache synchronously, so a revocation issued through Meter
# is visible on the very next request, not after the TTL. The window only exists for a
# change made *outside* this process — a direct UPDATE against the database, or a second
# proxy instance, which `render.yaml` and `fly.toml` already warn must not exist because
# the reservation lock is in-process too.
#
# Set to 0 to disable caching entirely and pay the round trip on every request.
KEY_CACHE_TTL_S = _float("KEY_CACHE_TTL_S", 5.0)

# The judge money capability (PROPOSALS.md B20) is issued as a cookie with
# `SameSite=None; Secure`, because the console is on a different origin from the proxy and
# a cross-site cookie is not sent otherwise. `Secure` requires HTTPS, so on a plain-HTTP
# local dev origin the browser silently drops it and every money route 403s.
#
# Set this to relax the cookie to `SameSite=Lax` without `Secure` for local development
# ONLY. It must never be set in a deployment: it is what makes the capability a
# same-site-only secret rather than a cross-site one, and on http:// it is sent in clear.
JUDGE_CAPABILITY_INSECURE = _bool("JUDGE_CAPABILITY_INSECURE", False)

# Soft budgets (PROPOSALS.md D2). The hard ceiling refuses with 429; this warns before it,
# while there is still time to act. Deliberately a background poll rather than a check in
# the request path: crossing a threshold is a *level*, not an edge, so testing it per
# request would re-evaluate it thousands of times to send at most one message — and would
# put that work in front of production traffic for a notification nobody reads inline.
BUDGET_SOFT_ALERT_ENABLED = _bool("BUDGET_SOFT_ALERT_ENABLED", True)
# Fraction of a ceiling that counts as "close". 0.8 matches LiteLLM's default soft budget.
BUDGET_SOFT_ALERT_RATIO = _float("BUDGET_SOFT_ALERT_RATIO", 0.8)
# How often the poll runs. Cheap: one indexed SUM per configured ceiling.
BUDGET_SOFT_ALERT_INTERVAL_S = _float("BUDGET_SOFT_ALERT_INTERVAL_S", 60.0)

# The pre-flight estimate (ARCHITECTURE.md §2 step 3). Off puts `predicted_*` NULL on
# every row and reserves nothing, which is Phase 1 behaviour exactly.
PREDICT_ENABLED = _bool("PREDICT_ENABLED", True)

# The predictor's online learning loop: periodically re-fit the per-(project, feature)
# correction from the ledger. Safe to leave on — `refresh.refresh_now` fits on the older
# 75% of rows and installs a candidate ONLY if it beats the current factors on the newest
# 25%, which the fit never saw, so the worst case is "no change" rather than "worse".
#
# Measured value depends on traffic shape and both numbers are real: on templated feature
# traffic it takes median error from 82.6% to 28.8% out-of-sample; on unrelated one-off
# prompts it is flat and the gate simply keeps rejecting. See CONTEXT.md §6a.
PREDICT_REFRESH_ENABLED = _bool("PREDICT_REFRESH_ENABLED", True)

# Seconds between refreshes. This reads the ledger and re-fits, so it is deliberately not
# frequent: the factors move on the timescale of hundreds of requests, not seconds, and
# the request path never waits on it either way.
PREDICT_REFRESH_INTERVAL_S = _float("PREDICT_REFRESH_INTERVAL_S", 120.0)

# ── Meter keys ───────────────────────────────────────────────────────────────
# `key:project:environment` triples, comma separated. Phase 1 shim; superseded once
# key provisioning moves into the database.
METER_KEYS = _str("METER_KEYS", "mk_dev_local:demo-project:dev")

# ── Circuit breaker ──────────────────────────────────────────────────────────
BREAKER_ENABLED = _bool("BREAKER_ENABLED", True)

# Detection is two conditions, both of which must hold — see proxy/breaker.py for the
# full reasoning and ARCHITECTURE.md §6 for the spec.
#
# 1. FLOOR: trailing `BREAKER_WINDOW_S` spend must clear `BREAKER_WINDOW_USD`. This is
#    CONTEXT.md §5C's "$20 in 5 minutes" and it is what makes detection fast.
BREAKER_WINDOW_S = _int("BREAKER_WINDOW_S", 300)
BREAKER_WINDOW_USD = _float("BREAKER_WINDOW_USD", 20.0)

# 2. BURST: the short window's spend *rate* must exceed the trailing baseline window's
#    average rate by this multiple. This is what stops a legitimately expensive but
#    steady workload from tripping every five minutes forever.
#
# The ratio is capped by the window sizes: with a 300s short window inside a 3600s
# baseline, the maximum achievable ratio is 3600/300 = 12 (every dollar of the hour
# landed in the last five minutes). Set BREAKER_BURST_RATIO above that and the breaker
# can never trip; set it to 0 to disable the burst check and fall back to the flat
# floor alone.
BREAKER_BASELINE_WINDOW_S = _int("BREAKER_BASELINE_WINDOW_S", 3600)
BREAKER_BURST_RATIO = _float("BREAKER_BURST_RATIO", 3.0)

BREAKER_MODE = _str("BREAKER_MODE", "throttle").strip().lower()
BREAKER_COOLDOWN_S = _int("BREAKER_COOLDOWN_S", 120)

# ── Browser origins ──────────────────────────────────────────────────────────
# The judge console runs on the dashboard's origin and calls this API directly from the
# browser, so the API has to say that origin is allowed. Comma-separated; `*` permits any.
#
# Not wildcarded by default. The console sends a session token in `X-Judge-Session`, and
# a token is worth exactly as much as the origins allowed to send it -- a wildcard would
# let any page on the internet drive somebody's live session. The default names the
# deployed dashboard and local development, and DEPLOY.md says to update it when the
# dashboard URL changes.
# Whether to additionally allow Vercel PREVIEW deployments of the projects named above.
#
# Off by default, and that default is the security decision (PROPOSALS.md B22). The preview
# pattern is built from the configured origins, so it cannot match an unrelated project —
# but it necessarily allows `<project>-<anything>.vercel.app`, and `vercel.app` subdomains
# are first-come-first-served. Anyone can register `meter-three-beta-evil.vercel.app` in
# about a minute and be inside the allowlist.
#
# That was survivable when CORS carried no credentials. It is not now: the judge money
# capability is a cookie, so a browser on a matching origin attaches it automatically, and
# the second factor that exists to stop a leaked session token from spending would travel
# to an origin an attacker owns.
#
# Turn it on only when testing the console from a branch preview, and prefer naming the
# preview URL in CORS_ALLOW_ORIGINS instead.
CORS_ALLOW_VERCEL_PREVIEWS = _bool("CORS_ALLOW_VERCEL_PREVIEWS", False)

CORS_ALLOW_ORIGINS = _str(
    "CORS_ALLOW_ORIGINS",
    "https://meter-five-nu.vercel.app,http://localhost:3000,http://localhost:3001,http://127.0.0.1:3000",
)
