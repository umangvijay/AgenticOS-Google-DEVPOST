"use client";

import { useCallback, useState } from "react";
import {
  clearDraft as clearStoredDraft, loadDraft, saveDraft, getAuthUserId,
} from "@/lib/draft-store";

/**
 * Draft preservation (Phase 18 §18.19, finding F1) — ONE authoritative
 * draft value backed by the versioned, identity-scoped draft store.
 *
 * Restoration is a STATE: `restored` flips true once the stored draft
 * has been read. ChatComposer must not render an empty initial value as
 * authoritative before restoration completes, and must adopt the stored
 * text when it arrives (the F1 fix: goal RESTORES from the draft).
 * Supports new conversations (threadId null) — the store namespaces by
 * "new" until a thread exists.
 */
export function useDraft(threadId: string | null | undefined,
                         surface = "workspace",
                         submissionIdentity: string | null = null) {
  const key = threadId ?? null;
  const [draft, setDraftState] = useState("");
  const [restored, setRestored] = useState(false);
  const [storageError, setStorageError] = useState<string | null>(null);
  // §19 (C9): restoration RESETS when the authenticated identity
  // changes — a stale identity's draft is never adopted or overwritten.
  const [, setIdentity] = useState(getAuthUserId());

  // Restore once per key AND identity — BEFORE any empty value can
  // overwrite storage. react-hooks v6 (set-state-in-effect): identity
  // changes and (key, surface) changes adjust DURING RENDER — the
  // official adjust-on-prop-change pattern — so no setState runs
  // synchronously inside an effect.
  const [restoreStamp, setRestoreStamp] = useState(() => ({
    identity: getAuthUserId(), key, surface,
  }));
  if (restoreStamp.identity !== getAuthUserId() ||
      restoreStamp.key !== key || restoreStamp.surface !== surface) {
    const identityChanged = restoreStamp.identity !== getAuthUserId();
    setRestoreStamp({ identity: getAuthUserId(), key, surface });
    if (identityChanged) {
      setIdentity(getAuthUserId());
      setRestored(false);
      setDraftState("");
    } else {
      const stored = loadDraft(surface, key);
      setDraftState(stored?.text ?? "");
      setRestored(true);
    }
  }

  const setDraft = useCallback((value: string) => {
    setDraftState(value);
    if (!restored) return;   // never persist before restoration
    const out = saveDraft(surface, key, {
      text: value,
      selected_model: null,
      attachment_references: [],
      // §19 (C9): the PENDING submission identity persists with the
      // draft — refresh after an accepted-but-unacknowledged submission
      // can recover the same intent.
      submission_identity: submissionIdentity,
    });
    setStorageError(out.saved ? null : (out.error ?? "storage failed"));
  }, [key, surface, restored, submissionIdentity]);

  const clearDraft = useCallback(() => {
    setDraftState("");
    clearStoredDraft(surface, key);
  }, [key, surface]);

  return { draft, setDraft, clearDraft, restored, storageError };
}
