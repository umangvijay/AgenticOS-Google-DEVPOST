"use client";

import { useCallback, useState, useSyncExternalStore } from "react";
import {
  clearDraft as clearStoredDraft, loadDraft, saveDraft, getAuthUserId,
} from "@/lib/draft-store";
import { newSubmissionKey } from "@/lib/api";
import type { DraftRecord } from "@/lib/draft-store";
import { AUTH_CHANGED } from "@/lib/auth-session";

function subscribeIdentity(onChange: () => void) {
  window.addEventListener(AUTH_CHANGED, onChange);
  window.addEventListener("storage", onChange);
  return () => {
    window.removeEventListener(AUTH_CHANGED, onChange);
    window.removeEventListener("storage", onChange);
  };
}
const serverIdentity = () => null;

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
                         surface = "workspace") {
  const key = threadId ?? null;
  const [draft, setDraftState] = useState("");
  const [restored, setRestored] = useState(false);
  const [storageError, setStorageError] = useState<string | null>(null);
  const identity = useSyncExternalStore(subscribeIdentity, getAuthUserId, serverIdentity);
  const [submissionIdentity, setSubmissionIdentity] = useState(newSubmissionKey);
  const [attachments, setAttachments] = useState<DraftRecord["attachment_references"]>([]);
  const [restoreStamp, setRestoreStamp] = useState<{ identity: string | null; key: string | null; surface: string } | null>(null);
  // Restore on the first mount as well as on thread/account changes. The
  // external store supplies the server snapshot during hydration.
  if (!restoreStamp || restoreStamp.identity !== identity ||
      restoreStamp.key !== key || restoreStamp.surface !== surface) {
    setRestoreStamp({ identity, key, surface });
    const stored = identity ? loadDraft(surface, key) : null;
    setDraftState(stored?.text ?? "");
    setSubmissionIdentity(stored?.submission_identity || newSubmissionKey());
    setAttachments(stored?.attachment_references ?? []);
    setRestored(true);
    setStorageError(null);
  }

  const setDraft = useCallback((value: string, references = attachments) => {
    if (getAuthUserId() !== identity) return;
    setAttachments(references);
    setDraftState(value);
    if (!restored) return;   // never persist before restoration
    const out = saveDraft(surface, key, {
      text: value,
      selected_model: null,
      attachment_references: references,
      // §19 (C9): the PENDING submission identity persists with the
      // draft — refresh after an accepted-but-unacknowledged submission
      // can recover the same intent.
      submission_identity: submissionIdentity,
    });
    setStorageError(out.saved ? null : (out.error ?? "storage failed"));
  }, [key, surface, restored, submissionIdentity, attachments, identity]);

  const clearDraft = useCallback(() => {
    if (getAuthUserId() !== identity) return;
    setDraftState("");
    setAttachments([]);
    setSubmissionIdentity(newSubmissionKey());
    clearStoredDraft(surface, key);
  }, [key, surface, identity]);

  return { draft, setDraft, clearDraft, restored, storageError, submissionIdentity, attachments };
}
