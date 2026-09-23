#!/usr/bin/env python3
"""Unattended batch: build the corpus, tune shrinkage, seed the demo, write a report.

    python scripts/overnight.py --dry-run     # plan and cost bound, sends nothing
    nohup python scripts/overnight.py --yes > overnight.log 2>&1 &

Designed to be launched and left. Nobody is watching, so every stage is bounded and
every failure is recorded rather than raised:

  1. CORPUS   real calls for every tag with no data yet, TPM-paced, hard spend cap.
              Rows are flushed per call, so a crash keeps everything already paid for
              and a re-run skips what is already on disk.
  2. TUNE     sweep the shrinkage constant against HELD-OUT slots only. Reports the
              best value; does not apply it. A constant that changes every prediction
              in the product is a human's decision, and the evidence will still be
              there in the morning.
  3. COLDSTART re-fit the scope heuristic on the templated corpus, held out BY
              FEATURE. The constants currently ship tuned on WildChat, which is the
              traffic we have measured to matter least. The heuristic's only job is
              predicting a feature with no history yet, so leave-features-out is the
              evaluation that matches the job.
  4. CROSS    the same tasks on Anthropic, cost decomposed into rate vs verbosity.
              Closes PLAN.md Phase 2/3 and PROPOSALS.md B11.
  5. PREQ     does the loop actually learn over this corpus, test-then-train.
  6. SEED     load the demo ledger from everything collected.
  7. VERIFY   run the full test suite and the end-to-end journey, so a morning report
              cannot claim success on a tree that no longer passes.
  8. REPORT   one markdown file with everything above.

Deliberately NOT autonomous about code changes. It gathers evidence and computes
recommendations; it does not edit the engine while nobody is looking.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

CORPUS = REPO / "data" / "templated" / "corpus.jsonl"
STAGES = REPO / "data" / "overnight-stages.json"
REPORT = REPO / "data" / "overnight-report.md"
SOURCES = ("gpt-4o-mini.jsonl", "gpt-4o-mini-v2.jsonl", "corpus.jsonl")


def log(msg: str) -> None:
    print(f"[{datetime.now(timezone.utc):%H:%M:%S}] {msg}", flush=True)


def existing_features() -> set[str]:
    have = set()
    for name in SOURCES:
        path = REPO / "data" / "templated" / name
        if path.exists():
            for line in path.read_text().splitlines():
                if line.strip():
                    have.add(json.loads(line)["feature"])
    return have


def load_all() -> list[dict]:
    rows = []
    for name in SOURCES:
        path = REPO / "data" / "templated" / name
        if path.exists():
            rows += [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    return rows


def stage_corpus(cap_usd: float, n: int, holdout: int, yes: bool) -> dict:
    from scripts.corpus_probe import TAGS

    todo = sorted(set(TAGS) - existing_features())
    if not todo:
        log("corpus: nothing to collect, every tag already has data")
        return {"tags": [], "skipped": True}

    log(f"corpus: {len(todo)} tags to collect -> {', '.join(todo)}")
    cmd = [sys.executable, str(REPO / "scripts" / "corpus_probe.py"),
           "--tags", ",".join(todo), "--n", str(n), "--holdout", str(holdout),
           "--cap-usd", str(cap_usd)]
    if yes:
        cmd.append("--yes")
    # Streamed, not captured: an unattended run that dies silently after two hours is
    # worse than one that leaves a partial log.
    proc = subprocess.run(cmd)
    return {"tags": todo, "returncode": proc.returncode}


def stage_tune() -> dict:
    """Sweep the shrinkage constant on held-out slots only.

    `k` controls geometric shrinkage toward 1.0. The shipped value is 1; compare
    it against alternatives without changing production code automatically.
    """
    import numpy as np

    from predictor import Predictor

    rows = [r for r in load_all() if r.get("output_tokens")]
    # Only rows carrying an explicit holdout flag can answer this honestly.
    tagged = [r for r in rows if "holdout" in r]
    if not tagged:
        log("tune: no held-out rows yet, skipping")
        return {}

    pred = Predictor()
    pred._history = {}
    for r in tagged:
        p = pred.predict([{"role": "user", "content": r["prompt"]}], r["model"],
                         max_tokens=r.get("max_tokens"))
        r["_scope"] = p.scope_tokens

    feats = sorted({r["feature"] for r in tagged})
    ks = (0, 1, 2, 5, 10, 15, 20, 30)
    table: dict[str, dict[int, float]] = {}
    for f in feats:
        fit = [r for r in tagged if r["feature"] == f and not r["holdout"]]
        ho = [r for r in tagged if r["feature"] == f and r["holdout"]]
        if len(fit) < 10 or len(ho) < 4:
            continue
        n = len(fit)
        raw = float(np.median([r["output_tokens"] / r["_scope"] for r in fit]))
        a = np.array([r["output_tokens"] for r in ho], float)
        s = np.array([r["_scope"] for r in ho], float)
        table[f] = {k: float(np.median(np.abs(s * np.exp(n * np.log(max(raw, 1e-9))
                                                   / (n + k)) - a) / a) * 100)
                    for k in ks}

    if not table:
        return {}
    overall = {k: float(np.median([v[k] for v in table.values()])) for k in ks}
    best = min(overall, key=overall.get)
    log(f"tune: best k={best} ({overall[best]:.1f}% median across {len(table)} features); "
        f"current k=1 is {overall[1]:.1f}%")
    return {"per_feature": table, "overall": overall, "best_k": best,
            "ks": ks, "method": "geometric"}


def save_stage(name: str, payload) -> None:
    """Checkpoint a completed stage to disk.

    The first run finished every stage and then lost the lot to a crash in the report
    writer. Stage results are expensive -- one of them is an hour of fitting -- so they
    are persisted as they complete and the report is regenerable from this file alone.
    """
    try:
        blob = json.loads(STAGES.read_text()) if STAGES.exists() else {}
    except Exception:
        blob = {}
    blob[name] = payload
    STAGES.write_text(json.dumps(blob, indent=2, default=str))


def _run_cmd(name: str, cmd: list[str], timeout: int) -> dict:
    """Run a stage, capture its tail, never raise. Nobody is awake to intervene."""
    log(f"{name}: starting")
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        out = (proc.stdout or "") + (proc.stderr or "")
        log(f"{name}: rc={proc.returncode}")
        return {"rc": proc.returncode, "out": out[-4000:]}
    except subprocess.TimeoutExpired:
        log(f"{name}: TIMED OUT after {timeout}s")
        return {"rc": -1, "out": f"timed out after {timeout}s"}
    except Exception as exc:
        log(f"{name}: {type(exc).__name__}: {exc}")
        return {"rc": -1, "out": f"{type(exc).__name__}: {exc}"}


def stage_coldstart() -> dict:
    split = REPO / "data" / "corpus-split"
    s = _run_cmd("coldstart/split",
                 [sys.executable, str(REPO / "scripts" / "split_corpus.py"),
                  "--out", str(split)], 300)
    if s["rc"] != 0:
        return {"split": s}
    # No --apply: this reports what the constants WOULD become. Applying them changes
    # every cold-start prediction, which is a decision to take awake.
    o = _run_cmd("coldstart/optimize",
                 [sys.executable, "-m", "predictor.optimize",
                  "--data", str(split), "--rounds", "3"], 5400)
    d = _run_cmd("coldstart/discover",
                 [sys.executable, "-m", "predictor.discover",
                  "--data", str(split)], 3600)
    return {"split": s, "optimize": o, "discover": d}


def stage_cross(cap_usd: float, yes: bool) -> dict:
    cmd = [sys.executable, str(REPO / "scripts" / "cross_model.py"),
           "--cap-usd", str(cap_usd)]
    if yes:
        cmd.append("--yes")
    return _run_cmd("cross-model", cmd, 3600)


def stage_prequential() -> dict:
    return _run_cmd("prequential",
                    [sys.executable, str(REPO / "scripts" / "prequential.py"),
                     "--source", "templated", "--shuffle", "--batch", "40"], 1800)


def stage_verify() -> dict:
    out = {}
    for name in ("test_predictor", "test_proxy", "test_treasury", "test_alerts"):
        out[name] = _run_cmd(name, [sys.executable, str(REPO / "tests" / f"{name}.py")], 900)
    out["journey"] = _run_cmd("e2e journey",
                              [sys.executable, str(REPO / "scripts" / "e2e_journey.py"),
                               "--offline"], 900)
    return out


def stage_seed() -> dict:
    proc = subprocess.run([sys.executable, str(REPO / "scripts" / "seed_demo.py")],
                          capture_output=True, text=True)
    tail = proc.stdout.strip().splitlines()[-1:] if proc.stdout else []
    log(f"seed: rc={proc.returncode} {tail}")
    return {"returncode": proc.returncode, "stdout": proc.stdout[-2000:]}


def write_report(corpus: dict, tune: dict, seed: dict, started: float,
                 cold=None, cross=None, preq=None, verify=None) -> None:
    import numpy as np

    rows = load_all()
    feats = sorted({r["feature"] for r in rows})
    lines = [
        "# Overnight batch report",
        "",
        f"Started {datetime.fromtimestamp(started, timezone.utc):%Y-%m-%d %H:%M} UTC, "
        f"ran {(time.time() - started) / 60:.0f} minutes.",
        "",
        f"**{len(rows)} observations across {len(feats)} feature tags.**",
        "",
    ]

    if tune:
        ks = tune["ks"]
        method = tune.get("method", "arithmetic (historical checkpoint)")
        reference_k = 1 if method == "geometric" else 20
        lines += [
            f"## Shrinkage sweep ({method}; held-out slots only)",
            "",
            "`k` pulls a fitted factor back toward 1.0. Lower trusts the data more. "
            "Every number below is median APE on slot fillings the engine never saw.",
            "",
            "| feature | " + " | ".join(f"k={k}" for k in ks) + " |",
            "|---|" + "---|" * len(ks),
        ]
        for f, v in sorted(tune["per_feature"].items()):
            lines.append(f"| `{f}` | " + " | ".join(f"{v[k]:.0f}%" for k in ks) + " |")
        o = tune["overall"]
        lines += ["| **median** | " + " | ".join(f"**{o[k]:.0f}%**" for k in ks) + " |", ""]
        lines += [
            f"**Best k = {tune['best_k']}** at {o[tune['best_k']]:.1f}%, against "
            f"{o[reference_k]:.1f}% for that run's reference k={reference_k} — "
            f"a {o[reference_k] - o[tune['best_k']]:.1f} point "
            "improvement.",
            "",
            "NOT APPLIED. Changing `k` changes every prediction the product makes, so it "
            "wants a human decision and a gate run, not an unattended edit.",
            "",
        ]

    lines += ["## Corpus", ""]
    if corpus.get("skipped"):
        lines.append("Every tag already had data; nothing collected.")
    else:
        lines.append(f"Collected {len(corpus.get('tags', []))} tags "
                     f"(exit {corpus.get('returncode')}).")
    lines += ["", "| feature | rows | median in | median out | p90/p10 |", "|---|---|---|---|---|"]
    for f in feats:
        sub = [r for r in rows if r["feature"] == f]
        o = np.array([r["output_tokens"] for r in sub], float)
        i = np.array([r["input_tokens"] for r in sub], float)
        spread = np.percentile(o, 90) / max(np.percentile(o, 10), 1)
        lines.append(f"| `{f}` | {len(sub)} | {np.median(i):,.0f} | {np.median(o):,.0f} "
                     f"| {spread:.1f}x |")

    def block(title: str, payload, tail: int = 2500) -> None:
        # `nonlocal` is required: an augmented assignment to `lines` anywhere in this
        # function would rebind it as a local and make every read above it an
        # UnboundLocalError -- which is exactly what killed the first report after
        # three hours of stage work had already succeeded.
        nonlocal lines
        if not payload:
            return
        lines.extend(["", f"## {title}", ""])
        if isinstance(payload, dict) and "out" in payload:
            lines.extend([f"exit {payload['rc']}", "", "```",
                          payload["out"].strip()[-tail:], "```"])
        else:
            for k, v in (payload or {}).items():
                lines.extend([f"### {k}", "", f"exit {v['rc']}", "",
                              "```", v["out"].strip()[-tail:], "```", ""])

    block("Cold-start refit (held out by feature)", (cold or {}).get("optimize"))
    block("Feature discovery", (cold or {}).get("discover"))
    block("Cross-model efficiency", cross)
    block("Prequential — does the loop learn", preq)
    block("Verification", verify, tail=800)
    lines += ["", "## Demo ledger", "", "```", seed.get("stdout", "").strip()[-1500:], "```"]
    REPORT.write_text("\n".join(lines) + "\n")
    log(f"report -> {REPORT}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--cap-usd", type=float, default=4.0, help="OpenAI corpus cap")
    ap.add_argument("--cross-cap-usd", type=float, default=1.5, help="Anthropic cap")
    ap.add_argument("--n", type=int, default=40)
    ap.add_argument("--holdout", type=int, default=8)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--report-only", action="store_true",
                    help="rebuild the report from data/overnight-stages.json")
    ap.add_argument("--yes", action="store_true")
    args = ap.parse_args()

    if args.report_only:
        blob = json.loads(STAGES.read_text())
        write_report(blob.get("corpus", {}), blob.get("tune", {}), blob.get("seed", {}),
                     time.time(), cold=blob.get("coldstart"), cross=blob.get("cross"),
                     preq=blob.get("prequential"), verify=blob.get("verify"))
        print(f"report rebuilt -> {REPORT}")
        return 0

    from scripts.corpus_probe import TAGS
    todo = sorted(set(TAGS) - existing_features())

    print(f"tags to collect   {len(todo)}  ({', '.join(todo) or 'none'})")
    print(f"calls             {len(todo) * args.n}")
    print(f"hard spend cap    ${args.cap_usd:.2f}")
    print(f"anthropic cap     ${args.cross_cap_usd:.2f}")
    print("stages            corpus -> shrinkage sweep -> cold-start refit -> "
          "cross-model\n                  -> prequential -> seed -> verify -> report")
    print(f"report            {REPORT}")
    if args.dry_run:
        subprocess.run([sys.executable, str(REPO / "scripts" / "corpus_probe.py"),
                        "--tags", ",".join(todo) or "rfc-draft", "--n", str(args.n),
                        "--dry-run"])
        print("\ndry run — nothing sent")
        return 0

    started = time.time()
    log("=== overnight batch starting ===")
    corpus = stage_corpus(args.cap_usd, args.n, args.holdout, args.yes)
    save_stage("corpus", corpus)
    tune = stage_tune()
    save_stage("tune", tune)
    cold = stage_coldstart()
    save_stage("coldstart", cold)
    cross = stage_cross(args.cross_cap_usd, args.yes)
    save_stage("cross", cross)
    preq = stage_prequential()
    save_stage("prequential", preq)
    seed = stage_seed()
    save_stage("seed", seed)
    verify = stage_verify()
    save_stage("verify", verify)
    write_report(corpus, tune, seed, started,
                 cold=cold, cross=cross, preq=preq, verify=verify)
    log(f"=== done in {(time.time() - started) / 60:.0f} min ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
