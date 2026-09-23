"""The Meter proxy.

Implements PLAN.md Phase 1 for Shubh (Proxy & Infra), plus the Phase 3 circuit breaker
pulled forward, plus SSE usage extraction.

The request lifecycle here is ARCHITECTURE.md §2, with reservations held in-process
rather than in Redis (PROPOSALS.md A5):

    1. AUTHENTICATE   resolve Meter key -> project, environment
    2. ATTRIBUTE      read X-Meter-Feature / X-Meter-Actor / X-Meter-Trace
    3. ESTIMATE       predict output tokens and cost before the call (predictor/)
    4. BREAKER CHECK  rolling-window spend for this attribution tag
    5. RESERVE        hold the estimate against the daily ceiling (proxy/budget.py)
    6. FORWARD        stream bytes to the client unbuffered while teeing for usage
    7. CAPTURE        price actual usage, write the ledger row, release the hold —
                      storing the prediction beside the actual so accuracy is a query

Step 7 never blocks the client. By the time it runs, the caller already has every byte.
Steps 4 and 5 are swapped relative to ARCHITECTURE.md's numbering — see the comment at
the breaker check for why.

Owner: Shubh (Proxy & Infra).
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import uuid
from contextlib import asynccontextmanager
from typing import Any

import httpx
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse

from treasury import db as treasury_db
from treasury import mock_provider
from treasury import prava as treasury_prava
from treasury import routes as treasury_routes
from treasury import treasurer

from . import breaker, budget, config, db, pg, providers
from .pricing import Usage, estimate_from_bytes, price

# Optional at import time on purpose. `predictor` pulls in tiktoken and numpy, and a
# teammate whose virtualenv predates Phase 2 would otherwise find the whole proxy refusing
# to boot over a pre-flight estimate that is allowed to be absent. Degrade to no
# prediction — the same state PREDICT_ENABLED=false produces — and say so once.
try:
    from predictor import predict
except ImportError as _exc:  # pragma: no cover - depends on the local venv
    predict = None
    _PREDICTOR_IMPORT_ERROR = str(_exc)
else:
    _PREDICTOR_IMPORT_ERROR = ""

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
)
log = logging.getLogger("meter.proxy")

# Capture tasks are parked here for the duration of their run. Without a strong
# reference the event loop is free to garbage-collect a task mid-flight, which would
# silently drop ledger rows under exactly the load that makes the ledger interesting.
_capture_tasks: set[asyncio.Task] = set()

# Response headers that describe the upstream socket rather than the payload. Forwarding
# them corrupts the client's own framing — `content-length` in particular will be wrong
# whenever we strip an injected usage chunk.
_HOP_BY_HOP = {
    "content-length",
    "content-encoding",
    "transfer-encoding",
    "connection",
    "keep-alive",
}


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.connect()
    seeded = db.seed_keys(config.METER_KEYS)
    # Names the database actually in use, not `config.DB_PATH` — that is the old SQLite
    # path, which survived the Postgres port and made the first line of every boot announce
    # a `meter.db` that does not exist. Pointing at the wrong database is the failure that
    # looks like "where did all the data go", so the host and schema are worth printing.
    # `ledger_target()` also strips credentials: a boot line is what gets pasted into chat.
    log.info("ledger ready at %s (%d meter key(s) seeded)", db.ledger_target(), seeded)

    # Create the treasury tables at boot rather than on first use. `treasury.db.connect()`
    # is lazy, so without this the `wallets` table does not exist until somebody happens to
    # call a treasury route — and the dashboard reads that table directly and read-only
    # (dashboard/src/lib/db.ts). It would fail with "no such table: wallets" on a machine
    # where nobody had hit the endpoint yet, which is every teammate's machine.
    treasury_db.connect()
    log.info("treasury tables ready (wallets, mandates, treasury_events)")

    # Budgets are declared in meter.yaml and projected into the ledger at boot, so the
    # file stays the reviewable source of truth and the request path still reads a table
    # (PROPOSALS.md A6). No file means no ceilings, which is exactly Phase 1 behaviour.
    await asyncio.to_thread(budget.load_meter_yaml)

    if config.PREDICT_ENABLED and predict is None:
        log.warning(
            "PREDICT_ENABLED is on but the predictor could not be imported (%s). "
            "Running without pre-flight estimates: predicted_* columns stay NULL and "
            "reservations hold $0. Fix with `pip install -r requirements.txt`.",
            _PREDICTOR_IMPORT_ERROR,
        )

    if not config.OPENAI_API_KEY and not config.ANTHROPIC_API_KEY:
        log.warning("no provider keys configured — every upstream call will 401")

    # A breaker that cannot fire is worse than no breaker, because everyone believes it is
    # protecting them. The burst ratio is bounded by the window sizes, so a threshold above
    # that ceiling silently disables detection — shout about it at boot rather than
    # discovering it during the incident it was supposed to catch.
    if config.BREAKER_ENABLED and config.BREAKER_BURST_RATIO > 0:
        ceiling = config.BREAKER_BASELINE_WINDOW_S / max(config.BREAKER_WINDOW_S, 1)
        if config.BREAKER_BURST_RATIO >= ceiling:
            log.error(
                "BREAKER CANNOT FIRE: BREAKER_BURST_RATIO=%.2f is at or above the %.2fx "
                "ceiling implied by BREAKER_WINDOW_S=%ds inside "
                "BREAKER_BASELINE_WINDOW_S=%ds. Lower the ratio, widen the baseline, or "
                "set BREAKER_BURST_RATIO=0 to use the flat floor alone.",
                config.BREAKER_BURST_RATIO,
                ceiling,
                config.BREAKER_WINDOW_S,
                config.BREAKER_BASELINE_WINDOW_S,
            )

    # Load the tokenizer vocabularies now rather than on the first request. tiktoken
    # builds an encoder lazily, and that first build measured 124ms of overhead on the
    # first call while every subsequent call was under 1ms. Paying it at boot keeps the
    # published overhead number honest and stops a cold demo box from looking slow on
    # exactly the request someone is watching.
    try:
        from predictor.tokenizer import warm

        warmed = await asyncio.to_thread(warm)
        log.info("tokenizer warm for %d model(s)", warmed)
    except Exception:
        # A cold tokenizer is a latency problem, not a correctness one.
        log.debug("tokenizer warmup skipped", exc_info=True)

    # One client for the process. Connection reuse is most of the reason the proxy can
    # claim single-digit-millisecond overhead: a fresh TLS handshake per request would
    # cost more than everything else in this file put together.
    app.state.http = httpx.AsyncClient(
        timeout=httpx.Timeout(
            config.UPSTREAM_TIMEOUT_S,
            connect=config.UPSTREAM_CONNECT_TIMEOUT_S,
        ),
        limits=httpx.Limits(max_connections=200, max_keepalive_connections=50),
        follow_redirects=False,
    )

    # Sweep expired judge credentials on a timer.
    #
    # `judge.sessions` holds a judge's Prava merchant key and Linq key in memory, with a
    # TTL, and that TTL is the whole justification for holding them at all. `create()`
    # sweeps opportunistically, which covers a busy console -- but a quiet one would leave
    # the last judge's credentials resident until the process restarted, which on a
    # free-tier host that stays warm is indefinitely. The promise is only true if
    # something enforces it when nobody is looking.
    async def _sweep_judge_credentials() -> None:
        from judge import sessions as judge_sessions

        while True:
            await asyncio.sleep(300)
            try:
                await asyncio.to_thread(judge_sessions.purge_expired)
            except Exception:  # noqa: BLE001 — a sweep failure must not kill the loop
                log.debug("judge credential sweep failed", exc_info=True)

    sweeper = asyncio.create_task(_sweep_judge_credentials())

    # Start the Treasurer Agent loop (Phase 3). Watches burn rate, projects runway, and
    # autonomously tops up wallets when balance drops below the threshold. This is "the 3am
    # save" from the pitch narrative — production never dies on a drained wallet.
    #
    # `start()` owns its own task and no-ops unless TREASURER_ENABLED, so a process that
    # charges a card on a timer takes two deliberate switches to turn on — the second
    # being TREASURER_DRY_RUN.
    treasurer.start()

    # Prove the payment rail's credentials at boot. A revoked Prava key does not return 401
    # on this sandbox — it stalls — so without this the first symptom is a top-up hanging at
    # 3am. Never fatal: it logs and the operator decides.
    await treasury_prava.verify_credentials()

    # The predictor's online learning loop. Without this the estimator only ever uses the
    # constants fitted offline against a dataset, and never learns anything from the
    # traffic this deployment actually serves — which is where the accuracy is: a feature
    # calls one prompt template repeatedly, and output length inside a template is far
    # more regular (p90/p10 spread ~1.2x) than across prompts in general (~10x).
    #
    # Kept off the request path entirely. It refits on a timer into an in-memory dict, and
    # `predict()` only ever reads that dict, so the pre-flight estimate stays I/O-free and
    # single-digit-millisecond (ARCHITECTURE.md §2).
    refresh_task = None
    if config.PREDICT_ENABLED and config.PREDICT_REFRESH_ENABLED:
        try:
            from predictor.refresh import start_background

            refresh_task = asyncio.create_task(
                start_background(interval_s=config.PREDICT_REFRESH_INTERVAL_S)
            )
            log.info("predictor refresh loop started (every %.0fs)",
                     config.PREDICT_REFRESH_INTERVAL_S)
        except Exception:
            # Same posture as the tokenizer warmup: losing the learning loop costs
            # accuracy over time, never correctness on any single request.
            log.debug("predictor refresh loop not started", exc_info=True)

    # Soft-budget warnings (PROPOSALS.md D2). Kept off the request path on purpose: a
    # ceiling being 80% full is a level rather than an edge, so checking it per request
    # would re-evaluate the same condition thousands of times to send at most one message.
    soft_budget_task = None
    if config.BUDGET_SOFT_ALERT_ENABLED:
        soft_budget_task = asyncio.create_task(_soft_budget_loop())

    try:
        yield
    finally:
        # Cancel the background loops cleanly on shutdown. The Treasurer owns its own task
        # and cancels it through `stop()`, so it is not in this list.
        await treasurer.stop()
        for task in (refresh_task, soft_budget_task, sweeper):
            if task is None:
                continue
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        # Let in-flight ledger writes land before the loop closes, but never hang
        # shutdown on them.
        if _capture_tasks:
            await asyncio.wait(set(_capture_tasks), timeout=5.0)
        await app.state.http.aclose()
        # Close the Postgres pool last: the capture tasks above may still be writing
        # ledger rows, and a closed pool would drop exactly the rows written during
        # shutdown — the ones you most want after a crash.
        pg.close()


async def _soft_budget_loop() -> None:
    """Poll ceilings and warn before the hard 429 (`PROPOSALS.md` D2).

    Mirrors `breaker.notify` in posture: the alert send is fire-and-forget on a daemon
    thread inside `alerts`, and the per-scope cooldown lives there too, so a ceiling that
    stays full for the rest of the day texts once rather than once per poll.

    Does no work at all when nothing is configured — no `meter.yaml` means no ceilings
    means the loop sleeps, which keeps this free for anyone who has not opted in.
    """
    # Imported once, not per iteration. Kept local to the function rather than at module
    # scope so `alerts` stays an optional dependency of the proxy, the same posture the
    # predictor import takes at the top of this file.
    from alerts import send_budget_alert

    ratio = config.BUDGET_SOFT_ALERT_RATIO
    while True:
        try:
            if budget.active_ceilings():
                # `soft_breaches` does blocking SQLite reads; keeping it in a thread is
                # what stops a budget notification from stalling every in-flight request.
                breaches = await asyncio.to_thread(budget.soft_breaches, ratio)
                for b in breaches:
                    log.info(
                        "SOFT BUDGET | %s at %.0f%% of ceiling ($%.4f of $%.2f)",
                        b["scope"], b["ratio"] * 100, b["spend_usd"], b["ceiling_usd"],
                    )
                    try:
                        send_budget_alert(b["scope"], b["spend_usd"], b["ceiling_usd"])
                    except Exception:
                        # An alerting failure must never take down the loop that finds
                        # the next one. Same rule as the breaker's notify seam.
                        log.debug("soft budget alert failed", exc_info=True)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("soft budget poll failed; continuing")
        await asyncio.sleep(config.BUDGET_SOFT_ALERT_INTERVAL_S)


app = FastAPI(
    title="Meter",
    description="The autonomous inference treasurer — metering proxy.",
    version="0.1.0",
    lifespan=lifespan,
)

# Shivam's treasury surface, folded in so the backend is one process on one port rather
# than the proxy plus a second app at repo root. Both packages already share `meter.db`;
# sharing the process removes the port nobody remembers and the second thing to start
# before a demo. `treasury.db` writes `wallets`/`treasury_events` to the same file the
# ledger uses — the proxy is no longer its sole writer, which WAL and `busy_timeout`
# handle (see treasury/db.py).
#
# Kept off the `/v1` prefix on purpose: `/v1` is the surface a caller's provider SDK
# targets, and control-plane routes do not belong in it.
# The judge console is a browser app on the dashboard's origin calling this API
# directly, which a browser refuses without the API saying so. Explicit origins rather
# than `*`: the console carries a session token in a header, and a wildcard would let any
# page on the internet drive somebody's live session.
#
# It does not widen anything for the API's normal callers -- a provider SDK is not a
# browser and never sends an Origin header, so CORS is inert on that path.
_ORIGINS = [o.strip() for o in config.CORS_ALLOW_ORIGINS.split(",") if o.strip()]


def _preview_origin_regex(origins: list[str]) -> str | None:
    """Allow *our* Vercel preview deployments, and nothing else on vercel.app.

    Every push gets a preview URL of the form `<project>-<hash>-<scope>.vercel.app`, so the
    console cannot be tested on a branch unless previews are allowed — which is why a regex
    exists here at all rather than a fixed list.

    It used to be `https://.*\\.vercel\\.app`, which matched **any** Vercel deployment. Since
    anyone can deploy to vercel.app in about a minute, that was the wildcard the comment
    above says it is avoiding — the exact hole, wearing a regex. The pattern is now built
    from the configured production origins, so it cannot match an unrelated project.

    ⚠ **It still matches `<project>-<anything>.vercel.app`, and it has to** — a preview URL
    is exactly that shape and the suffix is not predictable. Since `vercel.app` subdomains
    are first-come-first-served, someone can register `meter-three-beta-evil.vercel.app`
    and land inside the allowlist. That is why `CORS_ALLOW_VERCEL_PREVIEWS` gates this and
    defaults to **off** (PROPOSALS.md B22): with credentialed CORS on for the judge money
    capability, an allowed origin gets that cookie attached automatically, and the second
    factor protecting a leaked session token would travel somewhere an attacker owns.

    Prefer naming a specific preview URL in `CORS_ALLOW_ORIGINS` over turning this on.
    """
    projects = sorted(
        {
            host[: -len(".vercel.app")]
            for o in origins
            if (host := o.removeprefix("https://")).endswith(".vercel.app")
        }
    )
    if not projects:
        return None
    alts = "|".join(re.escape(p) for p in projects)
    return rf"https://(?:{alts})(?:-[a-z0-9]+)*\.vercel\.app"


# The judge money capability (PROPOSALS.md B20) is a cookie, and the console is on another
# origin, so it only ever arrives if credentialed CORS is on.
#
# It is off whenever origins are wildcarded, and that is not caution — it is the only
# coherent option. `Access-Control-Allow-Origin: *` with credentials is rejected outright
# by every browser, so a wildcard deployment that flipped this on would get *no* cookie and
# a console that fails on the money routes only, which reads as a backend bug. Refusing to
# combine them keeps the failure legible: wildcard origins mean no capability, and the
# money routes 403 with a message that says what to fix.
_ALLOW_CREDENTIALS = "*" not in _ORIGINS

app.add_middleware(
    CORSMiddleware,
    allow_origins=_ORIGINS,
    allow_origin_regex=(
        _preview_origin_regex(_ORIGINS)
        if config.CORS_ALLOW_VERCEL_PREVIEWS and "*" not in _ORIGINS
        else None
    ),
    allow_credentials=_ALLOW_CREDENTIALS,
    allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
    # Enumerated rather than `*`. These are every header the console and a provider SDK
    # actually send; `*` additionally reflects whatever a caller asks for, which is free
    # surface area for no benefit.
    allow_headers=[
        "authorization",
        "content-type",
        "anthropic-version",
        "x-api-key",
        "x-judge-session",
        "x-meter-actor",
        "x-meter-feature",
        "x-meter-provider",
        "x-meter-provider-key",
        "x-meter-request-id",
        "x-meter-trace",
    ],
    # The console reads the request id off the response to link a call to its ledger row.
    # A browser cannot see any response header that is not named here, so omitting this
    # made `x-meter-request-id` invisible to exactly the caller that wanted it.
    expose_headers=[
        "x-meter-request-id",
        "x-meter-overhead-ms",
        "x-meter-breaker-mode",
        "x-meter-breaker-scope",
        "x-meter-budget-scope",
        "x-meter-budget-spend-usd",
        "x-meter-budget-ceiling-usd",
    ],
    max_age=600,
)

app.include_router(treasury_routes.router)
app.include_router(mock_provider.router)

# The judge console's own surface (`/judge/*`), mounted for the same reason and kept off
# `/v1` for the same reason. Imported here rather than at module top so the dependency
# stays one-directional: `judge/` reads `proxy/`, never the reverse, and this mount is
# the single seam — the same exception `treasury` gets above.
from judge import routes as judge_routes  # noqa: E402

app.include_router(judge_routes.router)


# ─────────────────────────────────────────────────────────────────────────────
# Auth and attribution
# ─────────────────────────────────────────────────────────────────────────────


def _presented_key(request: Request) -> str | None:
    """Pull the Meter key out of whichever header the client's SDK uses.

    On a base-URL swap the SDK puts *its* configured key in the auth header, so that is
    where the Meter key has to go. The proxy substitutes the real provider key on the way
    out, which is what lets a caller point an SDK at Meter without Meter's operator ever
    holding the caller's provider credentials.

    Both header styles are accepted because an OpenAI SDK sends `Authorization: Bearer`
    and an Anthropic SDK sends `x-api-key`, and the whole promise is that neither has to
    be reconfigured beyond the base URL.
    """
    authorization = request.headers.get("authorization")
    if authorization and authorization.lower().startswith("bearer "):
        return authorization[7:].strip()
    api_key = request.headers.get("x-api-key")
    if api_key:
        return api_key.strip()
    return None


def _attribution(request: Request) -> dict[str, str | None]:
    """Rungs 1 and 2 of the README attribution ladder. All optional, all free."""
    return {
        "feature": request.headers.get("x-meter-feature"),
        "actor": request.headers.get("x-meter-actor"),
        "trace_id": request.headers.get("x-meter-trace"),
    }


def _error(
    status: int, message: str, code: str, request_id: str | None = None
) -> JSONResponse:
    """OpenAI-shaped error envelope.

    Clients pointed at Meter are running provider SDKs, and those SDKs parse
    `error.message` and `error.type`. Returning FastAPI's default `{"detail": ...}` would
    surface as an unhelpful generic exception inside the caller's own error handling.

    ``request_id`` is echoed when the request got far enough to have one. Refusals are the
    responses that most need it: a breaker trip and a budget 429 both write a ledger row,
    so the id is the link between the error a developer is looking at and the row that
    explains it. Omitted on the auth failures above it, where no row exists to point at.
    """
    response = JSONResponse(
        status_code=status,
        content={"error": {"message": message, "type": code, "code": code, "param": None}},
    )
    if request_id:
        response.headers["X-Meter-Request-Id"] = request_id
    return response


# ─────────────────────────────────────────────────────────────────────────────
# Routes
# ─────────────────────────────────────────────────────────────────────────────


def _learned_factor_count() -> int:
    """Number of correction factors the online loop has installed, for /healthz.

    Returns 0 rather than raising when the predictor is absent — this is a liveness
    endpoint, and it must not be the thing that breaks on a box with no tiktoken.
    """
    try:
        from predictor.engine import current_history

        return len(current_history())
    except Exception:
        return 0


@app.get("/", include_in_schema=False)
async def root(request: Request):
    """A landing page, because the first thing anyone does with a URL is open it.

    Nothing was mounted here, so a browser got FastAPI's `{"detail":"Not Found"}` — and
    this project is judged asynchronously from a link, where "Not Found" reads as a
    broken deployment rather than as "this is an API with no root route". A judge who
    bounces here never reaches the parts that work.

    Content-negotiated: a browser (Accept: text/html) gets the page, curl and SDKs get
    JSON, so nothing that scripts against this has to special-case the root.
    """
    endpoints = {
        "health": "/healthz",
        "api_docs": "/docs",
        "chat_completions": "/v1/chat/completions",
        "messages": "/v1/messages",
        "annotate": "/v1/annotate",
        "wallets": "/wallets",
        "mandates": "/mandates",
        "treasury_assess": "/treasury/assess",
        "treasury_events": "/treasury/events",
    }
    if "text/html" not in (request.headers.get("accept") or ""):
        return {"service": "meter-proxy", "status": "ok", "endpoints": endpoints,
                "docs": "https://github.com/ammarjiruwala/meter/blob/main/WALKTHROUGH.md"}

    links = "".join(
        f'<li><a href="{path}"><code>{path}</code></a> — {name.replace("_", " ")}</li>'
        for name, path in endpoints.items()
    )
    return HTMLResponse(
        "<!doctype html><meta charset=utf-8><title>Meter — proxy</title>"
        "<style>body{background:#08080C;color:#e8e8ef;font:15px/1.7 ui-sans-serif,system-ui;"
        "max-width:44rem;margin:12vh auto;padding:0 1.5rem}a{color:#a9a2ff}"
        "code{background:#16161f;padding:.1rem .35rem;border-radius:4px}"
        "li{margin:.2rem 0}h1{font-size:1.5rem;margin-bottom:.2rem}"
        ".m{color:#8a8a9a}</style>"
        "<h1>Meter — metering proxy</h1>"
        "<p class=m>This is the API, not the dashboard. Point an OpenAI SDK at this host "
        "and every call is metered, attributed and cost-predicted before it runs.</p>"
        f"<ul>{links}</ul>"
        "<p class=m>Start with <a href='/healthz'>/healthz</a>, or read the "
        "<a href='https://github.com/ammarjiruwala/meter/blob/main/WALKTHROUGH.md'>"
        "walkthrough</a> to run every feature yourself.</p>"
    )


def _breaker_overrides() -> int | None:
    """Per-project floor count for `/healthz`, or None if the ledger cannot answer.

    Swallows its own errors on purpose: `/healthz` is what a load balancer and UptimeRobot
    poll, and a liveness probe that 500s because one diagnostic field could not be read
    turns a degraded ledger into a dead service.
    """
    try:
        return db.count_breaker_overrides()
    except Exception:  # noqa: BLE001 — diagnostics must never fail liveness
        return None


@app.get("/healthz")
async def healthz() -> dict[str, Any]:
    """Liveness plus enough config echo to diagnose a misconfigured demo box fast."""
    return {
        "status": "ok",
        "pricing_version": config.PRICING_VERSION,
        "fail_mode": config.FAIL_MODE,
        "breaker": {
            "enabled": config.BREAKER_ENABLED,
            "mode": config.BREAKER_MODE,
            "window_s": config.BREAKER_WINDOW_S,
            # The *default* floor. Per-project overrides live in
            # `projects.breaker_floor_usd`, so this number may apply to nobody — hence the
            # count beside it (db.count_breaker_overrides explains why it is there).
            "threshold_usd": config.BREAKER_WINDOW_USD,
            "threshold_usd_scope": "default; per-project overrides may apply",
            "project_floor_overrides": _breaker_overrides(),
            "baseline_window_s": config.BREAKER_BASELINE_WINDOW_S,
            "burst_ratio": config.BREAKER_BURST_RATIO,
            # Surfaced so a misconfiguration is visible from a health check rather than
            # only from a log line someone missed at boot.
            "burst_ratio_ceiling": round(
                config.BREAKER_BASELINE_WINDOW_S / max(config.BREAKER_WINDOW_S, 1), 2
            ),
            "can_fire": (
                not config.BREAKER_ENABLED
                or config.BREAKER_BURST_RATIO <= 0
                or config.BREAKER_BURST_RATIO
                < config.BREAKER_BASELINE_WINDOW_S / max(config.BREAKER_WINDOW_S, 1)
            ),
        },
        "providers": {
            "openai": bool(config.OPENAI_API_KEY),
            "anthropic": bool(config.ANTHROPIC_API_KEY),
        },
        # A ceiling nobody can see is a ceiling nobody trusts. Surfacing the loaded set
        # makes "is meter.yaml actually being read?" answerable without a test request —
        # the question every misconfigured budget starts with.
        "budget": {
            "meter_yaml": str(config.METER_YAML_PATH),
            "meter_yaml_found": config.METER_YAML_PATH.exists(),
            "window_s": config.BUDGET_WINDOW_S,
            "ceilings": budget.active_ceilings(),
            "model_allowlists": budget.active_allowlists(),
            "soft_alert_ratio": (
                config.BUDGET_SOFT_ALERT_RATIO if config.BUDGET_SOFT_ALERT_ENABLED else None
            ),
            **budget.outstanding(),
        },
        "predictor": {
            "enabled": config.PREDICT_ENABLED,
            "available": predict is not None,
            "reservation_ttl_s": config.RESERVATION_TTL_S,
            # How many correction factors the online loop currently holds. Zero after a
            # cold start, and zero forever on traffic too varied for the gate to accept a
            # candidate — both are expected states, which is why this is reported rather
            # than assumed.
            "refresh_enabled": config.PREDICT_REFRESH_ENABLED,
            "learned_factors": _learned_factor_count(),
        },
        # Whether the Treasurer has backed off the payment rail (PROPOSALS.md C3). A
        # tripped Treasurer is not topping anything up, which is exactly the state you
        # want visible on a liveness check rather than buried in a log line.
        "treasurer": treasurer.trip_state(),
    }


@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    """OpenAI-shaped endpoint. The one the README's base-URL swap targets."""
    return await _proxy(request, providers.SHAPE_OPENAI)


@app.post("/v1/messages")
async def messages(request: Request):
    """Anthropic-native endpoint.

    Exists because the README promises a drop-in swap for every provider, and an
    Anthropic SDK will never call `/v1/chat/completions` — it calls this path with an
    `x-api-key` header. Without this route "every provider" means "OpenAI".
    """
    return await _proxy(request, providers.SHAPE_ANTHROPIC)


@app.post("/v1/annotate")
async def annotate(request: Request):
    """Attribution rung 3 — attach a business outcome to a trace.

        curl -X POST localhost:8080/v1/annotate -H 'Authorization: Bearer mk_...' \
             -d '{"trace_id":"tkt_9812","outcome":"resolved","value_usd":40}'

    The proxy structurally cannot know whether a support ticket was resolved, so this is
    how that fact gets in. `requests × annotations` on `trace_id` is then dollars per
    resolved outcome — the margin metric of ARCHITECTURE.md §4, and the difference
    between a cost tool and a margin tool.

    Owned by nobody in PLAN.md despite being documented in both README.md and
    ARCHITECTURE.md; picked up here because it is proxy surface (PROPOSALS.md B9).

    On `/v1` unlike the treasury routes: this is called by the caller's own application
    with the caller's own Meter key, so it belongs on the same surface as the proxied
    calls it annotates.
    """
    raw_key = _presented_key(request)
    if not raw_key:
        return _error(401, "Missing Meter key.", "authentication_error")
    try:
        key = await asyncio.to_thread(db.resolve_key, raw_key)
    except Exception:
        log.exception("ledger unreachable during annotate")
        return _error(503, "Ledger unreachable.", "service_unavailable")
    if key is None:
        return _error(401, "Unknown Meter key.", "authentication_error")
    if key.get("revoked_at"):
        # A revoked key is cut for writes too. Annotations are cheap, but they are still
        # writes against a credential someone deliberately killed.
        return _error(403, "This Meter key has been revoked.", "key_revoked")

    body = await _json_body(request)
    if body is None:
        return _error(400, "Request body must be a JSON object.", "invalid_request_error")

    trace_id = body.get("trace_id")
    if not isinstance(trace_id, str) or not trace_id.strip():
        return _error(
            400,
            "`trace_id` is required and must be a non-empty string.",
            "invalid_request_error",
        )
    trace_id = trace_id.strip()

    outcome = body.get("outcome")
    if outcome is not None and not isinstance(outcome, str):
        return _error(400, "`outcome` must be a string.", "invalid_request_error")

    value_usd = body.get("value_usd")
    if value_usd is not None:
        try:
            value_usd = float(value_usd)
        except (TypeError, ValueError):
            return _error(400, "`value_usd` must be a number.", "invalid_request_error")

    try:
        annotation_id = await asyncio.to_thread(
            db.record_annotation, key["project_id"], trace_id, outcome, value_usd
        )
        # Returned so the caller sees the cost-per-outcome number immediately instead of
        # having to query the ledger to find out what it just annotated.
        totals = await asyncio.to_thread(db.trace_cost, key["project_id"], trace_id)
    except Exception:
        log.exception("failed to record annotation for trace %s", trace_id)
        return _error(503, "Ledger unreachable.", "service_unavailable")

    margin_usd = None if value_usd is None else round(value_usd - totals["cost_usd"], 6)
    return JSONResponse(
        {
            "id": annotation_id,
            "trace_id": trace_id,
            "outcome": outcome,
            "value_usd": value_usd,
            "cost_usd": totals["cost_usd"],
            "request_count": totals["request_count"],
            # None when the caller sent no `value_usd` — the trace's cost is still known,
            # but its margin is not, and reporting 0 would read as "broke even".
            "margin_usd": margin_usd,
        }
    )


@app.post("/v1/breaker/reset")
async def breaker_reset(request: Request):
    """Manual breaker reset (ARCHITECTURE.md §6).

    Authenticated with the same Meter key as a proxied call, and scoped to that key's own
    project so one project cannot reset another's breaker.
    """
    raw_key = _presented_key(request)
    if not raw_key:
        return _error(401, "Missing Meter key.", "authentication_error")
    try:
        key = await asyncio.to_thread(db.resolve_key, raw_key)
    except Exception:
        log.exception("ledger unreachable during breaker reset")
        return _error(503, "Ledger unreachable.", "service_unavailable")
    if key is None:
        return _error(401, "Unknown Meter key.", "authentication_error")

    body = await _json_body(request) or {}
    scope = breaker.scope_for(key["project_id"], body.get("feature"))
    result = await asyncio.to_thread(
        breaker.reset, scope, key["key_id"], body.get("reset_by") or "manual"
    )
    return JSONResponse(result)


# ─────────────────────────────────────────────────────────────────────────────
# The proxy itself
# ─────────────────────────────────────────────────────────────────────────────


async def _json_body(request: Request) -> dict[str, Any] | None:
    raw = await request.body()
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        return None
    return parsed if isinstance(parsed, dict) else None


async def _proxy(request: Request, shape: str) -> Response:
    started = time.perf_counter()
    request_id = _request_id(request)

    # ── 1. AUTHENTICATE ──────────────────────────────────────────────────────
    raw_key = _presented_key(request)
    if not raw_key:
        return _error(
            401,
            "Missing Meter key. Send it as `Authorization: Bearer <key>` or `x-api-key`.",
            "authentication_error",
        )
    try:
        key = await asyncio.to_thread(db.resolve_key, raw_key)
    except Exception:
        # Deliberately NOT subject to FAIL_MODE. Fail-open exists so a ledger outage does
        # not take production down; it is not licence to serve traffic we cannot
        # authenticate. An unauthenticated call is an unbounded call against someone
        # else's provider key, which is a worse outcome than a 503.
        log.exception("ledger unreachable during authentication")
        return _error(503, "Ledger unreachable; cannot authenticate.", "service_unavailable")
    if key is None:
        return _error(401, "Unknown Meter key.", "authentication_error")

    body = await _json_body(request)
    if body is None:
        return _error(400, "Request body must be a JSON object.", "invalid_request_error")

    # ── 2. ATTRIBUTE ─────────────────────────────────────────────────────────
    tags = _attribution(request)
    model = body.get("model") if isinstance(body.get("model"), str) else None
    streaming = providers.is_streaming(body)

    # ── 2.5 MODEL ALLOWLIST ─────────────────────────────────────────────────
    # meter.yaml can restrict a feature to a set of models (`features.<name>.models`).
    # A request for a model outside it is refused before ESTIMATE — there is no point
    # spending tokens or holding budget on a call that cannot go out. Refusal is 403:
    # retrying will not help; the config file is the fix, and the header says what it
    # would have to contain.
    if not budget.models_allowed(key["project_id"], tags["feature"], model):
        allowed = budget.feature_models(key["project_id"], tags["feature"])
        response = _error(
            403,
            f"Feature {tags['feature']!r} may not call model {model!r}. "
            f"Allowlisted: {', '.join(sorted(allowed)) or '(none)'}.",
            "model_not_allowed",
            request_id,
        )
        response.headers["X-Meter-Feature"] = tags["feature"] or ""
        response.headers["X-Meter-Allowed-Models"] = ",".join(sorted(allowed))
        _schedule_capture(
            _row(
                request_id,
                key,
                tags,
                provider_name="-",
                model=model,
                endpoint=request.url.path,
                usage=Usage(),
                status=403,
                is_stream=streaming,
                latency_ms=0.0,
                ttft_ms=None,
                overhead_ms=(time.perf_counter() - started) * 1000,
                prompt_hash=providers.prompt_hash(shape, body),
                prediction=_predict(body, model, shape, key, tags),
            )
        )
        return response

    # ── 3. ESTIMATE ──────────────────────────────────────────────────────────
    prediction = _predict(body, model, shape, key, tags)

    # ── 4. BREAKER CHECK ─────────────────────────────────────────────────────
    # ARCHITECTURE.md §2 numbers RESERVE before BREAKER CHECK. Swapped deliberately: the
    # breaker is the cheaper check and it is a pure rejection, so running it first keeps
    # a revoked key or a throttled tag from taking — and immediately releasing — a budget
    # hold on every attempt. Nothing outside this function can observe the difference.
    try:
        decision = await asyncio.to_thread(breaker.check, key["project_id"], tags["feature"], key)
    except Exception:
        # This one IS subject to FAIL_MODE. Losing enforcement is a degradation; losing
        # availability is an outage, and README.md picks degradation by default.
        log.exception("breaker evaluation failed")
        if config.FAIL_MODE == "closed":
            return _error(503, "Ledger unreachable; failing closed.", "service_unavailable")
        decision = breaker.Decision(blocked=False)

    if decision.blocked:
        response = _error(
            decision.status_code, decision.detail, "circuit_breaker_open", request_id
        )
        response.headers["X-Meter-Breaker-Scope"] = decision.scope
        response.headers["X-Meter-Breaker-Mode"] = decision.mode
        if decision.status_code == 429:
            response.headers["Retry-After"] = str(config.BREAKER_COOLDOWN_S)
        # Blocked calls are ledgered too, at zero cost. A rejection is a fact about the
        # system's behaviour, and "the breaker held" needs to be provable from the same
        # table as everything else.
        _schedule_capture(
            _row(
                request_id,
                key,
                tags,
                provider_name="-",
                model=model,
                endpoint=request.url.path,
                usage=Usage(),
                status=decision.status_code,
                is_stream=streaming,
                latency_ms=0.0,
                ttft_ms=None,
                overhead_ms=(time.perf_counter() - started) * 1000,
                prompt_hash=providers.prompt_hash(shape, body),
                prediction=prediction,
            )
        )
        return response

    # ── 5. RESERVE ───────────────────────────────────────────────────────────
    # Holds the estimate against the daily ceiling before the call goes out, so a burst
    # of concurrent requests cannot each read the same under-ceiling total and all be
    # let through (ARCHITECTURE.md §2). Free when no meter.yaml is configured.
    #
    # Hold `bound_cost_usd`, not the forecast. It is an output guarantee only when an
    # explicit provider output cap is present; the uncapped p95 fallback is statistical.
    # Reserving the forecast would leak even more often. CAPTURE uses actual usage.
    try:
        budget_decision = await budget.authorize(
            key["project_id"],
            tags["feature"],
            getattr(prediction, "bound_cost_usd", None) or 0.0,
        )
    except Exception:
        # Same posture as the breaker: enforcement is degradable, availability is not.
        log.exception("budget authorization failed")
        if config.FAIL_MODE == "closed":
            return _error(503, "Ledger unreachable; failing closed.", "service_unavailable")
        budget_decision = budget.Decision(blocked=False)

    if budget_decision.blocked:
        response = _error(429, budget_decision.detail, "budget_exceeded", request_id)
        # Naming the ceiling that was hit is the difference between an actionable 429 and
        # a mystery: a project can have several, and the caller cannot see meter.yaml.
        response.headers["X-Meter-Budget-Scope"] = budget_decision.scope
        response.headers["X-Meter-Budget-Ceiling-Usd"] = f"{budget_decision.ceiling_usd:.2f}"
        response.headers["X-Meter-Budget-Spend-Usd"] = f"{budget_decision.spend_usd:.6f}"
        response.headers["Retry-After"] = str(config.BUDGET_WINDOW_S)
        _schedule_capture(
            _row(
                request_id,
                key,
                tags,
                provider_name="-",
                model=model,
                endpoint=request.url.path,
                usage=Usage(),
                status=429,
                is_stream=streaming,
                latency_ms=0.0,
                ttft_ms=None,
                overhead_ms=(time.perf_counter() - started) * 1000,
                prompt_hash=providers.prompt_hash(shape, body),
                prediction=prediction,
            )
        )
        return response

    reservation_id = budget_decision.reservation_id

    # ── 6. FORWARD ───────────────────────────────────────────────────────────
    # Past this point every exit path must release the hold, including the error ones —
    # a leaked hold counts against the ceiling until its TTL reaps it.
    try:
        provider_name = providers.route(model, shape, request.headers.get("x-meter-provider"))
        provider = providers.with_key(
            providers.providers()[provider_name],
            request.headers.get("x-meter-provider-key"),
        )
        if not provider.api_key:
            await budget.release(reservation_id)
            return _error(
                502,
                f"No upstream API key configured for provider {provider_name!r}.",
                "upstream_configuration_error",
            )

        outbound, injected_usage = providers.prepare_body(body, shape, streaming)
        url = providers.upstream_url(provider, providers.upstream_path(provider_name, shape))
        headers = providers.upstream_headers(provider, request.headers.items())

        common = dict(
            request_id=request_id,
            key=key,
            tags=tags,
            provider_name=provider_name,
            model=model,
            endpoint=request.url.path,
            started=started,
            prompt_hash=providers.prompt_hash(shape, body),
            prompt_chars=providers.prompt_chars(outbound),
            prediction=prediction,
            reservation_id=reservation_id,
        )

        if streaming:
            # The streamed path releases inside its own generator, after the last byte.
            return await _forward_stream(
                request.app.state.http,
                url,
                headers,
                outbound,
                shape,
                injected_usage,
                common,
            )
        return await _forward_unary(request.app.state.http, url, headers, outbound, shape, common)
    except Exception:
        await budget.release(reservation_id)
        raise


async def _forward_unary(
    client: httpx.AsyncClient,
    url: str,
    headers: dict[str, str],
    body: dict[str, Any],
    shape: str,
    common: dict[str, Any],
) -> Response:
    """Non-streamed call. Usage is in the response body, so capture is straightforward."""
    upstream_started = time.perf_counter()
    try:
        upstream = await client.post(url, headers=headers, json=body)
    except httpx.HTTPError as exc:
        return _upstream_failure(exc, common, upstream_started, is_stream=False)

    latency_ms = (time.perf_counter() - upstream_started) * 1000

    if upstream.status_code >= 400:
        # ARCHITECTURE.md §7: pass the error through, ledger it at zero cost. Meter must
        # not turn a provider's 429 into a Meter 500 — the caller's own retry logic is
        # keyed on the provider's status code.
        usage, status = Usage(), upstream.status_code
    else:
        try:
            payload: Any = json.loads(upstream.content)
        except (ValueError, UnicodeDecodeError):
            payload = None
        usage = providers.usage_from_response(shape, payload)
        status = upstream.status_code
        if not usage:
            usage = estimate_from_bytes(common["prompt_chars"], len(upstream.content))

    _schedule_capture(
        _row(
            common["request_id"],
            common["key"],
            common["tags"],
            common["provider_name"],
            common["model"],
            common["endpoint"],
            usage,
            status,
            is_stream=False,
            latency_ms=latency_ms,
            ttft_ms=latency_ms,
            overhead_ms=(time.perf_counter() - common["started"]) * 1000 - latency_ms,
            prompt_hash=common["prompt_hash"],
            prediction=common["prediction"],
            reservation_id=common["reservation_id"],
        ),
        release=common["reservation_id"],
    )

    response = Response(
        content=upstream.content,
        status_code=upstream.status_code,
        media_type=upstream.headers.get("content-type"),
    )
    _stamp(response, common["request_id"], common["started"], latency_ms)
    return response


async def _forward_stream(
    client: httpx.AsyncClient,
    url: str,
    headers: dict[str, str],
    body: dict[str, Any],
    shape: str,
    injected_usage: bool,
    common: dict[str, Any],
) -> Response:
    """Streamed call — the hard one.

    The stream is opened before the response object is constructed so the upstream status
    code and headers are known up front; the connection then stays open inside the body
    generator. Bytes are forwarded unbuffered as they arrive: buffering to parse the
    whole response first would destroy time-to-first-token, which is the number users
    actually feel.
    """
    upstream_started = time.perf_counter()
    upstream_request = client.build_request("POST", url, headers=headers, json=body)
    try:
        upstream = await client.send(upstream_request, stream=True)
    except httpx.HTTPError as exc:
        return _upstream_failure(exc, common, upstream_started, is_stream=True)

    if upstream.status_code >= 400:
        # An error response is small and not a stream. Drain it, close, and hand it back
        # as an ordinary body so the client's SDK sees the provider's own error shape.
        content = await upstream.aread()
        await upstream.aclose()
        latency_ms = (time.perf_counter() - upstream_started) * 1000
        _schedule_capture(
            _row(
                common["request_id"],
                common["key"],
                common["tags"],
                common["provider_name"],
                common["model"],
                common["endpoint"],
                Usage(),
                upstream.status_code,
                is_stream=True,
                latency_ms=latency_ms,
                ttft_ms=None,
                overhead_ms=(time.perf_counter() - common["started"]) * 1000 - latency_ms,
                prompt_hash=common["prompt_hash"],
                prediction=common["prediction"],
                reservation_id=common["reservation_id"],
            ),
            release=common["reservation_id"],
        )
        return Response(
            content=content,
            status_code=upstream.status_code,
            media_type=upstream.headers.get("content-type"),
        )

    tap = providers.StreamTap(shape, drop_injected_usage=injected_usage)
    state: dict[str, Any] = {
        "ttft_ms": None,
        "status": upstream.status_code,
        "last_heartbeat": time.monotonic(),
    }

    async def stream_body():
        try:
            # `aiter_bytes`, NOT `aiter_raw`. httpx puts `Accept-Encoding: gzip, deflate`
            # on every outbound request by default, and real providers honour it — so the
            # raw stream arrives gzipped. `aiter_raw` yields those compressed bytes, which
            # breaks this two ways at once: the tap parses gzip as SSE and finds no usage
            # (silently degrading every streamed row to a byte estimate), and we forward
            # compressed bytes while stripping `content-encoding` as hop-by-hop below, so
            # the client is handed gzip labelled as `text/event-stream` and cannot read the
            # stream at all. `aiter_bytes` decompresses, which is what the headers we send
            # actually promise. Keeping compression on the proxy↔provider hop is worth it;
            # it is the one that crosses the internet.
            async for chunk in upstream.aiter_bytes():
                if state["ttft_ms"] is None:
                    state["ttft_ms"] = (time.perf_counter() - upstream_started) * 1000

                # Heartbeat the reservation (ARCHITECTURE.md §2). Reservation TTLs are
                # short so a crashed worker releases its holds, but streams routinely run
                # for minutes — longer than the TTL — and a hold that expires mid-flight
                # fails *silently*: nothing raises, the ceiling simply stops counting the
                # largest request in the system. The stream is its own clock, so no
                # background task is needed; the monotonic compare is the cost.
                now = time.monotonic()
                if now - state["last_heartbeat"] >= config.RESERVATION_HEARTBEAT_S:
                    state["last_heartbeat"] = now
                    budget.extend(common["reservation_id"])

                forward = tap.feed(chunk)
                if forward:
                    yield forward
            tail = tap.flush()
            if tail:
                yield tail
        except (asyncio.CancelledError, GeneratorExit):
            # Client hung up mid-stream. The tokens were still generated and still
            # billed by the provider, so this is a capture path, not an error path.
            state["status"] = 499  # nginx's "client closed request"
            raise
        except httpx.HTTPError as exc:
            log.warning("stream aborted for %s: %s", common["request_id"], exc)
            state["status"] = 502
        finally:
            # Capture is scheduled BEFORE the connection is closed, and the ordering is
            # load-bearing. On a client disconnect this generator is being cancelled, so
            # the first `await` in here re-raises CancelledError and everything after it
            # is skipped. `_schedule_capture` is fully synchronous — it only hands a
            # coroutine to the loop — so putting it first is what guarantees the ledger
            # row survives exactly the case ARCHITECTURE.md §7 says must be captured.
            latency_ms = (time.perf_counter() - upstream_started) * 1000
            usage = tap.final_usage(common["prompt_chars"])
            _schedule_capture(
                _row(
                    common["request_id"],
                    common["key"],
                    common["tags"],
                    common["provider_name"],
                    common["model"],
                    common["endpoint"],
                    usage,
                    state["status"],
                    is_stream=True,
                    latency_ms=latency_ms,
                    ttft_ms=state["ttft_ms"],
                    overhead_ms=(time.perf_counter() - common["started"]) * 1000 - latency_ms,
                    prompt_hash=common["prompt_hash"],
                    prediction=common["prediction"],
                    reservation_id=common["reservation_id"],
                ),
                # Same reason `_schedule_capture` is first in this block: releasing here
                # is synchronous scheduling, so it survives the client-disconnect
                # cancellation that skips everything after the next `await`.
                release=common["reservation_id"],
            )

            # Shielded so the close still completes on the background task even though
            # this await is cancelled out from under us; otherwise the upstream socket
            # leaks on every disconnect. BaseException, not Exception, because the thing
            # being caught here is CancelledError.
            try:
                await asyncio.shield(asyncio.ensure_future(upstream.aclose()))
            except BaseException:  # noqa: BLE001 - see comment above
                pass

    response_headers = {k: v for k, v in upstream.headers.items() if k.lower() not in _HOP_BY_HOP}
    response = StreamingResponse(
        stream_body(),
        status_code=upstream.status_code,
        headers=response_headers,
        media_type=upstream.headers.get("content-type", "text/event-stream"),
    )
    response.headers["X-Meter-Request-Id"] = common["request_id"]
    # Overhead is not final until the stream ends, so the streamed variant reports only
    # the pre-flight cost (auth + breaker + routing). That is the number the proxy
    # actually controls; everything after it is the provider's latency.
    response.headers["X-Meter-Overhead-Ms"] = f"{(upstream_started - common['started']) * 1000:.2f}"
    return response


def _upstream_failure(
    exc: httpx.HTTPError,
    common: dict[str, Any],
    upstream_started: float,
    is_stream: bool,
) -> Response:
    """Upstream unreachable or timed out. Ledger it, then hand back a 502."""
    latency_ms = (time.perf_counter() - upstream_started) * 1000
    log.warning("upstream error for %s: %s", common["request_id"], exc)
    _schedule_capture(
        _row(
            common["request_id"],
            common["key"],
            common["tags"],
            common["provider_name"],
            common["model"],
            common["endpoint"],
            Usage(),
            502,
            is_stream=is_stream,
            latency_ms=latency_ms,
            ttft_ms=None,
            overhead_ms=(time.perf_counter() - common["started"]) * 1000 - latency_ms,
            prompt_hash=common["prompt_hash"],
            prediction=common["prediction"],
            reservation_id=common["reservation_id"],
        ),
        release=common["reservation_id"],
    )
    return _error(502, f"Upstream provider error: {exc}", "upstream_error")


# A client-supplied id has to be safe to use as a primary key and safe to log. Anything
# outside this is ignored rather than rejected: a malformed idempotency key is not a
# reason to refuse a request that is otherwise fine, and refusing would make adopting the
# header riskier than not sending it.
_CLIENT_REQUEST_ID = re.compile(r"^[A-Za-z0-9._:-]{8,128}$")


def _request_id(request: Request) -> str:
    """Use the caller's request id when they send one, otherwise mint one.

    `PROPOSALS.md` D1. Every major gateway lets a client name its own request —
    Helicone's `Helicone-Request-Id`, OpenRouter's `request_id` — because it is what makes
    a retry answerable: `db.record_request` is `INSERT OR REPLACE` on a caller-supplied id
    (B8), so a client that retries with the same id overwrites its own row instead of
    double-counting spend. Minting one internally, as this did before, means a retry storm
    inflates the ledger and nobody can prove which rows were duplicates.

    The value is echoed back on every response either way, so a caller that sends nothing
    still gets an id it can quote in a bug report.
    """
    supplied = (request.headers.get("x-meter-request-id") or "").strip()
    if supplied and _CLIENT_REQUEST_ID.match(supplied):
        return supplied
    return f"req_{uuid.uuid4().hex}"


def _stamp(response: Response, request_id: str, started: float, latency_ms: float) -> None:
    """Expose Meter's own overhead on every response.

    ARCHITECTURE.md §8 says to publish the overhead number. Putting it on the response
    means anyone can verify the claim from their own client without access to our
    dashboard, which is worth considerably more than the same number on a slide.
    """
    response.headers["X-Meter-Request-Id"] = request_id
    overhead = (time.perf_counter() - started) * 1000 - latency_ms
    response.headers["X-Meter-Overhead-Ms"] = f"{max(0.0, overhead):.2f}"


def _predict(
    body: dict[str, Any],
    model: str | None,
    shape: str,
    key: dict[str, Any] | None = None,
    tags: dict[str, str | None] | None = None,
) -> Any | None:
    """ESTIMATE — what will this call cost, before we make it (ARCHITECTURE.md §2).

    Returns None rather than raising, always. Three reasons a prediction is absent and
    all of them are normal: the model has no local tokenizer (Anthropic — predictor
    raises by design rather than guessing), the body is not a shape we can read, or the
    predictor hit an unexpected error. None of those are grounds for failing a request
    that would otherwise succeed. A missing prediction costs us one row of training
    data; a 500 costs the customer their traffic.

    Pure computation, no I/O — measured p50 0.031ms, roughly 0.6% of the 5ms pre-flight
    budget, so this does not meaningfully move the overhead number.

    `PREDICT_ENABLED=false` turns the whole step off: `predicted_*` columns stay NULL and
    reservations hold $0, which is exactly the pre-estimate behaviour. `predict` is the
    module-level guarded import — None when the predictor's dependencies are missing.
    """
    if not config.PREDICT_ENABLED or predict is None or not model:
        return None
    try:
        payload = body.get("messages")
        if not isinstance(payload, list) or not payload:
            # Anthropic's `system` is a sibling of `messages`, and completions-style
            # bodies use `prompt`. Fall back rather than mis-count.
            prompt = body.get("prompt")
            if not isinstance(prompt, str):
                return None
            payload = prompt
        max_tokens = body.get("max_completion_tokens", body.get("max_tokens"))
        response_format = None
        rf = body.get("response_format")
        if isinstance(rf, dict):
            response_format = rf.get("type")
        return predict(
            payload,
            model,
            max_tokens if isinstance(max_tokens, int) else None,
            response_format=response_format,
            request_extras={k: body[k] for k in ("tools", "functions", "response_format", "tool_choice")
                            if k in body},
            # Attribution keys the history correction (DESIGN.md §8), which is how the
            # estimator learns a given team's prompting style rather than assuming one
            # global average fits everybody.
            project=key.get("project_id") if key else None,
            feature=tags.get("feature") if tags else None,
            actor=tags.get("actor") if tags else None,
        )
    except Exception:
        # debug, not warning: an unsupported model is expected and would otherwise log
        # on every single Anthropic request.
        log.debug("prediction unavailable for model %r", model, exc_info=True)
        return None


def _row(
    request_id: str,
    key: dict[str, Any],
    tags: dict[str, str | None],
    provider_name: str,
    model: str | None,
    endpoint: str,
    usage: Usage,
    status: int,
    *,
    is_stream: bool,
    latency_ms: float,
    ttft_ms: float | None,
    overhead_ms: float,
    prompt_hash: str | None,
    prediction: Any | None = None,
    reservation_id: str | None = None,
) -> dict[str, Any]:
    """Build one ledger row, priced."""
    cost, pricing_version, estimated = price(usage, model or "", config.PRICING_VERSION)
    prediction = prediction or {}
    return {
        "id": request_id,
        "ts": db.now_iso(),
        "project_id": key["project_id"],
        "environment": key.get("environment"),
        "actor": tags["actor"],
        "feature": tags["feature"],
        "trace_id": tags["trace_id"],
        "provider": provider_name,
        "model": model,
        "endpoint": endpoint,
        "input_tokens": usage.input_tokens,
        "output_tokens": usage.output_tokens,
        "cache_read_tokens": usage.cache_read_tokens,
        "cache_write_tokens": usage.cache_write_tokens,
        "pricing_version": pricing_version,
        "cost_usd": cost,
        "latency_ms": round(latency_ms, 3),
        "ttft_ms": round(ttft_ms, 3) if ttft_ms is not None else None,
        "overhead_ms": round(max(0.0, overhead_ms), 3),
        "status": status,
        "is_stream": int(is_stream),
        "estimated": int(estimated),
        "prompt_hash": prompt_hash,
        # Non-NULL as of Phase 2. Reservations are held in-process rather than in the
        # Redis ARCHITECTURE.md §2 specifies (PROPOSALS.md A5), but the id is written
        # either way, so a row can be traced back to the hold that authorized it.
        "reservation_id": reservation_id,
        # CAPTURE — the prediction (a PredictionResult, or None) made before the call,
        # stored beside what actually happened. This pairing is the whole feedback loop:
        # predictor/learner.py refits from it, and predictor accuracy becomes a query
        # against the ledger rather than a claim in a README. NULL is meaningful — no
        # supported tokenizer, or prediction disabled — and must not be confused with a
        # prediction of zero.
        "predicted_output_tokens": getattr(prediction, "predicted_output_tokens", None),
        "predicted_cost_usd": getattr(prediction, "predicted_cost_usd", None),
        "bucket": getattr(prediction, "bucket", None),
        "prediction_method": getattr(prediction, "method", None),
        # The raw heuristic, before buffer/history/clamp. This is the fixed baseline
        # the learner fits against; fitting against predicted_output_tokens instead
        # divides by the previous correction each refresh and oscillates.
        "predicted_scope_tokens": getattr(prediction, "scope_tokens", None),
        # The ceiling, stored separately because it answers a different question and
        # is what the budget check should consult.
        "bound_output_tokens": getattr(prediction, "bound_output_tokens", None),
        "bound_cost_usd": getattr(prediction, "bound_cost_usd", None),
        "bound_is_hard": int(prediction.bound_is_hard) if prediction is not None else None,
        "history_factor": getattr(prediction, "history_factor", None),
    }


def _schedule_capture(row: dict[str, Any], release: str | None = None) -> None:
    """Write a ledger row without ever blocking the caller (ARCHITECTURE.md §2 step 7).

    ``release`` is the reservation this row settles, dropped once the write lands. The
    ordering is the whole point of authorize/capture: release before the row is in
    `requests` and this request's cost is counted by neither the hold nor the ledger, so
    a concurrent authorize sees headroom that does not exist. Scheduling the release here
    rather than at the call site is what keeps that ordering true — `_schedule_capture`
    only *starts* the write, so awaiting a release next to it would race the same gap.
    """

    async def write() -> None:
        try:
            await asyncio.to_thread(db.record_request, row)
        except Exception:
            # A failed ledger write must not become a failed request — the client already
            # has its bytes. Loud, because silently losing spend history is the one bug
            # that makes every downstream number wrong.
            log.exception("LEDGER WRITE FAILED for %s (spend not recorded)", row.get("id"))
        finally:
            # In `finally` so a failed write still frees the hold. The row is lost either
            # way; keeping the reservation on top of that would spend the ceiling twice.
            await budget.release(release)

    task = asyncio.create_task(write())
    _capture_tasks.add(task)
    task.add_done_callback(_capture_tasks.discard)
