"use client";

import { FormEvent, useEffect, useState } from "react";
import Link from "next/link";
import { apiFetch } from "@/lib/api";
import { useAuth } from "@/lib/auth-context";

interface Report {
  id: string; category: string; comment: string; expected_result: string;
  triage_status: string; triage_note: string | null; run_id: string | null;
}

export default function FeedbackPage() {
  const { user, isLoading } = useAuth();
  if (isLoading) return <p role="status">Loading account…</p>;
  if (!user || !["admin", "super_admin"].includes(user.role)) {
    return <p>Feedback triage is available to administrators. You can submit and track feedback from each workspace run.</p>;
  }
  return <TriageQueue key={user.id} />;
}

function TriageQueue() {
  const [reports, setReports] = useState<Report[]>([]);
  const [error, setError] = useState("");
  const [revision, setRevision] = useState(0);
  useEffect(() => {
    const abort = new AbortController();
    void apiFetch<{ feedback: Report[] }>("/feedback", { signal: abort.signal })
      .then(result => { if (!abort.signal.aborted) { setReports(result.feedback); setError(""); } })
      .catch(err => { if (!abort.signal.aborted) setError(err instanceof Error ? err.message : "Could not load feedback"); });
    return () => abort.abort();
  }, [revision]);
  return <main style={{ padding: 24, maxWidth: 960, margin: "0 auto" }}>
    <h1>Feedback triage</h1>
    <p>Investigate user reports and record the verified outcome. Run references appear only when the user shares diagnostics.</p>
    <button className="btn btn-secondary" onClick={() => setRevision(value => value + 1)}>Refresh reports</button>
    {error && <p role="alert">{error}</p>}
    {!error && reports.length === 0 && <p>No reports loaded.</p>}
    {reports.map(report => <ReportCard key={`${report.id}:${report.triage_status}`} report={report}
      updated={() => setRevision(value => value + 1)} />)}
  </main>;
}

function ReportCard({ report, updated }: { report: Report; updated: () => void }) {
  const [status, setStatus] = useState(report.triage_status);
  const [note, setNote] = useState(report.triage_note || "");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  async function save(event: FormEvent) {
    event.preventDefault();
    if (busy) return;
    setBusy(true); setError("");
    try {
      await apiFetch(`/feedback/${encodeURIComponent(report.id)}/triage`, {
        method: "POST", body: JSON.stringify({ status, note }),
      });
      updated();
    } catch (err) { setError(err instanceof Error ? err.message : "Could not save triage"); }
    finally { setBusy(false); }
  }
  return <article className="glass-card" style={{ padding: 16, marginTop: 16 }}>
    <h2>{report.category.replaceAll("_", " ")}</h2>
    <p style={{ whiteSpace: "pre-wrap" }}>{report.comment}</p>
    <p style={{ whiteSpace: "pre-wrap" }}>Expected: {report.expected_result || "Not supplied"}</p>
    {report.run_id && <Link href={`/dashboard/workspace/${encodeURIComponent(report.run_id)}`}>Open shared run</Link>}
    <form onSubmit={save} style={{ display: "grid", gap: 10, marginTop: 12 }}>
      <label>Status <select value={status} onChange={event => setStatus(event.target.value)}>
        {["received", "investigating", "planned", "fixed", "closed"].map(value => <option key={value}>{value}</option>)}
      </select></label>
      <label>Investigation and verification notes<textarea value={note} maxLength={2000}
        onChange={event => setNote(event.target.value)} style={{ display: "block", width: "100%" }} /></label>
      <button className="btn btn-primary" disabled={busy}>{busy ? "Saving…" : "Save triage"}</button>
    </form>
    {error && <p role="alert">{error}</p>}
  </article>;
}
