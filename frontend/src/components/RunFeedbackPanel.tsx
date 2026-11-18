"use client";

import { FormEvent, useCallback, useEffect, useState } from "react";
import { apiFetch } from "@/lib/api";
import { useAuth } from "@/lib/auth-context";
import Link from "next/link";

interface Feedback {
  id: string;
  category: string;
  triage_status: string;
  created_at: string;
}

export default function RunFeedbackPanel({ runId }: { runId: string }) {
  const { user } = useAuth();
  return user ? <>
    {["admin", "super_admin"].includes(user.role) && <Link href="/dashboard/feedback">Open feedback triage</Link>}
    <FeedbackForm key={`${user.id}:${runId}`} runId={runId} />
  </> : null;
}

function FeedbackForm({ runId }: { runId: string }) {
  const [items, setItems] = useState<Feedback[]>([]);
  const [helpful, setHelpful] = useState("no");
  const [category, setCategory] = useState("incorrect_result");
  const [comment, setComment] = useState("");
  const [expected, setExpected] = useState("");
  const [diagnostics, setDiagnostics] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");
  const fetchHistory = useCallback(async (signal?: AbortSignal) => {
    const result = await apiFetch<{ feedback: Feedback[] }>(
      `/feedback/mine?run_id=${encodeURIComponent(runId)}`, { signal });
    if (!Array.isArray(result?.feedback) || !result.feedback.every(item =>
      typeof item?.id === "string" && typeof item.category === "string" && typeof item.triage_status === "string"
    )) throw new Error("Feedback history is unavailable.");
    return result.feedback;
  }, [runId]);
  useEffect(() => {
    const controller = new AbortController();
    void fetchHistory(controller.signal).then(history => {
      if (!controller.signal.aborted) setItems(history);
    }).catch((err: unknown) => {
      if (!controller.signal.aborted) setError(err instanceof Error ? err.message : "Feedback is unavailable");
    });
    return () => controller.abort();
  }, [fetchHistory]);

  async function refreshHistory() {
    setItems(await fetchHistory());
    setError("");
  }

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (busy) return;
    setBusy(true); setError(""); setMessage("");
    try {
      await apiFetch("/feedback", { method: "POST", body: JSON.stringify({
        run_id: runId, helpful: helpful === "yes", category, comment,
        expected_result: expected, share_diagnostics: diagnostics,
      }) });
      setComment(""); setExpected("");
      setMessage("Feedback received. You can follow its status below.");
      await refreshHistory();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not submit feedback");
    } finally { setBusy(false); }
  }

  async function remove(id: string) {
    setBusy(true); setError("");
    try {
      await apiFetch(`/feedback/${encodeURIComponent(id)}`, { method: "DELETE" });
      setItems(current => current.filter(item => item.id !== id));
      setMessage("Feedback deleted.");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not delete feedback");
    } finally { setBusy(false); }
  }

  return <details className="glass-card" style={{ padding: 16, marginBottom: 12 }}>
    <summary style={{ cursor: "pointer", fontWeight: 600 }}>Feedback on this run</summary>
    <form onSubmit={submit} style={{ display: "grid", gap: 12, marginTop: 12 }}>
      <label>Was this result helpful? <select value={helpful} onChange={e => setHelpful(e.target.value)}>
        <option value="no">No</option><option value="yes">Yes</option>
      </select></label>
      <label>Category <select value={category} onChange={e => setCategory(e.target.value)}>
        <option value="incorrect_result">Incorrect result</option>
        <option value="failed_action">Failed action</option>
        <option value="slow_run">Slow run</option>
        <option value="missing_app">Missing application</option>
        <option value="confusing_ui">Confusing interface</option>
        <option value="other">Other</option>
      </select></label>
      <label>What happened?<textarea value={comment} maxLength={4000}
        onChange={e => setComment(e.target.value)} style={{ display: "block", width: "100%" }} /></label>
      <label>What did you expect?<textarea value={expected} maxLength={2000}
        onChange={e => setExpected(e.target.value)} style={{ display: "block", width: "100%" }} /></label>
      <label><input type="checkbox" checked={diagnostics} onChange={e => setDiagnostics(e.target.checked)} />
        {" "}Share run and task references with the team investigating this report</label>
      <p style={{ fontSize: 12 }}>Do not include passwords or API keys. Feedback does not automatically train a model.</p>
      <button type="submit" className="btn btn-primary" disabled={busy}>{busy ? "Saving…" : "Send feedback"}</button>
    </form>
    {error && <p role="alert">{error}</p>}
    {message && <p role="status">{message}</p>}
    <button className="btn btn-ghost btn-sm" disabled={busy} onClick={() => void refreshHistory().catch(
      err => setError(err instanceof Error ? err.message : "Could not refresh feedback"))}>Refresh status</button>
    <ul>{items.map(item => <li key={item.id}>
      {item.category.replaceAll("_", " ")} — {item.triage_status}
      {" "}<button className="btn btn-ghost btn-sm" disabled={busy} aria-label={`Delete feedback ${item.id}`}
        onClick={() => void remove(item.id)}>Delete</button>
    </li>)}</ul>
  </details>;
}
