"use client";

import Link from "next/link";
import { RotateCcw } from "lucide-react";

export default function ErrorPage({ retry }: { error: Error & { digest?: string }; retry: () => void }) {
  return <main className="route-state">
    <section className="route-state-panel" aria-labelledby="error-title">
      <span className="draft-eyebrow">SANDTABLE / PAGE INTERRUPTED</span>
      <h1 id="error-title">Let’s pick up the thread.</h1>
      <p role="alert">This page couldn’t load. Try again, or return home to continue.</p>
      <div className="route-state-actions">
        <button type="button" className="btn btn-primary" onClick={retry}><RotateCcw size={16} /> Try again</button>
        <Link className="btn btn-secondary" href="/">Back home</Link>
      </div>
    </section>
  </main>;
}
