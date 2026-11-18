"use client";

/**
 * ThreadRail — Z.ai/Claude-style conversation minimap.
 * A slim vertical strip (left of the chat) with one pill per exchange.
 * Each pill shows the user's prompt; hovering reveals the agent's reply
 * snippet; clicking smooth-scrolls to that message in the thread.
 * Appears automatically once the thread has 3+ turns.
 */

import { useMemo } from "react";

export interface RailTurn {
  run_id: string;
  goal: string;
  reply: string;
}

export default function ThreadRail({
  turns, activeRunId, onJump,
}: {
  turns: RailTurn[];
  activeRunId?: string;
  onJump: (runId: string) => void;
}) {
  const items = useMemo(
    () => turns.map((t) => ({
      run_id: t.run_id,
      prompt: (t.goal || "").replace(/\s+/g, " ").trim(),
      reply: (t.reply || "").replace(/\s+/g, " ").trim(),
    })),
    [turns],
  );
  if (items.length < 3) return null;

  return (
    <nav className="thread-rail" aria-label="Conversation outline">
      {items.map((it) => (
        <button
          key={it.run_id}
          type="button"
          className={`rail-pill ${it.run_id === activeRunId ? "rail-pill-active" : ""}`}
          onClick={() => onJump(it.run_id)}
          title={it.prompt.slice(0, 120)}
        >
          <span className="rail-prompt">{it.prompt.slice(0, 60) || "(no text)"}</span>
          {it.reply && <span className="rail-reply">{it.reply.slice(0, 110)}</span>}
        </button>
      ))}
    </nav>
  );
}
