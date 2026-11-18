import PublicNavbar from "@/components/PublicNavbar";
import SheetHeader from "@/components/SheetHeader";
import Link from "next/link";
import { DOC_PAGES } from "@/content/docs";

export const metadata = { title: "Docs" };

export default function DocsIndexPage() {
  return (
    <div className="mesh-gradient" style={{ minHeight: "100vh" }}>
      <PublicNavbar />
      <main className="px-4 py-12 md:py-20" style={{ maxWidth: 1080, margin: "0 auto" }}>
        <SheetHeader
          eyebrow="DOCUMENTATION"
          index="04"
          title={<>How Sandtable <em>works.</em></>}
          deck="Plans, MCP creation, live execution, vaults, and your own API keys — with diagrams on every page."
        />
        <div style={{ height: 32 }} />
        <div className="docs-grid" data-scroll-reveal style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(260px, 1fr))", gap: 18 }}>
          {DOC_PAGES.map((p) => (
            <Link key={p.slug} href={`/docs/${p.slug}`} className="glass-panel docs-card console-frame" style={{ padding: 0, textDecoration: "none", overflow: "hidden" }}>
              <div className="docs-card-art">
                {/* eslint-disable-next-line @next/next/no-img-element */}
                <img src={p.image} alt="" width={400} height={140} />
              </div>
              <div style={{ padding: 20 }}>
                <h2 style={{ fontSize: 18, margin: "0 0 8px", color: "var(--text-primary)" }}>{p.title}</h2>
                <p style={{ margin: 0, color: "var(--text-secondary)", fontSize: 14, lineHeight: 1.5 }}>{p.blurb}</p>
              </div>
            </Link>
          ))}
        </div>
        <div className="glass-panel" data-scroll-reveal style={{ marginTop: 36, padding: 28, display: "flex", justifyContent: "space-between", gap: 16, flexWrap: "wrap", alignItems: "center" }}>
          <div>
            <h2 style={{ margin: "0 0 6px", fontSize: 22 }}>Still stuck?</h2>
            <p style={{ margin: 0, color: "var(--text-secondary)" }}>Accordion answers for guests, keys, MCP builds, and quota errors.</p>
          </div>
          <Link href="/faq" className="btn btn-primary">Open FAQs</Link>
        </div>
      </main>
    </div>
  );
}
