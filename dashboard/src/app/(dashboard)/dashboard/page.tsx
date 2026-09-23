import { DashboardJumpNav, TopNav } from "@/components/TopNav";
import { SpendHero } from "@/components/SpendHero";
import { TeamSpendTable } from "@/components/TeamSpendTable";
import { TeamBudgetCard } from "@/components/TeamBudgetCard";
import { ProviderBalancesCard } from "@/components/ProviderBalancesCard";
import { LiveLogsTable } from "@/components/LiveLogsTable";
import { CostPerOutcomeTable } from "@/components/CostPerOutcomeTable";
import { ModelEfficiencyTable } from "@/components/ModelEfficiencyTable";
import { AgentLog } from "@/components/AgentLog";
import { TryItYourself } from "@/components/judge/SessionControls";
import {
  getTeamSpend,
  getBudgets,
  getBreakerState,
  getTreasuryEvents,
  getOutcomeCosts,
  getOutcomeCoverage,
  getLiveLogs,
  getProviderBalances,
  getHeadlineMetrics,
  getModelEfficiency,
  isLedgerAvailable,
} from "@/lib/db";

// Reads the ledger on every request — must not be statically prerendered at build
// time, or the dashboard would freeze on whatever spend existed the moment
// `next build` ran. It also means `next build` never needs a reachable database.
export const dynamic = "force-dynamic";

export default async function Home() {
  // Promise.all, not nine awaits in a row. Every one of these is a network round trip
  // to a hosted database now — ~50ms each from outside its region — so serial awaits
  // would put half a second of dead time in front of first paint for no reason. None
  // of them depends on another.
  const [
    ledgerAvailable,
    spend,
    wallets,
    budgets,
    breaker,
    treasuryEvents,
    liveLogs,
    outcomes,
    outcomeCoverage,
    headline,
    modelEfficiency,
  ] = await Promise.all([
    isLedgerAvailable(),
    getTeamSpend(),
    getProviderBalances(),
    getBudgets(),
    getBreakerState(),
    getTreasuryEvents(),
    getLiveLogs(),
    getOutcomeCosts(),
    getOutcomeCoverage(),
    getHeadlineMetrics(),
    getModelEfficiency(),
  ]);

  return (
    <>
      <TopNav />

      {/* z-10 puts the content plane above all three fixed background layers. */}
      <main
        id="top"
        className="relative z-10 mx-auto w-full max-w-[1400px] px-[32px] pb-[60px] pt-[48px]"
      >
        {/* The page title. Condensed and large, with the deployment it is reading
            from stated underneath — on a screen showing live money, "which
            database is this" is the first question anyone asks. */}
        <div className="mb-[32px] flex flex-wrap items-end justify-between gap-[20px]">
          <div>
            <h1 className="t-display">Control Room</h1>
            <div className="t-eyebrow mt-[8px]">
              Live system status · metered through the proxy
            </div>
          </div>
          {/* Right-aligned with the title, not buried in the nav. This is the only
              action on the page and the one thing a visitor with no context needs
              to find. */}
          <TryItYourself />
        </div>
        <DashboardJumpNav breaker={breaker} />

        {!ledgerAvailable && (
          <div className="glass mb-[20px] p-[20px]">
            <p className="t-body text-text-primary">
              Ledger not found — start the proxy (see{" "}
              <span className="text-text-primary">proxy/README.md</span>) to
              begin logging requests.
            </p>
          </div>
        )}

        {/* The metric row runs full width — it is the summary everything below
            expands on, and boxing it beside something else would make it look
            like one of several equal concerns. */}
        <div className="mb-[24px]">
          <SpendHero metrics={headline} wallets={wallets} />
        </div>

        {/* The budget rail takes about half the row rather than all of it — at full
            width it fitted six cards, which is more of a constraint than anyone
            reads at a glance, and the scrolling stopped meaning anything. Two in
            view with a third peeking is enough to say "there are more".

            Balances takes the other half, so the width the rail gave up is used
            rather than left as dead space beside it. items-start, because these two
            have genuinely different natural heights. */}
        <div className="mb-[24px] grid grid-cols-1 items-start gap-[24px] lg:grid-cols-[minmax(0,1fr)_minmax(0,1fr)]">
          <TeamBudgetCard scopes={budgets} />
          <ProviderBalancesCard wallets={wallets} />
        </div>

        <div className="mb-[24px]">
          <AgentLog initialEvents={treasuryEvents} />
        </div>

        <div className="animate-in delay-5 mb-[24px]">
          <LiveLogsTable initialRows={liveLogs} />
        </div>

        <div className="grid grid-cols-1 gap-[24px] xl:grid-cols-2">
          <div className="animate-in delay-6">
            <TeamSpendTable rows={spend} />
          </div>
          <div className="animate-in delay-6">
            <CostPerOutcomeTable rows={outcomes} coverage={outcomeCoverage} />

            <ModelEfficiencyTable rows={modelEfficiency} />
          </div>
        </div>
      </main>
    </>
  );
}
