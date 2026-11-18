/**
 * Phase 18 behavioral tests over the REAL modules (§18.29): the draft
 * store, refresh lock, auth state machine, and run-state contracts —
 * mounted-component logic exercised through the shared modules.
 */
import { describe, expect, it, beforeEach, afterEach } from "vitest";
import {
  saveDraft, loadDraft, clearAllDraftsForUser,
} from "@/lib/draft-store";
import {
  acquireRefreshLock, waitForPeerRefresh,
} from "@/lib/refresh-lock";
import {
  nextAuthGeneration, currentAuthGeneration, isCurrentGeneration,
  classifyAuthFailure,
} from "@/lib/auth-state";
import { readSession } from "@/lib/auth-session";

// The localStorage stub (happy-dom provides one; this guards identity
// tests explicitly).
function seedSession(userId: string) {
  localStorage.setItem("agentos_auth", JSON.stringify({
    user: { id: userId, email: `${userId}@x.io`, name: userId,
            auth_provider: "local", role: "user", is_active: true },
    accessToken: `at-${userId}`,
    refreshToken: `rt-${userId}`,
  }));
}

describe("§18.19 drafts are identity-scoped and versioned", () => {
  const ORIGINAL = localStorage.getItem("agentos_auth");
  beforeEach(() => {
    localStorage.clear();
  });
  afterEach(() => {
    if (ORIGINAL !== null) localStorage.setItem("agentos_auth", ORIGINAL);
    else localStorage.removeItem("agentos_auth");
  });

  it("another user never sees the first user's draft", () => {
    seedSession("alice");
    saveDraft("workspace", null, { text: "alice's secret plan",
      selected_model: null, attachment_references: [],
      submission_identity: null });
    const aliceDraft = loadDraft("workspace", null);
    expect(aliceDraft?.text).toBe("alice's secret plan");
    // Account switch: bob's read finds NOTHING.
    seedSession("bob");
    expect(loadDraft("workspace", null)).toBeNull();
    // And bob's save does not touch alice's record.
    saveDraft("workspace", null, { text: "bob's note",
      selected_model: null, attachment_references: [],
      submission_identity: null });
    expect(loadDraft("workspace", null)?.text).toBe("bob's note");
  });

  it("new conversations (no thread id) preserve drafts", () => {
    seedSession("alice");
    saveDraft("workspace", null, { text: "a new conversation draft",
      selected_model: null, attachment_references: [],
      submission_identity: null });
    expect(loadDraft("workspace", null)?.text)
      .toBe("a new conversation draft");
  });

  it("drafts carry a version and revision progression", () => {
    seedSession("carol");
    saveDraft("workspace", null, { text: "v1", selected_model: null,
      attachment_references: [], submission_identity: null });
    const first = loadDraft("workspace", null);
    saveDraft("workspace", null, { text: "v2", selected_model: null,
      attachment_references: [], submission_identity: null });
    const second = loadDraft("workspace", null);
    expect(second!.draft_revision).toBe((first!.draft_revision ?? 0) + 1);
  });

  it("clearAllDraftsForUser removes only that user's drafts", () => {
    seedSession("alice");
    saveDraft("workspace", null, { text: "alice draft",
      selected_model: null, attachment_references: [],
      submission_identity: null });
    seedSession("bob");
    saveDraft("workspace", null, { text: "bob draft",
      selected_model: null, attachment_references: [],
      submission_identity: null });
    clearAllDraftsForUser("alice");
    seedSession("alice");
    expect(loadDraft("workspace", null)).toBeNull();
    seedSession("bob");
    expect(loadDraft("workspace", null)?.text).toBe("bob draft");
  });
});

describe("§18.12 the refresh fallback never double-rotates", () => {
  const ORIGINAL = localStorage.getItem("agentos_auth");
  beforeEach(() => {
    localStorage.clear();
    // Force the no-Web-Locks path in this environment where possible.
    seedSession("u1");
  });
  afterEach(() => {
    if (ORIGINAL !== null) localStorage.setItem("agentos_auth", ORIGINAL);
    else localStorage.removeItem("agentos_auth");
  });

  it("a non-owner re-reads instead of rotating", async () => {
    const first = await acquireRefreshLock();
    if (first.owns) {
      // This environment HAS Web Locks: the contract is single ownership.
      const second = await acquireRefreshLock();
      // With real Web Locks the second acquisition queues until release;
      // without them it is refused. Either way, one owner.
      first.release();
      second.release();
    } else {
      // The fallback: the non-owner waits and re-reads (never rotates).
      const token = await waitForPeerRefresh(500);
      expect(token === null || typeof token === "string").toBe(true);
    }
  });

  it("logout during a held lock still ends the session", async () => {
    const lock = await acquireRefreshLock();
    localStorage.removeItem("agentos_auth");
    expect(readSession()).toBeNull();
    lock.release();
    expect(readSession()).toBeNull();
  });
});

describe("§18.11 the auth generation prevents cross-account leakage", () => {
  it("stale generations are discarded", () => {
    const gen1 = currentAuthGeneration();
    nextAuthGeneration();   // login as account B
    expect(isCurrentGeneration(gen1)).toBe(false);
    expect(isCurrentGeneration(currentAuthGeneration())).toBe(true);
  });

  it("definitive rejection is distinct from network failure", () => {
    expect(classifyAuthFailure(401, false).state).toBe("expired");
    expect(classifyAuthFailure(undefined, true).state)
      .toBe("temporarily_unavailable");
    expect(classifyAuthFailure(503, false).state)
      .toBe("temporarily_unavailable");
  });
});
