"use client";

import { FormEvent, useState } from "react";
import { checkSiteHealth, debugSource, generateProject, generateModelAsset, artifactFileUrl, fetchArtifactText, GenerateProgress } from "@/lib/api";

export default function StudioPage() {
  const [progress, setProgress] = useState<GenerateProgress | null>(null);
  const [healthUrl, setHealthUrl] = useState("https://example.com");
  const [health, setHealth] = useState<Record<string, unknown> | null>(null);
  const [src, setSrc] = useState("def add(a, b):\n    return a - b\n");
  const [debug, setDebug] = useState<Record<string, unknown> | null>(null);
  const [brief, setBrief] = useState("");
  const [kind, setKind] = useState("website");
  const [scale, setScale] = useState("compact");
  const [artifact, setArtifact] = useState<{ artifact_id: string; files: string[]; name: string; summary: string } | null>(null);
  const [previewHtml, setPreviewHtml] = useState("");
  const [selectedFile, setSelectedFile] = useState("");
  // §27.15: explicit 3D model job — parameters never fall back to a website.
  const [modelKind, setModelKind] = useState("box");
  const [modelSize, setModelSize] = useState("2, 1, 1");
  const [modelSegments, setModelSegments] = useState(16);
  const [modelResult, setModelResult] = useState<{ artifact_id: string; files: string[]; name: string; model?: { vertices: number; faces: number; sha256: string } } | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState("");

  async function run<T>(key: string, fn: () => Promise<T>, setter: (v: T) => void) {
    setError("");
    setBusy(key);
    try {
      setter(await fn());
    } catch (e) {
      setError(e instanceof Error ? e.message : "Request failed");
    } finally {
      setBusy(null);
    }
  }

  return (
    <div className="animate-fade-in-up" style={{ maxWidth: 960, margin: "0 auto" }}>
      <h1 style={{ fontSize: 24, fontWeight: 700, marginBottom: 8 }}>Studio</h1>
      <p style={{ color: "var(--text-secondary)", marginBottom: 24 }}>
        Site health, debugging, and generating a website or app — live calls, not canned output.
      </p>
      {error && <div className="glass-card" style={{ padding: 12, marginBottom: 16, color: "var(--error)" }}>{error}</div>}

      <div style={{ display: "grid", gap: 20 }}>
        <section className="glass-card" style={{ padding: 20 }}>
          <h2 style={{ fontSize: 16, margin: "0 0 12px" }}>Website health</h2>
          <form onSubmit={(e: FormEvent) => { e.preventDefault(); run("h", () => checkSiteHealth(healthUrl), setHealth); }} style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
            <input className="input" style={{ flex: 1, minWidth: 200 }} value={healthUrl} onChange={(e) => setHealthUrl(e.target.value)} />
            <button className="btn btn-primary" disabled={busy === "h"}>{busy === "h" ? "Checking…" : "Check"}</button>
          </form>
          {health && <div style={{marginTop: 16}}>
            <strong>{String(health.title || health.host || health.url || "Health report")}</strong>
            <dl style={{display: "grid", gridTemplateColumns: "140px 1fr", gap: 8, fontSize: 14}}>
              <dt>Status</dt><dd>{String(health.status_code || "Unreachable")}</dd>
              <dt>Latency</dt><dd>{typeof health.latency_ms === "number" ? `${health.latency_ms} ms` : "Unavailable"}</dd>
              <dt>Assessment</dt><dd>{String(health.grade || "unknown").replaceAll("_", " ")}</dd>
              <dt>Checked</dt><dd>{String(health.checked_at || "")}</dd>
            </dl>
            <details><summary>Technical details</summary><pre style={{fontSize: 12, overflow: "auto"}}>{JSON.stringify(health, null, 2)}</pre></details>
          </div>}

        </section>

        <section className="glass-card" style={{ padding: 20 }}>
          <h2 style={{ fontSize: 16, margin: "0 0 12px" }}>Debug code</h2>
          <textarea className="input" rows={8} value={src} onChange={(e) => setSrc(e.target.value)} style={{ fontFamily: "var(--font-mono)", fontSize: 13 }} />
          <button className="btn btn-secondary" style={{ marginTop: 10 }} disabled={busy === "d"} onClick={() => run("d", () => debugSource(src), setDebug)}>
            {busy === "d" ? "Analyzing…" : "Diagnose"}
          </button>
          {debug && (
            <pre style={{ marginTop: 12, fontSize: 12, fontFamily: "var(--font-mono)", overflow: "auto", maxHeight: 280 }}>
              {JSON.stringify(debug, null, 2)}
            </pre>
          )}
        </section>

        <section className="glass-card" style={{ padding: 20 }}>
          <h2 style={{ fontSize: 16, margin: "0 0 12px" }}>Generate a 3D model (OBJ)</h2>
          <p style={{ fontSize: 13, color: "var(--text-secondary)", margin: "0 0 10px" }}>
            A bounded modeling job: parameterized geometry → validated, downloadable asset.
          </p>
          <div style={{ display: "flex", gap: 8, flexWrap: "wrap", alignItems: "center" }}>
            <select className="input" value={modelKind} onChange={(e) => setModelKind(e.target.value)} style={{ width: "auto" }} aria-label="Model kind">
              <option value="box">Box</option>
              <option value="sphere">Sphere</option>
              <option value="cylinder">Cylinder</option>
              <option value="assembly">Assembly</option>
            </select>
            <input className="input" aria-label="Size (w, h, d)" value={modelSize} onChange={(e) => setModelSize(e.target.value)} style={{ width: 140 }} placeholder="width, height, depth" />
            <label style={{ fontSize: 13 }}>Segments
              <input className="input" type="number" min={3} max={128} value={modelSegments} onChange={(e) => setModelSegments(Number(e.target.value) || 16)} style={{ width: 80, marginLeft: 6 }} />
            </label>
            <button className="btn btn-primary" disabled={busy === "m"} onClick={() => run("m", async () => {
              const size = modelSize.split(",").map((v) => parseFloat(v.trim())).filter((v) => Number.isFinite(v));
              return generateModelAsset({
                kind: modelKind,
                size: size.length === 3 ? size : [1, 1, 1],
                segments: modelSegments,
                name: `${modelKind}-${Date.now().toString(36)}`,
              });
            }, setModelResult)}>
              {busy === "m" ? "Generating…" : "Generate model"}
            </button>
          </div>
          {modelResult && (
            <div style={{ marginTop: 14, fontSize: 13 }}>
              <strong>{modelResult.name}</strong>
              {modelResult.model && (
                <dl style={{ display: "grid", gridTemplateColumns: "120px 1fr", gap: 6, margin: "8px 0" }}>
                  <dt>Vertices</dt><dd>{modelResult.model.vertices}</dd>
                  <dt>Faces</dt><dd>{modelResult.model.faces}</dd>
                  <dt>SHA-256</dt><dd style={{ fontFamily: "var(--font-mono)", fontSize: 12 }}>{modelResult.model.sha256?.slice(0, 16)}…</dd>
                </dl>
              )}
              <ul style={{ paddingLeft: 18 }}>
                {modelResult.files.map((f) => (
                  <li key={f}>
                    <a href={artifactFileUrl(modelResult.artifact_id, f)} target="_blank" rel="noreferrer">{f}</a>
                  </li>
                ))}
              </ul>
            </div>
          )}
        </section>

        <section className="glass-card" style={{ padding: 20 }}>
          <h2 style={{ fontSize: 16, margin: "0 0 12px" }}>Generate a website or app</h2>
          <textarea className="input" rows={4} placeholder="Describe the product, pages, audience, and features." value={brief} onChange={(e) => setBrief(e.target.value)} />
          <div style={{ display: "flex", gap: 8, flexWrap: "wrap", marginTop: 10 }}>
            <select className="input" value={kind} onChange={(e) => setKind(e.target.value)} style={{ width: "auto" }}>
              <option value="website">Website</option>
              <option value="app">App</option>
            </select>
            <select className="input" value={scale} onChange={(e) => setScale(e.target.value)} style={{ width: "auto" }}>
              <option value="compact">Compact</option>
              <option value="standard">Standard (medium)</option>
              <option value="full">Full (large)</option>
            </select>
            <button className="btn btn-primary" disabled={!brief.trim() || busy === "g"} onClick={() => run("g", async () => {
              setProgress(null);
              const result = await generateProject(brief, kind, "", scale, setProgress);
              const files = result.files || [];
              const entry = files.find((f) => f === "index.html") || files.find((f) => f.endsWith(".html")) || files[0];
              setSelectedFile(entry || "");
              setPreviewHtml("");
              if (entry && (entry.endsWith(".html") || entry.endsWith(".md") || entry.endsWith(".txt") || entry.endsWith(".css") || entry.endsWith(".js"))) {
                try {
                  let html = await fetchArtifactText(result.artifact_id, entry);
                  if (entry.endsWith(".html")) {
                    const cssName = files.find((f) => f.endsWith(".css"));
                    if (cssName) {
                      try {
                        const css = await fetchArtifactText(result.artifact_id, cssName);
                        html = html.replace("</head>", `<style>${css}</style></head>`);
                        if (!html.includes("</head>")) html = `<style>${css}</style>` + html;
                      } catch { /* preview without css */ }
                    }
                  }
                  setPreviewHtml(html);
                } catch { /* list still works */ }
              }
              return result;
            }, setArtifact)}>
              {busy === "g" ? "Generating…" : "Generate"}
            </button>
          </div>
          {progress && <div role="status" style={{marginTop: 12, fontSize: 13}}>
            {progress.stage?.replaceAll("_", " ") || progress.status}
            {Boolean(progress.total_files) && ` · ${progress.completed_files || 0} / ${progress.total_files} files`}
          </div>}
          {artifact && (
            <div style={{ marginTop: 14 }}>
              <strong>{artifact.name}</strong>
              <p style={{ fontSize: 13, color: "var(--text-secondary)" }}>{artifact.summary}</p>
              <div style={{ display: "grid", gap: 16, gridTemplateColumns: "minmax(160px, 220px) 1fr" }} className="studio-preview-grid">
                <ul style={{ fontSize: 13, paddingLeft: 18, maxHeight: 360, overflow: "auto" }}>
                  {artifact.files.map((f) => (
                    <li key={f}>
                      <button
                        type="button"
                        className="btn btn-ghost btn-sm"
                        style={{ fontWeight: f === selectedFile ? 700 : 400 }}
                        onClick={async () => {
                          setSelectedFile(f);
                          try {
                            setPreviewHtml(await fetchArtifactText(artifact.artifact_id, f));
                          } catch {
                            setPreviewHtml("");
                          }
                        }}
                      >
                        {f}
                      </button>
                      {" "}
                      <a href={artifactFileUrl(artifact.artifact_id, f)} target="_blank" rel="noreferrer">open</a>
                    </li>
                  ))}
                </ul>
                <div>
                  {selectedFile.endsWith(".html") && previewHtml ? (
                    <iframe
                      title="Preview"
                      sandbox="allow-scripts" referrerPolicy="no-referrer"
                      srcDoc={previewHtml}
                      style={{ width: "100%", minHeight: 360, border: "1px solid var(--border-primary)", borderRadius: 12, background: "#fff" }}
                    />
                  ) : (
                    <pre style={{ fontSize: 12, fontFamily: "var(--font-mono)", overflow: "auto", maxHeight: 360, whiteSpace: "pre-wrap" }}>
                      {previewHtml.slice(0, 20000) || "Select a file to preview."}
                    </pre>
                  )}
                </div>
              </div>
            </div>
          )}
        </section>
      </div>
    </div>
  );
}
