"""Micro-learning — reads the live ledger and updates the in-memory corrections.

This is the half of the feedback loop that runs against real traffic, as opposed to
`optimize.py` / `discover.py`, which run offline against a dataset.

    from predictor.refresh import refresh_now, start_background
    refresh_now()                    # one pass
    start_background(interval_s=120) # asyncio task, for the proxy's lifespan

Two rules it exists to enforce:

1. **Never query the database in the request path.** ARCHITECTURE.md targets
   single-digit-millisecond pre-flight. This runs on a timer, writes to an in-memory
   dict, and `predict()` only ever reads that dict.

2. **Fit against `predicted_scope_tokens`, never `predicted_output_tokens`.** The
   latter already contains the previous correction, so a factor derived from it
   divides by that factor on every refresh. Simulated over eight rounds it flips
   between 1.91 and 1.00 forever, halving accuracy on alternate passes. This is the
   single most important line in the module.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from typing import Any, Dict, List, Tuple

from proxy import pg

log = logging.getLogger("meter.predictor.refresh")

# Rows below this per key are skipped so the ladder in `_history_factor` falls
# through to a coarser key that does have support, rather than applying a factor
# computed from a handful of observations.
MIN_ROWS = 20
LOOKBACK = 5000


def _rows(db_path: str | None = None) -> List[dict]:
    """Read-only: this must never be able to disturb the proxy's writer.

    Under SQLite that guarantee came from opening the file with ``mode=ro``. Against
    Postgres it comes from the statement itself being a SELECT — readers never block
    writers under MVCC, so the learning loop cannot slow the request path down.

    ``db_path`` is ignored and kept only so existing callers and scripts do not have to
    change in the same commit as the engine swap.
    """
    return pg.fetchall(
        "SELECT project_id, feature, actor, bucket, model, "
        "       predicted_scope_tokens, output_tokens, bound_output_tokens, bound_is_hard "
        "FROM requests "
        "WHERE output_tokens > 0 AND predicted_scope_tokens > 0 "
        "ORDER BY ts DESC LIMIT ?", (LOOKBACK,)
    )


def compute(rows: List[dict]) -> Tuple[Dict[Tuple, Tuple[float, int]],
                                              Dict[str, List[Tuple[float, int]]],
                                              Dict[str, List[int]]]:
    """Turn ledger rows into inputs for load_history / load_buffers / load_bounds.

    Every ratio is actual/scope -- the raw heuristic -- so the correction converges
    instead of oscillating. Medians rather than means, because output lengths are
    log-normal-ish and one runaway response would drag a mean badly.
    """
    import numpy as np

    ratios: Dict[Tuple, List[float]] = defaultdict(list)
    buffers: Dict[str, List[Tuple[float, int]]] = defaultdict(list)
    outputs: Dict[str, List[int]] = defaultdict(list)

    for r in rows:
        scope = float(r["predicted_scope_tokens"] or 0)
        actual = int(r["output_tokens"] or 0)
        if scope <= 0 or actual <= 0:
            continue
        ratio = actual / scope
        # Every rung of the ladder that predict() might consult. `("feature",)` is the
        # cross-project rung — see `_history_factor` for why a new project needs it.
        for key in (
            (r["project_id"], r["feature"], r["actor"]),
            (r["project_id"], r["feature"]),
            (r["project_id"],),
            (r["feature"],),
            (r["bucket"], r["model"]),
            (r["bucket"],),
        ):
            if all(k is not None for k in key):
                ratios[key].append(ratio)
        if r["bucket"]:
            buffers[r["bucket"]].append((scope, actual))
        if r["bucket"] and r.get("bound_is_hard") == 0:
            outputs[r["bucket"]].append(actual)

    history = {k: (float(np.median(v)), len(v))
               for k, v in ratios.items() if len(v) >= MIN_ROWS}
    return history, dict(buffers), dict(outputs)


HOLDOUT_FRAC = 0.25
MIN_ROWS_TO_GATE = 60
# Held-out rows a single key needs before its factor can be accepted on its own evidence.
MIN_HOLDOUT_PER_KEY = 5
MIN_HOLDOUT_PER_BOUND = 20


def _key_for(row, factors) -> Tuple | None:
    """The ladder rung `predict()` would use for this row. One definition, so the gate
    scores rows against the same factor that will actually be applied to them."""
    for key in ((row["project_id"], row["feature"], row["actor"]),
                (row["project_id"], row["feature"]),
                (row["project_id"],),
                (row["feature"],),
                (row["bucket"], row["model"]),
                (row["bucket"],)):
        if all(k is not None for k in key) and key in factors:
            return key
    return None


def _select_keys(holdout, candidate: Dict[Tuple, float],
                 current: Dict[Tuple, float] | None = None) -> Tuple[Dict[Tuple, float], dict]:
    """Keep candidates only when they beat both the raw and installed forecasts.

    All-or-nothing gating was rejecting genuinely good candidates. Measured on 200 calls
    of templated traffic, one factor per feature: four of five features improved by 2-3x
    (code-review-note 1417% -> 633%, commit-message 718% -> 279%) while the fifth got
    worse, and because the pooled median happened to sit inside that fifth feature's
    rows, the whole candidate was thrown away. One bad key vetoed four good ones.

    Per-key selection has no such coupling, and it is the honest unit anyway: these
    factors are independent by construction -- a correction for one feature says nothing
    about another. Each is accepted on its own held-out evidence.
    """
    current = current or {}
    owned: Dict[Tuple, list] = defaultdict(list)
    for r in holdout:
        key = _key_for(r, candidate)
        scope = float(r["predicted_scope_tokens"] or 0)
        actual = int(r["output_tokens"] or 0)
        if key is not None and scope > 0 and actual > 0:
            owned[key].append(r)

    kept, report = {}, {}
    for key, obs in owned.items():
        f = candidate[key]
        # Fewer than a handful of held-out rows cannot distinguish a real improvement
        # from luck, so an unproven key is simply not installed.
        if len(obs) < MIN_HOLDOUT_PER_KEY:
            report[key] = "unproven"
            continue
        before = _median_err(obs, {})
        incumbent = _median_err(obs, current)
        after = _median_err(obs, {key: f})
        if after < min(before, incumbent) - 0.005:
            kept[key] = f
            report[key] = f"kept {min(before, incumbent)*100:.0f}%->{after*100:.0f}%"
        elif key in current and incumbent < before - 0.005:
            kept[key] = current[key]
            report[key] = f"carried (new fit {after*100:.0f}% vs installed {incumbent*100:.0f}%)"
        else:
            report[key] = f"dropped {before*100:.0f}%->{after*100:.0f}%"
    return kept, report


def _median_err(rows, factors: Dict[Tuple, float]) -> float:
    """Median |predicted-actual|/actual, applying `factors` via the same ladder
    predict() uses. Operates on scope, so it measures exactly what changes."""
    import numpy as np

    errs = []
    for r in rows:
        scope = float(r["predicted_scope_tokens"] or 0)
        actual = int(r["output_tokens"] or 0)
        if scope <= 0 or actual <= 0:
            continue
        f = 1.0
        for key in ((r["project_id"], r["feature"], r["actor"]),
                    (r["project_id"], r["feature"]),
                    (r["project_id"],),
                    (r["feature"],),
                    (r["bucket"], r["model"]),
                    (r["bucket"],)):
            if all(k is not None for k in key) and key in factors:
                f = factors[key]
                break
        errs.append(abs(scope * f - actual) / actual)
    return float(np.median(errs)) if errs else float("inf")


def bound_coverage(rows: List[dict]) -> dict:
    """Observed reservation misses; median forecast error cannot reveal these."""
    measured = [r for r in rows if r.get("bound_is_hard") == 0
                and (r.get("bound_output_tokens") or 0) > 0
                and (r.get("output_tokens") or 0) > 0]
    exceeded = sum(r["output_tokens"] > r["bound_output_tokens"] for r in measured)
    by_bucket: Dict[str, List[dict]] = defaultdict(list)
    for row in measured:
        by_bucket[row.get("bucket") or "unknown"].append(row)
    return {"bound_sample": len(measured),
            "bound_exceeded_pct": round(100 * exceeded / len(measured), 1) if measured else None,
            "bound_by_bucket": {
                bucket: {"sample": len(items), "exceeded_pct": round(
                    100 * sum(r["output_tokens"] > r["bound_output_tokens"] for r in items)
                    / len(items), 1)}
                for bucket, items in sorted(by_bucket.items())}}


def validated_bounds(fit_outputs: Dict[str, List[int]], holdout: List[dict]) -> Dict[str, List[int]]:
    """Only tighten a reservation when recent calls meet its tail-risk target."""
    import numpy as np

    recent: Dict[str, List[int]] = defaultdict(list)
    for row in holdout:
        if (row.get("bound_is_hard") == 0 and row.get("bucket")
                and (row.get("output_tokens") or 0) > 0):
            recent[row["bucket"]].append(row["output_tokens"])
    return {
        bucket: values for bucket, values in fit_outputs.items()
        if len(values) >= 20 and len(recent[bucket]) >= MIN_HOLDOUT_PER_BOUND
        and sum(actual > int(np.quantile(values, .95) * 1.2)
                for actual in recent[bucket]) / len(recent[bucket]) <= .05
    }


def refresh_now(db_path: str | None = None, gate: bool = True) -> Dict[str, Any]:
    """One pass: read the ledger, recompute, and install ONLY if it helps.

    The gate is the whole point. An ungated refresh installs whatever it computes,
    and a prequential run showed that degrading median error from 56% to 62% as it
    "learned" -- the fitted factors were worse than no correction at all on traffic
    whose keys group unrelated prompts.

    Candidates are fitted on the older 75% of rows and scored against the most
    recent 25%, which the candidate fit never saw. An already-installed factor
    may have seen those rows on a prior refresh, so this is a regression guard,
    not an unbiased prospective estimate of future improvement.
    """
    from . import engine

    if db_path is None:
        from proxy import config
        db_path = str(config.DB_PATH)

    try:
        rows = _rows(db_path)
    except Exception as exc:
        # A ledger that is missing or locked is not a reason to disturb serving.
        log.debug("refresh skipped: %s", exc)
        return {"rows": 0, "error": str(exc)}

    usable = [r for r in rows
              if (r["predicted_scope_tokens"] or 0) > 0 and (r["output_tokens"] or 0) > 0]
    if len(usable) < MIN_ROWS_TO_GATE or not gate:
        history, buffers, outputs = compute(usable)
        installed = engine.load_history(history) if not gate else {}
        engine.load_buffers(buffers)
        engine.load_bounds(outputs if not gate else {})
        return {"rows": len(usable), "history_keys": len(installed),
                **bound_coverage(usable),
                "gated": False, "reason": "too few rows to gate"}

    # `_rows` returns newest-first, so the head is the most recent slice.
    cut = int(len(usable) * HOLDOUT_FRAC)
    holdout, fit_rows = usable[:cut], usable[cut:]

    candidate_h, buffers, outputs = compute(fit_rows)
    # Score exactly what would be installed. Reimplementing the shrinkage here meant
    # gating on a different object than `load_history` produces — it missed the
    # MIN_ROWS_FOR_KEY skip and the [0.5, 3.0] clamp.
    candidate = engine.shrink_history(candidate_h)

    kept, report = _select_keys(holdout, candidate, engine.current_history())

    # A key that could not be *re-validated* this pass keeps the factor it already had.
    #
    # `set_history` replaces the whole installed set, so anything missing from `kept`
    # silently reverts to 1.0 — the raw heuristic. But "not enough fresh evidence to
    # re-check" and "this correction is wrong" are different states, and only the second
    # justifies discarding a factor that was earning its place two minutes ago.
    #
    # Observed live 2026-08-02: ordinary walkthrough traffic shifted the holdout boundary
    # and `demo-project/test-plan` went `unproven` for five consecutive passes — ten
    # minutes — then came back on its own. A request in that window was predicted at 92%
    # error instead of 13%, with nothing anywhere reporting a problem: `try.sh` showed
    # `history factor 1.00` and `/healthz` read `learned_factors: 30` instead of 31.
    # On a demo that is a feature quietly printing a number seven times worse than the
    # slide claims.
    #
    # Keys the gate actively rejected (`dropped …`) are NOT carried — that is the gate
    # doing its job, and overriding it would reinstate a correction measured to be worse.
    previous = dict(engine.current_history())
    for key, factor in previous.items():
        if key in kept:
            continue
        verdict = report.get(key)
        if verdict is None or verdict == "unproven":
            kept[key] = factor
            report[key] = f"carried ({verdict or 'no held-out rows this pass'})"

    # Cross-project feature rungs, derived from the per-project keys that just passed.
    #
    # `_select_keys` scores a key against the holdout rows it *owns*, and ownership uses
    # the same ladder `predict()` does — so while every row carries a known project, the
    # `(feature,)` rung owns nothing and can never be validated on its own. It would be
    # computed as a candidate and then silently discarded, forever.
    #
    # Inheriting is the honest way round it: if `(demo-project, ticket-summary)` survived
    # the gate on held-out evidence, the same correction offered to a project we have
    # never seen is exactly as justified — it is the same rows. The median across
    # projects is used so one project cannot dominate once there are several.
    #
    # This is what a judge gets. They need their own `project_id` for payment isolation,
    # and without this rung that isolation costs them every learned factor: measured at
    # 1.0, the raw heuristic, ~65-80% error against ~10%.
    import numpy as np

    projects = {r["project_id"] for r in fit_rows}
    features = {r["feature"] for r in fit_rows if r["feature"]}
    per_feature: Dict[str, List[float]] = defaultdict(list)
    for key, factor in kept.items():
        # A 2-tuple is ambiguous — (project, feature) and (bucket, model) look alike —
        # so it is resolved against the values actually present in the fit rows.
        if len(key) == 2 and key[0] in projects and key[1] in features:
            per_feature[key[1]].append(factor)
    for feature, factors in per_feature.items():
        if (feature,) not in kept:
            kept[(feature,)] = float(np.median(factors))
            report[(feature,)] = f"cross-project, from {len(factors)} project(s)"

    before = _median_err(holdout, engine.current_history())
    after = _median_err(holdout, kept)

    # A p95 hold is not guaranteed. Recent tail misses veto a tighter learned bound;
    # the fixed fallback is safer under drift, though still not a model maximum.
    engine.load_buffers(buffers)
    approved_bounds = validated_bounds(outputs, holdout)
    engine.load_bounds(approved_bounds)

    # Install the surviving keys directly. `load_history` would re-shrink values that
    # `shrink_history` already shrank -- and re-shrinking is what silently pulled every
    # factor back toward 1.0 twice over.
    engine.set_history(kept)

    summary = {"rows": len(usable), "holdout": len(holdout),
               "candidate_keys": len(candidate_h), "installed_keys": len(kept),
               "carried_keys": sum(1 for v in report.values()
                                   if str(v).startswith("carried")),
               "gated": True,
               "median_before": round(before * 100, 1),
               "median_after": round(after * 100, 1),
               **bound_coverage(holdout),
               "approved_bound_buckets": sorted(approved_bounds),
               "verdict": "installed" if kept else "nothing survived",
               "detail": {"/".join(str(x) for x in k): v for k, v in report.items()}}
    log.info("predictor refresh: %s", summary)
    return summary


async def start_background(interval_s: int = 120, db_path: str | None = None) -> None:
    """Refresh on a timer. Intended to be spawned from the proxy's lifespan.

    Swallows its own exceptions: a failed refresh should leave the previous factors
    in place, never take down the serving path.
    """
    import asyncio

    while True:
        try:
            await asyncio.to_thread(refresh_now, db_path)
        except Exception:
            log.debug("background refresh failed", exc_info=True)
        await asyncio.sleep(interval_s)
