"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import Link from "next/link";
import { Intro } from "./Intro";
import { STEPS, CODE } from "./usage-content";

const RING_1 = [
  { t: "Explain" },
  { t: "quantum", cls: "hot", cost: "$0.003" },
  { t: "computing" },
  { t: "summarize", cls: "gold" },
  { t: "this" },
  { t: "document", cls: "hot", cost: "$0.005" },
  { t: "write" },
  { t: "code", cls: "mint" },
  { t: "parse" },
  { t: "returns", cls: "hot", cost: "$0.009" },
];

const SLIDES = [
  {
    accent: "coral",
    num: "01",
    h: "Meter every call.",
    // The design said 0.35ms. Our measured figure is 1.49ms, and CONTEXT.md requires
    // it to carry its qualifier — on a page whose own lede says "no mocks, no
    // illustrations", an invented latency is the wrong thing to be caught on.
    p: "Change one base URL. Every AI call now passes through Meter — priced, attributed, and written to the ledger with a measured p50 overhead of 1.49ms on loopback.",
    foot: "✓ Attribution recorded on every row",
  },
  {
    accent: "gold",
    num: "02",
    h: "Pay while you sleep.",
    p: "When the provider balance runs low, the Treasurer agent charges a pre-approved Prava mandate and tops up the wallet before anything fails. You get the iMessage in the morning.",
    foot: "✓ Auto-topup via Prava mandate",
  },
  {
    accent: "mint",
    num: "03",
    h: "Cut spend anomalies.",
    // 3×, not the 50× this said. BREAKER_BURST_RATIO defaults to 3.0, and
    // proxy/config.py notes the ratio is capped by the window sizes at 3600/300 = 12
    // — set above that and the breaker can never trip at all. So 50× did not merely
    // overstate the threshold, it described a breaker that would never fire.
    p: "Named after the fuse box in a house. When one feature's spend rate triples against its own trailing hour, the Circuit Breaker cuts it off. Everything else keeps flowing. Leaked keys die immediately.",
    foot: "✓ Per-feature circuit breaker",
  },
];

const SLIDE_TICK_MS = 60;
const SECTION_IDS = ["hero-section", "overview", "usage", "final"];

export function Home() {
  const scroller = useRef<HTMLDivElement>(null);
  const [landed, setLanded] = useState(false);
  const [hintIn, setHintIn] = useState(false);
  const [scrolled, setScrolled] = useState(false);
  const [progress, setProgress] = useState(0);
  const [section, setSection] = useState(0);
  const [slide, setSlide] = useState(0);
  const [slideProgress, setSlideProgress] = useState(0);
  const [step, setStep] = useState(0);
  const [copied, setCopied] = useState<string | null>(null);

  // The hero waits on the intro's event. The scroll hint comes 400ms after the
  // wordmark, matching the design's two-stage landing.
  useEffect(() => {
    const land = () => {
      setLanded(true);
      setTimeout(() => setHintIn(true), 400);
    };
    window.addEventListener("meter:intro-done", land);
    return () => window.removeEventListener("meter:intro-done", land);
  }, []);

  // `.snap-container` is the scrolling element, not the window — it carries its own
  // `overflow-y: auto`. A listener on window here would silently never fire, which
  // looks exactly like "the progress bar is broken".
  useEffect(() => {
    const el = scroller.current;
    if (!el) return;

    const onScroll = () => {
      const max = el.scrollHeight - el.clientHeight;
      setProgress(max > 0 ? (el.scrollTop / max) * 100 : 0);
      setScrolled(el.scrollTop > 50);

      let current = 0;
      el.querySelectorAll<HTMLElement>(".snap-section").forEach((sec, i) => {
        if (el.scrollTop >= sec.offsetTop - 100) current = i;
      });
      setSection(current);
    };

    onScroll();
    el.addEventListener("scroll", onScroll, { passive: true });
    return () => el.removeEventListener("scroll", onScroll);
  }, []);

  // Reveal-on-scroll, rooted at the snap container for the same reason.
  useEffect(() => {
    const el = scroller.current;
    if (!el) return;
    const io = new IntersectionObserver(
      (entries) =>
        entries.forEach((e) => {
          if (e.isIntersecting) e.target.classList.add("in-view");
        }),
      { root: el, threshold: 0.15 },
    );
    el.querySelectorAll(".reveal").forEach((t) => io.observe(t));
    return () => io.disconnect();
  }, []);

  // Sticky-code sync: whichever step block is in the reading band drives which code
  // window is shown. rootMargin trims the band to the middle of the viewport so the
  // active step is the one being read, not the one just entering.
  useEffect(() => {
    const el = scroller.current;
    if (!el) return;
    const io = new IntersectionObserver(
      (entries) =>
        entries.forEach((e) => {
          if (e.isIntersecting && e.intersectionRatio > 0.4) {
            setStep(Number((e.target as HTMLElement).dataset.step));
          }
        }),
      { root: el, threshold: [0.4, 0.6], rootMargin: "-20% 0px -30% 0px" },
    );
    el.querySelectorAll(".step-block").forEach((b) => io.observe(b));
    return () => io.disconnect();
  }, []);

  // Carousel autoplay. One ticker drives both the bar and the slide change, so they
  // cannot drift apart.
  useEffect(() => {
    if (window.matchMedia("(prefers-reduced-motion: reduce)").matches) return;
    const id = setInterval(() => {
      setSlideProgress((p) => {
        if (p >= 100) {
          setSlide((s) => (s + 1) % SLIDES.length);
          return 0;
        }
        return p + 1;
      });
    }, SLIDE_TICK_MS);
    return () => clearInterval(id);
  }, []);

  const goto = useCallback((i: number) => {
    setSlide((i + SLIDES.length) % SLIDES.length);
    setSlideProgress(0);
  }, []);

  const copy = useCallback(async (id: string, text: string) => {
    try {
      await navigator.clipboard.writeText(text);
      setCopied(id);
      setTimeout(() => setCopied(null), 2000);
    } catch {
      // Clipboard access is permission-gated and blocked outright in some embeds.
      // Failing quietly is right — the snippet is on screen and selectable.
    }
  }, []);

  const jump = (id: string) => (e: React.MouseEvent) => {
    e.preventDefault();
    document.getElementById(id)?.scrollIntoView({ behavior: "smooth" });
  };

  return (
    <>
      <Intro />

      <div className="progress" style={{ width: `${progress}%` }} />

      <div className={`topbar${scrolled ? " scrolled" : ""}`}>
        <div className="wrap">
          <span className="mark">
            <span className="dot" />
            METER
          </span>
          <nav>
            <a href="#overview" className="glass-pill" onClick={jump("overview")}>
              Overview
            </a>
            <a href="#usage" className="glass-pill" onClick={jump("usage")}>
              How to use
            </a>
            {/* Full page load into the predictor explainer, which has its own root
                layout — same reason as the dashboard link below: no stylesheet crosses. */}
            <Link href="/how-it-works" className="glass-pill">
              How prediction works
            </Link>
            {/* Next turns this into a full page load by itself — the dashboard is a
                separate root layout — which is what we want: no stylesheet crosses. */}
            <Link href="/dashboard" className="glass-cta">
              Open dashboard <span className="arr">→</span>
            </Link>
          </nav>
        </div>
      </div>

      <nav className="section-dots" aria-label="Page sections">
        {SECTION_IDS.map((id, i) => (
          <a
            key={id}
            href={`#${id}`}
            onClick={jump(id)}
            className={section === i ? "active" : undefined}
            aria-current={section === i ? "location" : undefined}
            aria-label={id.replace("-section", "")}
          />
        ))}
      </nav>

      <div className="snap-container" ref={scroller}>
        {/* ── HERO ───────────────────────────────────────────────── */}
        <section
          className={`snap-section hero${landed ? " landed" : ""}`}
          id="hero-section"
        >
          <div className="orbit-container">
            {/* One ring. Tokens are spread evenly around it; the radius comes from
                the ring's class, so it sits beside the tilt that determines how it
                projects rather than being a loose number here. */}
            <div className="orbit-ring ring-1">
              {RING_1.map((tok, i) => (
                <div
                  key={tok.t}
                  className="t3d"
                  style={
                    {
                      "--a": `${(360 / RING_1.length) * i}deg`,
                      "--r": "var(--ring-r)",
                    } as React.CSSProperties
                  }
                >
                  <span className={`tk${tok.cls ? ` ${tok.cls}` : ""}`}>
                    {tok.t}
                    {tok.cost && <span className="cost">{tok.cost}</span>}
                  </span>
                </div>
              ))}
            </div>
          </div>

          {/* Drifting price tags. Positions keep clear of the middle band, where the
              wordmark and the orbit ring already are — anything placed there lands
              on top of the M or behind a rotating token. Delays and durations are
              all mutually non-round so nothing visibly pulses in unison. */}
          {(
            [
              ["20%", "12%", "1.8s", "7s", "8px", "-12px", "$0.0012"],
              ["15%", "58%", "2.6s", "7.4s", "-7px", "9px", "$0.0007"],
              ["34%", "86%", "3s", "6s", "6px", "-10px", "$0.0091"],
              ["47%", "5%", "1.4s", "6.6s", "7px", "10px", "$0.0026"],
              ["70%", "74%", "2.4s", "8s", "-10px", "8px", "$0.0034"],
              ["79%", "26%", "2.1s", "9s", "-8px", "6px", "$0.0044"],
              ["86%", "62%", "3.3s", "8.6s", "9px", "-7px", "$0.0158"],
            ] as const
          ).map(([top, left, delay, dur, x2, y2, label]) => (
            <div
              key={label}
              className="cost-float"
              style={
                {
                  top,
                  left,
                  "--delay": delay,
                  "--dur": dur,
                  "--x1": "0px",
                  "--y1": "0px",
                  "--x2": x2,
                  "--y2": y2,
                } as React.CSSProperties
              }
            >
              {label}
            </div>
          ))}

          <div className="hero-center">
            <div className={`hero-glow${landed ? " animate" : ""}`} />
            <h1 className={`hero-name${landed ? " animate" : ""}`}>
              {["M", "E", "T", "E", "R"].map((ch, i) => (
                <span
                  key={i}
                  className={`letter${i === 2 ? " accent" : ""}`}
                  style={{ "--i": i } as React.CSSProperties}
                >
                  {ch}
                </span>
              ))}
            </h1>
          </div>

          <div
            className={`scroll-hint${hintIn ? " animate" : ""}`}
            aria-hidden="true"
          >
            <span>Scroll</span>
            <span className="line-drop" />
          </div>
        </section>

        {/* ── OVERVIEW ───────────────────────────────────────────── */}
        <section className="snap-section overview" id="overview">
          <div className="wrap">
            <div className="overview-layout">
              <div className="overview-text">
                <p className="section-label reveal">What Meter is</p>
                {/* The <em> is the gradient half of the heading (.overview h2 em),
                    so the emphasis has to sit on a whole sentence rather than a
                    fragment — the colour break reads as the second beat. */}
                <h2 className="reveal d1">
                  Your AI spend has a treasurer. <em>It never sleeps.</em>
                </h2>
                <p className="lede reveal d2">
                  Meter predicts inference costs before they happen, enforces
                  budgets per feature, and autonomously tops up provider credits
                  — so you don&apos;t have to worry about running out at 3am.
                </p>
                {/* No figure here on purpose. This badge carried $847.31, which
                    came from the design mockup rather than from anything we have
                    ever metered — real spend is around $2 — and it sat behind a
                    pulsing "live" dot on a page that elsewhere promises "no mocks,
                    no illustrations". The claim is now about what the proxy does
                    to every call, which is true and needs no number to stand up. */}
                <div className="live-badge reveal d3">
                  <span className="live-dot" />
                  Live · <b>every call</b> priced and attributed
                </div>
              </div>

              <div className="carousel reveal d2">
                <div className="carousel-viewport">
                  <div
                    className="carousel-track"
                    style={{ transform: `translateX(-${slide * 100}%)` }}
                  >
                    {SLIDES.map((s) => (
                      <div
                        key={s.num}
                        className="carousel-slide"
                        data-accent={s.accent}
                      >
                        <div className="slide-bg-num">{s.num}</div>
                        <div className="slide-accent-line" />
                        <h3>{s.h}</h3>
                        <p>{s.p}</p>
                        <div className="slide-foot">{s.foot}</div>
                      </div>
                    ))}
                  </div>
                </div>

                <div className="carousel-controls">
                  <div className="carousel-counter">
                    <span className="current">
                      {String(slide + 1).padStart(2, "0")}
                    </span>
                    <span>/</span>
                    <span>{String(SLIDES.length).padStart(2, "0")}</span>
                  </div>
                  <div className="carousel-dots">
                    {SLIDES.map((s, i) => (
                      <button
                        key={s.num}
                        className={`carousel-dot${slide === i ? " active" : ""}`}
                        data-index={i}
                        onClick={() => goto(i)}
                        aria-label={`Slide ${i + 1}`}
                      />
                    ))}
                  </div>
                  <div className="carousel-arrows">
                    <button
                      className="carousel-btn"
                      onClick={() => goto(slide - 1)}
                      aria-label="Previous"
                    >
                      ←
                    </button>
                    <button
                      className="carousel-btn"
                      onClick={() => goto(slide + 1)}
                      aria-label="Next"
                    >
                      →
                    </button>
                  </div>
                </div>

                <div className="carousel-progress">
                  <div
                    className={`carousel-progress-fill accent-${SLIDES[slide].accent}`}
                    style={{ width: `${slideProgress}%` }}
                  />
                </div>
              </div>
            </div>
          </div>
        </section>

        {/* ── USAGE ──────────────────────────────────────────────── */}
        <section className="snap-section usage" id="usage">
          <div className="wrap">
            <div className="usage-header">
              <p className="section-label reveal">How to use it</p>
              <h2 className="reveal d1">
                Three steps. <span className="grad-text">All of it real.</span>
              </h2>
              <p className="lede reveal d2">
                Scroll through the process. The code updates in real-time on the
                right. No mocks, no illustrations. Install takes about a minute.
              </p>
            </div>

            <div className="usage-grid">
              <div className="usage-steps">
                {STEPS.map((s, i) => (
                  <div
                    key={s.num}
                    className={`step-block${step === i ? " active" : ""}`}
                    data-step={i}
                  >
                    <div className="step-node">{s.num}</div>
                    <div className="step-content">
                      <span className="step-tag">{s.tag}</span>
                      <h3>{s.title}</h3>
                      <p>{s.body}</p>
                      <div className="foot">{s.foot}</div>
                    </div>
                  </div>
                ))}
              </div>

              {/* Sticky: the windows are stacked in one grid cell and cross-faded,
                  so the panel keeps its height and nothing reflows on change. */}
              <div className="usage-code-wrapper">
                {CODE.map((c, i) => (
                  <div
                    key={c.file}
                    className={`code-window${step === i ? " active" : ""}`}
                    data-code={i}
                  >
                    <div className="head">
                      <div
                        style={{ display: "flex", alignItems: "center", gap: 14 }}
                      >
                        <div className="dots">
                          <span />
                          <span />
                          <span />
                        </div>
                        <span className="fname">{c.file}</span>
                      </div>
                      <button
                        className={`copy${copied === c.file ? " copied" : ""}`}
                        onClick={() => copy(c.file, c.raw)}
                      >
                        {copied === c.file ? "Copied!" : "Copy"}
                      </button>
                    </div>
                    <pre>{c.body}</pre>
                  </div>
                ))}
              </div>
            </div>
          </div>
          {/* This section is 150vh, so its last screenful needs its own snap point
              — without one the scroll has no reason to settle where step 03 is
              finally readable, and you get carried into the CTA instead. */}
          <div className="snap-end" aria-hidden="true" />
        </section>

        {/* ── FINAL CTA ──────────────────────────────────────────── */}
        <section className="snap-section final-cta" id="final">
          <div className="wrap">
            <h2>
              Stop watching charts. <br />
              <em>Start paying bills.</em>
            </h2>
            <p>
              Meter is live for select teams. Point your first request at us today
              and see exactly where the money goes.
            </p>
            <Link href="/dashboard" className="hero-cta">
              Open dashboard <span className="arr">→</span>
            </Link>
          </div>
        </section>

        <footer>
          <div className="wrap">
            <span>Meter — the autonomous inference treasurer</span>
            <span>Built for AI engineers</span>
          </div>
        </footer>
      </div>
    </>
  );
}
