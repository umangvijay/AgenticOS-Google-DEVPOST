"use client";

import dynamic from "next/dynamic";
import Link from "next/link";
import { useCallback, useEffect, useRef, useState } from "react";
import { ArrowUpRight, Pause, Play, Layers, Workflow, Grid2X2, Stamp, Focus, Globe2, RotateCcw, SlidersHorizontal } from "lucide-react";
import type { SceneFocus, SceneStatus } from "@/components/CyanotypeScene";
import PublicNavbar from "@/components/PublicNavbar";
import { API_BASE } from "@/lib/api";
import { readThemePref, resolveTheme } from "@/lib/theme";

const CyanotypeScene = dynamic(() => import("@/components/CyanotypeScene"), {
  ssr: false, loading: () => <div className="scene-fallback" />,
});
const modules = [
  { label: "Board", href: "/dashboard", icon: Grid2X2, number: "01" },
  { label: "Stamps", href: "/dashboard/integrations", icon: Stamp, number: "02" },
  { label: "Builds", href: "/dashboard/workflows", icon: Workflow, number: "03" },
  { label: "Sheets", href: "/dashboard/operations", icon: Layers, number: "04" },
];

/** Live vitals from the real /health endpoint — the same self-healing
    loop (recovery agent, adaptive policy) that runs the workspace. Hidden
    entirely when the API is unreachable, never faked. */
function LoomVitals() {
  const [vitals, setVitals] = useState<{ uptime: number; storage: string } | null>(null);
  useEffect(() => {
    let alive = true;
    const load = async () => {
      try {
        const res = await fetch(`${API_BASE.replace("/api/v1", "")}/health`, { cache: "no-store" });
        if (!res.ok) { if (alive) setVitals(null); return; }
        const data = await res.json();
        if (alive && data?.status === "healthy") {
          setVitals({
            uptime: Math.max(0, Math.floor((Number(data.uptime_seconds) || 0) / 60)),
            storage: String(data.storage || "unknown").toUpperCase(),
          });
        } else if (alive) setVitals(null);
      } catch { if (alive) setVitals(null); }
    };
    // load() commits real telemetry only after the fetch resolves.
    load();
    const id = setInterval(load, 30000);
    return () => { alive = false; clearInterval(id); };
  }, []);
  if (!vitals) return null;
  return (
    <div className="loom-vitals" role="status">
      <span className="draft-dot" aria-hidden /> LOOM VITALS · UPTIME {vitals.uptime}M · {vitals.storage} · API CONNECTED
    </div>
  );
}

export default function LandingPage() {
  const [mode, setMode] = useState<"light" | "dark">("dark");
  const [tempo, setTempo] = useState(1);
  const lastTempo = useRef(1);
  const [sceneStatus, setSceneStatus] = useState<SceneStatus>("loading");
  const [sceneAttempt, setSceneAttempt] = useState(0);
  const [weave, setWeave] = useState(50);
  const retryScene = () => { setSceneStatus("loading"); setFocus("overview"); setSceneAttempt(attempt => attempt + 1); };
  const [zoom, setZoom] = useState(1);
  const [focus, setFocus] = useState<SceneFocus>("overview");
  const [reducedMotion, setReducedMotion] = useState(false);
  const updateSceneStatus = useCallback((status: SceneStatus) => {
    setSceneStatus(status);
    if (status === "fallback") setFocus("overview");
  }, [setFocus, setSceneStatus]);
  useEffect(() => {
    const reset = (event: KeyboardEvent) => { if (event.key === "Escape") { setFocus("overview"); setZoom(1); } };
    window.addEventListener("keydown", reset);
    return () => window.removeEventListener("keydown", reset);
  }, []);
  useEffect(() => {
    const sync = () => setMode(resolveTheme(readThemePref()));
    sync();
    const motion = window.matchMedia("(prefers-reduced-motion: reduce)");
    const reduce = () => { setReducedMotion(motion.matches); setTempo(motion.matches ? 0 : 1); };
    reduce();
    const theme = new MutationObserver(sync);
    theme.observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme"] });
    motion.addEventListener("change", reduce);
    return () => { theme.disconnect(); motion.removeEventListener("change", reduce); };
  }, []);

  return <div className={`drafting-home${focus !== "overview" ? " is-inspecting" : ""}`}>
    <CyanotypeScene key={sceneAttempt} zoom={zoom} onZoom={setZoom} status={sceneStatus} onStatus={updateSceneStatus} onWeave={setWeave} mode={mode} tempo={tempo} focus={focus} onFocus={setFocus} reducedMotion={reducedMotion} />
    <div className="home-vignette" aria-hidden="true" />
    <div className="home-chrome">
      <a className="skip-link" href="#main-content">Skip to content</a>
      <PublicNavbar />
      <div className="draft-ruler" aria-hidden="true"><span>ST / THE DRAFTING WORKS</span><span>0 ——— 120 ——— 240 ——— 480 ——— 960</span><span>VOL. 01 / IN MOTION</span></div>
      <main tabIndex={-1} id="main-content" className="draft-stage">
        <section className="hero-card console-frame" aria-labelledby="hero-title">
          <span className="frame-tr" /><span className="frame-bl" />
          <div className="draft-eyebrow"><span className="draft-dot" /> INTELLIGENCE, TAKING SHAPE</div>
          <h1 id="hero-title">Say it.<br /><em>It&apos;s drawn.</em><br />It&apos;s built.</h1>
          <p className="hero-deck">Plans printed in blue.<br />Built exactly as drawn.</p>
          <p className="hero-description">Describe the outcome. Watch a plan take shape, connect your tools, and turn the blueprint into work — with you in control.</p>
          <div className="hero-actions">
            <Link href="/get-started" className="btn btn-primary">Start a sheet <ArrowUpRight size={17} /></Link>
            <Link href="/dashboard/integrations" className="btn btn-secondary">Open stamp drawer <Stamp size={16} /></Link>
          </div>
          <div className="hero-footnote"><span>GOAL → BLUEPRINT → BUILD</span><span>HUMAN APPROVED</span></div>
        </section>
      </main>
      <footer className="draft-footer">
        <details className="scene-inspector">
          <summary><SlidersHorizontal size={14} /> Scene controls</summary>
          <div className="scene-inspector-panel">
            <div className="draft-eyebrow">PLANETARY LOOM / VIEW</div>
            <div className="hud-views" role="group" aria-label="Camera view">
              <button type="button" disabled={sceneStatus !== "ready"} aria-pressed={focus === "planet"} onClick={() => setFocus("planet")}><Globe2 size={15} /> Planet</button>
              <button type="button" disabled={sceneStatus !== "ready"} aria-pressed={focus === "sheet"} onClick={() => setFocus("sheet")}><Focus size={15} /> Blueprint</button>
              <button type="button" aria-label="Reset camera" onClick={() => { setFocus("overview"); setZoom(1); }}><RotateCcw size={15} /></button>
            </div>
            <label className="camera-zoom" htmlFor="scene-zoom"><span>ZOOM</span><output>{Math.round(zoom * 100)}%</output><input id="scene-zoom" type="range" min="0.4" max="2.8" step="0.01" value={zoom} disabled={sceneStatus !== "ready"} onChange={event => setZoom(Number(event.target.value))} /></label>
            {sceneStatus === "ready" && <div className="weave-meter"><div><span>BLUEPRINT WEAVE</span><span>{weave}%</span></div><meter min={0} max={100} value={weave} aria-label="Blueprint weave progress" /></div>}
            <p className="scene-control-hint">Click the planet or foreground blueprint to focus. Press Esc to reset.</p>
          </div>
        </details>
        <LoomVitals />
        {sceneStatus !== "ready" && <div className="scene-notice" role="status">
          <p>{sceneStatus === "loading" ? "Preparing your 3D world…" : "Showing the still illustration. Your workspace is ready to use."}</p>
          {sceneStatus === "fallback" && <button type="button" className="btn btn-secondary" onClick={retryScene}>Retry 3D scene</button>}
        </div>}
        <div className="scene-controls" role="group" aria-label="Scene animation controls">
          <button type="button" disabled={sceneStatus !== "ready"} onClick={() => { if (tempo > 0) lastTempo.current = tempo; setTempo(tempo === 0 ? lastTempo.current : 0); }} aria-pressed={tempo === 0} aria-label={tempo === 0 ? "Play scene" : "Pause scene"}>{tempo === 0 ? <Play size={14} /> : <Pause size={14} />}</button>
          <span> TEMPO </span>
          {[.5, 1, 2].map(speed => <button type="button" disabled={sceneStatus !== "ready"} key={speed} aria-pressed={tempo === speed} onClick={() => { lastTempo.current = speed; setTempo(speed); }}>{speed.toFixed(1)}×</button>)}
        </div>
        <nav className="module-dock" aria-label="Workspace modules">
          {modules.map(({ label, href, icon: Icon, number }) => <Link href={href} key={href}><span>{number}</span><Icon size={15} />{label}<ArrowUpRight size={12} /></Link>)}
        </nav>
        <span className="scene-edition">SANDTABLE®<br />AN OPEN FIELD FOR IDEAS</span>
      </footer>
    </div>
  </div>;
}
