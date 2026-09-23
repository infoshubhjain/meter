# Meter research and engineering audit (2026-09-23)

This is an experiment plan, not a claim that Meter has a publishable result today.
`CONTEXT.md` and `ARCHITECTURE.md` remain the project source of truth; their unresolved
"exact input" and "hard ceiling" language is tracked in `PROPOSALS.md` B23.

## What the current evidence supports

- Meter separates a typical-cost forecast from a preflight budget reservation. An
  explicit provider output cap makes the *output-token* part structural. An uncapped
  hold uses at least the forecast and learned p95 × 1.2 or fixed 4,096 fallback;
  these are statistical holds, not guarantees.
- The five-template, 200-call probe scores 82.6% → 8.1% median absolute percentage
  error with the **current** geometric shrinkage, using five-fold out-of-fold fitting.
  The second eight-template, 264-call probe scores 65.0% → 9.0%. Reproduce with
  `python scripts/history_value.py` and `python scripts/consistency_check.py`.
  These are task-template results, not general-language performance or an online
  randomized trial. The second corpus influenced later tuning, so it is not a
  never-touched test set.
- Open-ended WildChat held-out prompts remain much harder: 49.2% median error,
  897.3% p90 error (75 calls; `python scripts/accuracy_report.py`). This is the clearest
  product limitation, not a number to hide behind the template results.
- An uncapped forecast was previously clipped to a learned *statistical* hold,
  then labelled as if `max_tokens` had capped it. Only a real provider cap now
  clips the forecast; an uncapped reservation is raised to at least the forecast.
  This increases the open-ended test's p90 forecast error from the old clipped
  829.0% to 897.3%, which is less flattering but measures the actual forecast.
- A prior learner compared each new factor only with the raw 1.0 baseline. It could
  replace a better installed factor with a worse refit. The gate now compares the
  candidate with both the raw and currently installed prediction on the same
  held-out rows, and keeps the incumbent when it wins. The regression test pins it.
  Since the incumbent may have seen those rows during an earlier refresh, this is
  an operational regression guard, not unbiased proof it will win on future calls.
- In a test-then-train replay of 1,224 templated calls (19 features, shuffled with
  a fixed seed, batches of 40), first-third median error averaged 39.0% and
  last-third 15.7%. Reproduce with `python scripts/prequential.py --source
  templated --shuffle --batch 40`. This checks learning over a synthetic arrival
  order; it does not replace a deployment trace or a never-touched test set.
- A learned p95 installed without recent validation missed 80% of a later 50-call
  templated slice. The current gate rejected that bound. Only 10 final-slice calls
  ended naturally and were eligible for the uncapped-bound check; none exceeded
  the 4,096 fallback. Ten observations are too few to establish 95% coverage.

## Why the technical boundaries matter

OpenAI's [token-counting guide](https://developers.openai.com/api/docs/guides/token-counting)
explains that local tokenizers omit provider framing and can miss tools, files and
images; its provider-side count endpoint returns the full Responses input count.
Anthropic provides a [Messages token-count endpoint](https://platform.claude.com/docs/en/api/cli/messages/count_tokens)
that accepts tools, images and documents. Both would improve accuracy for their
supported APIs, but putting an external count round trip in every proxy hot path
would change latency and availability. First measure local-estimate error against
provider usage; make provider-side counting an explicit strict-mode option only
after its latency, cost, and failure policy are evaluated.

Output-length prediction is not itself novel. The [ACL 2025 output-length study](https://aclanthology.org/2025.acl-srw.61/)
and [TRAIL at ICLR 2025](https://openreview.net/pdf?id=7JhGdZvW4T) forecast
remaining generation length using model internals for scheduling. Meter's possible
contribution is different: black-box, multi-provider **preflight** prediction tied
to budget authorization/capture, drift-aware risk calibration, and the measured
trade-off among oversubscribed holds, miss rate, latency, and task quality.
Conformal calibration is a promising baseline, but its finite-sample coverage
relies on exchangeability; [adaptive conformal inference](https://arxiv.org/abs/2106.00170)
addresses changing streams. Neither paper licenses calling Meter's current p95
heuristic a hard guarantee.

## Next experiments, in dependency order

1. **Create a frozen, consented benchmark.** Collect at least several hundred
   uncapped or naturally stopped calls per traffic slice (model, task family,
   text/tools/multimodal, new vs established feature). Store only minimal
   de-identified features and provider-reported usage. Mark output-cap hits as
   censored for natural-length research, while keeping actual billed tokens for
   cost prediction. Freeze chronological train/calibration/test periods *before*
   choosing any parameters. Do not use the current tuned probe as the final test.
2. **Measure the right outcomes.** Report median and p90 absolute percentage error,
   token-weighted error, signed bias, reservation exceedance with binomial
   confidence intervals, held dollars per request, legitimate requests denied,
   added p50/p95 latency, and task quality when output caps change. Break every
   result down by traffic slice; publish sample sizes and censored fractions.
3. **Run baselines and ablations.** Compare fixed 4,096, explicit provider cap,
   global quantiles, per-bucket quantiles, per-feature history, split-conformal
   upper bounds, and an adaptive calibration variant. Remove each Meter signal
   (length instruction, task bucket, feature history, incumbent gate) in turn.
   Use a chronological replay that scores a request before updating from it.
4. **Test drift deliberately.** Inject a template change, model version change,
   new tenant, tool-schema growth, and long-response burst. A useful result is a
   Pareto curve of exceedance risk versus reserved dollars, with detection/recovery
   time and valid-task rejection measured. If the learned bound cannot beat the
   simple fallback on unseen traffic, do not ship it.
5. **Then choose a strict-budget contract.** B23 needs a team decision: reject
   unsupported/uncapped requests for ceiling-backed projects or state plainly
   that those ceilings are soft. An implicit output cap is not neutral—it can
   truncate useful work, as constrained-output studies show. Preserve the default
   behavior until this policy and its quality impact are agreed.

## Codebase audit priorities beyond the predictor

1. **Restore the hosted trial and ledger.** Render currently rejects the Vercel
   production origin on `/judge/session` CORS preflight, and Vercel's production
   ledger connection remains unverified. These are deployment settings, not an
   algorithm issue; verify a real browser session and scoped dashboard after
   correcting them. No claim of a complete public trial until that passes.
2. **Make ceiling semantics honest end to end.** The code now labels statistical
   reservations, but README/architecture language still promises hard ceilings
   and exact local input counting. B23 holds the conflicting source-of-truth
   wording for the team decision. Expose reservation type and observed misses in
   operator surfaces after the contract is settled.
3. **Keep one backend replica until shared serialization exists.** The current
   budget lock is process-local. A second replica can authorize concurrently
   against the same headroom. Use a database/Redis atomic reservation design and
   soak test before scaling horizontally; do not rely on dashboard polish here.
4. **Audit live billing and pricing.** Verify current provider prices and cache,
   reasoning, and multimodal usage fields against actual bills on a schedule;
   add new dated pricing files rather than rewriting old versions. A wrong rate
   defeats both forecast evaluation and spend enforcement.
5. **Keep research claims executable.** `history_value.py`, `accuracy_report.py`,
   `consistency_check.py`, and the shrinkage sweep now use or describe the
   shipped shrinkage. Do not copy an old reported percentage into the site
   without rerunning its named script and recording dataset, cap, and split.

## Publication path

There is a credible *research question*, not yet a paper: can a low-latency,
black-box inference proxy allocate spend headroom with calibrated tail risk
while preserving task quality and throughput under workload drift? A systems
paper would need an operational deployment or substantial trace-driven study,
multi-model/provider data, strong baselines, ablations, reproducible artifacts,
and a distinct systems contribution beyond the existing length predictors.

[MLSys 2027's research call](https://mlsys.org/Conferences/2027/CallForResearchPapers)
explicitly includes inference/serving and ML systems. [OSDI 2027](https://www.usenix.org/conference/osdi27/call-for-papers)
has an Operational Systems track, but would expect a much stronger deployed
systems result; its listed abstract and paper deadlines are December 1 and 8,
2026. A technical report or reproducible workshop submission is a more realistic
first milestone. Do not present the existing 200/264-call template probes as a
frontier result or promise acceptance at any venue.
