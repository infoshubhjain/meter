/**
 * The loading state for a Control Room, drawn as a one-metre rule.
 *
 * **Why it exists.** The dashboard is `force-dynamic` and server-renders roughly ten
 * queries against a database in another region, so clicking through to it takes about five
 * seconds. With no feedback that reads as a dead button — people click again, or leave. A
 * `loading.tsx` gives React a Suspense fallback that paints immediately while the segment
 * streams, and the real page swaps itself in when it is ready.
 *
 * **Why a ruler.** The product is called Meter. The bar fills in centimetres and finishes
 * at one metre, which is the only pun this codebase gets to make.
 *
 * **What the bar does and does not claim.** It is not a progress percentage — nothing here
 * knows how far along ten streaming queries are, and a bar that pretended to would be
 * inventing a number. It decelerates and settles short of the full metre, so it reads as
 * "still measuring" rather than "97% done", and the page replacing it is what marks
 * completion. That is the honest version of this pattern and the one people already
 * understand.
 *
 * Pure CSS, no client component, no state. It has to paint on the first frame, and
 * anything waiting for hydration would defeat the point.
 */

/** Major graduations, labelled. Minor ticks sit between them. */
const MAJOR = [0, 10, 20, 30, 40, 50, 60, 70, 80, 90, 100];

export function MeterLoader({
  title = "Control Room",
  note = "Reading the ledger — every number on this page is a live query.",
}: {
  title?: string;
  note?: string;
}) {
  return (
    <><TopNav /><main className="relative z-10 mx-auto w-full max-w-[1400px] px-[32px] pb-[60px] pt-[48px]">
      <div className="mb-[32px]">
        <h1 className="t-display">{title}</h1>
        <div className="t-eyebrow mt-[8px]">Measuring…</div>
      </div>

      <div className="glass panel p-[28px]">
        <div className="meter-rule" role="status" aria-live="polite" aria-busy="true">
          <span className="sr-only">Loading the dashboard</span>

          {/* The graduations. Drawn behind the fill so it sweeps across them. */}
          <div className="meter-rule-ticks" aria-hidden>
            {Array.from({ length: 101 }, (_, cm) => (
              <span
                key={cm}
                className={
                  cm % 10 === 0
                    ? "meter-tick meter-tick-major"
                    : cm % 5 === 0
                      ? "meter-tick meter-tick-mid"
                      : "meter-tick"
                }
              />
            ))}
          </div>

          <div className="meter-rule-track">
            <div className="meter-rule-fill" />
          </div>

          <div className="meter-rule-labels" aria-hidden>
            {MAJOR.map((cm) => (
              <span key={cm} className="meter-label">
                {cm === 100 ? "1 m" : cm}
              </span>
            ))}
          </div>
        </div>

        <p
          className="mt-[24px] text-[13px]"
          style={{ color: "var(--color-text-tertiary)" }}
        >
          {note}
        </p>
      </div>
    </main></>
  );
}
import { TopNav } from "@/components/TopNav";
