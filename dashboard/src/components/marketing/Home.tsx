import Link from "next/link";
import { TopNav } from "@/components/TopNav";

const evidence = [
  ["01", "Pre-flight control", "Estimate cost before a request reaches a model. Reserve budget before a concurrent workload can overspend it."],
  ["02", "A ledger with context", "Attach every request to a project, feature, actor, and trace—then price the actual usage with a versioned rate card."],
  ["03", "A bounded treasurer", "When runway is short, evaluate a mandate, caps, cooldown, and daily limits before any top-up can proceed."],
];

export function Home() {
  return <><TopNav /><main>
    <section className="lab-hero" id="top">
      <div className="hero-kicker"><span /> INFERENCE SYSTEMS / 01</div>
      <div className="hero-copy"><p className="eyebrow">Meter is an inference control plane.</p><h1>Know the cost<br />before the call.</h1><p className="hero-lede">A financial operating layer for AI systems: measure every request, enforce the limits you set, and protect production when usage changes shape.</p><div className="hero-actions"><Link href="/docs" className="button button-primary">Read the technical brief <span>→</span></Link><Link href="/try" className="button button-secondary">Try the system</Link></div></div>
      <div className="signal-card" aria-label="Meter request lifecycle"><div className="signal-card-top"><span>LIVE REQUEST MODEL</span><i>●</i></div><div className="signal-grid"><b>01</b><span>Estimate</span><em>tiktoken + output heuristic</em><b>02</b><span>Reserve</span><em>concurrency-safe budget hold</em><b>03</b><span>Capture</span><em>actual priced usage</em></div><div className="signal-footer">AUTH → ATTRIBUTE → ESTIMATE → RESERVE → FORWARD → CAPTURE</div></div>
      <p className="hero-footnote">Built for teams running model APIs in production. No agent decides what to spend; code enforces the policy.</p>
    </section>
    <section className="statement" id="system"><p className="section-index">THE PREMISE / 02</p><h2>AI spend is a systems problem, not a reporting problem.</h2><p>Meter lives in the request path. It turns a provider call into a measured, attributable, policy-bound operation—without turning your application into a finance project.</p></section>
    <section className="evidence-grid" id="principles">{evidence.map(([number, title, body]) => <article key={number}><span>{number}</span><h3>{title}</h3><p>{body}</p></article>)}</section>
    <section className="technical-callout"><div><p className="section-index">TECHNICAL BRIEF / 03</p><h2>Designed for the difficult parts of inference operations.</h2></div><ul><li><b>Reserve / capture</b><span>keeps ceilings meaningful under concurrency.</span></li><li><b>Streaming-aware accounting</b><span>captures usage without buffering the client response.</span></li><li><b>Versioned pricing</b><span>keeps historical cost rows reproducible.</span></li><li><b>Safety rails</b><span>bound every autonomous treasury decision.</span></li></ul><Link href="/docs" className="text-link">Explore architecture, API reference, configuration, and deployment <span>→</span></Link></section>
    <section className="closing"><p className="eyebrow">METER / INFERENCE SYSTEMS</p><h2>Make inference<br />operable.</h2><Link href="/dashboard" className="button button-primary">Open control room <span>→</span></Link></section>
    <footer><span>METER</span><span>Inference systems for teams that need a number they can act on.</span><Link href="/docs">Docs</Link></footer>
  </main></>;
}
