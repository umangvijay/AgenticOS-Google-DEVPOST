"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { Search, X } from "lucide-react";
import { DOC_PAGES } from "@/content/docs";

export default function DocsToc({ currentSlug }: { currentSlug: string }) {
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const matches = DOC_PAGES.filter(page => `${page.title} ${page.blurb} ${page.sections.map(section => section.h).join(" ")}`.toLowerCase().includes(query.trim().toLowerCase()));

  useEffect(() => {
    // Reset the menu when the documentation route changes.
    // eslint-disable-next-line react-hooks/set-state-in-effect
    setOpen(false);
  }, [currentSlug]);

  return (
    <div className="docs-toc-slot">
      <button
        type="button"
        className="docs-toc-toggle"
        aria-expanded={open}
        aria-controls="documentation-navigation"
        onClick={() => setOpen((v) => !v)}
      >
        {open ? "Hide docs menu" : "Docs menu"}
      </button>
      <aside id="documentation-navigation" aria-label="Documentation navigation" className={`docs-toc glass-panel ${open ? "is-open" : ""}`}>
        <p className="eyebrow">Docs</p>
        <div className="docs-search">
          <Search size={15} aria-hidden="true" />
          <input type="search" value={query} onChange={event => setQuery(event.target.value)} placeholder="Find a guide…" aria-label="Search documentation" />
          {query && <button type="button" aria-label="Clear documentation search" onClick={() => setQuery("")}><X size={14} /></button>}
        </div>
        {query && <p className="docs-search-status" role="status">{matches.length ? `${matches.length} guides found` : "No guides found. Try a different term."}</p>}
        {matches.map((p) => (
          <Link
            key={p.slug}
            href={`/docs/${p.slug}`}
            aria-current={p.slug === currentSlug ? "page" : undefined}
            className={p.slug === currentSlug ? "docs-toc-link active" : "docs-toc-link"}
          >
            {p.title}
          </Link>
        ))}
        <Link href="/faq" className="docs-toc-link">FAQs</Link>
      </aside>
    </div>
  );
}
