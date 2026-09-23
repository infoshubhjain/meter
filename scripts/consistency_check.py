#!/usr/bin/env python3
"""Is the result consistent, or was it a property of five templates the author wrote?

    python scripts/consistency_check.py

Set v1 (200 calls, 5 templates) lets us evaluate the shipped per-(project, feature)
factor against a fixed probe. Two things could undermine that result:

  * 80 of those 200 responses stopped at max_tokens=400, so the "actual" was a
    lower bound on natural length, not the natural length.
  * The templates were written by the same person who designed the estimator.

Set v2 is 264 calls over 8 DIFFERENT templates with max_tokens=1500, chosen so
almost nothing truncates. Each scored row is held out from its own factor fit,
but this dataset has informed later shrinkage choices: it is not a pristine
untouched test set for a paper.

Reports every metric from accuracy_report.py, base and corrected, plus the live
gated loop's own verdict -- because "does k-fold say the factor helps" and "does
the shipped feedback loop install it" are different questions and only the second
one ships.
"""

from __future__ import annotations

import json
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from scripts._scratch import scratch_ledger              # noqa: E402
from scripts.accuracy_report import stats, show          # noqa: E402

FOLDS = 5
TARGET_MEDIAN = 30.0        # the MVP target for templated traffic
TARGET_WITHIN_2X = 60.0


def kfold_corrected(rows: list[dict], keys: list[str]) -> np.ndarray:
    """Out-of-sample corrected prediction for every row.

    Each row is predicted using a factor fitted only on folds that exclude it, so no
    row ever contributes to its own correction.
    """
    idx = np.arange(len(rows))
    from scripts.history_value import fit_factors

    np.random.default_rng(0).shuffle(idx)
    out = np.zeros(len(rows))
    for f in range(FOLDS):
        test = set(idx[f::FOLDS].tolist())
        ratios: dict[str, list[float]] = {}
        for i, r in enumerate(rows):
            if i not in test and r["predicted_scope_tokens"] > 0:
                ratios.setdefault(keys[i], []).append(
                    r["output_tokens"] / r["predicted_scope_tokens"])
        fac = fit_factors(ratios)
        for i in test:
            out[i] = rows[i]["predicted_scope_tokens"] * fac.get(keys[i], 1.0)
    return out


def live_loop(rows: list[dict]) -> dict:
    """Run the SHIPPED gate against a ledger built from these rows.

    k-fold says whether the signal exists. This says whether `refresh.py` -- with its
    75/25 temporal split, MIN_ROWS_FOR_KEY, shrinkage, clamp and per-key selection --
    actually installs it. Those can disagree, and only this one ships.
    """
    from predictor import Predictor, engine, refresh

    with scratch_ledger("consistency") as conn:
        base = datetime.now(timezone.utc) - timedelta(hours=3)
        # Interleaved, because real traffic interleaves features and the gate's holdout
        # is temporal -- writing template-by-template would put whole features in the
        # holdout that the fit half never saw.
        order = list(range(len(rows)))
        np.random.default_rng(0).shuffle(order)
        for n, i in enumerate(order):
            r = rows[i]
            conn.execute(
                "INSERT INTO requests (id, ts, project_id, actor, feature, bucket, "
                "model, provider, endpoint, output_tokens, predicted_scope_tokens) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (uuid.uuid4().hex, (base + timedelta(seconds=n * 5)).isoformat(),
                 r["project"], r["actor"], r["feature"], r["bucket"], r["model"],
                 "openai", "/v1/chat/completions",
                 r["output_tokens"], r["predicted_scope_tokens"]))
        conn.commit()

        p = Predictor()
        p._history = {}
        saved, engine._default = engine._default, p
        try:
            summary = refresh.refresh_now()
        finally:
            engine._default = saved
    return {"summary": summary, "history": dict(p._history)}


def main() -> int:
    src = REPO / "data" / "templated" / "gpt-4o-mini-v2.jsonl"
    if not src.exists():
        sys.exit(f"missing {src} — run: python scripts/templated_probe.py --set v2 --yes")
    rows = [json.loads(line) for line in src.read_text().splitlines() if line.strip()]
    keys = [f"{r['project']}/{r['feature']}" for r in rows]
    trunc = sum(1 for r in rows if r["finish_reason"] != "stop")

    print(f"SET v2 — {len(rows)} calls, {len(set(keys))} features, "
          f"{trunc} truncated ({trunc/len(rows)*100:.1f}%)")
    print("out-of-fold scoring; this corpus informed later shrinkage choices\n")

    actual = np.array([r["output_tokens"] for r in rows], float)
    base = np.array([r["predicted_output_tokens"] for r in rows], float)
    corr = kfold_corrected(rows, keys)

    out = [stats(base, actual, "v2 base (no history)"),
           stats(corr, actual, "v2 + history (k-fold)")]

    # v1 alongside, so consistency is visible rather than asserted.
    v1p = REPO / "data" / "templated" / "gpt-4o-mini.jsonl"
    if v1p.exists():
        v1 = [json.loads(line) for line in v1p.read_text().splitlines() if line.strip()]
        k1 = [f"{r['project']}/{r['feature']}" for r in v1]
        a1 = np.array([r["output_tokens"] for r in v1], float)
        out.insert(0, stats(np.array([r["predicted_output_tokens"] for r in v1], float),
                            a1, "v1 base (reference)"))
        out.insert(1, stats(kfold_corrected(v1, k1), a1, "v1 + history (reference)"))
    show(out)

    print("\n  per-feature (v2), out-of-sample:")
    print(f"  {'feature':<24}{'n':>4}{'med actual':>12}{'base':>10}{'+history':>11}"
          f"{'p90 out':>10}")
    print("  " + "-" * 71)
    hit = 0
    for k in sorted(set(keys)):
        m = np.array([kk == k for kk in keys])
        b = float(np.median(np.abs(base[m] - actual[m]) / actual[m]) * 100)
        c = float(np.median(np.abs(corr[m] - actual[m]) / actual[m]) * 100)
        p90 = float(np.percentile(np.abs(corr[m] - actual[m]) / actual[m], 90) * 100)
        hit += c <= TARGET_MEDIAN
        print(f"  {k.split('/')[1]:<24}{int(m.sum()):>4}"
              f"{np.median(actual[m]):>12.0f}{b:>9.1f}%{c:>10.1f}%{p90:>9.1f}%")
    print(f"\n  features meeting the <={TARGET_MEDIAN:.0f}% target: "
          f"{hit}/{len(set(keys))}")

    # ── the shipped loop, not just the k-fold ────────────────────────────────
    print("\n  LIVE GATED LOOP (predictor/refresh.py, as it runs in the proxy)")
    live = live_loop(rows)
    s = live["summary"]
    print(f"    rows {s.get('rows')}  candidates {s.get('candidate_keys')}  "
          f"installed {s.get('installed_keys')}")
    for key, verdict in sorted(s.get("detail", {}).items()):
        print(f"      {key:<46} {verdict}")

    c = out[-1]
    print(f"\n  VERDICT vs MVP targets (templated: median <={TARGET_MEDIAN:.0f}%, "
          f"within-2x >={TARGET_WITHIN_2X:.0f}%)")
    print(f"    median APE   {c['median_ape']:.1f}%   "
          f"{'MET' if c['median_ape'] <= TARGET_MEDIAN else 'MISSED'}")
    print(f"    within 2x    {c['within_2x']:.1f}%   "
          f"{'MET' if c['within_2x'] >= TARGET_WITHIN_2X else 'MISSED'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
