/**
 * Phase 16 §16.28: the memory UI state contract — "deleted" never
 * means a card disappeared locally, and refreshing cannot restore an
 * older version over a correction (states are server-derived).
 */
import { describe, expect, it } from "vitest";
import { memoryStateStyle, memoryUiState } from "@/lib/task-states";

describe("§16.28 memory lifecycle states are explicit", () => {
  it("every lifecycle state renders a distinct, honest label", () => {
    const states = ["saved", "indexing", "retrievable", "superseded",
      "deletion_requested", "hidden", "cleanup_complete", "cleanup_failed"];
    const labels = states.map((s) => memoryStateStyle(s).label);
    expect(new Set(labels).size, "no two states share a label")
      .toBe(states.length);
    expect(memoryStateStyle("hidden").label)
      .toBe("Hidden from retrieval");
    expect(memoryStateStyle("cleanup_failed").badge).toBe("badge-error");
  });

  it("the UI state derives from the SERVER record, not local optimism", () => {
    // A deletion REQUESTED record shows the barrier state — the card
    // does not silently vanish pretending cleanup finished.
    expect(memoryUiState({ deletion_state: "hidden" }).label)
      .toBe("Hidden from retrieval");
    expect(memoryUiState({ deletion_state: "deletion_requested" }).label)
      .toBe("Deletion requested");
    // A superseded record stays visible as historical — a refresh
    // cannot resurrect it over the correction.
    expect(memoryUiState({ superseded_by: "m-2" }).label)
      .toBe("Superseded");
    // Confirmed + active → available for retrieval.
    expect(memoryUiState({ verification_status: "confirmed" }).label)
      .toBe("Available for retrieval");
    // Unconfirmed → merely saved (never rendered as retrievable).
    expect(memoryUiState({ verification_status: "inferred" }).label)
      .toBe("Saved");
    // Unknown states degrade honestly.
    expect(memoryStateStyle("mystery").label).toBe("mystery");
  });
});
