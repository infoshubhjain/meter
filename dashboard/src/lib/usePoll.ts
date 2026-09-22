"use client";

import { useEffect, useRef, useState } from "react";

/**
 * Poll an endpoint, but only while someone is actually looking.
 *
 * The naive `setInterval(poll, 3000)` polls forever: a tab left open in a background
 * window keeps fetching all night. On a server you own that is merely wasteful. On a
 * serverless host it is metered — two endpoints at 3s is 2,400 function invocations an
 * hour, per tab, so a single forgotten tab can burn a month's allowance in a couple of
 * days.
 *
 * Three rules, in order of how much they save:
 *
 *  1. **Pause when the tab is hidden.** `document.visibilityState` — a backgrounded tab
 *     does nothing at all, and resumes with an immediate fetch so it is never stale when
 *     you look back at it. This is most of the saving, because "left open" almost always
 *     means "left in the background".
 *  2. **Back off when nothing is changing.** Stay at the fast interval while the payload
 *     keeps changing; ease out toward `maxIntervalMs` when it stops. A demo in progress
 *     stays live; an idle dashboard goes quiet.
 *  3. **Stop entirely after `stopAfterIdleMs` of no change.** A recruiter who opens the
 *     link and wanders off stops costing anything. Any interaction — focus, click,
 *     keypress — starts it again.
 *
 * Result: an abandoned tab costs a few dozen requests instead of tens of thousands,
 * while an actively watched one is as live as it was before.
 */
export type PollStatus = "live" | "paused" | "offline";

export function usePoll<T>(
  url: string,
  initial: T,
  {
    intervalMs = 3000,
    maxIntervalMs = 30000,
    stopAfterIdleMs = 5 * 60 * 1000,
  }: { intervalMs?: number; maxIntervalMs?: number; stopAfterIdleMs?: number } = {},
): { data: T; status: PollStatus } {
  const [data, setData] = useState<T>(initial);
  const [status, setStatus] = useState<PollStatus>("live");

  // Refs, not state: these change on every tick and must not re-render the table.
  const delay = useRef(intervalMs);
  // Seeded inside the effect, not here: `Date.now()` is impure and calling it during
  // render breaks the react-hooks/purity rule the compiler enforces.
  const lastChange = useRef(0);
  const lastPayload = useRef<string>("");

  useEffect(() => {
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    lastChange.current = Date.now();

    const wake = () => {
      lastChange.current = Date.now();
      delay.current = intervalMs;
      setStatus("live");
      if (timer) clearTimeout(timer);
      void tick();
    };

    async function tick() {
      if (cancelled) return;

      // Hidden tabs cost nothing. Re-armed by the visibilitychange listener below,
      // which fetches immediately so the view is current the moment it is seen again.
      if (typeof document !== "undefined" && document.visibilityState === "hidden") {
        timer = setTimeout(tick, 1000);
        return;
      }

      if (Date.now() - lastChange.current > stopAfterIdleMs) {
        setStatus("paused");
        return; // fully stopped until an interaction calls wake()
      }

      try {
        const res = await fetch(url, { cache: "no-store" });
        if (res.ok) {
          if (!cancelled) setStatus("live");
          const body = await res.text();
          if (!cancelled && body !== lastPayload.current) {
            lastPayload.current = body;
            lastChange.current = Date.now();
            delay.current = intervalMs; // something moved — go back to fast
            setData(JSON.parse(body) as T);
          } else if (!cancelled) {
            // Nothing changed: ease out, but never past maxIntervalMs.
            delay.current = Math.min(delay.current * 1.5, maxIntervalMs);
          }
        } else if (!cancelled) {
          setStatus("offline");
          delay.current = Math.min(delay.current * 2, maxIntervalMs);
        }
      } catch {
        // Transient failure. Keep the last good data rather than clearing the table,
        // and back off so a dead endpoint is not hammered.
        if (!cancelled) setStatus("offline");
        delay.current = Math.min(delay.current * 2, maxIntervalMs);
      }

      if (!cancelled) timer = setTimeout(tick, delay.current);
    }

    void tick();

    const onVisible = () => {
      if (document.visibilityState === "visible") wake();
    };
    document.addEventListener("visibilitychange", onVisible);
    window.addEventListener("focus", wake);
    window.addEventListener("pointerdown", wake);
    window.addEventListener("keydown", wake);

    return () => {
      cancelled = true;
      if (timer) clearTimeout(timer);
      document.removeEventListener("visibilitychange", onVisible);
      window.removeEventListener("focus", wake);
      window.removeEventListener("pointerdown", wake);
      window.removeEventListener("keydown", wake);
    };
  }, [url, intervalMs, maxIntervalMs, stopAfterIdleMs]);

  return { data, status };
}
