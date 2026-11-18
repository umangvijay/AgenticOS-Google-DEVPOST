import Link from "next/link";
import { ArrowUpRight, Compass } from "lucide-react";

export default function NotFound() {
  return <main className="route-state">
    <section className="route-state-panel" aria-labelledby="missing-title">
      <span className="draft-eyebrow"><Compass size={16} /> 404 / OFF THE DRAWING BOARD</span>
      <h1 id="missing-title">This sheet is missing.</h1>
      <p>The link may have changed, or this page may no longer be available. Start from the board or explore the documentation.</p>
      <div className="route-state-actions">
        <Link className="btn btn-primary" href="/dashboard">Open the board <ArrowUpRight size={16} /></Link>
        <Link className="btn btn-secondary" href="/docs">Read the docs</Link>
        <Link className="route-home-link" href="/">Back home</Link>
      </div>
    </section>
  </main>;
}
