#!/usr/bin/env python3
"""Self-check for the Meter proxy's non-trivial logic.

Run it directly, no test framework required:

    python tests/test_proxy.py

Deliberately does NOT test the FastAPI routes end to end. Those are mostly glue, and
testing them would mean standing up a fake upstream — meanwhile the places a bug actually
hides are all in here: prefix matching that mis-prices a model by 16x, an SSE parser that
loses usage when a chunk splits mid-line, a breaker that never re-closes.

Owner: Shubh (Proxy & Infra).
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
import tempfile
import uuid
from pathlib import Path

# Point the ledger at a throwaway Postgres SCHEMA before proxy.config is imported, so a
# test run can never touch the tables the demo and the judges are using. Under SQLite this
# was a tempfile; a hosted database needs the schema-level equivalent, and the run drops it
# at the end.
_TMP = tempfile.mkdtemp(prefix="meter-selfcheck-")
os.environ["DB_SCHEMA"] = "test_proxy_" + uuid.uuid4().hex[:8]
os.environ["METER_KEYS"] = "test_key_alpha:proj-alpha:test,test_key_beta:proj-beta:test"

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from proxy import breaker, config, db, pg, providers  # noqa: E402
from proxy.pricing import Usage, estimate_from_bytes, price  # noqa: E402

PASSED = 0


def check(label: str, condition: bool, detail: str = "") -> None:
    global PASSED
    if not condition:
        raise AssertionError(f"{label}{(' — ' + detail) if detail else ''}")
    PASSED += 1
    print(f"  ok  {label}")


# ─────────────────────────────────────────────────────────────────────────────
def test_routing() -> None:
    print("\nrouting")
    r = providers.route
    check(
        "claude model routes to anthropic",
        r("claude-sonnet-5", providers.SHAPE_OPENAI) == providers.ANTHROPIC,
    )
    check(
        "gpt model routes to openai", r("gpt-4o-mini", providers.SHAPE_OPENAI) == providers.OPENAI
    )
    check(
        "/v1/messages shape defaults to anthropic",
        r(None, providers.SHAPE_ANTHROPIC) == providers.ANTHROPIC,
    )
    check(
        "unknown model falls back to openai",
        r("some-finetune-v3", providers.SHAPE_OPENAI) == providers.OPENAI,
    )
    check(
        "explicit header overrides the model prefix",
        r("claude-sonnet-5", providers.SHAPE_OPENAI, "openai") == providers.OPENAI,
    )
    check(
        "bogus override falls back to the heuristic",
        r("claude-sonnet-5", providers.SHAPE_OPENAI, "not-a-provider") == providers.ANTHROPIC,
    )


def test_header_substitution() -> None:
    print("\nheader substitution")
    config.OPENAI_API_KEY = "sk-provider-secret"
    provider = providers.providers()[providers.OPENAI]
    client_headers = [
        ("authorization", "Bearer mk_dev_local"),  # the caller's METER key
        ("content-type", "application/json"),
        ("x-meter-feature", "summarize"),
        ("connection", "keep-alive"),
    ]
    out = providers.upstream_headers(provider, client_headers)
    check(
        "meter key never reaches the provider",
        out["Authorization"] == "Bearer sk-provider-secret",
        str(out),
    )
    check("content-type is forwarded", out.get("content-type") == "application/json")
    check("attribution headers are not leaked upstream", "x-meter-feature" not in out)
    check("hop-by-hop headers are dropped", "connection" not in out)

    anthropic = providers.providers()[providers.ANTHROPIC]
    config.ANTHROPIC_API_KEY = "sk-ant-secret"
    anthropic = providers.providers()[providers.ANTHROPIC]
    out = providers.upstream_headers(anthropic, client_headers)
    check("anthropic authenticates with x-api-key", out["x-api-key"] == "sk-ant-secret")
    check("anthropic-version is always set", "anthropic-version" in out)


def test_prepare_body() -> None:
    print("\nrequest shaping")
    body, injected = providers.prepare_body(
        {"model": "gpt-4o", "stream": True}, providers.SHAPE_OPENAI, streaming=True
    )
    check(
        "usage flag injected for openai streams",
        body["stream_options"]["include_usage"] is True and injected is True,
    )

    body, injected = providers.prepare_body(
        {"model": "gpt-4o", "stream": True, "stream_options": {"include_usage": True}},
        providers.SHAPE_OPENAI,
        streaming=True,
    )
    check("caller's own usage flag is left alone (chunk not dropped)", injected is False)

    body, injected = providers.prepare_body(
        {"model": "gpt-4o"}, providers.SHAPE_OPENAI, streaming=False
    )
    check("non-streamed bodies are untouched", "stream_options" not in body and injected is False)

    body, injected = providers.prepare_body(
        {"model": "claude-sonnet-5", "stream": True}, providers.SHAPE_ANTHROPIC, streaming=True
    )
    check("anthropic bodies are untouched", "stream_options" not in body and injected is False)


# ─────────────────────────────────────────────────────────────────────────────
def test_pricing() -> None:
    print("\npricing")
    cost, version, estimated = price(Usage(input_tokens=1_000_000), "gpt-4o")
    check("gpt-4o input priced from the yaml", cost == 2.50, f"got {cost}")
    check("pricing version is recorded", version == config.PRICING_VERSION)
    check("reported usage is not marked estimated", estimated is False)

    # The one that matters: shortest-prefix matching would price the mini model as the
    # full one, a 16x overstatement of the customer's bill.
    cost, _, _ = price(Usage(input_tokens=1_000_000), "gpt-4o-mini")
    check("gpt-4o-mini is NOT swallowed by the gpt-4o prefix", cost == 0.15, f"got {cost}")

    cost, _, _ = price(Usage(input_tokens=1_000_000), "gpt-4o-2024-11-20")
    check("dated snapshots resolve to their base model", cost == 2.50, f"got {cost}")

    # Anthropic kept Sonnet 5 at $2/$10 after its introductory period, so its cache
    # tiers remain 0.1x and 1.25x of $2.
    cost, _, _ = price(Usage(cache_read_tokens=1_000_000), "claude-sonnet-5")
    check("anthropic cache reads priced at 0.1x input", cost == 0.20, f"got {cost}")

    cost, _, _ = price(Usage(cache_write_tokens=1_000_000), "claude-sonnet-5")
    check("anthropic cache writes priced at 1.25x input", cost == 2.50, f"got {cost}")

    # Sonnet 4.6 keeps the standard $3 base, so it is the check that the two Sonnet
    # entries stayed distinct rather than one prefix-matching the other.
    cost, _, _ = price(Usage(cache_read_tokens=1_000_000), "claude-sonnet-4-6")
    check("sonnet 4.6 is not matched by the sonnet 5 entry", cost == 0.30, f"got {cost}")

    # OpenAI publishes no separate cache-write rate, so a cache write must fall back to
    # the plain input rate — NOT to zero, which would make every write look free.
    cost, _, _ = price(Usage(cache_write_tokens=1_000_000), "gpt-4o")
    check("openai cache writes fall back to the input rate", cost == 2.50, f"got {cost}")

    # Longest-prefix regressions that would misprice by 5x-25x if matching went shortest
    # -first. Every one of these pairs shares a prefix with the other.
    for model, expected in [
        ("gpt-5", 1.25),
        ("gpt-5-mini", 0.25),
        ("gpt-5-nano", 0.05),
        ("gpt-5.5", 5.00),
        ("gpt-5.5-pro", 30.00),
        ("gpt-5.4", 2.50),
        ("gpt-5.4-mini", 0.75),
        ("gpt-5.4-nano", 0.20),
        ("claude-opus-5", 5.00),
        ("claude-opus-4-1", 15.00),
        ("claude-haiku-4-5-20251001", 1.00),
        ("claude-fable-5", 10.00),
    ]:
        cost, _, estimated = price(Usage(input_tokens=1_000_000), model)
        check(f"{model} prices at ${expected}", cost == expected, f"got {cost}")
        check(f"{model} is a known model", estimated is False)

    # An unpriced model must not silently cost $0 and hide real spend.
    cost, _, estimated = price(Usage(input_tokens=1_000_000), "totally-unknown-model")
    check("unknown model uses default rates, not zero", cost > 0, f"got {cost}")
    check("unknown model is flagged estimated", estimated is True)

    usage = estimate_from_bytes(400, 200)
    check(
        "byte fallback yields ~4 chars per token",
        usage.input_tokens == 100 and usage.output_tokens == 50,
    )
    check("byte fallback is always flagged estimated", usage.estimated is True)


# ─────────────────────────────────────────────────────────────────────────────
OPENAI_STREAM = (
    b'data: {"id":"c1","choices":[{"delta":{"content":"Hel"}}]}\n\n'
    b'data: {"id":"c1","choices":[{"delta":{"content":"lo"}}]}\n\n'
    b'data: {"id":"c1","choices":[],"usage":{"prompt_tokens":120,"completion_tokens":8,'
    b'"prompt_tokens_details":{"cached_tokens":20}}}\n\n'
    b"data: [DONE]\n\n"
)

ANTHROPIC_STREAM = (
    b"event: message_start\n"
    b'data: {"type":"message_start","message":{"usage":{"input_tokens":95,'
    b'"cache_creation_input_tokens":10,"cache_read_input_tokens":25,"output_tokens":1}}}\n\n'
    b"event: content_block_delta\n"
    b'data: {"type":"content_block_delta","delta":{"text":"hi"}}\n\n'
    b"event: message_delta\n"
    b'data: {"type":"message_delta","usage":{"output_tokens":42}}\n\n'
)


def test_openai_stream() -> None:
    print("\nSSE parsing — openai")
    tap = providers.StreamTap(providers.SHAPE_OPENAI, drop_injected_usage=False)
    forwarded = tap.feed(OPENAI_STREAM) + tap.flush()
    check("all bytes forwarded when the caller asked for usage", forwarded == OPENAI_STREAM)
    check(
        "prompt tokens split from cached tokens",
        tap.usage.input_tokens == 100 and tap.usage.cache_read_tokens == 20,
        f"{tap.usage}",
    )
    check("completion tokens captured", tap.usage.output_tokens == 8)
    check("usage marked as reported, not estimated", tap.saw_usage is True)

    # Same stream, but Meter injected the flag — the caller must not see the extra chunk.
    tap = providers.StreamTap(providers.SHAPE_OPENAI, drop_injected_usage=True)
    forwarded = tap.feed(OPENAI_STREAM) + tap.flush()
    check("injected usage chunk stripped from the client's stream", b'"usage"' not in forwarded)
    check("content chunks still forwarded intact", b'"Hel"' in forwarded and b'"lo"' in forwarded)
    check("terminator still forwarded", b"[DONE]" in forwarded)
    check(
        "usage still captured after being stripped",
        tap.usage.input_tokens == 100 and tap.usage.output_tokens == 8,
    )

    # Anthropic's OpenAI-compatibility endpoint does NOT emit usage as a separate
    # content-free chunk the way OpenAI does — it merges usage into the final content
    # chunk, the one carrying finish_reason. This fixture is copied from a real response
    # observed on 2026-08-01. Dropping this chunk would strip the client's end-of-stream
    # signal, so it must be forwarded even though we injected the usage flag ourselves.
    compat = (
        b'data: {"id":"msg_1","choices":[{"index":0,"delta":{"content":"hi"}}],'
        b'"object":"chat.completion.chunk"}\n\n'
        b'data: {"id":"msg_1","choices":[{"index":0,"delta":{},"finish_reason":"stop"}],'
        b'"object":"chat.completion.chunk",'
        b'"usage":{"completion_tokens":13,"prompt_tokens":11,"total_tokens":24}}\n\n'
        b"data: [DONE]\n\n"
    )
    tap = providers.StreamTap(providers.SHAPE_OPENAI, drop_injected_usage=True)
    forwarded = tap.feed(compat) + tap.flush()
    check(
        "usage merged into a content chunk is still captured",
        tap.usage.input_tokens == 11 and tap.usage.output_tokens == 13,
        f"{tap.usage}",
    )
    check(
        "that chunk is NOT dropped — finish_reason must survive",
        b'"finish_reason":"stop"' in forwarded,
        forwarded.decode(),
    )
    check("compat stream is forwarded byte-identical", forwarded == compat)

    # The failure that only shows up on real networks: chunks do not arrive aligned to
    # SSE event boundaries. Feeding one byte at a time is the worst case.
    tap = providers.StreamTap(providers.SHAPE_OPENAI, drop_injected_usage=False)
    forwarded = b"".join(tap.feed(bytes([b])) for b in OPENAI_STREAM) + tap.flush()
    check("byte-at-a-time feed forwards identical bytes", forwarded == OPENAI_STREAM)
    check(
        "byte-at-a-time feed still captures usage",
        tap.usage.input_tokens == 100 and tap.usage.output_tokens == 8,
    )


def test_anthropic_stream() -> None:
    print("\nSSE parsing — anthropic")
    tap = providers.StreamTap(providers.SHAPE_ANTHROPIC)
    forwarded = tap.feed(ANTHROPIC_STREAM) + tap.flush()
    check("anthropic stream forwarded verbatim", forwarded == ANTHROPIC_STREAM)
    check("input tokens from message_start", tap.usage.input_tokens == 95)
    check("cache write tokens from message_start", tap.usage.cache_write_tokens == 10)
    check("cache read tokens from message_start", tap.usage.cache_read_tokens == 25)
    # message_start reports output_tokens=1 as a placeholder; message_delta carries the
    # real cumulative total. Reading only the first event undercounts output by ~40x.
    check(
        "output tokens taken from message_delta, not message_start",
        tap.usage.output_tokens == 42,
        f"got {tap.usage.output_tokens}",
    )

    tap = providers.StreamTap(providers.SHAPE_ANTHROPIC)
    for i in range(0, len(ANTHROPIC_STREAM), 7):
        tap.feed(ANTHROPIC_STREAM[i : i + 7])
    tap.flush()
    check(
        "split-chunk feed captures the same usage",
        tap.usage.input_tokens == 95 and tap.usage.output_tokens == 42,
    )


def test_truncated_stream() -> None:
    print("\nSSE parsing — client disconnect")
    truncated = OPENAI_STREAM[: OPENAI_STREAM.index(b'"usage"') - 40]
    tap = providers.StreamTap(providers.SHAPE_OPENAI)
    tap.feed(truncated)
    tap.flush()
    usage = tap.final_usage(prompt_chars=800)
    check("truncated stream still produces a usage record", bool(usage))
    check("truncated stream is flagged estimated", usage.estimated is True)
    cost, _, estimated = price(usage, "gpt-4o")
    check("truncated stream still costs money in the ledger", cost > 0, f"got {cost}")
    check("estimated flag survives pricing", estimated is True)


# ─────────────────────────────────────────────────────────────────────────────
def test_prompt_hash() -> None:
    print("\nprompt hashing")
    h = providers.prompt_hash
    base = {"model": "gpt-4o", "messages": [{"role": "user", "content": "Summarise this ticket"}]}
    reindented = {
        "model": "gpt-4o",
        "messages": [{"role": "user", "content": "Summarise   this\n\tticket"}],
    }
    check("whitespace differences hash the same", h("openai", base) == h("openai", reindented))

    jittered = dict(base, temperature=0.9)
    check(
        "a retry with jittered sampling params is the same prompt",
        h("openai", base) == h("openai", jittered),
    )

    other_model = dict(base, model="claude-sonnet-5")
    check(
        "same prompt to a different model is a different hash",
        h("openai", base) != h("openai", other_model),
    )

    different = {"model": "gpt-4o", "messages": [{"role": "user", "content": "Something else"}]}
    check("different prompts hash differently", h("openai", base) != h("openai", different))

    with_system = {
        "model": "claude-sonnet-5",
        "system": "You are terse.",
        "messages": [{"role": "user", "content": "hi"}],
    }
    without = {"model": "claude-sonnet-5", "messages": [{"role": "user", "content": "hi"}]}
    check(
        "anthropic system prompt is part of the hash",
        h("anthropic", with_system) != h("anthropic", without),
    )

    check("a body with no messages hashes to None", h("openai", {"model": "gpt-4o"}) is None)


# ─────────────────────────────────────────────────────────────────────────────
def _spend(project: str, feature: str | None, cost: float, n: int = 1, age_s: float = 0) -> None:
    """Write ledger rows. `age_s` backdates them, to build a trailing baseline."""
    for i in range(n):
        db.record_request(
            {
                "id": f"req_{project}_{feature}_{cost}_{i}_{os.urandom(4).hex()}",
                "ts": db.now_iso() if age_s <= 0 else db.iso_seconds_ago(age_s),
                "project_id": project,
                "environment": "test",
                "actor": None,
                "feature": feature,
                "trace_id": None,
                "provider": "openai",
                "model": "gpt-4o",
                "endpoint": "/v1/chat/completions",
                "input_tokens": 0,
                "output_tokens": 0,
                "cache_read_tokens": 0,
                "cache_write_tokens": 0,
                "pricing_version": "test",
                "cost_usd": cost,
                "latency_ms": 1.0,
                "ttft_ms": 1.0,
                "overhead_ms": 0.5,
                "status": 200,
                "is_stream": 0,
                "estimated": 0,
                "prompt_hash": None,
                "reservation_id": None,
            }
        )


def test_ledger() -> None:
    print("\nledger")
    db.connect()
    db.seed_keys(config.METER_KEYS)
    key = db.resolve_key("test_key_alpha")
    check(
        "seeded meter key resolves to its project",
        key is not None and key["project_id"] == "proj-alpha",
    )
    check("unknown key does not resolve", db.resolve_key("nope") is None)
    check(
        "meter keys are stored hashed, not in plaintext",
        db.hash_key("test_key_alpha") != "test_key_alpha",
    )

    _spend("proj-alpha", "summarize", 3.0, n=2)
    _spend("proj-alpha", "chat", 1.0)
    _spend("proj-alpha", None, 0.5)
    check(
        "feature-scoped window spend isolates features",
        abs(db.window_spend("proj-alpha", "summarize", 300) - 6.0) < 1e-9,
        str(db.window_spend("proj-alpha", "summarize", 300)),
    )
    # The isolation bug this suite was written to catch: untagged traffic is its own
    # scope, so a tagged feature's burn must not show up in it.
    check(
        "untagged scope counts only untagged rows",
        abs(db.window_spend("proj-alpha", None, 300) - 0.5) < 1e-9,
        str(db.window_spend("proj-alpha", None, 300)),
    )
    check(
        "project total sums every tag including untagged",
        abs(db.project_window_spend("proj-alpha", 300) - 7.5) < 1e-9,
        str(db.project_window_spend("proj-alpha", 300)),
    )
    check(
        "another project's spend is not counted", db.project_window_spend("proj-beta", 300) == 0.0
    )
    check("spend outside the window is excluded", db.project_window_spend("proj-alpha", 0) == 0.0)


def test_breaker() -> None:
    print("\ncircuit breaker")
    config.BREAKER_ENABLED = True
    config.BREAKER_WINDOW_S = 300
    config.BREAKER_WINDOW_USD = 5.0
    config.BREAKER_COOLDOWN_S = 120
    config.BREAKER_MODE = breaker.THROTTLE

    key = db.resolve_key("test_key_beta")
    scope = breaker.scope_for("proj-beta", "runaway")

    check(
        "quiet feature is not blocked", breaker.check("proj-beta", "runaway", key).blocked is False
    )

    _spend("proj-beta", "runaway", 6.0)
    decision = breaker.check("proj-beta", "runaway", key)
    check("breaker trips once the window threshold is cleared", decision.blocked is True)
    check("throttle mode returns 429, not 403", decision.status_code == 429)
    check(
        "trip records the numbers it compared",
        decision.metric is not None and decision.metric["threshold_usd"] == 5.0,
    )

    # Isolation: this is the whole point of throttle mode over revoke.
    check(
        "an untagged request on the same project still flows",
        breaker.check("proj-beta", None, key).blocked is False,
    )
    check(
        "a different feature on the same project still flows",
        breaker.check("proj-beta", "healthy", key).blocked is False,
    )

    check(
        "breaker stays shut during cooldown",
        breaker.check("proj-beta", "runaway", key).blocked is True,
    )

    # Half-open with the burst still in the window: must re-trip, not recover.
    config.BREAKER_COOLDOWN_S = 0
    check(
        "half-open re-trips while spend is still over threshold",
        breaker.check("proj-beta", "runaway", key).blocked is True,
    )

    # Half-open once the window has decayed below threshold: must recover on its own,
    # or the demo trips the breaker and never comes back.
    config.BREAKER_WINDOW_S = 0
    decision = breaker.check("proj-beta", "runaway", key)
    check("half-open closes itself once spend decays", decision.blocked is False)
    check("no open breaker event remains", db.active_breaker(scope) is None)

    # Manual reset always works (ARCHITECTURE.md §6).
    config.BREAKER_WINDOW_S = 300
    _spend("proj-beta", "runaway", 9.0)
    config.BREAKER_COOLDOWN_S = 120
    check(
        "breaker re-trips on fresh spend",
        breaker.check("proj-beta", "runaway", key).blocked is True,
    )
    breaker.reset(scope, key["key_id"], reset_by="selfcheck")
    check("manual reset clears the breaker", db.active_breaker(scope) is None)

    # Revoke mode cuts the key itself, not just the tag.
    config.BREAKER_MODE = breaker.REVOKE
    _spend("proj-beta", "leaked", 40.0)
    decision = breaker.check("proj-beta", "leaked", key)
    check("revoke mode returns 403", decision.status_code == 403, str(decision))
    revoked = db.resolve_key("test_key_beta")
    check("revoke mode actually revokes the key", revoked["revoked_at"] is not None)
    check(
        "a revoked key is blocked on every scope",
        breaker.check("proj-beta", "unrelated", revoked).blocked is True,
    )
    breaker.reset(breaker.scope_for("proj-beta", "leaked"), revoked["key_id"])
    check(
        "manual reset restores a revoked key", db.resolve_key("test_key_beta")["revoked_at"] is None
    )

    check(
        "disabling the breaker bypasses it entirely",
        _with_breaker_disabled("proj-beta", "runaway", db.resolve_key("test_key_beta")),
    )


def test_burst_detection() -> None:
    """The two-condition detector: absolute floor AND rate-vs-baseline burst.

    This suite exists because the floor alone has a specific, expensive failure mode —
    a feature that legitimately spends above the threshold trips the breaker every five
    minutes forever, and the operator's only fix is to raise the threshold until the
    breaker is useless for that project.
    """
    print("\ncircuit breaker — burst detection")
    config.BREAKER_ENABLED = True
    config.BREAKER_WINDOW_S = 300
    config.BREAKER_WINDOW_USD = 5.0
    config.BREAKER_BASELINE_WINDOW_S = 3600
    config.BREAKER_BURST_RATIO = 3.0
    config.BREAKER_COOLDOWN_S = 120
    config.BREAKER_MODE = breaker.THROTTLE

    key = db.resolve_key("test_key_alpha")

    # A tag with no history at all: every dollar of the hour is in the last 5 minutes,
    # so the ratio sits at its 12x ceiling. This is the leaked-key demo path — it must
    # still trip immediately, which is exactly what a literal two-absolute-threshold
    # port of the SRE pattern would have broken.
    _spend("proj-alpha", "leaked-key", 9.0)
    decision = breaker.check("proj-alpha", "leaked-key", key)
    check("a cold tag over the floor trips immediately", decision.blocked is True)
    check(
        "cold tag ratio is at the window ceiling",
        decision.metric["burst_ratio"] == decision.metric["burst_ratio_ceiling"],
        str(decision.metric),
    )
    breaker.reset(breaker.scope_for("proj-alpha", "leaked-key"))

    # A steady, legitimately expensive feature: $6 per 5 minutes, sustained for an hour.
    # It clears the floor on every single check and must never trip.
    # The +30s offset keeps bucket 1 clear of the 300s window edge. Without it, a row
    # landing exactly on the boundary lands inside or outside depending on microseconds of
    # clock drift between writing the row and computing the cutoff — which would make this
    # assertion pass or fail at random.
    _spend("proj-alpha", "steady", 6.0)
    for bucket in range(1, 12):
        _spend("proj-alpha", "steady", 6.0, age_s=bucket * 300 + 30)

    decision = breaker.check("proj-alpha", "steady", key)
    check(
        "steady spend over the floor does NOT trip", decision.blocked is False, str(decision.metric)
    )

    tripped, metric = breaker._evaluate("proj-alpha", "steady")
    check(
        "steady spend is recorded as such, not silently ignored",
        metric["result"] == "steady_spend_not_a_burst",
        str(metric),
    )
    check(
        "steady spend cleared the floor (so the floor alone would have tripped)",
        metric["window_spend_usd"] >= config.BREAKER_WINDOW_USD,
        str(metric),
    )
    check(
        "steady short-window rate ~= baseline rate",
        abs(metric["burst_ratio"] - 1.0) < 0.05,
        str(metric),
    )

    # Same feature, now with a genuine burst layered on top of that steady traffic.
    # Only the short window moves, so the ratio climbs past the threshold.
    _spend("proj-alpha", "steady", 30.0)
    decision = breaker.check("proj-alpha", "steady", key)
    check(
        "a burst on top of steady traffic DOES trip", decision.blocked is True, str(decision.metric)
    )
    check(
        "trip records both windows for auditability",
        decision.metric["baseline_spend_usd"] > decision.metric["window_spend_usd"],
        str(decision.metric),
    )
    breaker.reset(breaker.scope_for("proj-alpha", "steady"))

    # Setting the ratio to 0 reverts to the flat detector CONTEXT.md §5C specifies, so
    # the team can fall back without a code change if the burst check misbehaves live.
    config.BREAKER_BURST_RATIO = 0
    decision = breaker.check("proj-alpha", "steady", key)
    check(
        "burst_ratio=0 falls back to the flat floor detector",
        decision.blocked is True
        and decision.metric["result"] == "floor_cleared_burst_check_disabled",
        str(decision.metric),
    )
    breaker.reset(breaker.scope_for("proj-alpha", "steady"))
    config.BREAKER_BURST_RATIO = 3.0

    # Below the floor, the burst check must not even run — a tag that went from $0.01 to
    # $0.12 has a 12x ratio and is still spending nothing.
    _spend("proj-alpha", "tiny", 0.01)
    tripped, metric = breaker._evaluate("proj-alpha", "tiny")
    check("below the floor never trips regardless of ratio", tripped is False)
    check(
        "below-floor short-circuits before the second query",
        metric["result"] == "below_floor" and "burst_ratio" not in metric,
        str(metric),
    )


def test_revocation_fails_closed() -> None:
    """A revoked key must be blocked even when the ledger is unreachable.

    PROPOSALS.md B5 flagged that fail-open plus a breaker is contradictory: Redis/ledger
    down means no enforcement, during precisely the incident the breaker exists for. The
    revocation half is already safe by construction and this pins that down — the check
    reads `revoked_at` off the key resolved during authentication, so it never issues a
    query that could fail open.
    """
    print("\ncircuit breaker — revocation fails closed")
    config.BREAKER_ENABLED = True
    revoked_key = {"key_id": "mk_x", "project_id": "proj-gone", "revoked_at": db.now_iso()}

    # Patches the function the breaker actually calls. It used to be `window_spend`; the
    # breaker now reads everything it needs through `breaker_state`, and a test that
    # sabotages a function no longer on the path proves nothing.
    original = db.breaker_state

    def exploding_breaker_state(*args, **kwargs):
        raise RuntimeError("ledger unreachable")

    db.breaker_state = exploding_breaker_state
    try:
        decision = breaker.check("proj-gone", "anything", revoked_key)
        check("revoked key blocked with the ledger down", decision.blocked is True)
        check("revoked key returns 403, not 429", decision.status_code == 403)
        # And the breaker being disabled entirely must not resurrect a revoked key.
        config.BREAKER_ENABLED = False
        check(
            "revoked key stays blocked even with the breaker disabled",
            breaker.check("proj-gone", "anything", revoked_key).blocked is True,
        )
    finally:
        db.breaker_state = original
        config.BREAKER_ENABLED = True


def test_breaker_round_trip_count() -> None:
    """The breaker costs exactly ONE database round trip per request, in every branch.

    It used to cost two in the quiet case (`active_breaker`, then `window_spend`) and
    three once the floor was cleared, because `_evaluate` then went back for the baseline
    window. Round trips on the request path are sequential, so overhead is roughly
    count x RTT — against a hosted ledger each one is real latency on every single call,
    which is the whole argument in `proxy/README.md`.

    Counted at `pg._Connection.execute`, which is where a statement actually borrows a
    pooled connection and crosses the network. Counting calls to `db.breaker_state`
    instead would pass even if that function issued four queries internally.

    This is the kind of win that regresses silently: nothing fails, the number just goes
    back up.
    """
    print("\ncircuit breaker — round-trip count")
    config.BREAKER_ENABLED = True

    key = {"key_id": "mk_rt", "project_id": "proj-rt", "revoked_at": None,
           "breaker_floor_usd": 0.5}

    # Twelve $1 rows, one every 300s. The short window holds ~$1 at $1/300s and the
    # baseline holds $12 at $12/3600s — the same rate, so the floor is cleared but the
    # burst ratio is ~1 and the breaker does NOT trip. That is the branch that used to
    # cost three round trips, and it returns without writing, so the count stays clean.
    for i in range(12):
        db.record_request({
            "id": f"req-rt-{i}", "ts": db.iso_seconds_ago(i * 300 + 5),
            "project_id": "proj-rt", "feature": "alpha", "actor": "t",
            "model": "gpt-4o-mini", "provider": "openai",
            "endpoint": "/v1/chat/completions", "cost_usd": 1.0, "status": 200,
        })

    scope = breaker.scope_for("proj-rt", "alpha")
    open_row, short, base = db.breaker_state("proj-rt", "alpha", scope, 300, 3600)
    check("the folded read agrees with window_spend on the short window",
          abs(short - db.window_spend("proj-rt", "alpha", 300)) < 1e-9,
          f"{short} vs {db.window_spend('proj-rt', 'alpha', 300)}")
    check("and on the baseline window",
          abs(base - db.window_spend("proj-rt", "alpha", 3600)) < 1e-9,
          f"{base} vs {db.window_spend('proj-rt', 'alpha', 3600)}")
    check("and agrees with active_breaker that nothing is open",
          open_row is None and db.active_breaker(scope) is None)

    # Untagged must still mean `feature IS NULL` and not the project total. Folding two
    # queries into one CASE is exactly where that distinction gets quietly lost.
    db.record_request({
        "id": "req-rt-untagged", "ts": db.now_iso(), "project_id": "proj-rt",
        "feature": None, "actor": "t", "model": "gpt-4o-mini", "provider": "openai",
        "endpoint": "/v1/chat/completions", "cost_usd": 0.07, "status": 200,
    })
    _, untagged_short, _ = db.breaker_state(
        "proj-rt", None, breaker.scope_for("proj-rt", None), 300, 3600)
    check("untagged means feature IS NULL, not the project total",
          abs(untagged_short - 0.07) < 1e-9, str(untagged_short))

    statements: list[str] = []
    real_execute = pg._Connection.execute

    def counting(self, sql, params=()):
        statements.append(sql)
        return real_execute(self, sql, params)

    pg._Connection.execute = counting
    try:
        statements.clear()
        d = breaker.check("proj-rt", "alpha", key)
        check("over the floor but steady: one round trip", len(statements) == 1,
              f"{len(statements)} statements")
        # metric is None on a clean pass, so it cannot be dereferenced here — check()
        # evaluates its detail argument eagerly, pass or fail.
        check("and it does not trip on steady spend", d.blocked is False)

        # A tag far below the floor short-circuits before the burst check. Same count:
        # the fold must not have turned the cheap path into the expensive one.
        statements.clear()
        d = breaker.check("proj-rt", "quiet-tag", key)
        check("below the floor: one round trip", len(statements) == 1,
              f"{len(statements)} statements")
        check("and stays closed", d.blocked is False)

        # An already-open breaker in cooldown answers from the same single read.
        db.open_breaker(breaker.scope_for("proj-rt", "cooling"), "throttle",
                        {"detector": "test"})
        statements.clear()
        d = breaker.check("proj-rt", "cooling", key)
        check("open and cooling down: one round trip", len(statements) == 1,
              f"{len(statements)} statements")
        check("and it blocks", d.blocked is True)
    finally:
        pg._Connection.execute = real_execute


def test_key_cache() -> None:
    """Caching the auth lookup must not weaken revocation.

    `resolve_key` is the one query every request makes, so at a hosted ledger's RTT it is
    the largest fixed cost on the path. Caching it is worth a round trip per request — but
    `revoked_at` is explicitly NOT subject to fail-open (CLAUDE.md), so a cache that let a
    cut key keep working would be trading the wrong thing.

    The checks below pin both halves: the cache saves the trip, and every in-process path
    that changes the answer purges it synchronously.
    """
    print("\nmeter key cache")
    db.seed_keys("mk_cache_probe:proj-cache:dev")
    db.invalidate_key_cache()

    statements: list[str] = []
    real_execute = pg._Connection.execute

    def counting(self, sql, params=()):
        statements.append(sql)
        return real_execute(self, sql, params)

    real_ttl = config.KEY_CACHE_TTL_S
    pg._Connection.execute = counting
    try:
        config.KEY_CACHE_TTL_S = 60.0

        statements.clear()
        first = db.resolve_key("mk_cache_probe")
        check("a cold lookup hits the database", len(statements) == 1, str(len(statements)))
        check("and resolves the project", first["project_id"] == "proj-cache")

        statements.clear()
        second = db.resolve_key("mk_cache_probe")
        check("a warm lookup makes no round trip at all", statements == [],
              str(len(statements)))
        check("and returns the same answer", second["project_id"] == "proj-cache")

        # A copy each time, or one request could edit what every later request
        # authenticates against.
        second["project_id"] = "tampered"
        check("the cache hands out copies, not its own dict",
              db.resolve_key("mk_cache_probe")["project_id"] == "proj-cache")

        # The property that matters. Not "revocation is visible within the TTL" —
        # visible on the very next call.
        statements.clear()
        db.revoke_key(first["key_id"])
        revoked = db.resolve_key("mk_cache_probe")
        check("revocation is visible immediately, not after the TTL",
              revoked["revoked_at"] is not None, str(revoked))
        check("because revoking purged the cache and forced a re-read",
              any("SELECT" in st for st in statements))

        db.unrevoke_key(first["key_id"])
        check("and un-revoking is visible immediately too",
              db.resolve_key("mk_cache_probe")["revoked_at"] is None)

        # `resolve_key` joins `projects`, so a floor change must invalidate as well —
        # this is the judge flow, where the floor is set after the key already exists.
        db.set_breaker_floor("proj-cache", 0.25)
        check("a breaker floor change is visible immediately",
              db.resolve_key("mk_cache_probe")["breaker_floor_usd"] == 0.25)
        db.set_breaker_floor("proj-cache", None)

        # A miss must never be cached: a judge mints a key and uses it moments later.
        check("an unknown key does not resolve", db.resolve_key("mk_not_yet") is None)
        db.seed_keys("mk_not_yet:proj-cache:dev")
        check("and a key created after that miss resolves at once, not after the TTL",
              db.resolve_key("mk_not_yet") is not None)

        # Expired entries must LEAVE, not merely be ignored on read. A judge session
        # mints a key each, so a map that only ignores stale entries grows for the life
        # of the process — the same accumulation that left 228 ceilings registered.
        db.invalidate_key_cache()
        config.KEY_CACHE_TTL_S = 0.01
        for i in range(db._KEY_CACHE_SWEEP_AT + 4):
            db.seed_keys(f"mk_sweep_{i}:proj-cache:dev")
            db.resolve_key(f"mk_sweep_{i}")
        time.sleep(0.05)
        config.KEY_CACHE_TTL_S = 60.0
        db.resolve_key("mk_cache_probe")   # one insert past the threshold sweeps
        check("expired entries are evicted, not just ignored",
              len(db._key_cache) < db._KEY_CACHE_SWEEP_AT,
              f"{len(db._key_cache)} entries still cached")

        # Escape hatch: TTL 0 means pay the trip every time.
        config.KEY_CACHE_TTL_S = 0
        db.invalidate_key_cache()
        db.resolve_key("mk_cache_probe")
        statements.clear()
        db.resolve_key("mk_cache_probe")
        check("TTL 0 disables the cache entirely", len(statements) == 1,
              str(len(statements)))
    finally:
        pg._Connection.execute = real_execute
        config.KEY_CACHE_TTL_S = real_ttl
        db.invalidate_key_cache()


def test_model_efficiency() -> None:
    """The cross-model comparison the dashboard renders (PLAN.md Phase 3, B11).

    Written offline by `scripts/cross_model.py --push`, never from the request path:
    shadow-calling a second provider on live traffic doubles the customer's bill inside a
    tool whose job is to control it, and the comparison does not need it.

    The property that matters beyond "it stores rows" is that a re-push replaces a run
    rather than doubling it. A push that dies halfway gets retried, and a doubled run does
    not error — it silently halves every per-call cost the card displays.
    """
    print("\ncross-model efficiency")
    rows = [
        {"feature": "sql-from-question", "model": "claude-haiku-4-5",
         "baseline_model": "gpt-4o-mini", "samples": 12,
         "input_tokens": 900, "output_tokens": 260,
         "baseline_input_tokens": 900, "baseline_output_tokens": 400,
         "cost_usd": 0.0022, "baseline_cost_usd": 0.0004,
         "pricing_version": "2026-08-01"},
        {"feature": "ticket-summary", "model": "claude-haiku-4-5",
         "baseline_model": "gpt-4o-mini", "samples": 9,
         "input_tokens": 1200, "output_tokens": 300,
         "baseline_input_tokens": 1200, "baseline_output_tokens": 300,
         "cost_usd": 0.0025, "baseline_cost_usd": 0.0005,
         "pricing_version": "2026-08-01"},
    ]

    check("nothing to show before a run", db.latest_model_efficiency() == [])

    written = db.record_model_efficiency("cm_first", rows)
    check("a run stores one row per feature", written == 2, str(written))

    got = db.latest_model_efficiency()
    check("and reads back", len(got) == 2, str(len(got)))
    check("ordered by feature", [r["feature"] for r in got]
          == ["sql-from-question", "ticket-summary"], str([r["feature"] for r in got]))
    check("with both sides of the comparison kept apart",
          got[0]["baseline_output_tokens"] == 400 and got[0]["output_tokens"] == 260)

    # The decomposition the card exists to show: same tokens, different rate; or same
    # rate, different verbosity. Conflating them gives a number nobody can act on.
    verbosity = got[0]["output_tokens"] / got[0]["baseline_output_tokens"]
    check("verbosity is derivable and below 1 here", abs(verbosity - 0.65) < 1e-9,
          str(verbosity))
    check("while cost is still several times higher — a rate effect, not a length one",
          got[0]["cost_usd"] > got[0]["baseline_cost_usd"] * 4)

    # Re-pushing the SAME run replaces it. A doubled run does not raise; it silently
    # halves every per-call figure on the card.
    db.record_model_efficiency("cm_first", rows)
    check("re-pushing a run replaces it rather than doubling",
          len(db.latest_model_efficiency()) == 2,
          str(len(db.latest_model_efficiency())))

    # A newer run wins outright — the card shows one comparison, not a merge of every
    # comparison ever made against different models.
    db.record_model_efficiency("cm_second", [dict(rows[0], feature="only-this")])
    latest = db.latest_model_efficiency()
    check("the newest run is the one served", [r["feature"] for r in latest] == ["only-this"],
          str([r["feature"] for r in latest]))


def test_soft_breaches_is_one_round_trip() -> None:
    """The soft-budget poll must not scale its query count with the ceiling count.

    It asked per configured ceiling, inside a loop. With ceilings coming only from
    meter.yaml that was a handful of queries a minute and invisible. Judge sessions
    register theirs at runtime, and the deployed instance reached 228 — 228 sequential
    round trips every poll, at a measured ~85 ms each, which is far more work than the
    60-second interval allows and grows with every judge who ever visits.

    Nothing failed while that was true. It just got slower forever, which is why the check
    is on the query COUNT rather than on the result.
    """
    print("\nsoft budget poll — round-trip count")
    from proxy import budget

    budget._ceilings.clear()
    budget._file_ceilings.clear()
    try:
        for i in range(12):
            pid = f"proj-soft-{i}"
            budget.register_ceilings({(pid, None): 1.0, (pid, "alpha"): 0.5})
            db.record_request({
                "id": f"req-soft-{i}", "ts": db.now_iso(), "project_id": pid,
                "feature": "alpha", "actor": "t", "model": "gpt-4o-mini",
                "provider": "openai", "endpoint": "/v1/chat/completions",
                "cost_usd": 0.9, "status": 200,
            })

        check("24 ceilings are configured", len(budget.active_ceilings()) == 24,
              str(len(budget.active_ceilings())))

        statements: list[str] = []
        real_execute = pg._Connection.execute

        def counting(self, sql, params=()):
            statements.append(sql)
            return real_execute(self, sql, params)

        pg._Connection.execute = counting
        try:
            breaches = budget.soft_breaches(0.8)
        finally:
            pg._Connection.execute = real_execute

        check("24 ceilings still cost exactly one round trip", len(statements) == 1,
              f"{len(statements)} statements for 24 ceilings")
        # The fold must not have changed which number each scope is compared against.
        by_scope = {b["scope"]: b for b in breaches}
        check("a feature ceiling is measured on that feature",
              abs(by_scope["feature:proj-soft-0/alpha"]["spend_usd"] - 0.9) < 1e-9,
              str(by_scope.get("feature:proj-soft-0/alpha")))
        check("a project ceiling is measured on the whole project",
              abs(by_scope["project:proj-soft-0"]["spend_usd"] - 0.9) < 1e-9,
              str(by_scope.get("project:proj-soft-0")))
        check("every breaching scope is reported", len(breaches) == 24, str(len(breaches)))

        # Untagged spend counts towards the project ceiling but not a feature's.
        db.record_request({
            "id": "req-soft-untagged", "ts": db.now_iso(), "project_id": "proj-soft-0",
            "feature": None, "actor": "t", "model": "gpt-4o-mini", "provider": "openai",
            "endpoint": "/v1/chat/completions", "cost_usd": 0.05, "status": 200,
        })
        again = {b["scope"]: b for b in budget.soft_breaches(0.8)}
        check("untagged spend raises the project total",
              abs(again["project:proj-soft-0"]["spend_usd"] - 0.95) < 1e-9,
              str(again["project:proj-soft-0"]))
        check("but leaves the feature's own number alone",
              abs(again["feature:proj-soft-0/alpha"]["spend_usd"] - 0.9) < 1e-9,
              str(again["feature:proj-soft-0/alpha"]))
    finally:
        budget._ceilings.clear()
        budget._file_ceilings.clear()


def test_runtime_ceilings_are_reclaimed() -> None:
    """A runtime tenant's ceilings go when its session does — but a file's never do.

    `register_ceilings` only ever added, so `_ceilings` grew with every judge session and
    never shrank. An expired four-hour session was still being measured days later, and
    `soft_breaches` walked it on every poll.
    """
    print("\nruntime ceilings are reclaimed")
    from proxy import budget

    budget._ceilings.clear()
    budget._file_ceilings.clear()
    try:
        budget.register_ceilings({("judge-gone", None): 0.5,
                                  ("judge-gone", "alpha"): 0.1})
        budget.register_ceilings({("file-proj", None): 3.0})
        budget._file_ceilings.add(("file-proj", None))

        check("both tenants are registered", len(budget.active_ceilings()) == 3)

        dropped = budget.forget_project("judge-gone")
        check("the runtime tenant's ceilings are dropped", dropped == 2, str(dropped))
        check("and are gone from the active set",
              "project:judge-gone" not in budget.active_ceilings())

        # The important half. A runtime caller cannot widen a file ceiling; it must not be
        # able to delete one through a different door either.
        check("a meter.yaml ceiling is refused", budget.forget_project("file-proj") == 0)
        check("and is still enforced",
              budget.active_ceilings().get("project:file-proj") == 3.0)

        check("forgetting an unknown project is a no-op",
              budget.forget_project("never-existed") == 0)
    finally:
        budget._ceilings.clear()
        budget._file_ceilings.clear()


def test_gzipped_upstream_stream() -> None:
    """A compressed SSE stream must still be parsed AND forwarded readably.

    This is the one test in this file that talks over a real socket, and it exists because
    a fake upstream that does not compress cannot catch the bug it guards:

    httpx puts `Accept-Encoding: gzip, deflate` on every outbound request by default, and
    real providers honour it. Reading the response with `aiter_raw()` yields the still-
    compressed bytes, which fails twice over — the tap parses gzip as SSE, finds no usage,
    and silently downgrades the row to a byte estimate; and the proxy forwards compressed
    bytes while stripping `content-encoding` as hop-by-hop, handing the client gzip
    labelled `text/event-stream`. Both were invisible to every other test here, and both
    would have surfaced live.
    """
    print("\ngzipped upstream stream")
    import gzip
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    payload = gzip.compress(OPENAI_STREAM)

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            self.rfile.read(int(self.headers.get("content-length", 0) or 0))
            self.send_response(200)
            self.send_header("content-type", "text/event-stream")
            self.send_header("content-encoding", "gzip")
            self.send_header("content-length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *a):  # silence the default stderr access log
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()

    async def drive():
        import httpx
        from httpx import ASGITransport
        from proxy.app import app as proxy_app

        config.OPENAI_BASE_URL = f"http://127.0.0.1:{port}/v1"
        config.OPENAI_API_KEY = "sk-fake"
        config.BREAKER_ENABLED = False

        async with httpx.AsyncClient(
            transport=ASGITransport(app=proxy_app), base_url="http://meter"
        ) as client:
            proxy_app.state.http = httpx.AsyncClient(timeout=30)
            body = b""
            async with client.stream(
                "POST",
                "/v1/chat/completions",
                headers={
                    "Authorization": "Bearer test_key_alpha",
                    "X-Meter-Feature": "gzip-stream",
                },
                json={
                    "model": "gpt-4o",
                    "stream": True,
                    "messages": [{"role": "user", "content": "hi"}],
                },
            ) as resp:
                status = resp.status_code
                async for chunk in resp.aiter_bytes():
                    body += chunk
            await asyncio.sleep(0.6)  # let the capture task land
            await proxy_app.state.http.aclose()
            return status, body

    import asyncio

    status, body = asyncio.run(drive())
    server.shutdown()

    check("gzipped upstream stream returns 200", status == 200, str(status))
    check(
        "client receives DECOMPRESSED SSE, not gzip bytes",
        body.startswith(b"data:"),
        repr(body[:40]),
    )
    check(
        "content survived decompression intact",
        b'"Hel"' in body and b"[DONE]" in body,
        repr(body[:200]),
    )

    row = [r for r in _all_rows() if r["feature"] == "gzip-stream"]
    check("gzipped stream produced a ledger row", len(row) == 1, str(len(row)))
    if row:
        r = row[0]
        check(
            "usage parsed from the COMPRESSED stream (not byte-estimated)",
            r["estimated"] == 0,
            str(dict(r)),
        )
        check(
            "real token counts recovered through gzip",
            r["input_tokens"] == 100 and r["output_tokens"] == 8 and r["cache_read_tokens"] == 20,
            str(dict(r)),
        )


# ─────────────────────────────────────────────────────────────────────────────
def test_meter_yaml() -> None:
    """The loader, including the malformed inputs that must not disable enforcement quietly."""
    print("\nmeter.yaml loader")
    import textwrap

    from proxy import budget

    path = Path(_TMP) / "meter.yaml"
    path.write_text(
        textwrap.dedent("""
        projects:
          proj-alpha:
            ceiling_usd_per_day: 100
            features:
              summarize: { ceiling_usd_per_day: 10 }
              chat:      { ceiling_usd_per_day: 50 }
          proj-beta:
            ceiling_usd_per_day: 5
    """)
    )
    loaded = budget.load_meter_yaml(path)
    check("both projects loaded", loaded == 2, str(loaded))
    ceilings = budget.active_ceilings()
    check("project ceiling registered", ceilings.get("project:proj-alpha") == 100.0)
    check("feature ceiling registered", ceilings.get("feature:proj-alpha/summarize") == 10.0)
    check("second project's ceiling registered", ceilings.get("project:proj-beta") == 5.0)

    # features.<name>.models allowlist: parsed from the same file, enforced per feature.
    path.write_text(
        textwrap.dedent("""
        projects:
          proj-alpha:
            ceiling_usd_per_day: 100
            features:
              summarize: { ceiling_usd_per_day: 10, models: [claude-haiku, gpt-4o-mini] }
              chat:      { ceiling_usd_per_day: 50 }
    """)
    )
    budget.load_meter_yaml(path)
    allowed = budget.feature_models("proj-alpha", "summarize")
    check("allowlist parsed", allowed == frozenset({"claude-haiku", "gpt-4o-mini"}), str(allowed))
    check("on the list passes", budget.models_allowed("proj-alpha", "summarize", "claude-haiku"))
    check(
        "off the list is refused",
        not budget.models_allowed("proj-alpha", "summarize", "gpt-4o"),
        "gpt-4o is not on the allowlist",
    )
    check("no allowlist means unrestricted", budget.models_allowed("proj-alpha", "chat", "gpt-4o"))
    check(
        "project-scoped call has nothing to check",
        budget.models_allowed("proj-alpha", None, "gpt-4o"),
    )
    check("no model has nothing to check", budget.models_allowed("proj-alpha", "summarize", None))
    check("unknown feature is unrestricted", budget.models_allowed("proj-alpha", "nope", "gpt-4o"))

    # A malformed allowlist must not disable the whole file — same posture as a bad ceiling.
    path.write_text(
        textwrap.dedent("""
        projects:
          proj-alpha:
            ceiling_usd_per_day: 100
            features:
              summarize: { models: "not-a-list" }
    """)
    )
    budget.load_meter_yaml(path)
    check(
        "malformed allowlist is ignored, ceiling still enforced",
        budget.active_ceilings().get("project:proj-alpha") == 100.0
        and budget.feature_models("proj-alpha", "summarize") == frozenset(),
        str(budget.active_allowlists()),
    )

    # Re-loading must not accumulate. An operator lowering a ceiling and restarting has to
    # get the lower number, not the union of every ceiling the file ever had.
    path.write_text("projects:\n  proj-alpha:\n    ceiling_usd_per_day: 42\n")
    budget.load_meter_yaml(path)
    check(
        "reload replaces rather than merges",
        budget.active_ceilings().get("project:proj-alpha") == 42.0,
        str(budget.active_ceilings()),
    )
    # The half that upserting would get wrong: a ceiling DELETED from the file has to stop
    # being enforced, or a limit lives on that appears nowhere in the repo — the exact
    # failure budget-as-code exists to prevent.
    check(
        "a feature ceiling removed from the file stops being enforced",
        "feature:proj-alpha/summarize" not in budget.active_ceilings(),
        str(budget.active_ceilings()),
    )
    check(
        "and a whole project removed from the file stops being enforced",
        "project:proj-beta" not in budget.active_ceilings(),
        str(budget.active_ceilings()),
    )

    # ARCHITECTURE.md §4: a child budget may not exceed its parent's. Checked per feature.
    # A single feature above its project's ceiling can never mean what it says — the
    # project ceiling binds first — so the config is rejected and caught in review.
    path.write_text(
        textwrap.dedent("""
        projects:
          proj-alpha:
            ceiling_usd_per_day: 100
            features:
              a: { ceiling_usd_per_day: 250 }
    """)
    )
    budget.load_meter_yaml(path)
    check(
        "a feature ceiling above its project's is rejected",
        budget.active_ceilings() == {},
        str(budget.active_ceilings()),
    )

    # Siblings summing past the project total is a different case and is legitimate —
    # independent per-feature caps under a shared project cap. Warn, but keep enforcing:
    # rejecting here would answer an over-restrictive config by enforcing nothing at all.
    path.write_text(
        textwrap.dedent("""
        projects:
          proj-alpha:
            ceiling_usd_per_day: 100
            features:
              a: { ceiling_usd_per_day: 80 }
              b: { ceiling_usd_per_day: 80 }
    """)
    )
    budget.load_meter_yaml(path)
    ceilings = budget.active_ceilings()
    check(
        "features summing past the project total still load",
        ceilings.get("project:proj-alpha") == 100.0,
        str(ceilings),
    )
    check(
        "and both feature ceilings are enforced",
        ceilings.get("feature:proj-alpha/a") == 80.0
        and ceilings.get("feature:proj-alpha/b") == 80.0,
        str(ceilings),
    )

    path.write_text(
        textwrap.dedent("""
        projects:
          proj-alpha:
            ceiling_usd_per_day: 100
            features:
              a: { ceiling_usd_per_day: 60 }
              b: { ceiling_usd_per_day: 40 }
    """)
    )
    budget.load_meter_yaml(path)
    check(
        "allocating exactly the project ceiling is allowed",
        budget.active_ceilings().get("project:proj-alpha") == 100.0,
        str(budget.active_ceilings()),
    )

    # A ceiling of 0 or a negative one is always a typo, and enforcing it literally would
    # block every request in the project — a self-inflicted outage from a cost tool.
    path.write_text("projects:\n  proj-gamma:\n    ceiling_usd_per_day: 0\n")
    budget.load_meter_yaml(path)
    check(
        "a zero ceiling is rejected, not enforced as block-everything",
        "project:proj-gamma" not in budget.active_ceilings(),
        str(budget.active_ceilings()),
    )

    path.write_text("projects:\n  proj-gamma:\n    ceiling_usd_per_day: not-a-number\n")
    budget.load_meter_yaml(path)
    check("a non-numeric ceiling is ignored", "project:proj-gamma" not in budget.active_ceilings())

    # A malformed file must not stop the proxy booting — Meter is in the critical path.
    path.write_text("projects: [this is: not, valid: yaml\n")
    budget.load_meter_yaml(path)
    check(
        "malformed yaml degrades to no ceilings instead of raising", budget.active_ceilings() == {}
    )

    missing = Path(_TMP) / "absent.yaml"
    check("a missing meter.yaml is not an error", budget.load_meter_yaml(missing) == 0)
    check("no meter.yaml means no ceilings", budget.active_ceilings() == {})


def test_reservations() -> None:
    """Authorize/capture — the concurrency hole ARCHITECTURE.md §2 says a plain read leaves open."""
    print("\nbudget reservations")
    import asyncio
    import textwrap

    from proxy import budget

    path = Path(_TMP) / "meter-rsv.yaml"
    path.write_text(
        textwrap.dedent("""
        projects:
          proj-rsv:
            ceiling_usd_per_day: 10
            features:
              tight: { ceiling_usd_per_day: 1 }
    """)
    )
    budget.load_meter_yaml(path)

    async def scenarios() -> None:
        # No ceiling configured for this project at all -> free pass, no hold taken.
        d = await budget.authorize("proj-unbudgeted", "anything", 5.0)
        check("a project with no ceiling is never blocked", d.blocked is False)
        check("and takes no reservation", d.reservation_id is None)

        d = await budget.authorize("proj-rsv", "tight", 0.5)
        check("an estimate under the ceiling is authorized", d.blocked is False)
        check("an authorized request holds a reservation", d.reservation_id is not None)
        check(
            "the hold is counted as outstanding",
            abs(budget.outstanding()["held_usd"] - 0.5) < 1e-9,
            str(budget.outstanding()),
        )

        # THE test for this module. The hold from the first request must make the second
        # fail even though *nothing has been written to the ledger yet* — that gap is
        # exactly what a read-then-call check misses.
        d2 = await budget.authorize("proj-rsv", "tight", 0.75)
        check(
            "a second request is blocked by the first's UNSETTLED hold", d2.blocked is True, str(d2)
        )
        check(
            "the 429 names the ceiling that was hit", d2.scope == "feature:proj-rsv/tight", d2.scope
        )
        check("and reports that ceiling's value", d2.ceiling_usd == 1.0, str(d2.ceiling_usd))

        await budget.release(d.reservation_id)
        check(
            "releasing frees the held budget",
            budget.outstanding()["held_usd"] == 0.0,
            str(budget.outstanding()),
        )
        d3 = await budget.authorize("proj-rsv", "tight", 0.75)
        check("the same request succeeds once the hold is released", d3.blocked is False)
        await budget.release(d3.reservation_id)

        # The project ceiling has to bind independently of any feature ceiling, or a
        # project could exceed its own total by spreading spend across untagged features.
        held = []
        for _ in range(10):
            dn = await budget.authorize("proj-rsv", None, 1.0)
            if not dn.blocked:
                held.append(dn.reservation_id)
        check(
            "the project ceiling caps the total across untagged traffic",
            len(held) == 10,
            str(len(held)),
        )
        over = await budget.authorize("proj-rsv", None, 1.0)
        check("the 11th request exceeds the $10 project ceiling", over.blocked is True)
        check(
            "and is attributed to the project scope, not a feature",
            over.scope == "project:proj-rsv",
            over.scope,
        )
        for rid in held:
            await budget.release(rid)

        # Concurrency: fire many authorizes simultaneously against a ceiling that admits
        # only four. Without serialisation every one of them reads the same empty ledger
        # and all 40 are allowed — the exact failure the module exists to prevent.
        results = await asyncio.gather(
            *(budget.authorize("proj-rsv", None, 2.5) for _ in range(40))
        )
        allowed = [r for r in results if not r.blocked]
        check(
            "concurrent authorizes cannot all pass a $10 ceiling at $2.50 each",
            len(allowed) == 4,
            f"{len(allowed)} of 40 allowed",
        )
        for r in allowed:
            await budget.release(r.reservation_id)
        check(
            "everything is released afterwards",
            budget.outstanding()["reservations"] == 0,
            str(budget.outstanding()),
        )

        # TTL: a hold whose owner never released it must not strand the ceiling forever.
        d4 = await budget.authorize("proj-rsv", None, 9.0)
        budget._holds[d4.reservation_id].expires_at = 0.0  # pretend the TTL elapsed
        d5 = await budget.authorize("proj-rsv", None, 9.0)
        check("an expired hold is reaped rather than blocking forever", d5.blocked is False)
        await budget.release(d5.reservation_id)

        # Heartbeat: the ARCHITECTURE.md §2 failure mode is a stream outliving its TTL and
        # silently dropping out of the ceiling. extend() is what stops that.
        d6 = await budget.authorize("proj-rsv", None, 1.0)
        budget._holds[d6.reservation_id].expires_at = 0.0
        budget.extend(d6.reservation_id)
        check(
            "extend() pushes an expiring hold back out of reach",
            budget._holds[d6.reservation_id].expires_at > 0.0,
        )
        d7 = await budget.authorize("proj-rsv", None, 9.5)
        check("a heartbeaten hold still counts against the ceiling", d7.blocked is True)
        await budget.release(d6.reservation_id)

        # Settled ledger spend and live holds have to be summed together, not either/or.
        _spend("proj-rsv", None, 9.0)
        d8 = await budget.authorize("proj-rsv", None, 2.0)
        check("settled ledger spend counts toward the ceiling too", d8.blocked is True, str(d8))
        check(
            "extend() on an unknown reservation is a no-op, not a crash",
            budget.extend("rsv_does_not_exist") is None,
        )
        check("release() of None is a no-op", await budget.release(None) is None)

        # The claim the B17 decision rests on: features may allocate more in total than
        # their project has, because the project ceiling is checked independently and
        # still binds. If this ever fails, rejecting over-allocated configs at load time
        # becomes necessary again.
        over = Path(_TMP) / "meter-over.yaml"
        over.write_text(
            textwrap.dedent("""
            projects:
              proj-over:
                ceiling_usd_per_day: 10
                features:
                  a: { ceiling_usd_per_day: 8 }
                  b: { ceiling_usd_per_day: 8 }
        """)
        )
        budget.load_meter_yaml(over)
        held = []
        for feature in ("a", "b"):
            for _ in range(8):
                dn = await budget.authorize("proj-over", feature, 1.0)
                if not dn.blocked:
                    held.append(dn.reservation_id)
        check(
            "over-allocated features cannot breach the project ceiling",
            len(held) == 10,
            f"{len(held)} of 16 allowed against a $10 project ceiling",
        )
        # Which ceiling reports the refusal depends on which one is exhausted, and both
        # cases have to name themselves correctly or the 429 sends the operator to the
        # wrong line of meter.yaml. `a` is at its own $8 limit; `b` still has feature
        # headroom and is stopped by the project total instead.
        check(
            "a feature at its own limit is refused in the feature's name",
            (await budget.authorize("proj-over", "a", 1.0)).scope == "feature:proj-over/a",
        )
        check(
            "a feature with headroom is refused in the project's name",
            (await budget.authorize("proj-over", "b", 1.0)).scope == "project:proj-over",
        )
        for rid in held:
            await budget.release(rid)

    asyncio.run(scenarios())
    budget.load_meter_yaml(Path(_TMP) / "absent.yaml")  # leave no ceilings behind


def test_prediction() -> None:
    """The ESTIMATE step must degrade to 'no prediction' rather than ever failing a request.

    `_predict` returns the estimator v2 `PredictionResult` or None; None is the
    degradation path and every malformed input must land there, never in an exception.
    """
    print("\npre-flight estimate")
    from proxy.app import _predict

    messages = [{"role": "user", "content": "Write a python function that sorts a list."}]
    got = _predict({"messages": messages}, "gpt-4o", providers.SHAPE_OPENAI)
    check(
        "a supported model produces a token prediction",
        got is not None and got.predicted_output_tokens > 0,
        str(got),
    )
    check("and a forecast cost for the ledger row", got.predicted_cost_usd > 0, str(got))
    # DESIGN.md §1: the bound is the number the ceiling check reserves — it must never
    # be below the forecast, or the reserve would be weaker than the prediction itself.
    check(
        "and a bound at least as large to reserve",
        got.bound_cost_usd >= got.predicted_cost_usd,
        str(got),
    )
    check("and records which bucket it classified into", bool(got.bucket), str(got))

    capped = _predict({"messages": messages, "max_tokens": 5}, "gpt-4o", providers.SHAPE_OPENAI)
    check(
        "a caller's max_tokens is a hard cap on the prediction",
        capped.predicted_output_tokens == 5,
        str(capped),
    )
    check(
        "and makes the bound exact — reserve equals the cap's cost",
        capped.bound_output_tokens == 5,
        str(capped),
    )

    # Claude has no tiktoken vocabulary and the predictor raises rather than guessing
    # (predictor/README.md). The proxy must absorb that, not 500.
    claude = _predict({"messages": messages}, "claude-sonnet-5", providers.SHAPE_ANTHROPIC)
    check("an unsupported model yields no prediction instead of raising", claude is None)

    check(
        "a body with no messages is handled", _predict({}, "gpt-4o", providers.SHAPE_OPENAI) is None
    )
    check(
        "a missing model is handled",
        _predict({"messages": messages}, None, providers.SHAPE_OPENAI) is None,
    )
    # Non-string content blocks (multimodal) must degrade, not raise.
    blocks = [{"role": "user", "content": [{"type": "text", "text": "hi"}]}]
    try:
        _predict({"messages": blocks}, "gpt-4o", providers.SHAPE_OPENAI)
        check("structured message content does not crash the estimator", True)
    except Exception as exc:  # noqa: BLE001 - the assertion is "never raises"
        check("structured message content does not crash the estimator", False, repr(exc))

    prev = config.PREDICT_ENABLED
    config.PREDICT_ENABLED = False
    try:
        check(
            "PREDICT_ENABLED=false disables prediction entirely",
            _predict({"messages": messages}, "gpt-4o", providers.SHAPE_OPENAI) is None,
        )
    finally:
        config.PREDICT_ENABLED = prev


def test_annotate() -> None:
    """Attribution rung 3 — the requests x annotations join that yields cost per outcome."""
    print("\nannotations")
    import asyncio

    import httpx
    from httpx import ASGITransport

    from proxy.app import app as proxy_app

    # Two priced calls under one trace: the whole point is that an outcome spans calls.
    db.record_request(
        {
            **_row_template("ann-1", "proj-alpha"),
            "trace_id": "tkt_9812",
            "cost_usd": 1.25,
        }
    )
    db.record_request(
        {
            **_row_template("ann-2", "proj-alpha"),
            "trace_id": "tkt_9812",
            "cost_usd": 0.75,
        }
    )
    # Another project's row on the SAME trace id, to prove scoping.
    db.record_request(
        {
            **_row_template("ann-3", "proj-beta"),
            "trace_id": "tkt_9812",
            "cost_usd": 99.0,
        }
    )

    async def drive():
        async with httpx.AsyncClient(
            transport=ASGITransport(app=proxy_app), base_url="http://meter"
        ) as client:
            ok = await client.post(
                "/v1/annotate",
                headers={"Authorization": "Bearer test_key_alpha"},
                json={"trace_id": "tkt_9812", "outcome": "resolved", "value_usd": 40},
            )
            no_auth = await client.post("/v1/annotate", json={"trace_id": "t"})
            bad_key = await client.post(
                "/v1/annotate", headers={"Authorization": "Bearer nope"}, json={"trace_id": "t"}
            )
            no_trace = await client.post(
                "/v1/annotate",
                headers={"Authorization": "Bearer test_key_alpha"},
                json={"outcome": "resolved"},
            )
            bad_value = await client.post(
                "/v1/annotate",
                headers={"Authorization": "Bearer test_key_alpha"},
                json={"trace_id": "t", "value_usd": "forty"},
            )
            no_value = await client.post(
                "/v1/annotate",
                headers={"Authorization": "Bearer test_key_alpha"},
                json={"trace_id": "tkt_9812", "outcome": "resolved"},
            )
            return ok, no_auth, bad_key, no_trace, bad_value, no_value

    ok, no_auth, bad_key, no_trace, bad_value, no_value = asyncio.run(drive())

    check("annotate accepts a valid outcome", ok.status_code == 200, ok.text)
    payload = ok.json()
    check(
        "the trace's cost is summed across all of its requests",
        abs(payload["cost_usd"] - 2.0) < 1e-9,
        str(payload),
    )
    check(
        "another project's identical trace id is not counted",
        payload["request_count"] == 2,
        str(payload),
    )
    check(
        "margin is value minus cost — the cost-per-outcome number",
        abs(payload["margin_usd"] - 38.0) < 1e-9,
        str(payload),
    )
    check(
        "annotation without a value reports no margin rather than zero",
        no_value.json()["margin_usd"] is None,
        no_value.text,
    )
    check("missing meter key is rejected", no_auth.status_code == 401)
    check("unknown meter key is rejected", bad_key.status_code == 401)
    check("trace_id is required", no_trace.status_code == 400, no_trace.text)
    check("a non-numeric value_usd is rejected", bad_value.status_code == 400)


def test_end_to_end_budget_and_prediction() -> None:
    """The wiring, through the real request path against a fake upstream.

    The unit tests above prove `_estimate`, `budget.authorize` and the migration each work.
    This proves `_proxy` actually *calls* them and that what they return reaches the ledger
    — the failure mode where every part is correct and none of them are connected.
    """
    print("\nend to end — prediction and ceilings")
    import asyncio
    import json as _json
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    from proxy import budget
    from proxy.app import app as proxy_app

    body = _json.dumps(
        {
            "id": "chatcmpl-x",
            "object": "chat.completion",
            "model": "gpt-4o",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "hello"},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150},
        }
    ).encode()

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            self.rfile.read(int(self.headers.get("content-length", 0) or 0))
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()

    config.OPENAI_BASE_URL = f"http://127.0.0.1:{port}/v1"
    config.OPENAI_API_KEY = "sk-fake"
    config.BREAKER_ENABLED = False

    async def drive():
        import httpx
        from httpx import ASGITransport

        async with httpx.AsyncClient(
            transport=ASGITransport(app=proxy_app), base_url="http://meter"
        ) as client:
            proxy_app.state.http = httpx.AsyncClient(timeout=30)
            call = lambda feature: client.post(  # noqa: E731
                "/v1/chat/completions",
                headers={"Authorization": "Bearer test_key_alpha", "X-Meter-Feature": feature},
                json={
                    "model": "gpt-4o",
                    "messages": [{"role": "user", "content": "Summarize this text."}],
                },
            )
            priced = await call("e2e-priced")
            await asyncio.sleep(0.4)

            # Now impose a ceiling far below what this call already spent, and confirm
            # the next one is refused with the headers that name what it hit.
            path = Path(_TMP) / "meter-e2e.yaml"
            path.write_text(
                "projects:\n  proj-alpha:\n    features:\n"
                "      e2e-blocked: { ceiling_usd_per_day: 0.000001 }\n"
            )
            budget.load_meter_yaml(path)
            _spend("proj-alpha", "e2e-blocked", 0.01)
            blocked = await call("e2e-blocked")
            await asyncio.sleep(0.4)

            await proxy_app.state.http.aclose()
            return priced, blocked

    priced, blocked = asyncio.run(drive())
    server.shutdown()

    check(
        "a normal call still succeeds with budgets wired in",
        priced.status_code == 200,
        priced.text[:200],
    )

    rows = {r["feature"]: r for r in _all_rows() if r["feature"] in {"e2e-priced", "e2e-blocked"}}
    row = rows.get("e2e-priced")
    check("the served call produced a ledger row", row is not None)
    if row:
        check(
            "the prediction reached the ledger",
            row["predicted_output_tokens"] is not None and row["predicted_cost_usd"] is not None,
            str(dict(row)),
        )
        check("with the bucket it classified into", bool(row["bucket"]), str(row["bucket"]))
        # v2 method strings are compound ("stacked", "stacked+capped", "json_schema") —
        # assert one was recorded rather than pinning the estimator's internal vocabulary,
        # which belongs to predictor/ and its own suite.
        check(
            "and the method it used", bool(row["prediction_method"]), str(row["prediction_method"])
        )
        check(
            "actual cost is priced from the provider's real usage, not the prediction",
            row["output_tokens"] == 50 and row["estimated"] == 0,
            str(dict(row)),
        )
        check(
            "predicted and actual are both present, so variance is a subtraction",
            isinstance(row["cost_usd"] - row["predicted_cost_usd"], float),
        )

    check(
        "a call over its feature ceiling is refused",
        blocked.status_code == 429,
        str(blocked.status_code),
    )
    check(
        "the refusal names the ceiling that was hit",
        blocked.headers.get("X-Meter-Budget-Scope") == "feature:proj-alpha/e2e-blocked",
        str(dict(blocked.headers)),
    )
    check(
        "and reports the ceiling's value",
        blocked.headers.get("X-Meter-Budget-Ceiling-Usd") is not None,
    )
    check(
        "a budget refusal is ledgered too, at zero cost",
        "e2e-blocked" in rows and rows["e2e-blocked"]["cost_usd"] == 0.0,
    )
    check(
        "the refused call never reached a provider",
        rows["e2e-blocked"]["provider"] == "-",
        str(dict(rows["e2e-blocked"])),
    )

    # The hold taken by the served request must be gone once its row landed. A leak here
    # would silently shrink every subsequent ceiling until the TTL reaped it.
    check(
        "no reservation leaked after the requests completed",
        budget.outstanding()["reservations"] == 0,
        str(budget.outstanding()),
    )
    budget.load_meter_yaml(Path(_TMP) / "absent.yaml")
    config.BREAKER_ENABLED = True


def _row_template(request_id: str, project: str) -> dict:
    return {
        "id": request_id,
        "ts": db.now_iso(),
        "project_id": project,
        "environment": "test",
        "actor": None,
        "feature": None,
        "trace_id": None,
        "provider": "openai",
        "model": "gpt-4o",
        "endpoint": "/v1/chat/completions",
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
        "pricing_version": "test",
        "cost_usd": 0.0,
        "latency_ms": 1.0,
        "ttft_ms": 1.0,
        "overhead_ms": 0.5,
        "status": 200,
        "is_stream": 0,
        "estimated": 0,
        "prompt_hash": None,
        "reservation_id": None,
    }


def test_ledger_migration() -> None:
    """A ledger from Phase 1 must gain the new columns, not break on them."""
    print("\nledger migration")
    conn = db.connect()

    def columns() -> set[str]:
        return {r["column_name"] for r in conn.execute(
            "SELECT column_name FROM information_schema.columns"
            " WHERE table_schema = current_schema() AND table_name = 'requests'")}

    # Simulate the older shape in place by dropping a prediction column, then re-running
    # the boot path. Building a separate legacy database would mean a second schema, and
    # `search_path` is pinned per connection when the pool creates it — so an in-place
    # drop is both simpler and a truer rehearsal of a teammate's actual database.
    conn.execute("INSERT INTO requests (id, ts, project_id, provider, endpoint)"
                 " VALUES ('old_1', '2026-01-01T00:00:00.000000+00:00',"
                 " 'proj-alpha', 'openai', '/v1/chat/completions')"
                 " ON CONFLICT (id) DO NOTHING")
    conn.execute("ALTER TABLE requests DROP COLUMN IF EXISTS predicted_cost_usd")
    check("the older ledger lacks the column", "predicted_cost_usd" not in columns())

    db._schema_ready = False              # force the boot path to run again
    db.connect()

    for table, column, _decl in db._ADDED_COLUMNS:
        if table == "requests":
            check(f"migration adds requests.{column}", column in columns())
    check(
        "the pre-existing row survives the migration",
        conn.execute("SELECT COUNT(*) AS n FROM requests WHERE id = 'old_1'"
                     ).fetchone()["n"] == 1,
    )
    check(
        "and its new columns read as NULL, not as a wrong number",
        conn.execute("SELECT predicted_cost_usd FROM requests WHERE id = 'old_1'"
                     ).fetchone()["predicted_cost_usd"] is None,
    )

    db._migrate(conn)  # idempotent — a second boot must not fail on duplicate columns
    check("re-running the migration is a no-op", True)
    conn.close()


def _all_rows() -> list[dict]:
    conn = db.connect()
    with db._lock:
        return [dict(r) for r in conn.execute("SELECT * FROM requests")]


def _with_breaker_disabled(project: str, feature: str, key: dict) -> bool:
    config.BREAKER_ENABLED = False
    try:
        _spend(project, feature, 500.0)
        return breaker.check(project, feature, key).blocked is False
    finally:
        config.BREAKER_ENABLED = True


# ─────────────────────────────────────────────────────────────────────────────
def test_client_request_id() -> None:
    """PROPOSALS.md D1 — a caller may name its own request, so a retry is idempotent."""
    print("\nclient request id")
    from proxy.app import _CLIENT_REQUEST_ID, _request_id

    class _Req:
        def __init__(self, headers: dict[str, str]) -> None:
            self.headers = headers

    minted = _request_id(_Req({}))
    check("mints an id when the client sends none", minted.startswith("req_"))
    check("minted ids are unique", _request_id(_Req({})) != minted)

    check(
        "accepts a well-formed client id",
        _request_id(_Req({"x-meter-request-id": "trace-abc_123:9"})) == "trace-abc_123:9",
    )
    check(
        "surrounding whitespace is trimmed, not rejected",
        _request_id(_Req({"x-meter-request-id": "  order-42-retry  "})) == "order-42-retry",
    )

    # Ignored rather than refused, and that choice is the point: a malformed idempotency
    # key is not a reason to fail a request that is otherwise fine. Refusing would make
    # adopting the header riskier than never sending it.
    for bad, why in (
        ("short", "under 8 characters"),
        ("x" * 129, "over 128 characters"),
        ("has spaces here", "whitespace inside"),
        ("drop;--table", "punctuation outside the safe set"),
        ("", "empty"),
    ):
        got = _request_id(_Req({"x-meter-request-id": bad}))
        check(f"ignores a client id with {why}", got.startswith("req_"), f"got {got!r}")

    check(
        "the pattern anchors both ends",
        not _CLIENT_REQUEST_ID.match("ok-value\nInjected: header"),
    )


def test_soft_budget() -> None:
    """PROPOSALS.md D2 — warn before the 429, not instead of it."""
    print("\nsoft budget alerts")
    import textwrap

    from alerts.poke import compose_budget
    from proxy import budget

    path = Path(_TMP) / "meter-soft.yaml"
    path.write_text(
        textwrap.dedent("""
        projects:
          proj-soft:
            ceiling_usd_per_day: 10
            features:
              warm: { ceiling_usd_per_day: 1 }
    """)
    )
    budget.load_meter_yaml(path)

    check("nothing breached on an empty ledger", budget.soft_breaches(0.8) == [])

    # $0.85 against the feature's $1 ceiling: over 80%, under 100% — the window the
    # whole feature exists for. Also 8.5% of the project's $10, so the project ceiling
    # must NOT fire; a warning that cannot tell them apart is noise.
    db.record_request(
        {
            "id": "req-soft-1",
            "ts": db.now_iso(),
            "project_id": "proj-soft",
            "feature": "warm",
            "actor": "tester",
            "model": "gpt-4o-mini",
            "provider": "openai",
            "endpoint": "/v1/chat/completions",
            "cost_usd": 0.85,
            "status": 200,
        }
    )

    breaches = budget.soft_breaches(0.8)
    scopes = [b["scope"] for b in breaches]
    check("the feature over 80% is reported", "feature:proj-soft/warm" in scopes, str(scopes))
    check("the project well under its own ceiling is not", "project:proj-soft" not in scopes,
          str(scopes))

    hit = next(b for b in breaches if b["scope"] == "feature:proj-soft/warm")
    check("it reports the spend", abs(hit["spend_usd"] - 0.85) < 1e-6, str(hit))
    check("and the ceiling it is measured against", hit["ceiling_usd"] == 1.0, str(hit))
    check("and the ratio", abs(hit["ratio"] - 0.85) < 1e-6, str(hit))

    # The scope string is the contract between the warning and the eventual refusal: an
    # operator reading the iMessage has to be able to find the same line in meter.yaml
    # that the 429 will name.
    check(
        "the breach scope matches what a 429 would report",
        hit["scope"] == "feature:proj-soft/warm",
    )

    check("a higher threshold does not fire yet", budget.soft_breaches(0.9) == [])

    body = compose_budget("feature:proj-soft/warm", 0.85, 1.0)
    check("the message names the scope", "feature:proj-soft/warm" in body, body)
    check("leads with headroom, not just a percentage", "$0.15 left" in body, body)
    check("and says what happens next", "429" in body, body)

    budget.load_meter_yaml(Path(_TMP) / "does-not-exist.yaml")
    check("no ceilings configured means nothing to poll", budget.soft_breaches(0.8) == [])



def test_ceiling_spend_one_round_trip() -> None:
    """One query for both ceiling numbers — the round trip count IS the latency now."""
    print("\nceiling spend (one round trip)")
    from proxy import budget

    db.record_request({
        "id": "req-cs-1", "ts": db.now_iso(), "project_id": "proj-cs",
        "feature": "alpha", "actor": "t", "model": "gpt-4o-mini", "provider": "openai",
        "endpoint": "/v1/chat/completions", "cost_usd": 0.30, "status": 200,
    })
    db.record_request({
        "id": "req-cs-2", "ts": db.now_iso(), "project_id": "proj-cs",
        "feature": "beta", "actor": "t", "model": "gpt-4o-mini", "provider": "openai",
        "endpoint": "/v1/chat/completions", "cost_usd": 0.20, "status": 200,
    })
    db.record_request({
        "id": "req-cs-3", "ts": db.now_iso(), "project_id": "proj-cs",
        "feature": None, "actor": "t", "model": "gpt-4o-mini", "provider": "openai",
        "endpoint": "/v1/chat/completions", "cost_usd": 0.05, "status": 200,
    })

    f, p = db.ceiling_spend("proj-cs", "alpha", 3600)
    check("feature spend counts only that feature", abs(f - 0.30) < 1e-9, str(f))
    check("project spend counts every tag including untagged", abs(p - 0.55) < 1e-9, str(p))

    # The distinction throttle mode rests on: untagged means `feature IS NULL`, NOT the
    # project total. Collapsing the two queries must not have quietly changed this.
    f_untagged, p_untagged = db.ceiling_spend("proj-cs", None, 3600)
    check("untagged means feature IS NULL, not the project total",
          abs(f_untagged - 0.05) < 1e-9, str(f_untagged))
    check("project total is the same either way", abs(p_untagged - 0.55) < 1e-9,
          str(p_untagged))

    check("it agrees with the two queries it replaced",
          abs(f - db.window_spend("proj-cs", "alpha", 3600)) < 1e-9
          and abs(p - db.project_window_spend("proj-cs", 3600)) < 1e-9)

    # And the count itself: authorize must make exactly ONE database call. At ~50ms per
    # round trip to a hosted ledger, a second one is 50ms on every enforced request.
    calls = {"n": 0}
    real = db.ceiling_spend

    def counted(*a, **k):
        calls["n"] += 1
        return real(*a, **k)

    path = Path(_TMP) / "meter-rt.yaml"
    path.write_text(
        "projects:\n  proj-cs:\n    ceiling_usd_per_day: 100\n"
        "    features:\n      alpha: { ceiling_usd_per_day: 50 }\n"
    )
    budget.load_meter_yaml(path)
    db.ceiling_spend = counted
    try:
        d = asyncio.run(budget.authorize("proj-cs", "alpha", 0.01))
        check("authorize is allowed with both ceilings configured", d.blocked is False)
        check("and it made exactly ONE database round trip", calls["n"] == 1,
              f"{calls['n']} calls — a regression here is +50ms per request in production")
        asyncio.run(budget.release(d.reservation_id))
    finally:
        db.ceiling_spend = real


def test_cors_previews_are_opt_in() -> None:
    """Preview origins are off unless someone turns them on (PROPOSALS.md B22).

    The preview pattern is built from the configured origins, so it cannot match an
    unrelated project — but it necessarily matches `<project>-<anything>.vercel.app`,
    because that is the shape of a preview URL and the suffix is not predictable. Since
    `vercel.app` subdomains are first-come-first-served, someone can register
    `meter-three-beta-evil.vercel.app` and be inside the allowlist.

    That was survivable while CORS carried no credentials. With the judge money capability
    riding a cookie, an allowed origin now gets it attached automatically — so the second
    factor that exists to stop a leaked session token from spending would be delivered to
    an origin an attacker owns. Hence: off by default.
    """
    print("\nCORS preview origins are opt-in")
    from proxy.app import _preview_origin_regex

    origins = ["https://meter-three-beta.vercel.app"]
    rx = _preview_origin_regex(origins)
    check("the pattern still exists for those who opt in", rx is not None)

    import re as _re
    pat = _re.compile(rx)
    check("a genuine preview matches",
          pat.fullmatch("https://meter-three-beta-abc123-team.vercel.app") is not None)
    # The reason it is opt-in, asserted rather than argued.
    check("but so does an attacker-registrable lookalike",
          pat.fullmatch("https://meter-three-beta-evil.vercel.app") is not None)
    check("an unrelated project is still refused",
          pat.fullmatch("https://evil-meter-three-beta.vercel.app") is None)

    check("and the flag that gates it defaults to off",
          config.CORS_ALLOW_VERCEL_PREVIEWS is False,
          str(config.CORS_ALLOW_VERCEL_PREVIEWS))


def test_cors_preview_origins() -> None:
    """Vercel preview URLs are allowed; the rest of vercel.app is not.

    The regex here used to be `https://.*\\.vercel\\.app`. Anyone can deploy to vercel.app
    in about a minute, so that matched an attacker's origin as readily as ours — the
    wildcard the surrounding comment says it is avoiding. Starlette matches this with
    `fullmatch`, which is what the last two cases below pin down: a suffix like
    `notours.vercel.app.evil.com` must not pass, and neither must another project.
    """
    print("\ncors preview origins")
    import re as _re

    from proxy.app import _preview_origin_regex

    pattern = _preview_origin_regex(
        ["https://meter-three-beta.vercel.app", "http://localhost:3000"]
    )
    check("a regex is produced when a vercel origin is configured", pattern is not None)
    rx = _re.compile(pattern)

    def allows(origin: str) -> bool:
        return bool(rx.fullmatch(origin))

    check("the production origin matches", allows("https://meter-three-beta.vercel.app"))
    check("our own preview deployment matches",
          allows("https://meter-three-beta-git-shubh-abc123.vercel.app"))
    check("an unrelated project does NOT match",
          not allows("https://evil-attacker.vercel.app"),
          "any vercel.app deployment could drive the judge API")
    check("a lookalike suffix does NOT match",
          not allows("https://meter-three-beta.vercel.app.evil.com"))
    check("a lookalike prefix does NOT match",
          not allows("https://evil-meter-three-beta.vercel.app"))
    check("no regex when no vercel origin is configured",
          _preview_origin_regex(["http://localhost:3000"]) is None)


def main() -> int:
    try:
        return _run()
    finally:
        # Drop the throwaway schema whether the run passed or failed, or a hosted
        # database fills up with `test_proxy_*` after a few days.
        from proxy import pg as _pg
        try:
            _pg.drop_schema(config.DB_SCHEMA)
        finally:
            _pg.close()


def _run() -> int:
    for suite in (
        test_cors_preview_origins,
        test_cors_previews_are_opt_in,
        test_client_request_id,
        test_ceiling_spend_one_round_trip,
        test_soft_budget,
        test_routing,
        test_header_substitution,
        test_prepare_body,
        test_pricing,
        test_openai_stream,
        test_anthropic_stream,
        test_truncated_stream,
        test_prompt_hash,
        test_ledger,
        test_breaker,
        test_burst_detection,
        test_revocation_fails_closed,
        test_breaker_round_trip_count,
        test_key_cache,
        test_model_efficiency,
        test_soft_breaches_is_one_round_trip,
        test_runtime_ceilings_are_reclaimed,
        test_gzipped_upstream_stream,
        test_ledger_migration,
        test_meter_yaml,
        test_reservations,
        test_prediction,
        test_annotate,
        test_end_to_end_budget_and_prediction,
    ):
        suite()
    print(f"\n{PASSED} checks passed")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except AssertionError as exc:
        print(f"\nFAILED: {exc}", file=sys.stderr)
        sys.exit(1)
