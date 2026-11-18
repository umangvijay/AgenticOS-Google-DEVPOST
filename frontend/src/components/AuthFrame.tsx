import Link from "next/link";
import type { ReactNode } from "react";
import ThemeSwitch from "@/components/ThemeSwitch";
import BrandMark from "@/components/BrandMark";

export default function AuthFrame({ children, title, description }: { children: ReactNode; title: string; description: string }) {
  return <main className="auth-shell">
    <header className="auth-topline">
      <Link href="/" aria-label="Sandtable home"><BrandMark size={28} withWordmark={false} /><span>SANDTABLE.</span></Link>
      <ThemeSwitch />
    </header>
    <div className="auth-layout">
      <aside className="auth-story" aria-label="About your workspace">
        <span className="draft-eyebrow">THE DRAFTING WORKS / YOUR WORKSPACE</span>
        <h2>Good work starts<br />with <em>a clear plan.</em></h2>
        <p>Bring an idea. Shape the blueprint. Connect your tools and build with a workspace that keeps you in control.</p>
        <svg viewBox="0 0 460 280" fill="none" className="auth-diagram" aria-hidden="true">
          <g stroke="currentColor"><circle cx="250" cy="85" r="48" /><ellipse cx="250" cy="85" rx="20" ry="48" /><ellipse cx="250" cy="85" rx="65" ry="18" transform="rotate(-25 250 85)" /><path d="M230 125Q130 155 110 205M265 128Q350 140 330 200M245 130Q240 180 230 215" /><path d="m70 185 260-25 50 80-260 25zM90 210l260-25M110 240l260-25M150 177l50 80M230 169l50 80" /></g>
        </svg>
        <div className="auth-sequence"><span>01 / DESCRIBE</span><span>02 / REVIEW</span><span>03 / BUILD</span></div>
      </aside>
      <section className="auth-form-region" aria-labelledby="auth-title">
        <span className="draft-eyebrow">SANDTABLE / ACCESS</span>
        <h1 id="auth-title">{title}</h1><p className="auth-intro">{description}</p>
        {children}
      </section>
    </div>
    <footer className="auth-footer"><span>A WORKSPACE FOR IDEAS IN MOTION</span><nav aria-label="Account help"><Link href="/contact">Support</Link><Link href="/privacy">Privacy</Link><Link href="/terms">Terms</Link></nav></footer>
  </main>;
}
