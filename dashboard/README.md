# Meter dashboard

Meter's Next.js 16 control room. It reads the same Postgres ledger the proxy writes,
but never writes provider, wallet, budget, or request data itself.

## Run locally

```bash
cd dashboard
cp .env.example .env.local
npm install
npm run dev
```

Set `DATABASE_URL` in `.env.local` to the Postgres database used by the proxy. Without
it, the dashboard deliberately renders useful empty states instead of inventing data.
Start the backend separately with `uvicorn proxy.app:app --port 8080 --reload` from the
repository root.

## Checks

```bash
npm run lint
npm run build
npm audit --omit=dev
```

## Deployment

Deploy this directory as the Vercel project root. Use Supabase's **transaction pooler**
connection string for `DATABASE_URL`; the session pooler is for the long-lived proxy.
The dashboard needs no provider, Prava, or Meter credentials. See the repository's
[`DEPLOY.md`](../DEPLOY.md) for the complete backend/database/dashboard setup.

## Boundaries

- `/dashboard` is the team-wide, read-only control room.
- `/try` starts a short-lived, session-scoped judge walkthrough; actions still go through
  the proxy rather than directly from the browser to the ledger.
- Polling pauses after idle time and reports `Offline` while retaining the last successful
  rows, so the UI does not claim that stale data is live.
