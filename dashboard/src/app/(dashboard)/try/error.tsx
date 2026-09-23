"use client";

import { TopNav } from "@/components/TopNav";

export default function TryError({ reset }: { reset: () => void }) {
  return <><TopNav /><main className="load-shell" role="alert">
    <div className="load-kicker">METER / CONNECTION</div>
    <h1>We couldn&apos;t load your session.</h1>
    <p>Your session has not been deleted. The Meter API may be waking up or temporarily unavailable.</p>
    <button className="judge-btn" onClick={reset}>Try again</button>
  </main></>;
}
