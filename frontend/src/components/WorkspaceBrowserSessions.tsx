"use client";

import { useEffect, useState } from "react";
import { apiFetch } from "@/lib/api";
import { useAuth } from "@/lib/auth-context";
import { BrowserSessionPanel } from "@/components/BrowserSessionPanel";

interface Session { session_id: string; run_id: string; task_id: string }

export default function WorkspaceBrowserSessions({ runId }: { runId: string }) {
  const { user } = useAuth();
  return user ? <Sessions key={`${user.id}:${runId}`} runId={runId} /> : null;
}

function Sessions({ runId }: { runId: string }) {
  const [sessions, setSessions] = useState<Session[]>([]);
  const [error, setError] = useState("");
  useEffect(() => {
    const abort = new AbortController();
    let pending = false;
    const load = async () => {
      if (pending) return;
      pending = true;
      try {
        const result = await apiFetch<{ sessions: Session[] }>("/browser/sessions", { signal: abort.signal });
        if (!abort.signal.aborted) {
          setSessions(result.sessions.filter(s => s.run_id === runId));
          setError("");
        }
      } catch (err) {
        if (!abort.signal.aborted) {
          setSessions([]);
          setError(err instanceof Error ? err.message : "Browser sessions are unavailable");
        }
      } finally { pending = false; }
    };
    void load();
    const timer = setInterval(() => void load(), 5000);
    return () => { abort.abort(); clearInterval(timer); };
  }, [runId]);
  return <div>
    {error && <p role="status">Browser connection: {error}</p>}
    {sessions.map(session => <BrowserSessionPanel key={session.session_id} sessionId={session.session_id} />)}
  </div>;
}
