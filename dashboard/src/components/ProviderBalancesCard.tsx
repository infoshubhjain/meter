import type { WalletRow } from "@/lib/db";
import {
  usdColumnFormatter,
  providerLabel,
  relativeTime,
  isStale,
  WALLET_STALE_AFTER_MS,
} from "@/lib/format";
import { Panel } from "@/components/ui/primitives";

/**
 * PLAN.md Phase 3 states the Treasurer's trigger as "if provider_balance < $10".
 * There is no config key for it yet because the loop itself is not built (Shivam),
 * so this is the documented figure rather than a live setting.
 */
const LOW_BALANCE_USD = 10;

const ICONS: Record<string, { bg: string; fg: string }> = {
  anthropic: { bg: "rgba(209,143,64,0.12)", fg: "#d18f40" },
  openai: { bg: "rgba(16,163,127,0.12)", fg: "#10a37f" },
};

export function ProviderBalancesCard({
  wallets,
}: {
  // null means the treasury tables do not exist yet — a different situation from
  // "they exist and are empty", and the panel says so rather than implying $0.
  wallets: WalletRow[] | null;
}) {
  const showProject =
    wallets !== null && new Set(wallets.map((w) => w.project_id)).size > 1;
  const usd = usdColumnFormatter(wallets?.map((w) => w.balance_usd) ?? []);

  return (
    <section id="balances" className="scroll-mt-[90px]">
      <Panel
        className="animate-in delay-4"
        title="Provider Balances"
        tag={
          wallets && wallets.length > 0
            ? `${wallets.length} wallet${wallets.length === 1 ? "" : "s"}`
            : "Treasury"
        }
        bodyClassName="px-[20px] py-[4px]"
      >
        {wallets === null ? (
          <p className="t-cell text-text-secondary">
            Treasury not initialised — start the proxy to create the wallets
            table.
          </p>
        ) : wallets.length === 0 ? (
          <p className="t-cell text-text-secondary">
            No wallets yet. Seed one with{" "}
            {/* No /treasury prefix — the router is mounted bare (proxy/README.md);
                the prefixed path 404s. */}
            <span className="text-text-primary">POST /wallets/seed</span>.
          </p>
        ) : (
          wallets.map((w) => {
            const low = w.balance_usd < LOW_BALANCE_USD;
            const stale = isStale(w.updated_at, WALLET_STALE_AFTER_MS);
            const icon = ICONS[w.provider.toLowerCase()] ?? {
              bg: "rgba(255,255,255,0.06)",
              fg: "var(--color-text-secondary)",
            };

            return (
              <div
                key={w.id}
                className="flex items-center justify-between border-b border-border-subtle py-[12px] last:border-0"
              >
                <div className="flex items-center gap-[10px]">
                  <span
                    className="flex h-[32px] w-[32px] items-center justify-center rounded-[8px] text-[13px] font-bold"
                    style={{ background: icon.bg, color: icon.fg }}
                    aria-hidden="true"
                  >
                    {providerLabel(w.provider).charAt(0)}
                  </span>
                  <div>
                    <div className="text-[13px] font-medium text-text-primary">
                      {providerLabel(w.provider)}
                    </div>
                    <div className="flex items-center gap-[4px] text-[11px] text-text-tertiary">
                      <span
                        className="h-[5px] w-[5px] rounded-full"
                        style={{
                          background: stale
                            ? "var(--color-status-warn)"
                            : "var(--color-status-good)",
                        }}
                        aria-hidden="true"
                      />
                      {/* A balance is only meaningful with its age: "$4.00" is
                          reassuring and "$4.00, three hours ago" is alarming, and
                          reacting to the second is the Treasurer's whole job. */}
                      {stale ? relativeTime(w.updated_at) : "Fresh"}
                      {showProject && ` · ${w.project_id}`}
                    </div>
                  </div>
                </div>

                <div
                  className={`t-num text-[15px] font-semibold ${
                    low ? "text-status-warn" : "text-text-primary"
                  }`}
                >
                  {usd(w.balance_usd)}
                </div>
              </div>
            );
          })
        )}
      </Panel>
    </section>
  );
}
