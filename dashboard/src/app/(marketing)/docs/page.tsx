import { TopNav } from "@/components/TopNav";

const configuration = [
  ["DATABASE_URL", "Required Postgres connection. Use the session pooler for the proxy and the transaction pooler for the Vercel dashboard."],
  ["METER_KEYS", "Meter credentials mapped to project and environment. Never deploy the repository's development default."],
  ["TREASURER_DRY_RUN", "Defaults to true. Exercises the decision path without spending against Prava."],
  ["BREAKER_MODE", "throttle (default) scopes a 429 to the feature; revoke returns 403 for the whole key."],
];

function Section({ id, title, children }: { id: string; title: string; children: React.ReactNode }) {
  return <section className="doc-section" id={id}><h2>{title}</h2>{children}</section>;
}

export default function DocsPage() {
  return <><TopNav /><main>
    <div className="docs-shell">
      <section className="docs-hero"><div><p className="eyebrow">METER / TECHNICAL DOCUMENTATION</p><h1>Operate<br />inference.</h1><p>Meter is a FastAPI proxy and ledger for measuring, controlling, and safely replenishing model-provider spend. This is the implementation guide, not marketing copy.</p></div><nav className="docs-toc" aria-label="Documentation sections"><a href="#quickstart">01 / Quickstart</a><a href="#lifecycle">02 / Request lifecycle</a><a href="#configuration">03 / Configuration</a><a href="#api">04 / API reference</a><a href="#deployment">05 / Deployment</a></nav></section>
      <div className="docs-content"><aside className="docs-aside">ON THIS PAGE<a href="#quickstart">Quickstart</a><a href="#lifecycle">Lifecycle</a><a href="#configuration">Configuration</a><a href="#api">API reference</a><a href="#deployment">Deployment</a></aside><div>
        <Section id="quickstart" title="Quickstart"><p>Run the proxy and point an OpenAI-compatible client at it. Meter accepts a Meter credential, resolves it to a project, and substitutes the provider key only on the outbound request.</p><pre>{`cp .env.example .env\ncp meter.yaml.example meter.yaml\nuvicorn proxy.app:app --port 8080 --reload\n\nOPENAI_BASE_URL=http://localhost:8080/v1\nOPENAI_API_KEY=mk_your_meter_key`}</pre><p>Use <code>X-Meter-Feature</code>, <code>X-Meter-Actor</code>, and <code>X-Meter-Trace</code> to attribute cost without changing the request body.</p></Section>
        <Section id="lifecycle" title="Request lifecycle"><p>Meter uses an authorize/capture model. It reserves an estimate before forwarding a call, then captures actual usage after the provider responds. This prevents a burst of concurrent requests from all observing the same unreserved balance.</p><ol><li><b>Authenticate</b> the Meter key and resolve project/environment.</li><li><b>Attribute</b> the feature, actor, and trace headers.</li><li><b>Estimate</b> exact input tokens plus a predicted output cost.</li><li><b>Reserve</b> budget headroom and check the circuit breaker.</li><li><b>Forward</b> bytes to the provider, including streaming responses.</li><li><b>Capture</b> actual token usage, price it, and release unused reserve.</li></ol></Section>
        <Section id="configuration" title="Configuration"><table className="doc-table"><thead><tr><th>Variable</th><th>Purpose</th></tr></thead><tbody>{configuration.map(([key, value]) => <tr key={key}><td><code>{key}</code></td><td>{value}</td></tr>)}</tbody></table><p>Daily ceilings are declared in <code>meter.yaml</code>. The file is the source of truth; database rows are a read cache built at boot. Restart the proxy after changing it.</p></Section>
        <Section id="api" title="API reference"><table className="doc-table"><thead><tr><th>Route</th><th>Use</th></tr></thead><tbody><tr><td><code>POST /v1/chat/completions</code></td><td>OpenAI-shaped proxy endpoint with prediction, budget enforcement, and ledger capture.</td></tr><tr><td><code>POST /v1/annotate</code></td><td>Attach an outcome and value to a trace for cost-per-outcome reporting.</td></tr><tr><td><code>POST /v1/breaker/reset</code></td><td>Manually reset a tripped circuit breaker for a project scope.</td></tr><tr><td><code>GET /healthz</code></td><td>Read-only health state: pricing version, configured ceilings, and breaker configuration.</td></tr><tr><td><code>POST /treasury/tick</code></td><td>Run one Treasurer decision pass; requires a Meter key.</td></tr></tbody></table></Section>
        <Section id="deployment" title="Deployment"><p>Deploy the dashboard on Vercel and the proxy on a long-lived host such as Render. The proxy owns background Treasurer and refresh loops, so it must not run as a serverless function. Use a single proxy instance: reservation serialization is in-process until a shared reservation store is introduced.</p><pre>{`Dashboard (Vercel)     DATABASE_URL = Supabase transaction pooler\nProxy (Render)         DATABASE_URL = Supabase session pooler\n\nKeep TREASURER_DRY_RUN=true unless a monitored, deliberate sandbox charge is required.`}</pre></Section>
      </div></div>
    </div>
  </main></>;
}
