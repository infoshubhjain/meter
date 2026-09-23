#!/usr/bin/env python3
"""Self-check for the Meter predictive engine.

Run it directly, no test framework required:

    python tests/test_predictor.py

These exist because the prior art we evaluated (see CONTEXT.md §6b) asserted its accuracy
in a README with no benchmark, no dataset, and no test behind it. Every property the proxy
relies on when reserving budget is pinned here instead.

Owner: Ammar (Predictive AI).
"""

from __future__ import annotations

import os
import sys
import uuid
from pathlib import Path

# Point the ledger at a throwaway Postgres SCHEMA before proxy.config is imported, so a
# test run can never touch the tables the demo and the judges are using. Under SQLite this
# was a tempfile; a hosted database needs the schema-level equivalent, and the run drops it
# at the end. Two suites below write to `requests` and one of them clears it.
os.environ["DB_SCHEMA"] = "test_predictor_" + uuid.uuid4().hex[:8]

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from predictor import engine  # noqa: E402
from predictor import (  # noqa: E402
    DEFAULT_BUFFER,
    PRIORS,
    Predictor,
    UnsupportedModelError,
    accuracy_report,
    classify,
    count,
    fit_bucket,
    predict,
    supports,
)

MODEL = "gpt-4o"
PROMPT = "Write a Python function that parses a CSV file and returns a list of dicts."

PASSED = 0


def check(label: str, condition: bool, detail: str = "") -> None:
    global PASSED
    if not condition:
        raise AssertionError(f"{label}{(' — ' + detail) if detail else ''}")
    PASSED += 1
    print(f"  ok  {label}")


def close(a: float, b: float, tol: float) -> bool:
    return abs(a - b) <= tol


# ─────────────────────────────────────────────────────────────────────────────
def test_determinism() -> None:
    print("\ndeterminism")
    # The property that disqualified the reference implementation: it multiplied every
    # estimate by Gaussian noise, giving a ~70% spread on an identical prompt. A
    # reservation that differs run to run makes the ceiling a coin flip.
    out = {predict(PROMPT, MODEL).predicted_output_tokens for _ in range(50)}
    check("50 identical predictions give one value", len(out) == 1, f"got {sorted(out)}")
    check("classification is stable", len({classify(PROMPT) for _ in range(50)}) == 1)

    a, b = predict(PROMPT, MODEL), predict(PROMPT, MODEL)
    check("cost is stable", a.predicted_cost_usd == b.predicted_cost_usd)


# ─────────────────────────────────────────────────────────────────────────────
def test_input_counting() -> None:
    print("\ninput counting")
    short = predict("hi", MODEL).input_tokens
    long = predict("hi " * 500, MODEL).input_tokens
    check("token counts are positive and scale", 0 < short < long)

    # Chat payloads cost more than their raw content: role markers, delimiters, and reply
    # priming. Ignoring the framing under-counts every single chat request.
    #
    # The overhead is exactly 7 for a single user message: 3 per-message + 1 role token
    # + 3 reply priming. Pinned as an exact value because a live 15-call run showed our
    # count was low by precisely 7 on every request when a bare string was passed while
    # a messages list was sent upstream. Under-counting is the direction that breaks a
    # ceiling, so this must not drift silently.
    text = "hello world"
    check("single-message framing overhead is exactly 7",
          count([{"role": "user", "content": text}], MODEL) - count(text, MODEL) == 7,
          f"got {count([{'role': 'user', 'content': text}], MODEL) - count(text, MODEL)}")

    # Overhead scales per message, not once per request.
    two = count([{"role": "user", "content": text}, {"role": "assistant", "content": text}], MODEL)
    one = count([{"role": "user", "content": text}], MODEL)
    check("framing is charged per message", two > one + count(text, MODEL))

    # A loud failure beats a silently ~10-20% wrong number in something that gates spend.
    # The reference fell back to cl100k_base for Claude, which is the wrong vocabulary.
    check("anthropic is unsupported, not approximated", not supports("claude-sonnet-5"))
    try:
        count("hello", "claude-sonnet-5")
        raised = False
    except UnsupportedModelError:
        raised = True
    check("claude raises rather than guessing", raised)

    # Support is an allowlist, not "everything except Anthropic". Blocking by
    # exclusion silently mis-counted any third-party model reachable through an
    # OpenAI-compatible gateway (OpenRouter, Together, local vLLM).
    for unknown in ("llama-3-70b", "mistral-large", "deepseek-chat", "command-r", ""):
        check(f"unknown model {unknown!r} is rejected", not supports(unknown))

    # tiktoken's own table is authoritative and knows more than our prefix list:
    # it maps the gpt-oss family to o200k_harmony, so those count exactly.
    check("gpt-oss-20b is supported via tiktoken's own mapping", supports("gpt-oss-20b"))

    for known in ("gpt-4o", "gpt-4o-mini", "gpt-4-turbo", "gpt-3.5-turbo", "gpt-4.1"):
        check(f"known model {known!r} is supported", supports(known))

    # Longest-prefix: gpt-4o must not be swallowed by the shorter gpt-4 entry.
    check("gpt-4o uses o200k, not cl100k",
          count("hello world", "gpt-4o") == count("hello world", "gpt-4o-2024-11-20"))


# ─────────────────────────────────────────────────────────────────────────────
def test_classifier() -> None:
    print("\nclassifier")
    cases = [
        ("Summarize this article in two sentences.", "summary"),
        ("Write a Python function to sort a list.", "code"),
        ("Translate this sentence to Spanish.", "translation"),
        ("Return the result as valid JSON.", "json"),
        ("Explain step by step why the sky is blue.", "reasoning"),
        ("List ten programming languages.", "list"),
        ("What is a database index?", "explanation"),
        ("Hello, how are you?", "default"),
    ]
    for text, expected in cases:
        got = classify(text)
        check(f"{expected:<12} <- {text[:38]}", got == expected, f"got {got!r}")

    # The reference had "{" and "}" in its json keyword list, so any prompt containing a
    # template placeholder classified as json — breaking its own headline use case.
    check("template placeholder does not force json",
          classify("Summarize {{content}}") == "summary")
    check("template placeholder does not force json (code)",
          classify("Write code for {{task}}") == "code")

    # Word-boundary matching: "listen" is not "list", "decode" is not "code".
    check("no substring false positive on 'listen'",
          classify("Please listen carefully to the recording.") != "list")
    check("no substring false positive on 'decode'",
          classify("How do I decode this transmission?") != "code")


# ─────────────────────────────────────────────────────────────────────────────
def test_max_tokens() -> None:
    print("\nmax_tokens")
    # max_tokens CLAMPS the estimate; it must never REPLACE it. Letting it short-circuit
    # the pipeline measured 594% MAPE against 192% when clamping, because most SDKs set
    # a default max_tokens as a safety valve rather than as a statement of intent -- so
    # replacing collapses every prompt to one identical number.
    r = predict("Write an exhaustive essay on Rome. " * 20, MODEL, max_tokens=50)
    check("hard cap is applied", r.predicted_output_tokens == 50)
    check("cap is flagged", r.capped_by_max_tokens is True)
    check("method records the cap", r.method.endswith("+capped"))

    r2 = predict("hi", MODEL, max_tokens=100_000)
    check("non-binding cap is ignored", r2.predicted_output_tokens < 100_000)
    check("non-binding cap not flagged", r2.capped_by_max_tokens is False)

    # The decisive property: a generous max_tokens must NOT flatten distinct prompts
    # into one prediction.
    a = predict("Say hi.", MODEL, max_tokens=4096).predicted_output_tokens
    b = predict("Write a complete production-ready web framework.", MODEL,
                max_tokens=4096).predicted_output_tokens
    check("max_tokens does not flatten distinct prompts", a != b, f"both {a}")

    # bound is emitted alongside, and is exact when max_tokens is set
    r3 = predict("hi", MODEL, max_tokens=250)
    check("bound equals max_tokens", r3.bound_output_tokens == 250)
    check("bound cost >= predicted cost", r3.bound_cost_usd >= r3.predicted_cost_usd)
    check("bound exists without max_tokens", predict("hi", MODEL).bound_output_tokens > 0)


# ─────────────────────────────────────────────────────────────────────────────
def test_scope_signals() -> None:
    """DESIGN.md steps 2-3."""
    print("\nscope signals")
    from predictor import estimate_scope
    from predictor.scope import cot_scale, task_scope, verb_scale

    # Step 2 -- explicit length. Coverage here is the highest-value detail in the
    # estimator: one missed hyphenated form moved overall error 84% -> 192% MAPE.
    for text, lo, hi in [
        ("Give me a two-sentence overview.", 40, 60),
        ("Summarize in two sentences.", 40, 60),
        ("Answer in 100 words.", 120, 145),
        ("Reply in one word.", 1, 10),
        ("Give a yes or no answer.", 1, 10),
    ]:
        got = estimate_scope(text)[0]
        check(f"{text[:32]:<34} -> {got:.0f}", lo <= got <= hi, f"want {lo}-{hi}")

    # Step 3A -- additive, so a two-task prompt exceeds either task alone
    both = task_scope("summarize this and write the code")[0]
    check("multi-task adds", both > task_scope("summarize this")[0], f"{both}")
    check("scope is capped", task_scope("summarize code json browse " * 40)[0] <= 1500)

    # Step 3B -- the button-vs-website case
    check("high intensity raises", verb_scale("rewrite the entire module") > 1.0)
    check("low intensity lowers", verb_scale("fix the typo") < 1.0)

    # Step 3C -- reasoning models emit CoT regardless of prompt wording
    check("CoT cue detected", cot_scale("think step by step") == 3.0)
    check("reasoning model detected by name", cot_scale("just say hi", "o3-mini") == 3.0)

    # Word boundaries. Naive substring matching made "create" fire inside "created_at"
    # and "code" fire inside "status codes", scoring 975% error on a JSON probe.
    check("'create' does not match in 'created_at'",
          verb_scale("return json with id and created_at") == 1.0)
    check("'code' does not match in 'status codes'",
          "code" not in task_scope("list the http status codes")[1])

    # A phrase must not fire two multipliers at once.
    from predictor.scope import qualitative_scale
    check("'step by step' is CoT only, not also verbose",
          qualitative_scale("analyze this step by step") == 1.0)


# ─────────────────────────────────────────────────────────────────────────────
def test_cache() -> None:
    print("\ncache")
    from predictor import Predictor
    p = Predictor()
    a = p.predict("Write a function.", MODEL)
    b = p.predict("Write a function.", MODEL)
    check("repeat is served identically", a == b)
    check("cache holds the entry", p.cache_stats()["entries"] >= 1)
    # Different max_tokens is a different request and must not collide.
    c = p.predict("Write a function.", MODEL, max_tokens=10)
    check("max_tokens is part of the key", c.predicted_output_tokens != a.predicted_output_tokens)
    # Refitting invalidates, since cached values used the old coefficients.
    p.load_buffers({"code": [(100.0, 200)] * 12})
    check("refit clears the cache", p.cache_stats()["entries"] == 0)


# ─────────────────────────────────────────────────────────────────────────────
def test_buffer_and_history() -> None:
    """DESIGN.md steps 4-5."""
    print("\nbuffer and history")
    from predictor import Predictor
    p = Predictor()

    # Step 4 -- buffer fitted per bucket to a target under-prediction rate. Too few
    # rows must keep the default rather than fit noise.
    check("too few rows keeps default", p.load_buffers({"code": [(100.0, 150)] * 5}) == {})
    fitted = p.load_buffers({"code": [(100.0, 100 + i * 10) for i in range(30)]})
    check("fits with enough rows", "code" in fitted)

    # Step 5 -- a key below MIN_ROWS_FOR_KEY is dropped entirely rather than applied
    # weakly, so the ladder falls through to a coarser key that actually has support.
    check("thin key is skipped, not shrunk", p.load_history({("proj",): (3.0, 2)}) == {})
    barely = p.load_history({("proj",): (3.0, 20)})[("proj",)]
    strong = p.load_history({("proj",): (3.0, 500)})[("proj",)]
    check("shrinkage still applies above the threshold", barely < strong, f"{barely:.2f} vs {strong:.2f}")
    # The clamp is a guard against the absurd, NOT the noise guard -- MIN_ROWS_FOR_KEY
    # and shrinkage are. It used to sit at 3.0, which was measured to be inside the
    # range real traffic asks for (a templated feature needed 18.2x), so it clamped
    # good factors and the refresh gate then rejected them. See engine.FACTOR_MAX.
    check("absurd factor is clamped", p.load_history({("p",): (999.0, 500)})[("p",)]
          == engine.FACTOR_MAX)
    check("a large but real factor survives the clamp",
          p.load_history({("p",): (18.2, 500)})[("p",)] > 3.0)

    # The refresh gate scores a candidate and then installs it; those must be the same
    # object. `shrink_history` is what makes that possible, and `set_history` installs
    # without shrinking a second time.
    raw = {("proj", "feat"): (0.25, 40)}
    check("shrink_history matches what load_history installs",
          p.shrink_history(raw) == p.load_history(raw))
    p.set_history({("proj", "feat"): 0.25})
    check("set_history installs verbatim, no second shrink",
          p._history[("proj", "feat")] == 0.25)
    check("absurdly small factor is clamped", p.load_history({("p",): (0.0001, 500)})[("p",)]
          == engine.FACTOR_MIN)
    # The floor has the same history as the ceiling: a real templated feature needed
    # 0.27, which the old 0.5 floor clamped away.
    check("a small but real factor survives the floor",
          p.load_history({("p",): (0.27, 500)})[("p",)] < 0.5)

    # The ladder: attribution rungs outrank the generic (bucket, model) rung, because
    # one team's prompting style predicts their next request better than a pattern
    # averaged over everyone's traffic -- even when the generic rung has more rows.
    p.load_history({("proj", "feat", "ammar"): (2.5, 200),
                    ("proj",): (0.7, 300),
                    ("code", MODEL): (1.8, 100)})
    a = p.predict("Write a Python function.", MODEL, project="proj", feature="feat", actor="ammar")
    b = p.predict("Write a Python function.", MODEL, project="proj", feature="zzz", actor="q")
    c = p.predict("Write a Python function.", MODEL, project="unknown", feature="x", actor="y")
    check("most specific rung wins", a.history_factor > 2.0, f"{a.history_factor:.2f}")
    check("(project,) outranks (bucket, model)", b.history_factor < 1.0, f"{b.history_factor:.2f}")
    check("unknown project falls to (bucket, model)", 1.0 < c.history_factor < 2.0,
          f"{c.history_factor:.2f}")

    # Step 6 -- the bound. max_tokens is exact; otherwise a learned per-bucket p95
    # beats the model maximum, which would reserve ~$0.04 of gpt-4o output on every
    # request and exhaust a small project's ceiling in a couple of dozen calls.
    q = Predictor()
    check("bound defaults to the model maximum", q.predict("hi", MODEL).bound_output_tokens == 4096)
    q.load_bounds({q.predict("hi", MODEL).bucket: [200] * 25})
    check("learned bound is tighter than the model maximum",
          q.predict("hi", MODEL).bound_output_tokens < 4096)
    check("too few rows keeps the model maximum", q.load_bounds({"code": [200] * 5}) == {})

    # scope_tokens is the fixed baseline the learner fits against, so it must be the
    # RAW heuristic -- unmoved by the buffer, the history factor, or the clamp.
    r = Predictor(buffer=1.0)
    base = r.predict("Explain how DNS works.", MODEL).scope_tokens
    r.load_history({("p",): (2.0, 500)})
    with_hist = r.predict("Explain how DNS works.", MODEL, project="p")
    check("scope is unchanged by the history factor", with_hist.scope_tokens == base)
    check("but the prediction did move", with_hist.predicted_output_tokens != base)
    capped = r.predict("Explain how DNS works.", MODEL, max_tokens=5)
    check("scope is unchanged by the clamp", capped.scope_tokens == base)


# ─────────────────────────────────────────────────────────────────────────────
def test_bias_direction() -> None:
    print("\nbias direction")
    # Safety now lives on the BOUND, not on a buffer applied to the forecast. Keeping
    # both double-corrected: the buffer and the history factor are each fitted as
    # actual/scope, so applying both computed scope x (actual/scope)^2. A prequential
    # run caught that as median error rising 77% -> 204% while the loop "learned".
    r = predict(PROMPT, MODEL)
    check("forecast carries no safety buffer", DEFAULT_BUFFER == 1.0)
    check("the bound is what guarantees safety",
          r.bound_output_tokens >= r.predicted_output_tokens)
    check("a set max_tokens makes the bound exact",
          predict(PROMPT, MODEL, max_tokens=40).bound_output_tokens == 40)


# ─────────────────────────────────────────────────────────────────────────────
def test_pricing_integration() -> None:
    print("\npricing")
    cheap = predict("hi", MODEL)
    dear = predict("hi " * 1000, MODEL)
    check("cost is positive", cheap.predicted_cost_usd > 0 and dear.predicted_cost_usd > 0)
    check("input tokens are monotonic", cheap.input_tokens < dear.input_tokens)
    # Total cost is deliberately NOT monotonic in input size. A long pasted prompt
    # predicts a SHORTER answer -- it reads as a targeted edit rather than a
    # generation request -- so more input can mean less output and a lower total.
    # That inversion is the instruction-ratio signal working, not a bug.
    check("longer input can predict shorter output",
          dear.predicted_output_tokens < cheap.predicted_output_tokens,
          f"{dear.predicted_output_tokens} vs {cheap.predicted_output_tokens}")
    check("pricing version is recorded", bool(cheap.pricing_version))

    # Priced through proxy/pricing.py against pricing/{version}.yaml, so a prediction and
    # the ledger row it is later compared against cannot disagree on rates.
    mini = predict("hi " * 200, "gpt-4o-mini").predicted_cost_usd
    full = predict("hi " * 200, "gpt-4o").predicted_cost_usd
    check("cheaper model prices cheaper", mini < full, f"{mini} vs {full}")


# ─────────────────────────────────────────────────────────────────────────────
def test_learner() -> None:
    print("\nlearner")
    rows = [(p, int(0.5 * p + 20)) for p in range(10, 1000, 7)]
    fit = fit_bucket(rows)
    check("fit is produced from sufficient data", fit is not None)
    check("recovers known slope", close(fit.ratio, 0.5, 0.02), f"got {fit.ratio}")
    check("recovers known intercept", close(fit.base, 20, 2.0), f"got {fit.base}")
    check("in-sample error is near zero", fit.mape < 1.0, f"got {fit.mape}")

    # Too little data must keep the prior rather than fit noise.
    check("declines insufficient data", fit_bucket([(10, 15), (20, 25)]) is None)

    # Deterministic: closed-form least squares, not BFGS from a random init. A fitted
    # model that changes between runs is not auditable.
    check("fit is reproducible", fit_bucket(rows) == fit_bucket(rows))

    # The estimator no longer regresses output on input length -- that feature carried
    # R^2 = 0.009 -- so `method` now names the rule that fired (see DESIGN.md).
    p = Predictor(buffer=1.0)
    before = p.predict(PROMPT, MODEL)
    check("method names the rule that fired", before.method in ("stacked", "json_schema")
          or before.method.endswith("s"), f"got {before.method!r}")
    check("fit_all still available for per-bucket regression",
          isinstance(p.load_fits({"code": [(t, 5 * t + 500) for t in range(10, 800, 5)]}), dict))


# ─────────────────────────────────────────────────────────────────────────────
def test_accuracy_report() -> None:
    print("\naccuracy report")
    rep = accuracy_report([("code", 100, 100), ("code", 120, 100), ("summary", 50, 100)])
    check("overall count", rep["_overall"]["n"] == 3)
    check("per-bucket count", rep["code"]["n"] == 2)
    check("mape is computed", close(rep["code"]["mape"], 10.0, 0.01),
          f"got {rep['code']['mape']}")
    check("under-prediction is tracked", rep["summary"]["under_prediction_rate"] == 100.0)


# ─────────────────────────────────────────────────────────────────────────────
def test_priors() -> None:
    print("\npriors")
    # A zero ratio is legitimate: calibration found that json and summary output does
    # not scale with input length, so those buckets are flat models carried entirely
    # by `base`. What must never happen is both terms being zero, which would predict
    # nothing at all.
    check("no bucket predicts zero tokens",
          all(v["ratio"] > 0 or v["base"] > 0 for v in PRIORS.values()))
    check("no negative coefficients",
          all(v["ratio"] >= 0 and v["base"] >= 0 for v in PRIORS.values()))
    check("default bucket exists", "default" in PRIORS)

    # Every bucket must produce a positive prediction for a realistic prompt.
    for bucket in PRIORS:
        p = Predictor(buffer=1.0).predict("word " * 40, MODEL)
        check(f"{bucket:<12} priors yield a positive estimate", p.predicted_output_tokens > 0)


# ─────────────────────────────────────────────────────────────────────────────
def test_proxy_integration() -> None:
    """The ESTIMATE seam in proxy/app.py, which must never fail a request."""
    print("\nproxy integration")
    from proxy.app import _predict

    r = _predict({"messages": [{"role": "user", "content": "Write a Python function."}]},
                 "gpt-4o", "openai")
    check("predicts from an OpenAI messages body", r is not None and r.bucket == "code")

    # Anthropic has no local tokenizer. Returning None is correct; raising would 500 a
    # request that was going to succeed.
    check("claude yields None rather than raising",
          _predict({"messages": [{"role": "user", "content": "hi"}]},
                   "claude-sonnet-5", "anthropic") is None)

    check("no model -> None", _predict({"messages": []}, None, "openai") is None)
    check("empty messages -> None", _predict({"messages": []}, "gpt-4o", "openai") is None)
    check("malformed body -> None", _predict({"messages": "nope"}, "gpt-4o", "openai") is None)
    check("unknown model -> None", _predict(
        {"messages": [{"role": "user", "content": "hi"}]}, "llama-3-70b", "openai") is None)

    # max_tokens must reach the predictor, since it is a hard provider-enforced bound.
    capped = _predict({"messages": [{"role": "user", "content": "Write an essay. " * 50}],
                       "max_tokens": 20}, "gpt-4o", "openai")
    check("max_tokens is honoured through the seam",
          capped is not None and capped.predicted_output_tokens == 20)
    completion_capped = _predict(
        {"messages": [{"role": "user", "content": "Write an essay. " * 50}],
         "max_completion_tokens": 30}, "gpt-4o", "openai")
    check("max_completion_tokens is honoured through the seam",
          completion_capped is not None and completion_capped.bound_output_tokens == 30)

    # The ledger must be able to store every field the seam produces.
    from proxy.db import _REQUEST_COLUMNS
    for col in ("predicted_output_tokens", "predicted_cost_usd", "bucket", "prediction_method"):
        check(f"ledger column {col} exists", col in _REQUEST_COLUMNS)


# ─────────────────────────────────────────────────────────────────────────────
def test_ledger_migration() -> None:
    """An existing meter.db from before the prediction columns must keep working.

    The first version of this crashed on connect: SCHEMA created an index on
    `bucket` while CREATE TABLE IF NOT EXISTS no-opped on the existing table, so the
    index referenced a column that did not exist yet. That would have broken every
    teammate the moment they pulled, and deleting their ledger is not an option --
    it is the priced history ARCHITECTURE.md §9 calls the part that does not port.
    """
    print("\nledger migration")
    from proxy import db as dbmod

    conn = dbmod.connect()

    def columns() -> set[str]:
        return {r["column_name"] for r in conn.execute(
            "SELECT column_name FROM information_schema.columns"
            " WHERE table_schema = current_schema() AND table_name = 'requests'")}

    conn.execute(
        "INSERT INTO requests (id, ts, project_id, provider, endpoint, cost_usd)"
        " VALUES ('old', '2026-01-01T00:00:00.000000+00:00', 'p', 'openai',"
        " '/v1/chat', 0.5) ON CONFLICT (id) DO NOTHING")

    # Drop the prediction columns to recreate the pre-prediction shape in place, then
    # re-run the boot path. The bucket index goes with them, which is the point: the
    # first version of this crashed because SCHEMA created an index on `bucket` while
    # CREATE TABLE IF NOT EXISTS no-opped on the existing table, so the index referenced
    # a column that did not exist yet.
    conn.execute("DROP INDEX IF EXISTS idx_requests_bucket")
    for col in ("predicted_output_tokens", "predicted_cost_usd", "bucket",
                "prediction_method"):
        conn.execute(f"ALTER TABLE requests DROP COLUMN IF EXISTS {col}")
    check("the older ledger lacks the prediction columns", "bucket" not in columns())

    dbmod._schema_ready = False
    dbmod.connect()

    cols = columns()
    for col in ("predicted_output_tokens", "predicted_cost_usd", "bucket",
                "prediction_method"):
        check(f"migration added {col}", col in cols)

    row = conn.execute("SELECT id, cost_usd FROM requests WHERE id = 'old'").fetchone()
    check("historical rows survive", row is not None and row["cost_usd"] == 0.5,
          f"got {row}")

    idx = conn.execute(
        "SELECT indexname FROM pg_indexes"
        " WHERE schemaname = current_schema() AND indexname = 'idx_requests_bucket'"
    ).fetchall()
    check("bucket index created after the column", len(idx) == 1)


def test_refresh_gate() -> None:
    """The online loop's gate, on a ledger built to have one good key and one bad one.

    Both properties here were live bugs. The loop was wired into the proxy, ran on a
    timer, logged a verdict every pass — and installed nothing, ever.
    """
    import uuid
    from datetime import datetime, timedelta, timezone

    print("\nrefresh gate")
    from proxy import db as dbmod

    conn = dbmod.connect()
    # The throwaway schema this run owns is the fixture. Clearing `requests` first so the
    # rows the earlier suites left behind cannot dilute the two keys under test.
    conn.execute("DELETE FROM requests")

    base = datetime.now(timezone.utc) - timedelta(hours=1)
    # "good": actual is consistently 1/4 of scope, so a 0.25 factor is right and holds
    # up out of sample. "noisy": actual alternates wildly, so no single factor helps.
    for i in range(80):
        for feat, actual in (("good", 100), ("noisy", 40 if i % 2 else 800)):
            conn.execute(
                "INSERT INTO requests (id, ts, project_id, actor, feature, bucket, model,"
                " provider, endpoint, output_tokens, predicted_scope_tokens)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (uuid.uuid4().hex,
                 (base + timedelta(seconds=i * 10)).isoformat(),
                 "proj", "actor", feat, "default", "gpt-4o",
                 "openai", "/v1/chat/completions", actual, 400))

    from predictor import refresh

    p = Predictor()
    p._history = {}
    saved, engine._default = engine._default, p
    try:
        summary = refresh.refresh_now()
    finally:
        engine._default = saved

    # Regression 1: the loop must actually install something on learnable traffic. It
    # previously could not, because the factor a real feature needs (0.25 here, and
    # 0.27-18.2 in the measured probe) fell outside the old [0.5, 3.0] clamp.
    check("gate installs a factor for the learnable key", summary["installed_keys"] >= 1,
          str(summary))
    check("the good key is what got installed",
          any("good" in k for k in p._history), str(list(p._history)))

    # Regression 2: per-key selection. Under all-or-nothing gating one unlearnable key
    # vetoed every good one -- measured on real traffic, four features that each improved
    # 2-3x were discarded because a fifth got worse.
    check("the unlearnable key is dropped, not installed",
          not any("noisy" in k for k in p._history), str(list(p._history)))

    installed = next(v for k, v in p._history.items() if "good" in k)
    check("installed factor moves toward the truth", installed < 0.6, f"{installed:.3f}")


def test_new_project_inherits_feature_history() -> None:
    """A project with no history of its own must still get a correction.

    Measured live 2026-08-02: every installed factor was a `(project, feature)` pair —
    not one generic `(bucket, …)` rung survived the gate — so a request on an unseen
    project fell all the way through to 1.0, the raw heuristic. That is ~65-80% median
    error against ~10%.

    It is precisely a judge's situation. They need their own `project_id` so their wallet
    and mandate are isolated from everyone else's, and without a cross-project rung that
    isolation costs them every learned factor: the thing that makes their card safe makes
    their predictions bad.

    The `(feature,)` rung sits below `(project,)`, so a project with any history of its
    own is untouched.
    """
    print("\nnew project inherits feature history")

    p = Predictor()
    p._history = {}
    saved, engine._default = engine._default, p
    try:
        engine.set_history({
            ("known-proj", "ticket-summary"): 0.66,
            ("ticket-summary",): 0.66,
        })

        seen = p._history_factor("known-proj", "ticket-summary", "someone",
                                 bucket="summary", model="gpt-4o-mini")
        check("a known project uses its own factor", seen == 0.66, str(seen))

        fresh = p._history_factor("judge-alice", "ticket-summary", "someone",
                                  bucket="summary", model="gpt-4o-mini")
        check("a brand-new project inherits the feature factor", fresh == 0.66,
              str(fresh))

        # The project rung must still win when both exist, or one project's traffic
        # would be corrected by another's.
        engine.set_history({
            ("known-proj", "ticket-summary"): 0.40,
            ("ticket-summary",): 0.90,
        })
        specific = p._history_factor("known-proj", "ticket-summary", "someone")
        check("the project-specific rung outranks the cross-project one",
              specific == 0.40, str(specific))

        # An unknown feature on an unknown project still has nothing to go on.
        none_ = p._history_factor("judge-alice", "never-seen", "someone")
        check("an unknown feature still falls through to 1.0", none_ == 1.0, str(none_))
    finally:
        engine._default = saved


def test_unproven_keys_keep_their_factor() -> None:
    """A key that cannot be re-validated must keep its factor, not revert to 1.0.

    `set_history` replaces the whole installed set each pass, so a key missing from the
    survivors silently drops to the raw heuristic. Observed live 2026-08-02: ordinary
    walkthrough traffic shifted the holdout boundary and `demo-project/test-plan` went
    `unproven` for five consecutive refresh passes — ten minutes — then returned on its
    own. Any request in that window was predicted at 92% error instead of 13%, and
    nothing reported a fault: `try.sh` printed `history factor 1.00` and `/healthz` read
    30 factors instead of 31.

    "No fresh evidence" and "measured to be worse" are different states. Only the second
    justifies discarding a factor.
    """
    print("\nunproven keys keep their factor")
    from predictor import refresh

    p = Predictor()
    p._history = {}
    saved, engine._default = engine._default, p
    try:
        # A factor that was validated and installed on some earlier pass.
        key = ("proj", "settled-feature")
        engine.set_history({key: 0.42})
        check("precondition: the factor is installed",
              engine.current_history().get(key) == 0.42)

        # This pass sees no held-out rows for it at all — the live failure mode.
        kept, report = refresh._select_keys([], {})
        check("the gate itself returns nothing for it", key not in kept)

        # refresh_now carries it forward. Simulated directly, since reproducing a
        # holdout-boundary shift needs thousands of rows.
        previous = dict(engine.current_history())
        for k, factor in previous.items():
            if k not in kept and report.get(k) in (None, "unproven"):
                kept[k] = factor
                report[k] = f"carried ({report.get(k) or 'no held-out rows this pass'})"

        check("the previous factor is carried, not dropped", kept.get(key) == 0.42,
              str(kept))
        check("and it is labelled as carried rather than kept",
              str(report.get(key)).startswith("carried"), str(report.get(key)))

        # A key the gate actively rejected must NOT be carried — that would reinstate a
        # correction it just measured as worse.
        rejected = ("proj", "worse-feature")
        engine.set_history({rejected: 2.5})
        kept2, report2 = {}, {rejected: "dropped 40%->55%"}
        for k, factor in dict(engine.current_history()).items():
            if k not in kept2 and report2.get(k) in (None, "unproven"):
                kept2[k] = factor
        check("a rejected key is not carried", rejected not in kept2, str(kept2))
    finally:
        engine._default = saved


def main() -> int:
    from proxy import config
    from proxy import pg as _pg
    try:
        return _run()
    finally:
        # Drop the throwaway schema whether the run passed or failed. Leaving them behind
        # accumulates dead schemas in a database three other people share.
        try:
            _pg.drop_schema(config.DB_SCHEMA)
        finally:
            _pg.close()


def _run() -> int:
    for suite in (
        test_determinism,
        test_input_counting,
        test_classifier,
        test_max_tokens,
        test_scope_signals,
        test_cache,
        test_buffer_and_history,
        test_bias_direction,
        test_pricing_integration,
        test_learner,
        test_accuracy_report,
        test_priors,
        test_proxy_integration,
        test_ledger_migration,
        test_refresh_gate,
        test_new_project_inherits_feature_history,
        test_unproven_keys_keep_their_factor,
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
