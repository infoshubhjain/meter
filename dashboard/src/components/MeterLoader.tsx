import { TopNav } from "@/components/TopNav";

export function MeterLoader({
  title = "Control Room",
  note = "Connecting to the live ledger.",
}: {
  title?: string;
  note?: string;
}) {
  return (
    <>
      <TopNav />
      <main className="load-shell" role="status" aria-live="polite" aria-busy="true">
        <div className="load-kicker"><span aria-hidden="true" /> METER / LIVE SYSTEM</div>
        <h1>{title}</h1>
        <p>{note}</p>
        <div className="load-track" aria-hidden="true"><div /></div>
        <small>Establishing a secure connection. This may take a moment if the service is waking up.</small>
      </main>
    </>
  );
}
