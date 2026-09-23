#!/usr/bin/env python3
"""Is shrinkage toward 1.0 helping or hurting? Swept three ways, on held-out slots.

    python scripts/shrinkage_sweep.py

`engine.Predictor.load_history` now uses geometric shrinkage with `k = 1`.
This replay compares that shipped value with the old `k = 20` on held-out slots.

Three questions, because the first two can each be answered misleadingly:

  1. OVERALL   which k minimises held-out error across every feature?
  2. BY SIZE   does that k survive at SMALL fit sets? The overall answer is dominated
               by features with 32 fit rows, but `MIN_ROWS_FOR_KEY` admits a factor at
               20, and a barely-shrunk factor is most exposed exactly when data is
               thin. Subsamples the fit set and refits, so the small-n regime is
               measured rather than assumed.
  3. BY FEATURE does ANY feature prefer heavy shrinkage? A global constant that helps
               on average while wrecking one workload is not obviously an improvement.

Everything is scored on slot fillings never used to fit -- the `holdout` flag written
by `corpus_probe.py`.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

SOURCES = ("gpt-4o-mini.jsonl", "gpt-4o-mini-v2.jsonl", "corpus.jsonl")
KS = (0, 1, 2, 5, 10, 20)


def load() -> list[dict]:
    from predictor import Predictor

    rows = []
    for name in SOURCES:
        p = REPO / "data" / "templated" / name
        if p.exists():
            rows += [json.loads(line) for line in p.read_text().splitlines() if line.strip()]
    rows = [r for r in rows if r.get("output_tokens") and "holdout" in r]
    if not rows:
        sys.exit("no held-out rows — run scripts/corpus_probe.py first")

    pred = Predictor()
    pred._history = {}          # the raw heuristic, so the factor is fitted from scratch
    for r in rows:
        r["_scope"] = pred.predict([{"role": "user", "content": r["prompt"]}],
                                   r["model"], max_tokens=r.get("max_tokens")).scope_tokens
    return rows


def split(rows, feat):
    fit = [r for r in rows if r["feature"] == feat and not r["holdout"]]
    ho = [r for r in rows if r["feature"] == feat and r["holdout"]]
    return fit, ho


def err(fit, ho, k) -> float:
    n = len(fit)
    raw = float(np.median([r["output_tokens"] / r["_scope"] for r in fit]))
    factor = float(np.exp(n * np.log(max(raw, 1e-9)) / (n + k)))
    a = np.array([r["output_tokens"] for r in ho], float)
    s = np.array([r["_scope"] for r in ho], float)
    return float(np.median(np.abs(s * factor - a) / a) * 100)


def main() -> int:
    rows = load()
    feats = sorted({r["feature"] for r in rows})
    usable = [f for f in feats if len(split(rows, f)[0]) >= 10 and len(split(rows, f)[1]) >= 4]
    print(f"{len(rows)} rows, {len(usable)} features with enough fit and held-out data\n")

    # ── 1. overall ───────────────────────────────────────────────────────────
    overall = {k: float(np.median([err(*split(rows, f), k) for f in usable])) for k in KS}
    print("1. OVERALL (median across features, held-out slots)")
    print("   " + "".join(f"{'k=' + str(k):>9}" for k in KS))
    print("   " + "".join(f"{overall[k]:>8.1f}%" for k in KS))
    best = min(overall, key=overall.get)
    print(f"   best k={best} at {overall[best]:.1f}%, against {overall[1]:.1f}% shipped\n")

    # ── 2. by fit-set size ───────────────────────────────────────────────────
    print("2. BY FIT-SET SIZE (subsample the fit set, refit, 40 draws each)")
    print(f"   {'fit rows':>9}" + "".join(f"{'k=' + str(k):>9}" for k in KS))
    rng = np.random.default_rng(0)
    for n_fit in (10, 15, 20, 25, 32):
        acc = {k: [] for k in KS}
        for f in usable:
            fit, ho = split(rows, f)
            if len(fit) < n_fit:
                continue
            for _ in range(40):
                sub = [fit[i] for i in rng.choice(len(fit), n_fit, replace=False)]
                for k in KS:
                    acc[k].append(err(sub, ho, k))
        if acc[KS[0]]:
            print(f"   {n_fit:>9}" + "".join(f"{np.median(acc[k]):>8.1f}%" for k in KS))
    print()

    # ── 3. by feature ────────────────────────────────────────────────────────
    print("3. BY FEATURE (does any workload prefer heavy shrinkage?)")
    print(f"   {'feature':<24}{'spread':>8}" + "".join(f"{'k=' + str(k):>8}" for k in KS)
          + f"{'best':>6}")
    prefer_high = []
    for f in usable:
        fit, ho = split(rows, f)
        o = np.array([r["output_tokens"] for r in fit], float)
        spread = np.percentile(o, 90) / max(np.percentile(o, 10), 1)
        e = {k: err(fit, ho, k) for k in KS}
        b = min(e, key=e.get)
        if b > 2:
            prefer_high.append(f)
        print(f"   {f:<24}{spread:>7.1f}x" + "".join(f"{e[k]:>7.1f}%" for k in KS)
              + f"{b:>6}")
    print(f"\n   features preferring k>2: {prefer_high or 'none'}")
    print("   no feature prefers the old k=20." if 20 not in
          {min({k: err(*split(rows, f), k) for k in KS}.items(), key=lambda t: t[1])[0]
           for f in usable} else "")
    return 0


if __name__ == "__main__":
    sys.exit(main())
