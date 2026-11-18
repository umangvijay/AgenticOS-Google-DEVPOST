import PublicNavbar from "@/components/PublicNavbar";
import Link from "next/link";

export const metadata = { title: "Integrations" };

const BUILD_PATHS = [
  { number: "01", title: "Give it a contract", detail: "Paste an OpenAPI document or point to public API docs.", method: "OpenAPI / docs" },
  { number: "02", title: "Describe the missing action", detail: "Tell the builder which operations your workflow needs.", method: "Natural-language brief" },
  { number: "03", title: "Test before use", detail: "Validate endpoints and tool inputs, then review before enabling access.", method: "Live validation" },
];

const EXAMPLES = [
  { name: "Public REST API", tags: "OpenAPI · no credential", icon: "{}", description: "Turn an API schema into typed, callable tools for a run." },
  { name: "Your SaaS workspace", tags: "Docs · scoped key", icon: "↗", description: "Describe the operations you need and attach access through the encrypted vault." },
  { name: "A website", tags: "Browser · approval-aware", icon: "⌘", description: "Use visible browser actions when there is no suitable API. Sign-in challenges pause for you." },
  { name: "An internal service", tags: "Custom · access-controlled", icon: "◇", description: "Build against your endpoint contract and keep credentials scoped to your workspace." },
];

export default function PublicIntegrationsPage() {
  return (
    <div className="mesh-gradient integrations-public" style={{ minHeight: "100vh" }}>
      <PublicNavbar />
      <main className="integration-page-main">
        <section className="integration-hero" data-scroll-reveal>
          <div className="integration-hero-copy">
            <p className="eyebrow">A connector is a result of the work</p>
            <h1 style={{ fontFamily: "var(--font-serif)", fontWeight: 500, letterSpacing: "-.035em" }}>Start with the task.<br /><em>Build the connection it needs.</em></h1>
            <p className="integration-lead">
              Sandtable can build a connector from an API contract, documentation, or a clear description. It validates the tools before a workflow can use them, and asks you before sensitive actions.
            </p>
            <div className="integration-hero-actions">
              <Link href="/get-started" className="btn btn-primary btn-lg">Open workspace <span aria-hidden="true">↗</span></Link>
              <Link href="/dashboard/integrations/create" className="btn btn-secondary btn-lg">Build a connector</Link>
            </div>
            <p className="integration-note">Examples below are build paths, not pre-connected accounts.</p>
          </div>
          <div className="integration-console glass-panel" aria-label="Connector build stages" data-scroll-reveal>
            <div className="integration-console-head"><span>CONNECTOR WORKBENCH</span><span>Review before enable</span></div>
            <div className="integration-console-title">A tool grows from a real contract</div>
            {BUILD_PATHS.map((step) => (
              <div className="integration-console-step" key={step.number}>
                <span className="integration-step-number">{step.number}</span>
                <div><strong>{step.title}</strong><small>{step.detail}</small></div>
                <span className="integration-step-tag">{step.method}</span>
              </div>
            ))}
            <div className="integration-console-foot"><span className="integration-state-dot" />Build state and test results appear here during a real request.</div>
          </div>
        </section>

        <section className="integration-examples" data-scroll-reveal>
          <div className="integration-section-head">
            <div><p className="eyebrow">Choose an entry point</p><h2>Different systems. One careful path.</h2></div>
            <Link href="/dashboard/integrations/create" className="integration-text-link">Open connector builder <span aria-hidden="true">→</span></Link>
          </div>
          <div className="integration-method-grid">
            {EXAMPLES.map((example) => (
              <Link className="integration-method-card glass-panel" href="/dashboard/integrations/create" key={example.name}>
                <div className="integration-method-top"><span className="integration-method-icon">{example.icon}</span><span className="integration-method-tag">{example.tags}</span></div>
                <h3>{example.name}</h3><p>{example.description}</p>
                <span className="integration-method-action">Build for a workflow <span aria-hidden="true">↗</span></span>
              </Link>
            ))}
          </div>
        </section>
      </main>
    </div>
  );
}
