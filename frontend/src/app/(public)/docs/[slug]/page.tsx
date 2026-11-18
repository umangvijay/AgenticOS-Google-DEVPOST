import PublicNavbar from "@/components/PublicNavbar";
import DocsToc from "@/components/DocsToc";
import Link from "next/link";
import { notFound } from "next/navigation";
import { DOC_PAGES } from "@/content/docs";

export function generateStaticParams() {
  return DOC_PAGES.map((p) => ({ slug: p.slug }));
}

export async function generateMetadata({ params }: { params: Promise<{ slug: string }> }) {
  const { slug } = await params;
  const page = DOC_PAGES.find((p) => p.slug === slug);
  return { title: page?.title || "Docs" };
}

export default async function DocArticle({ params }: { params: Promise<{ slug: string }> }) {
  const { slug } = await params;
  const page = DOC_PAGES.find((p) => p.slug === slug);
  if (!page) notFound();
  const pageIndex = DOC_PAGES.findIndex(item => item.slug === slug);
  const previous = DOC_PAGES[pageIndex - 1];
  const next = DOC_PAGES[pageIndex + 1];
  const words = [page.blurb, ...page.sections.flatMap(section => [section.h, section.p, ...(section.bullets ?? [])])].join(" ").split(/\s+/).length;
  const minutes = Math.max(1, Math.ceil(words / 200));

  return (
    <div className="mesh-gradient" style={{ minHeight: "100vh" }}>
      <PublicNavbar />
      <div className="docs-layout" data-scroll-reveal>
        <DocsToc currentSlug={slug} />
        <article id="doc-top" className="docs-article">
          <Link href="/docs" style={{ fontSize: 13, color: "var(--accent)", textDecoration: "none" }}>← All docs</Link>
          <div className="docs-reading-meta"><span>FIELD GUIDE / {String(pageIndex + 1).padStart(2, "0")}</span><span>{minutes} min read</span></div>
          <h1 className="apple-hero" style={{ fontSize: "clamp(28px, 5vw, 44px)", margin: "12px 0 16px" }}>{page.title}</h1>
          <p style={{ color: "var(--text-secondary)", fontSize: 18, lineHeight: 1.6, marginBottom: 28 }}>{page.blurb}</p>

          <div className="docs-hero-art glass-panel">
            {/* eslint-disable-next-line @next/next/no-img-element */}
            <img src={page.image} alt={`${page.title} diagram`} width={720} height={280} />
          </div>

          <div className="flow-diagram glass-panel" data-scroll-reveal style={{ padding: 20, margin: "28px 0" }}>
            <p className="eyebrow">Flow</p>
            <div className="flow-row">
              {page.diagram.map((step, i) => (
                <div key={step} className="flow-node">
                  <span className="flow-index">0{i + 1}</span>
                  <span>{step}</span>
                </div>
              ))}
            </div>
          </div>

          <nav className="docs-section-index" aria-label="On this page">
            <p className="eyebrow">On this sheet</p>
            {page.sections.map((section, index) => <a key={section.h} href={`#section-${index + 1}`}><span>{String(index + 1).padStart(2, "0")}</span>{section.h}</a>)}
          </nav>
          {page.sections.map((s, index) => (
            <section id={`section-${index + 1}`} key={s.h} className="docs-section">
              <h2><a className="docs-heading-anchor" href={`#section-${index + 1}`}>{s.h}<span aria-hidden="true">#</span></a></h2>
              <p>{s.p}</p>
              {s.bullets && (
                <ul>
                  {s.bullets.map((b) => (
                    <li key={b}>{b}</li>
                  ))}
                </ul>
              )}
            </section>
          ))}
          <nav className="docs-pagination" aria-label="More guides">
            {previous ? <Link href={`/docs/${previous.slug}`}><span>← PREVIOUS SHEET</span><strong>{previous.title}</strong></Link> : <Link href="/docs"><span>← THE COLLECTION</span><strong>All documentation</strong></Link>}
            {next ? <Link href={`/docs/${next.slug}`}><span>NEXT SHEET →</span><strong>{next.title}</strong></Link> : <Link href="/get-started"><span>PUT IT INTO PRACTICE →</span><strong>Start your first sheet</strong></Link>}
          </nav>
          <a className="docs-back-top" href="#doc-top">Back to top ↑</a>
        </article>
      </div>
    </div>
  );
}
