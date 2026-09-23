"""The predictor's public entry point. Implements DESIGN.md.

Contract for the proxy (ARCHITECTURE.md §2, step 3 ESTIMATE):

    from predictor import predict
    r = predict(messages, model="gpt-4o", payload=body)
    r.predicted_cost_usd   # forecast: dashboard, treasurer runway
    r.bound_cost_usd       # budget reservation (hard only with an explicit output cap)

Pipeline:

    0. CACHE                    hit -> return
    1. STRUCTURAL OVERRIDE      json schema -> to step 4
    2. EXPLICIT LENGTH          "in two sentences" -> to step 4      (scope.py)
    3. TASK STACKING            tasks x verb x CoT x instruction     (scope.py)
    4. SEPARATE HOLD            output cap or statistical reservation
    5. HISTORY CORRECTION       per (project, feature, actor)
    6. CLAMP                    min(prediction, max_tokens)

Three properties the proxy depends on:

  * Deterministic. Identical input always yields an identical prediction. A
    reservation that differs run to run makes the ceiling a coin flip.

  * Fast, and no I/O. Pure computation. Nothing here touches the network or a
    database -- the history correction reads an in-memory table refreshed on a timer,
    never a query in the request path.

  * Forecast and reservation are distinct. The forecast targets typical output;
    the budget hold is higher, but an uncapped hold is still not a guarantee.

The prediction never affects billing. Billing prices the provider's actual usage.
"""

from __future__ import annotations

import hashlib
import json
from collections import OrderedDict
from dataclasses import dataclass
from threading import Lock
from typing import Any, Dict, List, Optional, Tuple, Union

from proxy.pricing import Usage, price

from . import buckets, scope as scope_mod, tokenizer
from .learner import Fit, fit_all

Messages = List[Dict[str, Any]]
Payload = Union[str, Messages]

# No forecast safety multiplier: the old flat 1.30 double-corrected history.
# 1.0, deliberately. The buffer existed to buy safety by over-predicting, but safety
# is represented by a separate reservation; only an explicit output cap makes its
# output-token bound structural. A multiplicative buffer double-corrects: it and the
# history factor are BOTH fitted as actual/scope, so applying both computes
# scope x (actual/scope) x (actual/scope). A prequential run caught this as median
# error rising from 77% to 204% as the loop "learned".
DEFAULT_BUFFER = 1.0

# Bounds on a learned per-key correction factor. These were [0.5, 3.0] on the assumption
# that the scope heuristic is roughly right and history only nudges it. Measured against
# 200 real calls of templated traffic, that assumption is false: per feature, the factor
# the data actually asks for ranges from 0.27 to 18.2, because the heuristic reads the
# PROMPT and a feature's answer length is mostly a property of the TASK. The old ceiling
# clamped an 18.2 down to 3.0 and turned the loop's measured win (82.6% -> 28.8% median
# error) into a loss, so the gate correctly rejected every candidate and the loop
# silently did nothing.
#
# Widened twice, both times because it was clamping real signal rather than noise.
# First from [0.5, 3.0], when measured features wanted 0.27 to 18.2. Then from
# [0.05, 20.0], after lowering `task_code` for cold start shrank code-bucket scopes:
# `security-audit` then needed a factor of 51.7, was held to 20, and its held-out error
# went from 8% to 63%. The two constants interact -- a smaller base scope demands a
# larger correction -- so the ceiling has to clear the range the heuristic actually
# creates, not the range we expected it to.
#
# Widening is safe because the clamp was never the thing guarding against noise --
# MIN_ROWS_FOR_KEY (a key needs 20+ observations) and shrinkage toward 1.0 are. The clamp
# only has to stop the absurd, so it is set well outside the observed range rather than
# tightly around it.
FACTOR_MIN = 0.02
FACTOR_MAX = 100.0

# How hard a fitted factor is pulled back toward 1.0, via (n*raw + k) / (n + k).
# Was 20, set once and never revisited. Measured on 1,224 held-out slot fillings across
# 19 features, k=20 cost 25 points of median error: 32.1% against 6.7% at k=1, and it
# was worse for EVERY feature tested. It also holds at small fit sets -- subsampling
# down to 10 fit rows still favours k=1 (9.5% vs 58.6%), which was the obvious
# objection since MIN_ROWS_FOR_KEY admits a factor at 20 rows.
#
# The reason it was wrong: shrinkage guards against noisy estimates, but output length
# inside one prompt template varies by only 1.0-1.4x (p90/p10). There is almost no
# noise to suppress, so the blend contributed bias and nothing else -- visible as every
# held-out row being under-predicted. It is kept non-zero because the guard is still
# the right shape for a genuinely noisy feature; see scripts/shrinkage_sweep.py.
SHRINK_K = 1
TARGET_UNDER_PREDICTION = 0.15   # what a fitted buffer aims for

# Step 1.
JSON_BASE_TOKENS = 100.0
JSON_INPUT_RATIO = 0.1

MIN_PREDICTION = 15
CACHE_MAX = 10_000

# Conservative reservation fallback when no provider output cap is supplied.
# This is not a model maximum; the caller must cap output for a structural bound.
DEFAULT_FALLBACK_BOUND = 4096


@dataclass(frozen=True)
class PredictionResult:
    """Two numbers answering two different questions -- see DESIGN.md §1.

    `predicted_*` is a forecast: what this will probably cost. It drives the
    dashboard, the Treasurer's time-to-zero projection, and cost-per-outcome.

    `bound_*` drives the budget reservation. It is a hard output limit only when
    max_tokens is set; otherwise the learned p95 fallback is statistical.
    """

    input_tokens: int
    predicted_output_tokens: int
    # The raw heuristic output, before buffer/history/clamp. Carried through and
    # written to the ledger because the learner MUST fit its correction against a
    # fixed baseline: computing the factor from the corrected prediction divides by
    # the previous factor every refresh, which oscillates rather than converging.
    scope_tokens: int
    bound_output_tokens: int
    bucket: str
    predicted_cost_usd: float
    bound_cost_usd: float
    bound_is_hard: bool
    method: str
    model: str
    pricing_version: str
    tasks: Tuple[str, ...] = ()
    capped_by_max_tokens: bool = False
    history_factor: float = 1.0


class Predictor:
    """One instance per proxy process. Holds learned state; safe across threads."""

    @staticmethod
    def _load_fitted():
        """Load constants and per-bucket factors produced by predictor/optimize.py.

        Falls back to the shipped defaults when absent, so a fresh checkout works
        without a fitting run and a bad fit can be reverted by deleting one file.
        """
        import json as _json
        from pathlib import Path as _P
        path = _P(__file__).resolve().parent.parent / "data" / "fitted.json"
        if not path.exists():
            return None, {}
        try:
            blob = _json.loads(path.read_text())
            from .scope import ScopeConfig
            return ScopeConfig(**blob["config"]), blob.get("factors", {})
        except Exception:
            return None, {}

    def __init__(self, buffer: float = DEFAULT_BUFFER) -> None:
        self._default_buffer = buffer
        self._buffers: Dict[str, float] = {}
        self._bounds: Dict[str, int] = {}
        self._history: Dict[Tuple[str, ...], float] = {}
        self._fits: Dict[str, Fit] = {}
        self._cache: "OrderedDict[str, PredictionResult]" = OrderedDict()
        self._lock = Lock()
        self._cfg, self._factors = self._load_fitted()

    # --- step 0 ------------------------------------------------------------

    @staticmethod
    def _cache_key(payload: Payload, model: str, max_tokens: Optional[int],
                   response_format: Optional[str],
                   project: Optional[str], feature: Optional[str],
                   actor: Optional[str], request_extras: Optional[dict]) -> str:
        """Every input that can change the answer must be in the key.

        Attribution belongs here: it selects the history correction factor (step 5),
        so omitting it would serve one team's calibrated prediction to another team
        sending the same prompt -- silently, and worse the better the correction gets.

        sort_keys so equivalent dicts with different insertion order collide
        correctly rather than producing two entries for one logical request.
        """
        blob = json.dumps(
            {"p": payload, "m": model, "mt": max_tokens, "rf": response_format,
             "pr": project, "f": feature, "a": actor, "x": request_extras},
            sort_keys=True, default=str,
        )
        return hashlib.sha256(blob.encode()).hexdigest()

    def _cache_get(self, key: str) -> Optional[PredictionResult]:
        with self._lock:
            hit = self._cache.get(key)
            if hit is not None:
                self._cache.move_to_end(key)
            return hit

    def _cache_put(self, key: str, value: PredictionResult) -> None:
        with self._lock:
            self._cache[key] = value
            self._cache.move_to_end(key)
            # Bounded: an unbounded dict keyed on prompt content is a memory leak in a
            # long-running proxy, and the traffic that fills it fastest is exactly the
            # high-volume traffic we cannot afford to fall over on.
            while len(self._cache) > CACHE_MAX:
                self._cache.popitem(last=False)

    # --- steps 1-6 ---------------------------------------------------------

    def predict(
        self,
        payload: Payload,
        model: str,
        max_tokens: Optional[int] = None,
        *,
        response_format: Optional[str] = None,
        project: Optional[str] = None,
        feature: Optional[str] = None,
        actor: Optional[str] = None,
        request_extras: Optional[dict] = None,
    ) -> PredictionResult:
        key = self._cache_key(payload, model, max_tokens, response_format,
                              project, feature, actor, request_extras)
        cached = self._cache_get(key)
        if cached is not None:
            return cached

        input_tokens = tokenizer.count(payload, model, request_extras)
        text, _ = scope_mod._text_of(payload)
        bucket = buckets.classify(text)

        # ── 1. STRUCTURAL OVERRIDE ─────────────────────────────────────────
        if response_format == "json_object":
            raw = JSON_BASE_TOKENS + input_tokens * JSON_INPUT_RATIO
            method, tasks = "json_schema", []
        else:
            # ── 2 & 3. EXPLICIT LENGTH, else TASK STACKING ─────────────────
            raw, method, tasks = scope_mod.estimate(payload, model, self._cfg)

        # Per-bucket scale fitted on held-out data.
        raw *= self._factors.get(bucket, 1.0)

        # `scope_tokens` is recorded AFTER the bucket factor, because it is not merely a
        # diagnostic -- it is the baseline `refresh.py` fits the history factor against,
        # as `actual / predicted_scope_tokens`. Recording the pre-bucket value made those
        # two disagree: the factor was fitted against `scope` but applied to
        # `scope x bucket`, so the prediction came out as `actual x bucket_factor` --
        # inflated 2.4x to 4.7x depending on the bucket.
        #
        # It survived every offline check because those checks reproduced the same
        # mistake, multiplying scope by the history factor and omitting the bucket term.
        # Only driving real prompts through the proxy exposed it (median error 6% offline
        # against 111% live). The invariant to preserve: whatever the history factor
        # multiplies is exactly what gets written to the ledger.
        scope_tokens = max(1, int(round(raw)))

        # ── 4. SEPARATE FORECAST AND RESERVATION ───────────────────────────
        # See DEFAULT_BUFFER. The forecast optimises for accuracy; a reservation
        # may be statistical unless the caller supplies an output cap.

        # ── 5. HISTORY CORRECTION ──────────────────────────────────────────
        factor = self._history_factor(project, feature, actor, bucket, model)
        raw *= factor

        predicted = max(MIN_PREDICTION, int(round(raw)))

        # ── 6. CLAMP, and emit the bound ───────────────────────────────────
        # max_tokens CLAMPS the estimate; it must never REPLACE it. Measured: letting
        # it short-circuit the pipeline gave 594% MAPE against 192% when clamping,
        # because max_tokens is a safety valve most SDKs set by default rather than a
        # statement of intent. A team with max_tokens=4096 boilerplate would otherwise
        # get one identical prediction for every prompt they ever send.
        bound = self._bound_for(bucket, max_tokens)
        hard_cap = bool(max_tokens and max_tokens > 0)
        capped = hard_cap and predicted > bound
        if capped:
            predicted = bound
            method = f"{method}+capped"
        elif not hard_cap:
            bound = max(bound, predicted)

        pred_cost, pricing_version, _ = price(
            Usage(input_tokens=input_tokens, output_tokens=predicted), model
        )
        bound_cost, _, _ = price(
            Usage(input_tokens=input_tokens, output_tokens=bound), model
        )

        result = PredictionResult(
            input_tokens=input_tokens,
            predicted_output_tokens=predicted,
            scope_tokens=scope_tokens,
            bound_output_tokens=bound,
            bucket=bucket,
            predicted_cost_usd=pred_cost,
            bound_cost_usd=bound_cost,
            bound_is_hard=hard_cap,
            method=method,
            model=model,
            pricing_version=pricing_version,
            tasks=tuple(tasks),
            capped_by_max_tokens=capped,
            history_factor=factor,
        )
        self._cache_put(key, result)
        return result

    # --- learned state -----------------------------------------------------

    def _bound_for(self, bucket: str, max_tokens: Optional[int]) -> int:
        """Output reservation: hard with max_tokens, statistical otherwise.

        `max_tokens` is exact when the caller sets it. When they do not, falling back
        to the fixed 4096 fallback would reserve ~$0.04 of gpt-4o output on every
        request and exhaust a small project's ceiling within a couple of dozen calls.
        A learned per-bucket p95 is tighter, but tail responses can exceed it;
        the fixed 4096 fallback is not a verified model maximum either.
        """
        if max_tokens and max_tokens > 0:
            return int(max_tokens)
        with self._lock:
            learned = self._bounds.get(bucket)
        return int(learned) if learned else DEFAULT_FALLBACK_BOUND

    def load_bounds(self, observations: Dict[str, List[int]],
                    quantile: float = 0.95) -> Dict[str, int]:
        """Fit each bucket's fallback ceiling from observed outputs."""
        import numpy as np

        fitted = {b: int(np.quantile(v, quantile) * 1.2)
                  for b, v in observations.items() if len(v) >= 20}
        with self._lock:
            self._bounds = fitted
            self._cache.clear()
        return fitted

    def _buffer_for(self, bucket: str) -> float:
        with self._lock:
            return self._buffers.get(bucket, self._default_buffer)

    def _history_factor(self, project, feature, actor, bucket=None, model=None) -> float:
        """Descend a specificity ladder, taking the first level with enough data.

        A single composite key fragments the data: at 2,000 rows, keying on
        (bucket, model, user) leaves 40% of rows in cells too small to correct, so
        most factors sit at 1.0 and the learner silently does nothing. The ladder
        keeps specificity where the data supports it and degrades gracefully where it
        does not.

        Ordered most specific first, and ALL attribution rungs precede the generic
        (bucket, model) rungs: a particular customer's prompting style predicts their
        next request better than a pattern averaged over everybody's traffic, even
        when the generic rung has more rows behind it.

        `(feature,)` is the cross-project rung, and it exists for the case where a
        project is brand new. Measured 2026-08-02: every installed factor was a
        `(project, feature)` pair and not one generic rung survived the gate, so a
        request on an unseen project fell all the way through to 1.0 — the raw
        heuristic, roughly 65-80% median error against ~10% for a known project. That is
        exactly what a judge gets on their own `project_id`, which they need for payment
        isolation, so the isolation that made their card safe also made their
        predictions bad.

        It sits *below* `(project,)`, so a project with any history of its own is
        completely unaffected: this rung only fires when the first three miss. Feature
        tags are shared vocabulary (`ticket-summary`, `commit-message`), so a new project
        using them inherits what the rest of the traffic learned.
        """
        with self._lock:
            for key in (
                (project, feature, actor),
                (project, feature),
                (project,),
                (feature,),
                (bucket, model),
                (bucket,),
            ):
                if all(k is not None for k in key) and key in self._history:
                    return self._history[key]
        return 1.0

    def load_buffers(self, observations: Dict[str, List[Tuple[float, int]]]) -> Dict[str, float]:
        """Fit each bucket's SAFETY quantile. Consumed by the bound, not the forecast.

        `observations` is {bucket: [(unbuffered_scope, actual_output_tokens)]}. The
        buffer is the quantile of actual/scope that leaves only the target fraction
        of calls under-predicted -- which optimises the safety metric directly rather
        than hoping a global constant covers every bucket.
        """
        import numpy as np

        fitted: Dict[str, float] = {}
        for bucket, rows in observations.items():
            ratios = [a / s for s, a in rows if s > 0 and a > 0]
            if len(ratios) < 10:
                continue  # too few to fit; keep the default
            q = float(np.quantile(ratios, 1.0 - TARGET_UNDER_PREDICTION))
            fitted[bucket] = float(min(max(q, 0.5), 5.0))
        with self._lock:
            self._buffers = fitted
            self._cache.clear()   # cached predictions used the old buffers
        return fitted

    MIN_ROWS_FOR_KEY = 20

    def shrink_history(self, factors: Dict[Tuple[str, ...], Tuple[float, int]],
                       shrink_k: int = SHRINK_K) -> Dict[Tuple[str, ...], float]:
        """The exact transformation `load_history` applies, without installing anything.

        Exists so `refresh.py`'s held-out gate can score the factors that would ACTUALLY
        be installed. It previously reimplemented the shrinkage inline and got a
        different answer — it applied the blend but neither the MIN_ROWS_FOR_KEY skip
        nor the [0.5, 3.0] clamp, so the gate approved or rejected a candidate that
        never existed. Validating one object and installing another is the kind of bug
        that makes a gate worse than no gate, because it still reports a verdict.
        """
        import numpy as np

        out: Dict[Tuple[str, ...], float] = {}
        for key, (raw, n) in factors.items():
            # Below this the level is skipped entirely so the ladder falls through to
            # a coarser key that does have support, rather than applying a factor
            # computed from a handful of rows.
            if n < self.MIN_ROWS_FOR_KEY:
                continue
            # Shrinkage still applies on top: a level that just cleared the threshold
            # is trusted less than one with hundreds of rows.
            #
            # GEOMETRIC, not linear. These factors are multiplicative, so blending them
            # arithmetically toward 1.0 is asymmetric: it inflates factors below 1 far
            # more than it deflates factors above 1. `commit-message` needs 0.092 and a
            # linear blend returned 0.114 -- 24% high -- which showed up live as a
            # 51-token prediction for a feature whose ideal constant is 42. The
            # geometric blend returns 0.099, and across every held-out feature it takes
            # median error from 8.2% to 7.1%.
            blended = float(np.exp((n * np.log(max(raw, 1e-9))) / (n + shrink_k)))
            out[key] = float(min(max(blended, FACTOR_MIN), FACTOR_MAX))
        return out

    def set_history(self, factors: Dict[Tuple[str, ...], float]) -> None:
        """Install already-shrunk factors verbatim.

        `load_history` takes RAW (median, n) pairs and shrinks them. Callers that have
        already shrunk — the refresh gate, which must score exactly what it installs —
        need this instead, or the values get pulled toward 1.0 a second time.
        """
        with self._lock:
            self._history = dict(factors)
            self._cache.clear()

    def load_history(self, factors: Dict[Tuple[str, ...], Tuple[float, int]],
                     shrink_k: int = SHRINK_K) -> Dict[Tuple[str, ...], float]:
        """Install per-key correction factors with shrinkage toward 1.0.

        `factors` is {key_tuple: (median_actual_over_predicted, n)}. Shrinkage is not
        optional: a factor computed from two observations is noise, and applying it
        unshrunk lets a single outlier corrupt every future prediction for that key.
        """
        out = self.shrink_history(factors, shrink_k)
        with self._lock:
            self._history = out
            self._cache.clear()
        return out

    def load_fits(self, rows_by_bucket: Dict[str, List[Tuple[int, int]]]) -> Dict[str, Fit]:
        """Retained for the learner's per-bucket regression (predictor/learner.py)."""
        fits = fit_all(rows_by_bucket)
        with self._lock:
            self._fits = fits
            self._cache.clear()
        return fits

    @property
    def fits(self) -> Dict[str, Fit]:
        with self._lock:
            return dict(self._fits)

    @property
    def buffers(self) -> Dict[str, float]:
        with self._lock:
            return dict(self._buffers)

    def cache_stats(self) -> Dict[str, int]:
        with self._lock:
            return {"entries": len(self._cache), "max": CACHE_MAX}


_default = Predictor()


def predict(payload: Payload, model: str, max_tokens: Optional[int] = None,
            **kwargs: Any) -> PredictionResult:
    """Predict cost for a request. See `Predictor.predict`."""
    return _default.predict(payload, model, max_tokens, **kwargs)


def load_fits(rows_by_bucket): return _default.load_fits(rows_by_bucket)
def load_buffers(observations): return _default.load_buffers(observations)
def load_bounds(observations, quantile: float = 0.95): return _default.load_bounds(observations, quantile)
def load_history(factors, shrink_k: int = SHRINK_K): return _default.load_history(factors, shrink_k)
def set_history(factors): return _default.set_history(factors)
def shrink_history(factors, shrink_k: int = SHRINK_K): return _default.shrink_history(factors, shrink_k)
def current_fits(): return _default.fits
def current_history(): return dict(_default._history)
def current_buffers(): return _default.buffers
def cache_stats(): return _default.cache_stats()


# Back-compat: the old flat knob some callers still reference.
SAFETY_MARGIN = DEFAULT_BUFFER
