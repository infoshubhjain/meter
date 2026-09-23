import { cookies } from "next/headers";

/**
 * Resolving a judge's session on the *server*, so the Control Room can render their data.
 *
 * The judge view is not a second dashboard. It is the same page, the same components and
 * the same queries, with every read scoped to one project — so the token has to be
 * readable where those queries run, which is a server component. That rules out
 * `localStorage` and makes a cookie the only option.
 *
 * **Deliberately not `httpOnly`.** The console also calls the proxy directly from the
 * browser (`/judge/*` on another origin, where cookies are not sent), so the client needs
 * to read the same value. The alternative is storing it twice and keeping the copies in
 * sync, which is genuinely worse.
 *
 * The cost of that is no longer "small", and PROPOSALS.md B20 is the correction: this
 * token keys the server's credential vault, and `/judge/mandate` uses it to act with the
 * judge's own Prava *merchant* key. A readable token is a readable licence to charge a
 * real card, which is not what "a throwaway sandbox session" implies.
 *
 * So the token no longer carries that authority alone. Spending needs a second secret —
 * `meter_judge_capability`, httpOnly, set by the proxy on the proxy's own origin, checked
 * by `judge/routes.py::_require_capability`. This cookie stays readable and stays
 * sufficient for every read, which is all the server-rendered page needs it for.
 */
export const SESSION_COOKIE = "meter_judge_session";

/** How long the cookie lives. Matches `judge.sessions.SESSION_TTL_S`. */
export const SESSION_MAX_AGE_S = 4 * 3600;

export type JudgeContext = {
  token: string;
  projectId: string;
  displayName: string | null;
  callsUsed: number;
  callCap: number;
  expiresAt: string;
};

/**
 * The live judge session for this request, or `null`.
 *
 * Ask the proxy that owns the session. A broken dashboard ledger connection must not
 * make a successfully created session disappear from the trial page.
 *
 * Returns `null` for an expired session as well as an absent one. The distinction matters
 * to the *console*, which tells a judge their session timed out — but the page underneath
 * has nothing to render either way, and showing a stale project's numbers would be worse
 * than showing the team's. A proxy outage throws and shows the route's retry screen.
 */
export async function judgeContext(): Promise<JudgeContext | null> {
  const token = (await cookies()).get(SESSION_COOKIE)?.value;
  if (!token) return null;

  const proxy = (process.env.NEXT_PUBLIC_METER_PROXY_URL ?? "http://localhost:8080").replace(/\/$/, "");
  const response = await fetch(`${proxy}/judge/session`, {
    headers: { "X-Judge-Session": token },
    cache: "no-store",
    signal: AbortSignal.timeout(12000),
  });
  if (response.status === 401 || response.status === 440) return null;
  if (!response.ok) throw new Error(`Session service unavailable (${response.status})`);

  const session = await response.json();
  return {
    token,
    projectId: session.project_id,
    displayName: session.display_name,
    callsUsed: session.calls_used,
    callCap: session.call_cap,
    expiresAt: session.expires_at,
  };
}
