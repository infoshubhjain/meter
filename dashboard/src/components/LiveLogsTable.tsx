"use client";

import { usePoll } from "@/lib/usePoll";
import type { LiveLogRow } from "@/lib/db";
import {
  usdColumnFormatter,
  providerLabel,
  relativeTime,
  predictionError,
  isUnderPredicted,
} from "@/lib/format";
import { StatusBadge, toneForStatus } from "@/components/ui/primitives";
import { Cell, DataTable, IdentityCell, Row } from "@/components/ui/DataTable";

const POLL_INTERVAL_MS = 3000;

export function LiveLogsTable({
  initialRows,
  emptyHint,
}: {
  initialRows: LiveLogRow[];
  /**
   * What to say when there is nothing to show.
   *
   * The default tells you to start the proxy, which is right on the team dashboard and
   * actively misleading in a judge's session: their proxy is running, their ledger is
   * empty because they have not run anything yet, and being told to start a server they
   * never started reads as the product being broken.
   */
  emptyHint?: string;
}) {
  // Polls only while the tab is visible, backs off when nothing new arrives, and stops
  // once idle. See `usePoll` — the unconditional setInterval this replaced kept fetching
  // in a background window forever, which is metered on a serverless host.
  const { data, status } = usePoll<{ logs: LiveLogRow[] }>(
    "/api/live-logs",
    { logs: initialRows },
    { intervalMs: POLL_INTERVAL_MS },
  );
  const rows = data.logs;

  // Counted across every fetched row, not the visible ones. The table collapses by
  // default, and a footnote that changed when you expanded it would look unreliable
  // at the moment someone is reading it.
  const unpredicted = rows.filter((r) => r.predicted_cost_usd === null).length;

  const usd = usdColumnFormatter([
    ...rows.map((r) => r.predicted_cost_usd),
    ...rows.map((r) => r.cost_usd),
  ]);

  return (
    <section id="logs" className="scroll-mt-[90px]">
      <DataTable
        title="Request Ledger"
        tag={status === "live" ? "Live" : status === "paused" ? "Paused" : "Offline"}
        live={status === "live"}
        columns={[
          { label: "Member" },
          { label: "Model" },
          { label: "Predicted", align: "right" },
          { label: "Actual", align: "right" },
          { label: "Error", align: "right" },
          { label: "Status", align: "right" },
          { label: "Time", align: "right" },
        ]}
        empty={
          <p className="t-cell text-text-secondary">
            {emptyHint ??
              "No requests logged yet. Start the proxy and send a call through it."}
          </p>
        }
        rows={rows.map((row) => (
          <Row key={row.id}>
            <IdentityCell
              primary={row.actor ?? "Unattributed"}
              secondary={row.feature ?? "untagged"}
            />
            <IdentityCell
              primary={row.model ?? "—"}
              secondary={row.provider ? providerLabel(row.provider) : "—"}
            />
            <Cell align="right" muted>
              {usd(row.predicted_cost_usd)}
            </Cell>
            <Cell align="right">{usd(row.cost_usd)}</Cell>
            {/* Signed against the actual cost, matching show_ledger.py's
                denominator. Only an under-estimate is tinted: over-estimating is
                the safety margin doing its job and does not need attention, while
                a request that cost more than was reserved for it does. */}
            <Cell
              align="right"
              muted={!isUnderPredicted(row.predicted_cost_usd, row.cost_usd)}
              className={
                isUnderPredicted(row.predicted_cost_usd, row.cost_usd)
                  ? "text-status-warn"
                  : ""
              }
            >
              {predictionError(row.predicted_cost_usd, row.cost_usd)}
            </Cell>
            <Cell align="right" numeric={false}>
              <StatusBadge tone={toneForStatus(row.status)}>
                {row.status ?? "—"}
              </StatusBadge>
            </Cell>
            <Cell align="right" muted>
              {relativeTime(row.ts)}
            </Cell>
          </Row>
        ))}
        footnote={
          <>
            Last {rows.length} requests · polling every{" "}
            {POLL_INTERVAL_MS / 1000}s
            {status === "paused" && " · paused while idle; interact to resume"}
            {status === "offline" && " · last successful data retained; retrying"}
            {unpredicted > 0 && (
              <>
                {" · "}
                {unpredicted} of {rows.length} rows carry no forecast: prediction
                needs an exact token count, and Anthropic does not use a tiktoken
                vocabulary — the predictor declines rather than approximating
                ~10–20% wrong.
              </>
            )}
          </>
        }
      />
    </section>
  );
}
