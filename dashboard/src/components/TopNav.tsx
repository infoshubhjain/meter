"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { usePathname } from "next/navigation";
import type { BreakerState } from "@/lib/db";

const SECTION_LINKS = [
  { label: "Overview", href: "#top" },
  { label: "Budget", href: "#budget" },
  { label: "Balances", href: "#balances" },
  { label: "Agent", href: "#agent" },
  { label: "Logs", href: "#logs" },
  { label: "Spend", href: "#spend" },
  { label: "Outcomes", href: "#outcomes" },
];

const PRODUCT_LINKS = [
  { label: "Overview", href: "/" },
  { label: "Predictor", href: "/how-it-works" },
  { label: "Docs", href: "/docs" },
  { label: "Control room", href: "/dashboard" },
];

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

/** One product-level header for every public and operational route. */
export function TopNav() {
  const pathname = usePathname();

  return (
    <header className="product-nav">
      <Link href="/" className="product-wordmark" aria-label="Meter home">
        METER<span>.</span>
      </Link>
      <nav aria-label="Primary navigation" className="product-links">
        {PRODUCT_LINKS.map((link) => (
          <Link
            key={link.href}
            href={link.href}
            aria-current={pathname === link.href ? "page" : undefined}
          >
            {link.label}
          </Link>
        ))}
      </nav>
      <ThemeToggle />
      <Link href="/try" className="product-cta">Try the system <span>↗</span></Link>
    </header>
  );
}

function ThemeToggle() {
  useEffect(() => {
    const saved = window.localStorage.getItem("meter-theme");
    const next = saved === "dark" || (!saved && window.matchMedia("(prefers-color-scheme: dark)").matches) ? "dark" : "light";
    document.documentElement.dataset.theme = next;
  }, []);

  function select(next: "light" | "dark") {
    document.documentElement.dataset.theme = next;
    window.localStorage.setItem("meter-theme", next);
  }

  return <div className="theme-switch" role="group" aria-label="Color theme">
    <button type="button" className="theme-light" onClick={() => select("light")} aria-label="Use light theme">Light</button>
    <button type="button" className="theme-dark" onClick={() => select("dark")} aria-label="Use dark theme">Dark</button>
  </div>;
}

/** Local navigation for the long operational page; it is not product navigation. */
export function DashboardJumpNav({ breaker }: { breaker: BreakerState }) {
  const [active, setActive] = useState("#top");

  // Marks whichever section is under the top of the viewport.
  useEffect(() => {
    const ids = SECTION_LINKS.map((l) => l.href.slice(1));
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
    <nav className="dashboard-jumps" aria-label="Control Room sections">
      <span>Jump to</span>
      <div>
        {SECTION_LINKS.map((link) => (
            <a
              key={link.href}
              href={link.href}
              aria-current={active === link.href ? "location" : undefined}
              className={active === link.href ? "is-active" : ""}
            >
              {link.label}
            </a>
          ))}
      </div>
      <span
          className="dashboard-breaker"
          style={{ color: pill.color, borderColor: pill.border }}
          title={
            breaker.open && breaker.scope ? `Scope: ${breaker.scope}` : undefined
          }
        >
          <i
            style={{
              background: pill.color,
            }}
            aria-hidden="true"
          />
          {pill.label}
      </span>
    </nav>
  );
}
