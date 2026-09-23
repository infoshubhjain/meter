"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import type { BreakerState } from "@/lib/db";

const LINKS = [
  { label: "Overview", href: "#top" },
  { label: "Budget", href: "#budget" },
  { label: "Balances", href: "#balances" },
  { label: "Agent", href: "#agent" },
  { label: "Logs", href: "#logs" },
  { label: "Spend", href: "#spend" },
  { label: "Outcomes", href: "#outcomes" },
];

/** The pill reports real breaker state, so it has to be able to say something bad. */
function breakerPill(breaker: BreakerState) {
  if (!breaker.open) {
    return {
      label: "Breaker normal",
      color: "var(--color-status-good)",
      border: "rgba(110,220,196,0.3)",
    };
  }
  const revoked = breaker.mode === "revoke";
  return {
    label: revoked
      ? `Key revoked${breaker.count > 1 ? ` ·${breaker.count}` : ""}`
      : `Throttled${breaker.count > 1 ? ` ·${breaker.count}` : ""}`,
    color: revoked ? "var(--color-status-bad)" : "var(--color-status-warn)",
    border: revoked ? "rgba(240,104,92,0.3)" : "rgba(232,181,123,0.3)",
  };
}

export function TopNav({ breaker }: { breaker: BreakerState }) {
  const [active, setActive] = useState("#top");

  // Marks whichever section is under the top of the viewport.
  useEffect(() => {
    const ids = LINKS.map((l) => l.href.slice(1));
    // Just past the bar's own bottom edge. It has to sit ABOVE the content's top
    // padding, or at scroll zero the first section below the hero already qualifies
    // and the nav opens on the wrong item.
    const NAV_BOTTOM = 70;
    const onScroll = () => {
      let current = ids[0];
      for (const id of ids) {
        const el = document.getElementById(id);
        if (el && el.getBoundingClientRect().top <= NAV_BOTTOM) current = id;
      }
      setActive(`#${current}`);
    };
    onScroll();
    window.addEventListener("scroll", onScroll, { passive: true });
    return () => window.removeEventListener("scroll", onScroll);
  }, []);

  const pill = breakerPill(breaker);

  return (
    // `fixed`, not `sticky`. The layout nests this inside flex containers where
    // sticky silently stops pinning — and a sticky bar that has stopped sticking
    // looks identical to a working one until you scroll. z-100 clears the three
    // fixed background layers (0–3) and the content plane (10).
    //
    // Full-bleed with a hairline rule, matching the homepage bar exactly, rather
    // than the floating inset card this used to be.
    <nav
      aria-label="Dashboard sections"
      className="fixed left-0 right-0 top-0 z-[100] border-b border-border-subtle py-[12px]"
      style={{
        background: "rgba(241,240,234,0.92)",
        backdropFilter: "blur(12px)",
        WebkitBackdropFilter: "blur(12px)",
      }}
    >
      <div className="mx-auto flex w-full max-w-[1400px] items-center gap-[12px] px-[32px]">
        {/* The homepage links here; this closes the loop. Next turns this into a
            full page load by itself, because the two sides are separate root
            layouts — which is exactly what we want, so no stylesheet crosses. */}
        <Link href="/" className="brand-mark">
          Meter<span aria-hidden="true">.</span>
        </Link>

        {/* Deliberately NOT in LINKS: those are in-page anchors driven by the
            scroll spy, and a route link among them would light up wrongly and
            never turn off. Sits beside the brand instead, where it reads as
            "leave this page" rather than "jump down it". A judge who wants to
            know how the prediction is computed should not have to go back to
            the homepage to find the argument. */}
        <Link href="/how-it-works" className="glass-pill dashboard-predictor-link">
          How the predictor works
        </Link>
        <Link href="/docs" className="glass-pill dashboard-predictor-link">
          Docs
        </Link>

        {/* Hidden below 1024px — a hamburger is out of scope, and seven cramped
            links are worse than none on a small screen. */}
        <div className="ml-auto hidden items-center gap-[6px] lg:flex">
          {LINKS.map((link) => (
            <a
              key={link.href}
              href={link.href}
              aria-current={active === link.href ? "location" : undefined}
              className={`glass-pill ${
                active === link.href ? "glass-pill-active" : ""
              }`}
            >
              {link.label}
            </a>
          ))}
        </div>

        {/* The console entry point does NOT live here. A pill among six other pills is
            the definition of hidden, and this is the one action a judge arriving with no
            context needs to find. It sits beside the page title instead, where it reads
            as a call to action rather than another anchor link. */}
        <div
          className="glass-pill ml-auto cursor-default lg:ml-[6px]"
          style={{ color: pill.color, borderColor: pill.border }}
          title={
            breaker.open && breaker.scope ? `Scope: ${breaker.scope}` : undefined
          }
        >
          <span
            className="h-[6px] w-[6px] shrink-0 rounded-full"
            style={{
              background: pill.color,
              boxShadow: `0 0 6px ${pill.color}`,
            }}
            aria-hidden="true"
          />
          {pill.label}
        </div>
      </div>
    </nav>
  );
}
