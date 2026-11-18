import Image from "next/image";
import PublicNavbar from "@/components/PublicNavbar";
import SheetHeader from "@/components/SheetHeader";

export const metadata = { title: "Features" };

function FeatureList({ items }: { items: string[] }) {
  return (
    <ul style={{ listStyle: "none", padding: 0, display: "flex", flexDirection: "column", gap: 12, color: "var(--text-secondary)" }}>
      {items.map(item => (
        <li key={item} style={{ display: "flex", gap: 12 }}>
          <span aria-hidden style={{ color: "var(--success)", fontFamily: "var(--font-mono)" }}>✓</span>
          <span>{item}</span>
        </li>
      ))}
    </ul>
  );
}

export default function FeaturesPage() {
  return (
    <div className="mesh-gradient" style={{ minHeight: "100vh", display: "flex", flexDirection: "column" }}>
      {/* Navbar */}
      <PublicNavbar />

      {/* Content */}
      <main className="px-4 py-12 md:py-20" style={{ flex: 1, maxWidth: 1200, margin: "0 auto", textAlign: "center" }}>
        <SheetHeader
          eyebrow="MACHINE & MEDIUM"
          index="02"
          title={<>Drawn by intent,<br /><em>built by agents.</em></>}
          deck="Sandtable represents a paradigm shift from deterministic workflow engines to autonomous goal-oriented systems. Explore the architecture that makes it possible."
        />

        {/* Dynamic Builder */}
        <div className="grid grid-cols-1 md:grid-cols-2 gap-10 md:gap-16 mb-20 md:mb-24 mt-16 md:mt-24 text-left items-center">
          <div data-scroll-reveal>
            <div className="sheet-mono-label mb-3"><span className="draft-dot" aria-hidden /> 01 · THE DYNAMIC BUILDER</div>
            <h2 className="text-3xl md:text-4xl font-extrabold mb-4" style={{ fontFamily: "var(--font-serif)", fontWeight: 500, letterSpacing: "-.03em" }}>It writes its own tools.</h2>
            <p className="text-base md:text-lg text-[var(--text-secondary)] leading-relaxed mb-6">
              Say goodbye to missing integrations. When you ask Sandtable to interact with an API it doesn&apos;t recognize,
              it dispatches a sub-agent to find the API documentation, understand the authentication requirements,
              and generate validated API tool definitions that the workflow engine can call. When you need a portable deployment, export a standalone MCP server.
            </p>
            <FeatureList items={[
              "Google Search Grounding for accurate API specs",
              "AST Static Analysis prevents malicious code execution",
              "Automated Docker containerization",
            ]} />
          </div>
          <div style={{ display: "flex", flexDirection: "column", gap: 16, alignItems: "center" }}>
            <div data-scroll-reveal className="glass-card" style={{ padding: 12, width: "100%", height: "auto", display: "flex", alignItems: "center", justifyContent: "center", borderRadius: 12 }}>
              <Image src="/images/workflow-sketch.png" alt="Workflow Sketch" width={600} height={400} className="sketch-img" style={{ width: "100%", height: "auto", objectFit: "contain", borderRadius: 6 }} />
            </div>
            <div style={{ fontFamily: "var(--font-mono)", fontSize: 11, letterSpacing: ".08em", color: "var(--text-tertiary)", textAlign: "center" }}>FIG. 01 — ILLUSTRATIVE CONNECTOR BUILD</div>
          </div>
        </div>

        {/* Multi-Agent Orchestration */}
        <div className="flex flex-col-reverse md:flex-row gap-10 md:gap-16 mb-20 md:mb-24 text-left items-center">
          <div className="flex flex-col gap-4 items-center w-full md:w-1/2">
            <div data-scroll-reveal className="glass-card p-3 w-full h-auto flex items-center justify-center rounded-xl">
              <Image src="/images/network-sketch.png" alt="Network Sketch" width={600} height={400} className="sketch-img w-full h-auto object-contain rounded-md" />
            </div>
            <div style={{ fontFamily: "var(--font-mono)", fontSize: 11, letterSpacing: ".08em", color: "var(--text-tertiary)", textAlign: "center" }}>FIG. 02 — ILLUSTRATIVE SPECIALIST ROUTING</div>
          </div>
          <div data-scroll-reveal className="w-full md:w-1/2">
            <div className="sheet-mono-label mb-3"><span className="draft-dot" aria-hidden /> 02 · MULTI-AGENT ORCHESTRATION</div>
            <h2 className="text-3xl md:text-4xl font-extrabold mb-4" style={{ fontFamily: "var(--font-serif)", fontWeight: 500, letterSpacing: "-.03em" }}>A topology, not a prompt.</h2>
            <p className="text-base md:text-lg text-[var(--text-secondary)] leading-relaxed mb-6">
              Sandtable doesn&apos;t rely on a single massive prompt. It utilizes a topology of specialized agents powered by Google&apos;s ADK.
              The Intent Agent plans, the Orchestrator executes, the Research Agent gathers context, and the Recovery Agent handles failures.
            </p>
            <FeatureList items={[
              "Powered by Google Gemini Flash",
              "Circuit breakers for API rate limits",
              "Human-in-the-loop approval requests",
            ]} />
          </div>
        </div>
      </main>

      {/* Footer */}
      <footer className="py-12 px-6 md:py-16 md:px-10 border-t border-[var(--border-primary)] bg-[var(--bg-primary)] mt-12 md:mt-20">
        <div className="max-w-6xl mx-auto text-center text-[var(--text-secondary)]">
          <div style={{ display: "flex", alignItems: "center", justifyContent: "center", gap: 12, marginBottom: 24 }}>
            <div style={{
              width: 24, height: 24, borderRadius: "6px",
              background: "linear-gradient(135deg, var(--accent), var(--accent-pink))",
              display: "flex", alignItems: "center", justifyContent: "center",
              fontSize: 12, fontWeight: 800, color: "white"
            }}>S</div>
            <span style={{ fontSize: 18, fontWeight: 800 }}>Sandtable</span>
          </div>
          <p>© 2026 Sandtable. All rights reserved.</p>
        </div>
      </footer>
    </div>
  );
}
