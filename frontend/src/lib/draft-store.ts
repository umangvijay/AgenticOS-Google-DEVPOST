"use client";

/**
 * Versioned, identity-scoped drafts (Phase 18 §18.19, finding F1).
 * One authoritative draft record — ChatComposer no longer initializes a
 * second state that shadows the stored draft. Namespaced by
 * authenticated identity: another user NEVER sees the draft; account
 * switching isolates. Restoration completes BEFORE an empty value can
 * overwrite storage.
 */
import { readSession } from "./auth-session";

/** The authenticated identity for draft namespacing — from the session
  record (no separate accessor existed). */
export function getAuthUserId(): string | null {
  return readSession()?.user?.id ?? null;
}

const DRAFT_VERSION = 2;
const KEY_PREFIX = "agentos.draft";
const MAX_DRAFT_BYTES = 64 * 1024;   // §18.19: no casual large blobs

export interface DraftRecord {
  version: number;
  user_id: string;
  organization_id: string | null;
  surface: string;                       // "workspace" | "resume" | ...
  thread_id: string | null;              // null = a NEW conversation
  draft_revision: number;
  text: string;
  selected_model: string | null;
  attachment_references: Array<{ id: string; name: string }>;
  submission_identity: string | null;    // the pending command key
  updated_at: number;
}

function storageKey(surface: string, threadId: string | null,
                    userId: string): string {
  // §18.19: drafts are namespaced by identity + surface + thread.
  return `${KEY_PREFIX}.v${DRAFT_VERSION}.${userId}.${surface}.${threadId ?? "new"}`;
}

function safeStorage(): Storage | null {
  try {
    return window.localStorage;
  } catch {
    return null;   // storage failure handled visibly by the caller
  }
}

/** Read the draft for THIS identity. Returns null when absent, owned by
  another user, or corrupted — never another user's text. */
export function loadDraft(surface: string, threadId: string | null): DraftRecord | null {
  const userId = getAuthUserId() || "";
  const store = safeStorage();
  if (!store || !userId) return null;
  try {
    const raw = store.getItem(storageKey(surface, threadId, userId));
    if (!raw) return null;
    const parsed = JSON.parse(raw) as DraftRecord;
    if (parsed.version !== DRAFT_VERSION) return null;
    if (parsed.user_id !== userId) return null;   // identity isolation
    return parsed;
  } catch {
    return null;
  }
}

/** Persist the draft. Fails visibly (returns false) on storage errors
  or oversized drafts; secrets/large attachments are not persisted. */
export function saveDraft(surface: string, threadId: string | null,
                          draft: Omit<DraftRecord, "version" | "user_id"
                            | "organization_id" | "surface" | "thread_id"
                            | "draft_revision" | "updated_at">,
                          ): { saved: boolean; error?: string } {
  const userId = getAuthUserId() || "";
  const store = safeStorage();
  if (!store || !userId) {
    return { saved: false, error: "storage unavailable" };
  }
  const serialized = JSON.stringify(draft);
  if (serialized.length > MAX_DRAFT_BYTES) {
    return { saved: false, error: "draft too large to persist" };
  }
  const previous = loadDraft(surface, threadId);
  const record: DraftRecord = {
    ...draft,
    version: DRAFT_VERSION,
    user_id: userId,
    organization_id: null,
    surface,
    thread_id: threadId,
    draft_revision: (previous?.draft_revision ?? 0) + 1,
    updated_at: Date.now(),
  };
  try {
    store.setItem(storageKey(surface, threadId, userId),
                  JSON.stringify(record));
    return { saved: true };
  } catch (err) {
    return { saved: false, error: err instanceof Error ? err.message
      : "storage write failed" };
  }
}

/** Clear the draft (explicit logout/sent behavior). Only clears THIS
  identity's draft. */
export function clearDraft(surface: string, threadId: string | null): void {
  const userId = getAuthUserId() || "";
  const store = safeStorage();
  if (!store || !userId) return;
  try {
    store.removeItem(storageKey(surface, threadId, userId));
  } catch {
    /* best effort */
  }
}

/** §18.19: explicit logout clears every draft for the signing-out user. */
export function clearAllDraftsForUser(userId: string): void {
  const store = safeStorage();
  if (!store) return;
  try {
    const prefix = `${KEY_PREFIX}.v${DRAFT_VERSION}.${userId}.`;
    const doomed: string[] = [];
    for (let i = 0; i < store.length; i++) {
      const key = store.key(i);
      if (key && key.startsWith(prefix)) doomed.push(key);
    }
    for (const key of doomed) store.removeItem(key);
  } catch {
    /* best effort */
  }
}
