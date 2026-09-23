"use client";

import { useEffect, useRef, useState, useCallback } from "react";
import Link from "next/link";
import Lenis from "lenis";
import { gsap } from "gsap";

/**
 * The predictive-engine explainer, built as one interactive scene.
 *
 * The whole page hangs on a single metaphor: a prediction is a physical thing
 * moving through a machine. Every 3D plate, particle, and animated number serves
 * that, nothing is decoration for its own sake.
 *
 * SOURCE OF TRUTH: every constant and every figure below was read out of the
 * SHIPPED engine, not out of predictor/DESIGN.md. Those two have diverged, and
 * the divergence matters: `data/fitted.json` overrides ScopeConfig at
 * `Predictor()` construction, so the numbers that actually run are the fitted
 * ones (base_scope 80, task_code 0, cot 1.0), not the hand-written defaults.
 * Accuracy figures come from CONTEXT.md §6a, which supersedes DESIGN.md §11.
 * The worked example was produced by running predict() on that exact prompt.
 * If you change a constant, re-run the engine and update this file from output.
 *
 * 3D is CSS `preserve-3d` rather than WebGL on purpose: real depth and
 * cursor-reactive parallax, none of the WebGL failure surface, and it degrades to
 * a flat static page under prefers-reduced-motion.
 */

// The seven stages of `Predictor.predict()`, in the order the code runs them.
// `readout` is the running token figure for the worked example threaded through
// the page; null means the deterministic waterfall has produced nothing yet.
const STAGES: {
  n: string;
  tag: string;
  title: string;
  body: string;
  formula: string;
  readout: number | null;
  note: string;
}[] = [
  {
    n: "0",
    tag: "Cache",
    title: "Have we answered this exact call before?",
    body: "The key covers the payload, the model, max_tokens, and the attribution tags. Tags belong in it because they select the history correction seven stages later. Leave them out and one team gets served another team's calibrated answer, silently, and worse the better the correction gets.",
    formula: "key = sha256(payload, model, max_tokens, project, feature, actor)",
    readout: null,
    note: "cache miss",
  },
  {
    n: "1",
    tag: "Structural override",
    title: "Is the shape of the answer already fixed?",
    body: "A JSON-schema response is bounded by its own structure. The input term stays in, because pulling entities out of a 10k-token document is not the same size job as describing one user, and a flat constant would repeat the mistake this whole engine exists to fix.",
    formula: "scope = 100 + input_tokens x 0.1",
    readout: null,
    note: "not schema-bound",
  },
  {
    n: "2",
    tag: "Explicit length",
    title: "Did the prompt just tell us the length?",
    body: "If the user stated it, we do arithmetic instead of guessing, and this is the most accurate rule in the system. Coverage is the highest-value detail in the engine: one missed hyphenated form, two-sentence against in two sentences, moved total error from 84% to 192%. More than half the error came from a single hyphen.",
    formula: '"in three sentences" -> 3 x 35 = 105',
    readout: null,
    note: "no length stated",
  },
  {
    n: "3",
    tag: "Task stacking",
    title: "No hard signal. Read the scope of the work.",
    body: "A prompt is rarely one task. Summarize this AND write the query is both, so scopes add rather than compete. Five multipliers then read intent: how forceful the verb is, whether reasoning is being demanded, terse against exhaustive, how much of the message is instruction rather than pasted material, and whether it opens with a generation verb.",
    formula:
      "scope = min(80 + sum(tasks), 1500)\n      x verb x cot x qualitative x instruction x imperative",
    readout: 713,
    note: "scope built",
  },
  {
    n: "4",
    tag: "Bucket factor",
    title: "Scale it to how this kind of task actually behaves.",
    body: "A fitted per-bucket multiplier, searched offline against held-out calls and loaded from data/fitted.json at startup. This is the number written to the ledger as scope_tokens, and it has to be, because it is the fixed baseline the learner fits its correction against. Fit against anything downstream and each refresh divides by the last one and oscillates forever.",
    formula: "scope_tokens = scope x factor[bucket]   (code = 4.03)",
    readout: 2873,
    note: "bucket scaled",
  },
  {
    n: "5",
    tag: "History",
    title: "Learn how this one team prompts.",
    body: "Teams are consistent, because their prompts come from one template written by one person. Inside a single template, observed output length varies by only 1.0 to 1.4x, against 10x across templates. That regularity is the entire mechanism. The factor is a shrunk median of actual over predicted, looked up down a ladder from most specific to least.",
    formula:
      "factor = exp( n x ln(median(actual/scope)) / (n + 1) )\n(project,feature,actor) -> (project,feature) -> (project)\n  -> (feature) -> (bucket,model) -> (bucket) -> 1.0",
    readout: 2873,
    note: "no history yet, 1.0",
  },
  {
    n: "6",
    tag: "Clamp and bound",
    title: "Two numbers leave the engine.",
    body: "max_tokens clamps the forecast, it never replaces it. Letting it short-circuit the pipeline measured 594% error against 192% for clamping, because max_tokens is a safety valve most SDKs set by default, not a statement of intent. A team with 4096 boilerplate would otherwise get one identical prediction for every prompt they ever send.",
    formula: "predicted = min(max(15, scope x factor), bound)\nbound = max_tokens, else p95[bucket] x 1.2, else 4096",
    readout: 2873,
    note: "bound 4096",
  },
];

// Shipped values, read from data/fitted.json + engine.py. `fitted` means the
// offline optimizer chose it; `neutral` means the optimizer drove a cue we hand
// wrote down to no effect, which is a result worth showing rather than hiding.
const CONSTANTS: [string, string, "measured" | "property" | "fitted" | "neutral"][] = [
  ["tokens per word", "1.33", "property"],
  ["tokens per sentence", "35", "fitted"],
  ["base conversational scope", "80", "fitted"],
  ["summary / code / extract / search", "+250 / +0 / +50 / +400", "fitted"],
  ["verb intensity, build / fix", "x1.2 / x0.15", "fitted"],
  ["chain-of-thought cue", "x1.0", "neutral"],
  ["opens with a generation verb", "x1.2", "measured"],
  ["safety buffer on the forecast", "none", "measured"],
  ["shrinkage k, geometric", "1", "measured"],
];

const PROV_LABEL = {
  measured: "measured",
  property: "property of English",
  fitted: "fitted offline",
  neutral: "tuned to neutral",
} as const;

// The worked example, produced by running predict() on this exact prompt against
// the shipped engine. `v` marks the animated running total.
const TRACE: { t: string; cls?: string; v?: number }[] = [
  { t: 'prompt  "Summarize this incident and write the SQL' },
  { t: '         query that finds every affected account."' },
  { t: "" },
  { t: "input tokens, counted not guessed", cls: "c" },
  { t: "  tiktoken o200k_base", v: 16 },
  { t: "bucket  code        no length stated, no schema", cls: "c" },
  { t: "" },
  { t: "3A  task scopes add", cls: "c" },
  { t: "  base 80 + summary 250 + code 0", v: 330 },
  { t: "3B  verb intensity   every -> high", cls: "c" },
  { t: "  x 1.2", v: 396 },
  { t: "3C  chain-of-thought   none", cls: "c" },
  { t: "  x 1.0", v: 396 },
  { t: "3D  instruction to context   81 chars", cls: "c" },
  { t: "  x 1.5", v: 594 },
  { t: "3E  opens with a generation verb", cls: "c" },
  { t: "  x 1.2", v: 713 },
  { t: "4   bucket factor   code", cls: "c" },
  { t: "  x 4.031", v: 2873 },
  { t: "5   history factor   unseen feature", cls: "c" },
  { t: "  x 1.000", v: 2873 },
  { t: "6   clamp against bound 4096", cls: "c" },
  { t: "  predicted_output_tokens", v: 2873 },
  { t: "  bound_output_tokens", v: 4096 },
  { t: "" },
  { t: "  predicted_cost_usd   $0.028770", cls: "s" },
  { t: "  bound_cost_usd       $0.041000", cls: "s" },
];

// ARCHITECTURE.md §2. The predictor is step 3, and the point of the diagram is
// that it is the only step with nothing to wait on.
const LIFECYCLE: { n: string; name: string; detail: string; self?: boolean }[] = [
  { n: "1", name: "Authenticate", detail: "resolve key to project" },
  { n: "2", name: "Attribute", detail: "feature, actor, trace" },
  { n: "3", name: "Estimate", detail: "this engine", self: true },
  { n: "4", name: "Reserve", detail: "hold bound_cost_usd" },
  { n: "5", name: "Breaker", detail: "throttled? revoked?" },
  { n: "6", name: "Forward", detail: "stream, tee for usage" },
  { n: "7", name: "Capture", detail: "price actual, release" },
];

const ACCURACY: [string, string, string, string][] = [
  ["Templated traffic, held out", "6.8%", "96.1%", "1,224 calls / 32 features"],
  ["Templated, live through the proxy", "9.7%", "97%", "64 fresh prompts"],
  ["Templated, cold start with no history", "79.5%", "not measured", "same rows"],
  ["Open-ended strangers' prompts", "49.2%", "54.7%", "75, locked test set"],
];

const REDUCED = () =>
  typeof window !== "undefined" &&
  window.matchMedia("(prefers-reduced-motion: reduce)").matches;

export function PredictorPage() {
  const [activeStage, setActiveStage] = useState(0);
  const readoutRef = useRef<HTMLSpanElement>(null);
  const engineRef = useRef<HTMLDivElement>(null);
  const rootRef = useRef<HTMLDivElement>(null);

  // Smooth momentum scroll, plus the two scroll-driven behaviours: reveal-on-enter
  // and the pipeline's active stage.
  //
  // This deliberately does NOT use GSAP ScrollTrigger, which is what it used to
  // use and which shipped to main broken. ScrollTrigger measures every trigger's
  // start/end once at creation and then relies on being fed scroll updates. On
  // this page it ended up never firing at all: `gsap.from` had already stamped
  // opacity 0 onto all 48 `.reveal` elements, so every section below the hero was
  // invisible, permanently, at any scroll position. Verified dead by dispatching
  // both resize and scroll at it and watching nothing change.
  //
  // IntersectionObserver has no measurement cache to invalidate, so a webfont
  // reflow or a late layout shift cannot desynchronise it. It is also what
  // `Terminal` below already uses, so the page now has one mechanism, not two.
  useEffect(() => {
    const root = rootRef.current;
    if (!root || REDUCED()) return;

    // Opt in to the hidden-then-reveal state only now that the observers below
    // are about to be wired. The hidden state lives in CSS under `.px-js`, so if
    // this effect never runs (script error, chunk failure, reduced motion) the
    // page renders fully visible rather than blank. Content first, motion second.
    root.classList.add("px-js");

    const lenis = new Lenis({ duration: 1.1, smoothWheel: true });
    const raf = (time: number) => lenis.raf(time * 1000);
    gsap.ticker.add(raf);
    gsap.ticker.lagSmoothing(0);

    const revealIO = new IntersectionObserver(
      (entries) => {
        for (const e of entries) {
          if (!e.isIntersecting) continue;
          e.target.classList.add("in");
          revealIO.unobserve(e.target); // reveal once, then stop paying for it
        }
      },
      // Enter slightly before the element's top edge reaches the fold, matching
      // the old "top 88%" feel without the fragility.
      { rootMargin: "0px 0px -12% 0px", threshold: 0.01 },
    );
    root.querySelectorAll(".reveal").forEach((el) => revealIO.observe(el));

    // A thin band across the middle of the viewport: with 45% cut from the top
    // and bottom, only the step currently crossing the centre counts as
    // intersecting, which is the behaviour the sticky readout wants.
    const steps = Array.from(root.querySelectorAll<HTMLElement>(".pipe-step"));
    const stepIO = new IntersectionObserver(
      (entries) => {
        for (const e of entries) {
          if (!e.isIntersecting) continue;
          const i = steps.indexOf(e.target as HTMLElement);
          if (i >= 0) setActiveStage(i);
        }
      },
      { rootMargin: "-45% 0px -45% 0px", threshold: 0 },
    );
    steps.forEach((el) => stepIO.observe(el));

    return () => {
      revealIO.disconnect();
      stepIO.disconnect();
      gsap.ticker.remove(raf);
      lenis.destroy();
      root.classList.remove("px-js");
    };
  }, []);

  // Tween the big engine readout whenever the active stage changes.
  useEffect(() => {
    const el = readoutRef.current;
    if (!el) return;
    const target = STAGES[activeStage].readout;
    if (target == null) {
      el.textContent = "----";
      return;
    }
    if (REDUCED()) {
      el.textContent = target.toLocaleString();
      return;
    }
    const from = Number(el.textContent?.replace(/[^0-9]/g, "")) || 0;
    const obj = { v: from };
    const tween = gsap.to(obj, {
      v: target,
      duration: 0.9,
      ease: "expo.out",
      onUpdate: () => (el.textContent = Math.round(obj.v).toLocaleString()),
    });
    return () => {
      tween.kill();
    };
  }, [activeStage]);

  // Cursor-reactive depth on the hero engine.
  const onHeroMove = useCallback((e: React.MouseEvent) => {
    if (REDUCED()) return;
    const el = engineRef.current;
    if (!el) return;
    const r = el.getBoundingClientRect();
    const px = (e.clientX - r.left) / r.width - 0.5;
    const py = (e.clientY - r.top) / r.height - 0.5;
    el.style.setProperty("--rx", `${(-py * 16).toFixed(2)}deg`);
    el.style.setProperty("--ry", `${(px * 20).toFixed(2)}deg`);
  }, []);
  const onHeroLeave = useCallback(() => {
    const el = engineRef.current;
    if (el) {
      el.style.setProperty("--rx", "0deg");
      el.style.setProperty("--ry", "0deg");
    }
  }, []);

  return (
    <div className="px" ref={rootRef}>
      <Topbar />

      {/* ── HERO ─────────────────────────────────────────────── */}
      <section
        className="px-hero"
        onMouseMove={onHeroMove}
        onMouseLeave={onHeroLeave}
      >
        <div className="px-hero-grid wrap">
          <div className="px-hero-copy">
            <p className="section-label">The predictive engine</p>
            <h1 className="px-h1">
              We price the call <em>before</em> we make it.
            </h1>
            <p className="px-lede">
              Every other cost tool reads the bill after the money is gone. Meter
              reads the prompt and forecasts the bill first, in about three
              hundredths of a millisecond, with no network and no database. That
              forecast is what a budget ceiling reserves against, which is the
              only reason the ceiling can hold.
            </p>
            <div className="px-hero-cta-row">
              <a href="#engine" className="hero-cta">
                See it compute <span className="arr">↓</span>
              </a>
              <span className="px-hero-stat">0.031ms · no I/O · deterministic</span>
            </div>
          </div>

          {/* The engine object: seven translucent plates in real 3D, a token
              falling through them. Tilts toward the cursor. */}
          <div className="px-engine-stage">
            <div className="px-engine-tilt">
              <div className="px-engine" ref={engineRef}>
                {STAGES.map((s, i) => (
                  <div
                    className="px-plate"
                    key={s.n}
                    style={{ "--i": i } as React.CSSProperties}
                  >
                    <span className="px-plate-n">{s.n}</span>
                    <span className="px-plate-tag">{s.tag}</span>
                  </div>
                ))}
                <div className="px-token" aria-hidden="true" />
              </div>
            </div>
          </div>
        </div>
        <div className="px-scrollhint" aria-hidden="true">
          <span>Scroll</span>
          <span className="px-drop" />
        </div>
      </section>

      {/* ── WHERE IT SITS ────────────────────────────────────── */}
      <section className="px-sec wrap">
        <p className="section-label reveal">Where it sits</p>
        <h2 className="px-h2 reveal">
          One step of seven, and the only one{" "}
          <span className="px-grad">with nothing to wait on.</span>
        </h2>
        <p className="px-body reveal">
          Every request through Meter runs the same lifecycle. Steps 1 to 5 have
          to finish before a single byte reaches the provider, so the estimate
          cannot afford a database round trip. It does not make one. The engine
          is pure computation over the prompt already in hand.
        </p>
        <Lifecycle />
        <div className="px-archnote reveal">
          <div>
            <span className="px-archnote-k">The forecast is not decoration.</span>
            <p>
              Step 4 reserves real budget against it before the call goes out.
              Reserving after the fact is the bug: a thousand simultaneous
              requests all read the same healthy balance and all proceed.
            </p>
          </div>
          <div>
            <span className="px-archnote-k">Learning runs off the hot path.</span>
            <p>
              A background task refits from the ledger every 120 seconds into an
              in-memory dict. The request path only ever reads that dict, so the
              engine gets smarter without getting slower.
            </p>
          </div>
        </div>
      </section>

      {/* ── PROBLEM: interactive scatter ─────────────────────── */}
      <section className="px-sec wrap">
        <p className="section-label reveal">Why this is genuinely hard</p>
        <h2 className="px-h2 reveal">
          The obvious model is <span className="px-grad">wrong</span>, and we
          measured how wrong.
        </h2>
        <p className="px-body reveal">
          The tempting formula is <code>output ≈ ratio × input_tokens</code>.
          Longer prompt, longer answer. Toggle it on and watch the fit fall apart
          against real calls.
        </p>
        <Scatter />
        <div className="px-statrow reveal">
          {[
            ["+0.096", "correlation between input and output length"],
            ["0.9%", "of output variance that input length explains"],
            ["2", "task buckets that were negatively correlated"],
          ].map(([n, l]) => (
            <div className="px-stat" key={n}>
              <span className="px-stat-n">{n}</span>
              <span className="px-stat-l">{l}</span>
            </div>
          ))}
        </div>
        <blockquote className="px-quote reveal">
          Output length is set by the <b>scope of the work requested</b>, not by
          the length of the request.
        </blockquote>
        <p className="px-body px-muted reveal" style={{ marginTop: 32 }}>
          Build me a CRM is four tokens and produces five thousand. Fix this typo,
          followed by an 800-token file, produces twelve. Any model keyed on input
          length has it exactly backwards.
        </p>
      </section>

      {/* ── TWO NUMBERS: 3D flip cards ───────────────────────── */}
      <section className="px-sec wrap">
        <p className="section-label reveal">The structural trick</p>
        <h2 className="px-h2 reveal">
          Every call returns two numbers.{" "}
          <span className="px-grad">Hover to turn them over.</span>
        </h2>
        <p className="px-body reveal">
          Conflating these is why the first version got both wrong. A forecast
          wants to be accurate. A ceiling check wants to be safe. Those are
          different objectives and one number cannot serve both.
        </p>
        <div className="px-flips reveal">
          <FlipCard
            tag="predicted_output_tokens"
            front="What will this probably cost?"
            back="Dashboard figures, Treasurer runway, cost per outcome. Accuracy is the job, so it carries no safety padding at all."
          />
          <FlipCard
            bound
            tag="bound_output_tokens"
            front="What could it cost at worst?"
            back="The hard ceiling reserves against this one. With max_tokens set, output cannot exceed it, so the guarantee is structural rather than statistical."
          />
        </div>
        <div className="px-fastpath reveal" style={{ maxWidth: "none" }}>
          <span className="px-fastpath-tag">the bug this fixed</span>
          <p>
            The forecast used to carry a 1.30 safety buffer. It was removed on
            purpose. The buffer and the history factor are both fitted as actual
            over scope, so applying both computed the correction twice and median
            error <b>rose from 77% to 204%</b> as the loop learned. Safety now
            lives entirely in the bound, where it cannot corrupt the forecast.
          </p>
        </div>
      </section>

      {/* ── PIPELINE: scroll-driven engine readout ───────────── */}
      <section className="px-sec wrap" id="engine">
        <p className="section-label reveal">The pipeline, end to end</p>
        <h2 className="px-h2 reveal">
          Seven stages. Hard data exits early, soft signals stack.
        </h2>
        <p className="px-body reveal">
          Stages 0 to 2 are a deterministic waterfall: if hard evidence exists,
          take it and leave. Only when none does do the soft signals compound, and
          each of those terms is bounded so a keyword-stuffed prompt cannot run
          away. Scroll, and the readout on the left carries one real prompt
          through every stage.
        </p>
        <div className="px-pipe-layout">
          {/* Sticky readout: the number the engine is holding right now. */}
          <aside className="px-readout">
            <div className="px-readout-card">
              <span className="px-readout-label">predicted tokens</span>
              <span className="px-readout-n" ref={readoutRef}>
                ----
              </span>
              <span className="px-readout-note">
                {STAGES[activeStage].note}
              </span>
              <div className="px-readout-track">
                {STAGES.map((s, i) => (
                  <span
                    key={s.n}
                    className={`px-readout-pip${i === activeStage ? " on" : ""}${
                      i < activeStage ? " done" : ""
                    }`}
                  />
                ))}
              </div>
            </div>
          </aside>

          <ol className="px-pipe">
            {STAGES.map((s, i) => (
              <li
                className={`pipe-step px-pipe-step${
                  i === activeStage ? " active" : ""
                }`}
                key={s.n}
              >
                <div className="px-pipe-num">{s.n}</div>
                <div className="px-pipe-body">
                  <span className="px-pipe-tag">{s.tag}</span>
                  <h3 className="px-pipe-title">{s.title}</h3>
                  <p className="px-pipe-text">{s.body}</p>
                  <code className="px-formula">{s.formula}</code>
                </div>
              </li>
            ))}
          </ol>
        </div>
      </section>

      {/* ── THE WHOLE FORMULA ────────────────────────────────── */}
      <section className="px-sec wrap">
        <p className="section-label reveal">All of it, on one line</p>
        <h2 className="px-h2 reveal">
          The output-token formula, <span className="px-grad">whole.</span>
        </h2>
        <p className="px-body reveal">
          Everything above collapses to this. Read it inside out: build a scope
          from the prompt, scale it by what this kind of task normally does, scale
          again by what this specific team normally does, then hold it under a
          ceiling it structurally cannot cross.
        </p>
        <Formula />
      </section>

      {/* ── WORKED EXAMPLE: self-typing terminal ─────────────── */}
      <section className="px-sec wrap">
        <p className="section-label reveal">Watch it compute</p>
        <h2 className="px-h2 reveal">
          One prompt, <span className="px-grad">every step shown.</span>
        </h2>
        <p className="px-body reveal">
          This is not a mock-up. These are the numbers{" "}
          <code>predict()</code> returns for that prompt on the shipped engine.
        </p>
        <Terminal />
        <div className="px-fastpath reveal">
          <span className="px-fastpath-tag">fast path</span>
          <p>
            Had the prompt said <b>&quot;in three sentences&quot;</b>, stage 2
            fires first and short-circuits all of it: <code>3 × 35 = 105</code>,
            then straight to the bucket factor. A stated length is the most
            accurate rule we have, so we never guess past it.
          </p>
        </div>
      </section>

      {/* ── RESULTS ──────────────────────────────────────────── */}
      <section className="px-sec wrap">
        <p className="section-label reveal">Does it actually work</p>
        <h2 className="px-h2 reveal">
          Measured on real calls, <span className="px-grad">held out.</span>
        </h2>
        <p className="px-body reveal">
          A prediction engine that quotes one accuracy number is hiding
          something, because accuracy depends entirely on the shape of the
          traffic. Here is every shape we tested, including the one we are worst
          at.
        </p>
        <Accuracy />
        <div className="px-archnote reveal">
          <div>
            <span className="px-archnote-k">What we are good at.</span>
            <p>
              Templated production traffic, which is what agent workloads
              actually are. 24 of 32 feature tags sit at or under 15% median
              error. Best is <code>entity-tag</code> at 0.9%.
            </p>
          </div>
          <div>
            <span className="px-archnote-k">What we are not.</span>
            <p>
              Open-ended prompts from strangers sit at 49% and that is near a hard
              ceiling. Output spread is <code>std(log) = 1.16</code> and our
              features explain R² of 0.28, where reaching 30% error would need
              roughly 0.88.
            </p>
          </div>
          <div>
            <span className="px-archnote-k">The honest onboarding claim.</span>
            <p>
              A brand new feature tag starts around 80% error and needs about 20
              calls of its own. History does not generalise between features, and
              we measured that rather than assuming it.
            </p>
          </div>
        </div>
      </section>

      {/* ── HONESTY: provenance ──────────────────────────────── */}
      <section className="px-sec wrap">
        <p className="section-label reveal">What is measured, what is fitted</p>
        <h2 className="px-h2 reveal">
          We label every constant,{" "}
          <span className="px-grad">including the ones we lost.</span>
        </h2>
        <p className="px-body reveal">
          A number you cannot trace is a number you cannot trust. These are the
          values the engine loads at startup, not the ones in the design doc. Note
          the two marked tuned to neutral: we hand wrote those cues, an offline
          search over real traffic found they earned nothing, and we shipped the
          search result instead of our own idea.
        </p>
        <div className="px-table reveal">
          {CONSTANTS.map(([name, val, kind]) => (
            <div className="px-table-row" key={name}>
              <span className="px-table-name">{name}</span>
              <span className="px-table-val">{val}</span>
              <span className={`px-badge ${kind}`}>{PROV_LABEL[kind]}</span>
            </div>
          ))}
        </div>
        <p className="px-body px-muted reveal" style={{ marginTop: 30 }}>
          The optimizer went further than was comfortable. On real traffic our
          task keywords fire on 17.4% of prompts and chain-of-thought cues on
          1.7%. Predicting a flat constant per bucket and ignoring the prompt
          entirely scores 51.4% against 44.2% for the full machinery, so the scope
          extraction is worth about seven points, not the bulk of the estimate.
          That is written down here rather than quietly dropped.
        </p>
      </section>

      {/* ── THE FEAT: 3D loop ────────────────────────────────── */}
      <section className="px-sec wrap">
        <p className="section-label reveal">Why this is a first</p>
        <h2 className="px-h2 reveal">
          The loop the prior art left <span className="px-grad">dead.</span>
        </h2>
        <p className="px-body reveal">
          Pre-flight cost estimators exist. The ones we evaluated stopped at the
          guess: they never fed real usage back, so their learning tier was
          permanently inert. Closing that loop is the whole contribution here. At
          capture, the actual token count lands in the ledger beside the
          prediction that reserved for it, and a gated background fit turns those
          pairs into a per-feature correction.
        </p>
        <LoopRing />
        <div className="px-statrow reveal">
          {[
            ["82.6% → 28.8%", "median error once the loop installs, k-fold out of sample"],
            ["65.0% → 31.6%", "the same method on a second, independent set of 8 templates"],
            ["7 of 8", "features the live gate accepted, every one an improvement"],
          ].map(([n, l]) => (
            <div className="px-stat" key={n}>
              <span className="px-stat-n px-stat-sm">{n}</span>
              <span className="px-stat-l">{l}</span>
            </div>
          ))}
        </div>
        <p className="px-body reveal">
          The gate is per key, not all or nothing. An earlier version scored the
          whole candidate batch on a pooled median, so one bad feature vetoed four
          good ones and the loop silently installed nothing for days. It also
          scored a shrunk candidate and then installed the raw one, which means it
          validated an object that never existed. Both are fixed and pinned by a
          test, and both are the kind of bug that only appears when you boot the
          real thing against a real ledger.
        </p>
      </section>

      {/* ── CTA ──────────────────────────────────────────────── */}
      <section className="px-cta wrap">
        <h2 className="reveal">
          Every row in the ledger was <em>reserved before it happened.</em>
        </h2>
        <p className="reveal">
          See the predictions land against their actuals, live, on the dashboard.
        </p>
        <Link href="/dashboard" className="hero-cta reveal">
          Open dashboard <span className="arr">→</span>
        </Link>
      </section>

      <footer className="px-footer wrap">
        <span>Meter — the autonomous inference treasurer</span>
        <span>every figure read from the shipped engine and CONTEXT.md §6a</span>
      </footer>
    </div>
  );
}

/* ─────────────────────────────────────────────────────────── */

function Topbar() {
  return (
    <header className="site-header">
      <Link href="/" className="wordmark">METER<span>.</span></Link>
      <nav><Link href="/docs">Documentation</Link><Link href="/dashboard">Control room</Link></nav>
      <Link href="/try" className="header-cta">Try the system <span>↗</span></Link>
    </header>
  );
}

/** ARCHITECTURE.md §2, with step 3 called out as the one this page is about. */
function Lifecycle() {
  return (
    <div className="px-arch reveal">
      <div className="px-arch-rail">
        {LIFECYCLE.map((s) => (
          <div
            className={`px-arch-step${s.self ? " self" : ""}`}
            key={s.n}
          >
            <span className="px-arch-n">{s.n}</span>
            <span className="px-arch-name">{s.name}</span>
            <span className="px-arch-detail">{s.detail}</span>
          </div>
        ))}
      </div>
      <div className="px-arch-legend">
        <span className="px-arch-brace">
          steps 1 to 5 complete before the provider sees a byte
        </span>
      </div>
      <div className="px-arch-offpath">
        <span className="px-arch-offtag">off the request path</span>
        <p>
          <code>predictor/refresh.py</code> reads the ledger on a 120 second
          timer, fits on the older 75% of rows, scores against the newest 25%,
          and installs only the per-feature factors that beat their own held-out
          rows. It writes into memory that <code>predict()</code> reads. It never
          queries anything during a request.
        </p>
      </div>
    </div>
  );
}

/** The whole estimate as one annotated expression. */
function Formula() {
  return (
    <div className="px-formula-block reveal">
      <div className="px-fb-line">
        <span className="px-fb-lhs">scope</span>
        <span className="px-fb-eq">=</span>
        <span className="px-fb-rhs">
          min( <b>base</b> + Σ <b>task_scopes</b>, 1500 )
          <br />
          <span className="px-fb-ind">
            × verb × cot × qualitative × instruction × imperative
          </span>
        </span>
      </div>
      <div className="px-fb-line">
        <span className="px-fb-lhs">scope_tokens</span>
        <span className="px-fb-eq">=</span>
        <span className="px-fb-rhs">
          scope × <b>factor[bucket]</b>
          <em>the value written to the ledger, and the baseline the learner fits against</em>
        </span>
      </div>
      <div className="px-fb-line">
        <span className="px-fb-lhs">history</span>
        <span className="px-fb-eq">=</span>
        <span className="px-fb-rhs">
          exp( n · ln( median( actual / scope_tokens ) ) ÷ (n + 1) )
          <em>geometric, because these factors multiply. An arithmetic blend inflates factors below 1 far more than it deflates those above it.</em>
        </span>
      </div>
      <div className="px-fb-line">
        <span className="px-fb-lhs">bound</span>
        <span className="px-fb-eq">=</span>
        <span className="px-fb-rhs">
          max_tokens, else p95[bucket] × 1.2, else 4096
          <em>exact whenever the caller sets max_tokens</em>
        </span>
      </div>
      <div className="px-fb-line result">
        <span className="px-fb-lhs">predicted</span>
        <span className="px-fb-eq">=</span>
        <span className="px-fb-rhs">
          min( max( 15, scope_tokens × history ), bound )
        </span>
      </div>
      <div className="px-fb-foot">
        <span>
          Both numbers are then priced through the same versioned rate table the
          ledger uses, so a forecast and the row it is later compared against can
          never disagree about what a token costs.
        </span>
        <code>predicted_cost_usd = price(input_tokens, predicted, model)</code>
      </div>
    </div>
  );
}

function Accuracy() {
  const worst = 3;
  return (
    <div className="px-acc reveal">
      <div className="px-acc-head">
        <span>traffic shape</span>
        <span>median error</span>
        <span>within 2x</span>
        <span>sample</span>
      </div>
      {ACCURACY.map(([shape, median, within, n], i) => {
        // Bar width is the inverse of error, clamped, so "shorter bar is worse"
        // reads correctly without implying a scale we did not measure.
        const pct = parseFloat(median);
        const w = Math.max(6, Math.min(100, 100 - pct));
        return (
          <div className={`px-acc-row${i === worst ? " weak" : ""}`} key={shape}>
            <span className="px-acc-shape">{shape}</span>
            <span className="px-acc-median">
              {median}
              <span className="px-acc-bar" style={{ width: `${w}%` }} />
            </span>
            <span className="px-acc-within">{within}</span>
            <span className="px-acc-n">{n}</span>
          </div>
        );
      })}
      <p className="px-acc-foot">
        Median absolute percentage error. We do not quote MAPE: a handful of
        20-token answers dominate it permanently and it flatters nobody honestly.
      </p>
    </div>
  );
}

function FlipCard({
  tag,
  front,
  back,
  bound,
}: {
  tag: string;
  front: string;
  back: string;
  bound?: boolean;
}) {
  return (
    <div className={`px-flip${bound ? " bound" : ""}`}>
      <div className="px-flip-inner">
        <div className="px-flip-face">
          <span className="px-flip-tag">{tag}</span>
          <p className="px-flip-q">{front}</p>
          <span className="px-flip-hint">hover →</span>
        </div>
        <div className="px-flip-face px-flip-back">
          <p className="px-flip-a">{back}</p>
        </div>
      </div>
    </div>
  );
}

// Interactive proof: toggle between "predict by input length" (a flat, useless
// line) and "predict by scope" (points line up). Positions are illustrative, the
// correlation figure they dramatise is the real +0.096 from DESIGN.md.
const POINTS = [
  [12, 62], [18, 20], [40, 86], [55, 14], [30, 70], [8, 45],
  [70, 30], [22, 92], [48, 55], [90, 18], [35, 40], [60, 78],
  [15, 88], [80, 25], [25, 12],
];
function Scatter() {
  const [byScope, setByScope] = useState(false);
  const W = 100, H = 100;
  return (
    <div className="px-scatter reveal">
      <div className="px-scatter-head">
        <div className="px-toggle">
          <button
            className={!byScope ? "on" : ""}
            onClick={() => setByScope(false)}
          >
            predict by input length
          </button>
          <button
            className={byScope ? "on" : ""}
            onClick={() => setByScope(true)}
          >
            predict by scope
          </button>
        </div>
        <span className="px-scatter-verdict">
          {byScope ? (
            <b className="ok">points align — R² is real</b>
          ) : (
            <b className="bad">no line fits — R² = 0.9%</b>
          )}
        </span>
      </div>
      <svg viewBox={`0 0 ${W} ${H}`} className="px-scatter-svg" preserveAspectRatio="none">
        {/* The fit line: flat and wrong for input length, diagonal and tight for scope. */}
        <line
          className={`px-fit${byScope ? " ok" : " bad"}`}
          x1="0"
          y1={byScope ? 95 : 55}
          x2="100"
          y2={byScope ? 8 : 48}
        />
        {POINTS.map(([x, y], i) => {
          // In scope mode, snap points toward the diagonal so the eye sees the fit.
          const sy = byScope ? 100 - x * 0.9 - 5 : y;
          return (
            <circle
              key={i}
              cx={x}
              cy={sy}
              r="2.2"
              className={`px-dot${byScope ? " ok" : ""}`}
              style={{ transition: "cy 600ms cubic-bezier(0.16,1,0.3,1)" }}
            />
          );
        })}
      </svg>
      <div className="px-scatter-ax">
        <span>input tokens →</span>
        <span>↑ output tokens</span>
      </div>
    </div>
  );
}

function Terminal() {
  const ref = useRef<HTMLDivElement>(null);
  const [shown, setShown] = useState(0); // lines revealed
  const [counts, setCounts] = useState<Record<number, number>>({});

  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    if (REDUCED()) {
      // ponytail: rAF only to keep the state write out of the effect body
      // (react-hooks/set-state-in-effect); REDUCED() needs matchMedia, so it
      // can't be a useState initializer under SSR.
      const id = requestAnimationFrame(() => {
        setShown(TRACE.length);
        const final: Record<number, number> = {};
        TRACE.forEach((l, i) => l.v != null && (final[i] = l.v));
        setCounts(final);
      });
      return () => cancelAnimationFrame(id);
    }
    let started = false;
    const io = new IntersectionObserver(
      (entries) => {
        if (entries[0].isIntersecting && !started) {
          started = true;
          io.disconnect();
          let i = 0;
          const tick = () => {
            i += 1;
            setShown(i);
            const line = TRACE[i - 1];
            if (line?.v != null) {
              const target = line.v;
              const step = Math.max(1, Math.round(target / 22));
              let cur = 0;
              const idx = i - 1;
              const count = () => {
                cur = Math.min(target, cur + step);
                setCounts((c) => ({ ...c, [idx]: cur }));
                if (cur < target) requestAnimationFrame(count);
              };
              count();
            }
            if (i < TRACE.length) setTimeout(tick, 120);
          };
          tick();
        }
      },
      { threshold: 0.3 },
    );
    io.observe(el);
    return () => io.disconnect();
  }, []);

  return (
    <div className="px-term reveal" ref={ref}>
      <div className="px-term-head">
        <div className="px-term-dots">
          <span />
          <span />
          <span />
        </div>
        <span className="px-term-name">predict.trace</span>
      </div>
      <pre className="px-term-body">
        {TRACE.slice(0, shown).map((l, i) => (
          <div key={i} className={l.cls ? `px-t-${l.cls}` : undefined}>
            {l.t}
            {l.v != null && (
              <span className="px-t-num">
                {" "}
                {(counts[i] ?? 0).toLocaleString()}
              </span>
            )}
          </div>
        ))}
        {shown < TRACE.length && <span className="px-term-caret" />}
      </pre>
    </div>
  );
}

const LOOP = ["predict", "reserve", "call", "capture", "refit"];
function LoopRing() {
  return (
    <div className="px-loop reveal">
      <div className="px-loop-ring">
        {LOOP.map((label, i) => (
          <div
            className={`px-loop-node${label === "capture" ? " accent" : ""}${
              label === "refit" ? " gold" : ""
            }`}
            key={label}
            style={
              { "--a": `${(360 / LOOP.length) * i}deg` } as React.CSSProperties
            }
          >
            {label}
          </div>
        ))}
        <div className="px-loop-orbit" aria-hidden="true">
          <span className="px-loop-particle" />
        </div>
        <div className="px-loop-core">
          actual
          <br />
          feeds
          <br />
          forecast
        </div>
      </div>
    </div>
  );
}
