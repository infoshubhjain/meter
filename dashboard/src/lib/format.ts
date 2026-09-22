// The ledger stores cost as numeric(14,6) (ARCHITECTURE.md §4) because per-request
// inference costs are routinely fractions of a cent. Formatting to 2 decimals — the
// currency default — renders most real rows as "$0.00", which defeats the point of a
// cost tool. So precision scales with magnitude: enough decimals to show the number,
// consistent within a magnitude band so a column still scans cleanly.

/** Smallest value the ledger can represent — numeric(14,6). */
const LEDGER_EPSILON = 0.000001;

export function formatUsd(n: number | null | undefined): string {
  if (n === null || n === undefined) return "—";
  if (n === 0) return "$0.00";

  const abs = Math.abs(n);

  // Non-zero but below what the ledger can store: say so rather than rounding to
  // "$0.000000", which reads as "free" when it isn't.
  if (abs < LEDGER_EPSILON) return "<$0.000001";

  return usdAt(n, decimalsFor(abs));
}

function decimalsFor(abs: number): number {
  return abs >= 1 ? 2 : abs >= 0.01 ? 4 : 6;
}

/**
 * Prediction error for one row, as a percentage of the actual cost.
 *
 * Denominator and guard match `scripts/show_ledger.py --accuracy`, which computes
 * `abs(p - a) / a` over rows with a real actual. Sharing the denominator matters:
 * a column that measured error against the *prediction* would disagree with the
 * accuracy report the predictor is tuned against, and the two numbers would be
 * quietly incomparable.
 *
 * Signed here, where that report takes the absolute value, because per-row the
 * direction is the whole point. The predictor is deliberately biased high
 * (`SAFETY_MARGIN = 1.15`), so a column of `+` values is the estimator working as
 * designed; a `−` is an under-estimate, the one direction a budget tool cannot
 * afford — it means the request cost more than was reserved for it. The aggregate
 * report says the same thing with its "under" column.
 *
 * Returns "—" rather than a number in the two cases where a percentage would be a
 * fiction: no prediction at all (Anthropic, where the predictor declines rather
 * than returning a tiktoken figure that is 10–20% wrong), and no actual to divide
 * by (a refused request never reached the provider, so it has no true cost).
 */
export function predictionError(
  predicted: number | null | undefined,
  actual: number | null | undefined,
): string {
  if (predicted === null || predicted === undefined) return "—";
  if (actual === null || actual === undefined || actual <= 0) return "—";

  const pct = ((predicted - actual) / actual) * 100;
  // A tenth of a percent is noise at three figures and clutter at two, so the
  // precision follows the magnitude rather than being fixed.
  const decimals = Math.abs(pct) >= 10 ? 0 : 1;
  const sign = pct > 0 ? "+" : pct < 0 ? "−" : "";
  return `${sign}${Math.abs(pct).toFixed(decimals)}%`;
}

/** True when the estimate came in under the real cost — the unsafe direction. */
export function isUnderPredicted(
  predicted: number | null | undefined,
  actual: number | null | undefined,
): boolean {
  if (predicted === null || predicted === undefined) return false;
  if (actual === null || actual === undefined || actual <= 0) return false;
  return predicted < actual;
}

/**
 * Money for a headline card: always cents, always grouped.
 *
 * `formatUsd` widens to 4 and 6 decimals under a dollar, which is correct for a
 * ledger column where a single call really does cost $0.000037 — and wrong at
 * 32px, where "$0.8375" reads as a precision nobody asked for and breaks the
 * tabular rhythm across the row.
 *
 * Two decimals is right for a team's totals and wrong for a judge's. A session that
 * has genuinely made calls spends about $0.00003 of them, and rounding that to
 * "$0.00" tells someone who just watched three requests succeed that nothing
 * happened — the same misleading zero that made a breaker alert read "$0.00 in 5 min
 * against a $0.00 floor" on a real phone (EXPERIENCE.md #35).
 *
 * So: two decimals once there are cents to show, and widen below that until the
 * figure is visible. A true zero still prints "$0.00", because that one is honest.
 */
export function formatUsdHeadline(n: number | null | undefined): string {
  if (n === null || n === undefined) return "—";
  if (n === 0 || Math.abs(n) >= 0.01) return usdAt(n, 2);
  // Enough places for two significant figures, capped so the hero stays readable.
  const places = Math.min(
    Math.max(2, -Math.floor(Math.log10(Math.abs(n))) + 1),
    6,
  );
  return usdAt(n, places);
}

function usdAt(n: number, decimals: number): string {
  return n.toLocaleString("en-US", {
    style: "currency",
    currency: "USD",
    minimumFractionDigits: decimals,
    maximumFractionDigits: decimals,
  });
}

/**
 * A formatter shared across a column, so every cell prints the same number of
 * decimals and the points line up.
 *
 * Per-value precision is right for a lone figure and wrong in a table: a column
 * holding $0.005975 and $0.0128 renders them at six and four decimals, the
 * decimal points stop aligning, and the shorter number reads as the smaller one
 * when it is twice the size. Deciding once for the whole column fixes that.
 *
 * Pass every value that shares an axis of comparison — in Live Logs that means
 * predicted and actual together, since the entire point is reading one against
 * the other.
 */
export function usdColumnFormatter(
  values: (number | null | undefined)[],
): (n: number | null | undefined) => string {
  // Precision is set by the *smallest* value in the column, not the largest: the
  // column has to be able to show the finest figure in it without rounding to
  // nothing, and everything larger then pads to match. So a column holding both
  // $1.50 and $0.0084 prints $1.500000 and $0.008400 — wide, but aligned and
  // truthful, which is the trade an accounting column makes too.
  let needed = 0;
  for (const v of values) {
    if (v === null || v === undefined) continue;
    const abs = Math.abs(v);
    // Zero carries no magnitude information — letting it vote would flatten a
    // column of sub-cent values to two decimals.
    if (abs === 0 || abs < LEDGER_EPSILON) continue;
    needed = Math.max(needed, decimalsFor(abs));
  }
  const decimals = needed || 2;

  return (n) => {
    if (n === null || n === undefined) return "—";
    if (n === 0) return usdAt(0, decimals);
    if (Math.abs(n) < LEDGER_EPSILON) return "<$0.000001";
    return usdAt(n, decimals);
  };
}

/**
 * A formatter for configured limits rather than measured amounts.
 *
 * `usdColumnFormatter` scales precision by magnitude, which is right for spend: a figure
 * below a cent needs six decimals or it reads as zero. A ceiling is not a measurement —
 * it is a number someone typed into meter.yaml — so the same rule renders a $0.50 cap as
 * "$0.5000" and drags every sibling to "$10.0000" with it. Trailing zeros on a limit are
 * noise that makes it look computed.
 *
 * So: the fewest decimals (never under 2) that still represent every value exactly.
 */
export function usdCeilingFormatter(
  values: (number | null | undefined)[],
): (n: number | null | undefined) => string {
  let decimals = 2;
  for (const v of values) {
    if (v === null || v === undefined) continue;
    let d = 2;
    // Grow only until the rounded value is indistinguishable from the real one at
    // ledger resolution — a half-epsilon tolerance keeps binary float noise
    // (0.1 + 0.2) from demanding six decimals it does not need.
    while (d < 6 && Math.abs(Number(v.toFixed(d)) - v) > LEDGER_EPSILON / 2) d++;
    decimals = Math.max(decimals, d);
  }
  return (n) =>
    n === null || n === undefined ? "—" : usdAt(n, decimals);
}

// Providers are stored lowercase in the ledger (`openai`, `anthropic`). CSS
// `capitalize` turns that into "Openai", which is wrong for a brand name shown to
// the customer. Anything unmapped falls back to capitalize-first so a provider added
// on the backend still renders sanely without a frontend change.
const PROVIDER_LABELS: Record<string, string> = {
  openai: "OpenAI",
  anthropic: "Anthropic",
};

// A balance is only meaningful with its age: "$4.00" is reassuring and "$4.00, 3 hours
// ago" is alarming, and the Treasurer's whole job is reacting to the second one. The
// ledger writes `updated_at` as ISO-8601 UTC.
const RELATIVE = new Intl.RelativeTimeFormat("en-US", {
  numeric: "auto",
  style: "short",
});

/** A provider balance stops being operationally current after thirty minutes. */
export const WALLET_STALE_AFTER_MS = 30 * 60 * 1000;

export function relativeTime(iso: string): string {
  const then = Date.parse(iso);
  if (Number.isNaN(then)) return "";
  const seconds = Math.max(0, Math.round((Date.now() - then) / 1000));
  if (seconds < 45) return "just now";
  // Intl handles the pluralisation and the "1 min ago" / "2 min. ago" wording; the
  // table only needs to pick which unit reads best at this magnitude.
  const [per, unit]: [number, Intl.RelativeTimeFormatUnit] =
    seconds < 3600 ? [60, "minute"] : seconds < 86400 ? [3600, "hour"] : [86400, "day"];
  return RELATIVE.format(-Math.round(seconds / per), unit);
}

/**
 * Whether a timestamp is older than `ms`.
 *
 * Lives here rather than inline in a component for the same reason `relativeTime`
 * does: reading the clock during render is impure, and `react-hooks/purity` flags
 * it. Wrapping it keeps the one unavoidable `Date.now()` in a single place.
 */
export function isStale(iso: string, ms: number): boolean {
  const then = Date.parse(iso);
  if (Number.isNaN(then)) return false;
  return Date.now() - then > ms;
}

export function providerLabel(provider: string): string {
  return (
    PROVIDER_LABELS[provider.toLowerCase()] ??
    provider.charAt(0).toUpperCase() + provider.slice(1)
  );
}
