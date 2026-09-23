import type { Metadata } from "next";
import { DashboardJumpNav, TopNav } from "@/components/TopNav";
import { SpendHero } from "@/components/SpendHero";
import { TeamSpendTable } from "@/components/TeamSpendTable";
import { TeamBudgetCard } from "@/components/TeamBudgetCard";
import { ProviderBalancesCard } from "@/components/ProviderBalancesCard";
import { LiveLogsTable } from "@/components/LiveLogsTable";
import { CostPerOutcomeTable } from "@/components/CostPerOutcomeTable";
import { AgentLog } from "@/components/AgentLog";
import { JudgeConsole } from "@/components/judge/JudgeConsole";
import { JudgeStart } from "@/components/judge/JudgeStart";
import { EndSession } from "@/components/judge/SessionControls";
import { judgeContext } from "@/lib/session";
import {
  getBreakerState,
  getBudgets,
  getHeadlineMetrics,
  getLiveLogs,
  getOutcomeCosts,
  getOutcomeCoverage,
  getProviderBalances,
  getTeamSpend,
  getTreasuryEvents,
  isLedgerAvailable,
} from "@/lib/db";

export const metadata: Metadata = {
  title: "Meter — Try it yourself",
  description:
    "Your own session: predicted cost before every call, a runaway feature throttled, and an agent that pays its own bill.",
};

// Reads a cookie and live ledger state, so it can never be prerendered.
export const dynamic = "force-dynamic";

/**
 * The judge's Control Room.
 *
 * **This is the same page as `/dashboard`** — the same components, the same queries, the
 * same layout — with every read scoped to the judge's own project and one block inserted
 * between the Treasurer Agent and the Request Ledger.
 *
 * That is deliberate and it is the whole design. A judge should not be evaluating a
 * simplified demo built to show the product; they should be looking at the product, with
 * their own numbers in it. It also means there is no second dashboard to keep in sync:
 * a card added to `/dashboard` appears here, correct and scoped, for free.
 */
export default async function TryPage() {
  const judge = await judgeContext();

  // No session yet: collect a name and the optional keys, then come back with a cookie.
  if (!judge) return <JudgeStart />;

  const scope = judge.projectId;
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
  ] = await Promise.all([
    isLedgerAvailable(),
    getTeamSpend(scope),
    getProviderBalances(scope),
    getBudgets(scope),
    getBreakerState(scope),
    getTreasuryEvents(40, scope),
    getLiveLogs(50, scope),
    getOutcomeCosts(scope),
    getOutcomeCoverage(scope),
    getHeadlineMetrics(scope),
  ]);

  return (
    <>
      <TopNav />

      <main
        id="top"
        className="relative z-10 mx-auto w-full max-w-[1400px] px-[32px] pb-[60px] pt-[48px]"
      >
        <div className="mb-[32px] flex flex-wrap items-end justify-between gap-[20px]">
          <div>
            <h1 className="t-display">Control Room</h1>
            <div className="t-eyebrow mt-[8px]">
              Your session · {judge.displayName ?? "judge"} ·{" "}
              {judge.callCap - judge.callsUsed} of {judge.callCap} calls left
            </div>
          </div>
        </div>
        <DashboardJumpNav breaker={breaker} />

        {/* An empty Control Room is the correct initial state and looks exactly like a
            broken one. A judge who lands here with no context has to be told, before
            they read anything else, that the blank cards are waiting for them rather
            than failing — and given one obvious action that fills them. */}
        {judge.callsUsed < 3 && (
          <div
            className="mb-[24px] flex flex-wrap items-center justify-between gap-[16px] rounded-[10px] p-[20px]"
            style={{
              background: "var(--color-surface-2)",
              borderLeft: "3px solid var(--color-accent)",
            }}
          >
            <div className="max-w-[62ch]">
              <div
                className="text-[15px] font-semibold"
                style={{ color: "var(--color-text-primary)" }}
              >
                {judge.callsUsed === 0
                  ? "This is your own Control Room, and it starts empty. That is correct."
                  : `${judge.callsUsed} of 3 prompts run — keep going and the cards below fill in.`}
              </div>
              <div
                className="mt-[6px] text-[13px]"
                style={{ color: "var(--color-text-secondary)" }}
              >
                Every panel here is the real dashboard, showing only your session. Run the
                three prompts in the Console below — about 90 seconds — and the spend,
                budgets, accuracy and ledger populate as you go.{" "}
                <a href="/dashboard" className="underline">
                  See the team&rsquo;s dashboard
                </a>{" "}
                for what it looks like after 1,300 calls.
              </div>
            </div>
            <a href="#console" className="judge-cta">
              <span className="judge-cta-dot" aria-hidden />
              Start with prompt 1
            </a>
          </div>
        )}

        {!ledgerAvailable && (
          <div className="glass mb-[20px] p-[20px]">
            <p className="t-body text-text-primary">
              Ledger not found — the proxy is not reachable, so nothing can be metered.
            </p>
          </div>
        )}

        <div className="mb-[24px]">
          <SpendHero metrics={headline} wallets={wallets} />
        </div>

        <div className="mb-[24px] grid grid-cols-1 items-start gap-[24px] lg:grid-cols-[minmax(0,1fr)_minmax(0,1fr)]">
          <TeamBudgetCard scopes={budgets} />
          <ProviderBalancesCard wallets={wallets} />
        </div>

        <div className="mb-[24px]">
          <AgentLog
            initialEvents={treasuryEvents}
            emptyHint="Empty because your agent has not needed to act yet — it writes a row only when it decides to spend. Scroll to “The agent pays its own bill” below, press Check the runway, and its decision appears here."
          />
        </div>

        {/* The one thing this page has that `/dashboard` does not, and it sits here on
            purpose: directly under the agent whose decisions it triggers, and directly
            above the ledger every call it makes lands in. Both of those are visible
            while you use it, which is the point — the console is not a separate screen,
            it is a control on the dashboard. */}
        <div id="console" className="mb-[24px] scroll-mt-[100px]">
          <JudgeConsole session={judge} />
        </div>

        <div className="animate-in delay-5 mb-[24px]">
          <LiveLogsTable
            initialRows={liveLogs}
            emptyHint="Nothing here yet — this is your own ledger, and it starts empty. Run a prompt in the Console above and the row appears within a second, with the cost that was predicted before it ran."
          />
        </div>

        <div className="grid grid-cols-1 gap-[24px] xl:grid-cols-2">
          <div className="animate-in delay-6">
            <TeamSpendTable rows={spend} />
          </div>
          <div className="animate-in delay-6">
            <CostPerOutcomeTable rows={outcomes} coverage={outcomeCoverage} />
          </div>
        </div>

        {/* The way out, at the end rather than the top: a judge should reach this by
            finishing, not by looking for an escape. */}
        <div className="mt-[32px]">
          <EndSession token={judge.token} />
        </div>
      </main>
    </>
  );
}
