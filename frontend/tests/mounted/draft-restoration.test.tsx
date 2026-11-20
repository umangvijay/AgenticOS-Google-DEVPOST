import { afterEach, beforeEach, expect, it } from "vitest";
import { act, cleanup, renderHook } from "@testing-library/react";
import { useDraft } from "@/hooks/useDraft";
import { loadDraft, saveDraft } from "@/lib/draft-store";
import { writeSession, type Session } from "@/lib/auth-session";

function login(id: string) {
  writeSession({ accessToken: "test", refreshToken: "test", user: { id } } as Session);
}
beforeEach(() => { localStorage.clear(); login("alice"); });
afterEach(cleanup);
it("restores on the first mount, persists edits, and clears accepted drafts", () => {
  saveDraft("workspace", "thread", { text: "Unsent message", selected_model: null, attachment_references: [], submission_identity: "same-intent" });
  const { result } = renderHook(() => useDraft("thread", "workspace"));
  expect(result.current.restored).toBe(true);
  expect(result.current.draft).toBe("Unsent message");
  expect(result.current.submissionIdentity).toBe("same-intent");
  act(() => result.current.setDraft("Edited message"));
  expect(loadDraft("workspace", "thread")?.text).toBe("Edited message");
  act(() => result.current.clearDraft());
  expect(loadDraft("workspace", "thread")).toBeNull();
  expect(result.current.submissionIdentity).not.toBe("same-intent");
});
it("isolates drafts across accounts and threads without overwriting earlier text", () => {
  const { result, rerender } = renderHook(({ thread }) => useDraft(thread), { initialProps: { thread: "first" } });
  act(() => result.current.setDraft("Alice first"));
  act(() => login("bob"));
  expect(result.current.draft).toBe("");
  act(() => result.current.setDraft("Bob first"));
  rerender({ thread: "second" });
  expect(result.current.draft).toBe("");
  act(() => login("alice"));
  rerender({ thread: "first" });
  expect(result.current.draft).toBe("Alice first");
});

it("restores attachment-only drafts with the original key across remounts", () => {
  const refs = [{ id: "file-1", name: "notes.txt" }];
  const first = renderHook(() => useDraft("thread"));
  const key = first.result.current.submissionIdentity;
  expect(key).toBeTruthy();
  act(() => first.result.current.setDraft("", refs));
  first.unmount();
  const second = renderHook(() => useDraft("thread"));
  expect(second.result.current.attachments).toEqual(refs);
  expect(second.result.current.submissionIdentity).toBe(key);
  act(() => second.result.current.setDraft("Read this"));
  expect(loadDraft("workspace", "thread")).toMatchObject({
    text: "Read this", attachment_references: refs, submission_identity: key,
  });
});
