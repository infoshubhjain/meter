#!/usr/bin/env python3
"""Is the per-(project, feature) correction factor worth anything? K-fold answer.

    python scripts/history_value.py

`prequential.py` is the right harness for "does the loop learn over time", but on 200
rows spanning five features whose true outputs differ by 10x, each batch of 20 is a
different mixture and the curve mostly measures which features landed in which batch.
Batch 7 scored 18% and batch 10 scored 347% with identical factors installed.

This asks the narrower question the probe was actually paid for, with the variance
removed: fit the factor on 80% of a feature's rows, score the held-out 20%, rotate.
Every number is out-of-sample. The control -- shuffling the feature labels before
fitting -- is what separates "the factor captured real per-feature regularity" from
"any grouping of 200 rows into five buckets would have helped".
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

SRC = REPO / "data" / "templated" / "gpt-4o-mini.jsonl"
FOLDS = 5


def fit_factors(ratios: dict[str, list[float]]) -> dict[str, float]:
    """Use the installed engine's threshold, geometric shrinkage, and clamp."""
    from predictor import Predictor

    fitted = Predictor().shrink_history({(key,): (float(np.median(values)), len(values))
                                         for key, values in ratios.items()})
    return {key[0]: factor for key, factor in fitted.items()}


def ape(pred: np.ndarray, actual: np.ndarray) -> float:
    return float(np.median(np.abs(pred - actual) / actual) * 100)


def evaluate(rows: list[dict], keys: list[str]) -> tuple[float, float]:
    """K-fold: median APE without the factor, and with it. `keys` is the grouping
    used to fit -- passing a shuffled copy gives the control."""
    idx = np.arange(len(rows))
    rng = np.random.default_rng(0)
    rng.shuffle(idx)
    base, corrected = [], []

    for f in range(FOLDS):
        test = set(idx[f::FOLDS].tolist())
        # Factor per key, fitted ONLY on the training folds.
        ratios: dict[str, list[float]] = {}
        for i, r in enumerate(rows):
            if i in test:
                continue
            scope = r["predicted_scope_tokens"]
            if scope > 0:
                ratios.setdefault(keys[i], []).append(r["output_tokens"] / scope)
        factors = fit_factors(ratios)

        for i in sorted(test):
            r = rows[i]
            scope = r["predicted_scope_tokens"]
            if scope <= 0:
                continue
            a = r["output_tokens"]
            base.append(abs(r["predicted_output_tokens"] - a) / a)
            corrected.append(abs(scope * factors.get(keys[i], 1.0) - a) / a)

    return float(np.median(base) * 100), float(np.median(corrected) * 100)


def main() -> int:
    if not SRC.exists():
        sys.exit(f"missing {SRC} — run: python scripts/templated_probe.py")
    rows = [json.loads(line) for line in SRC.read_text().splitlines() if line.strip()]
    keys = [f"{r['project']}/{r['feature']}" for r in rows]

    print(f"{len(rows)} rows, {len(set(keys))} (project, feature) pairs, "
          f"{FOLDS}-fold\n")

    base, corrected = evaluate(rows, keys)
    print(f"  {'base engine (no history)':<34}{base:>8.1f}%")
    print(f"  {'+ per-(project, feature) factor':<34}{corrected:>8.1f}%")
    print(f"  {'improvement':<34}{base - corrected:>+8.1f} points")

    # CONTROL: same machinery, labels shuffled. If this also improves, the gain is
    # an artefact of fitting five free parameters, not of real per-feature signal.
    rng = np.random.default_rng(1)
    ctrl = []
    for _ in range(20):
        shuffled = list(keys)
        rng.shuffle(shuffled)
        ctrl.append(evaluate(rows, shuffled)[1])
    print(f"\n  control (labels shuffled, 20x)     {np.median(ctrl):>8.1f}%  "
          f"[{min(ctrl):.1f}–{max(ctrl):.1f}]")
    verdict = ("REAL — the factor beats every shuffled control"
               if corrected < min(ctrl) else
               "NOT PROVEN — a random grouping does about as well")
    print(f"  -> {verdict}")

    print(f"\n  {'feature':<24}{'n':>4}{'median actual':>15}{'base APE':>11}{'+factor':>10}")
    print("  " + "-" * 64)
    for k in sorted(set(keys)):
        sub = [r for r, kk in zip(rows, keys) if kk == k]
        sk = [k] * len(sub)
        b, c = evaluate(sub, sk)
        print(f"  {k.split('/')[1]:<24}{len(sub):>4}"
              f"{np.median([r['output_tokens'] for r in sub]):>15.0f}"
              f"{b:>10.1f}%{c:>9.1f}%")
    return 0


if __name__ == "__main__":
    sys.exit(main())
