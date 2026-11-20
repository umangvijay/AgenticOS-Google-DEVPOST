"use client";

import { FormEvent, useEffect, useRef, useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { submitGoal, getSettings, GoalAttachment, ApiError, BudgetErrorBody, AdmissionErrorBody, admissionBodyFrom, newSubmissionKey } from "@/lib/api";
import { useDraft } from "@/hooks/useDraft";
import BudgetBlockedNotice from "@/components/BudgetBlockedNotice";
import AdmissionNotice from "@/components/AdmissionNotice";
import ModelSelector from "@/components/ModelSelector";
import type { ModelCatalog } from "@/lib/api";

const SUGGESTIONS = [
  { label: "Connect an API", goal: "Create MCP tools from https://raw.githubusercontent.com/PokeAPI/pokeapi/master/openapi.yml then list pokemon" },
  { label: "Explore GitHub", goal: "Build MCP tools for GitHub so I can list public events, then list them" },
  { label: "Summarize a website", goal: "Create MCP tools for https://example.com then open home and summarize the page" },
];

const TEXT_TYPES = [
  "text/",
  "application/json",
  "application/xml",
  "application/javascript",
  "application/x-yaml",
  "application/yaml",
];
const TEXT_EXTS = [".txt", ".md", ".json", ".csv", ".py", ".js", ".ts", ".tsx", ".jsx", ".html", ".css", ".yml", ".yaml", ".xml", ".log", ".env", ".sql"];
const MAX_ATTACH = 5;
const MAX_BYTES = 4 * 1024 * 1024;
const MAX_TEXT = 12000;

type Attached = GoalAttachment & { id: string; kind: "file" | "photo" };

function isTextFile(file: File) {
  if (TEXT_TYPES.some((t) => file.type.startsWith(t) || file.type === t)) return true;
  const name = file.name.toLowerCase();
  return TEXT_EXTS.some((ext) => name.endsWith(ext));
}

async function fileToAttachment(file: File, kind: "file" | "photo"): Promise<Attached> {
  if (file.size > MAX_BYTES) {
    throw new Error(`${file.name} is larger than 4 MB.`);
  }
  const base = { id: `${file.name}-${file.size}-${Date.now()}`, name: file.name, mime: file.type || "application/octet-stream", kind };
  if (kind === "photo" || file.type.startsWith("image/")) {
    const image_base64 = await new Promise<string>((resolve, reject) => {
      const reader = new FileReader();
      reader.onload = () => {
        const result = String(reader.result || "");
        const comma = result.indexOf(",");
        resolve(comma >= 0 ? result.slice(comma + 1) : result);
      };
      reader.onerror = () => reject(reader.error || new Error("Could not read photo"));
      reader.readAsDataURL(file);
    });
    return { ...base, kind: "photo", mime: file.type || "image/jpeg", image_base64 };
  }
  if (isTextFile(file)) {
    const text = (await file.text()).slice(0, MAX_TEXT);
    return { ...base, text };
  }
  return base;
}

export default function ChatComposer({
  compact = false,
  disabled = false,
  threadId,
  parentRunId,
  onRunCreated,
  onSubmitting,
}: {
  compact?: boolean;
  disabled?: boolean;
  threadId?: string;
  parentRunId?: string;
  onRunCreated?: (res: { run_id: string; thread_id?: string; workflow_id: string; status: string; goal: string }) => void;
  onSubmitting?: (message: string | null) => void;
}) {
  const router = useRouter();
  const [models, setModels] = useState<string[]>([]);
  const [catalog, setCatalog] = useState<ModelCatalog>();
  const [model, setModel] = useState("");
  useEffect(() => {
    let active = true;
    getSettings().then(data => {
      if (active) {
        setModels(data.available_models || []);
        setCatalog(data.model_catalog);
        setModel(data.settings.default_model || "");
      }
    }).catch(() => {});
    return () => { active = false; };
  }, []);
  // §19 (C9): the submission identity is declared BEFORE the draft so
  // the hook persists it with the draft record.
  const [submissionKey, setSubmissionKey] = useState(() => newSubmissionKey());
  // §18.3: the draft survives refresh/auth expiry (sessionStorage keyed
  // per thread when one exists).
  const { draft, setDraft, clearDraft, restoredSubmissionIdentity, storageError } = useDraft(
    threadId ?? null, "workspace", submissionKey);
  const goal = draft;
  const setGoal = setDraft;
  const effectiveSubmissionKey = restoredSubmissionIdentity || submissionKey;
  const [ambiguousAcceptance, setAmbiguousAcceptance] = useState<{
    key: string; message: string;
  } | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [budgetError, setBudgetError] = useState<BudgetErrorBody | null>(null);
  const [admissionError, setAdmissionError] = useState<AdmissionErrorBody | null>(null);
  const [menuOpen, setMenuOpen] = useState(false);
  const [attachments, setAttachments] = useState<Attached[]>([]);
  const menuRef = useRef<HTMLDivElement>(null);
  const photoRef = useRef<HTMLInputElement>(null);
  const fileRef = useRef<HTMLInputElement>(null);
  const inputRef = useRef<HTMLTextAreaElement>(null);

  useEffect(() => {
    const input = inputRef.current;
    if (!input) return;
    input.style.height = "auto";
    input.style.height = `${Math.min(180, Math.max(compact ? 44 : 68, input.scrollHeight))}px`;
  }, [goal, compact]);

  useEffect(() => {
    const onDoc = (e: MouseEvent) => {
      if (menuRef.current && !menuRef.current.contains(e.target as Node)) setMenuOpen(false);
    };
    document.addEventListener("mousedown", onDoc);
    return () => document.removeEventListener("mousedown", onDoc);
  }, []);

  async function addFiles(list: FileList | null, kind: "file" | "photo") {
    if (!list?.length) return;
    setError("");
    const next = [...attachments];
    for (const file of Array.from(list)) {
      if (next.length >= MAX_ATTACH) {
        setError(`You can attach up to ${MAX_ATTACH} items.`);
        break;
      }
      try {
        next.push(await fileToAttachment(file, kind));
      } catch (err: unknown) {
        setError(err instanceof Error ? err.message : "Could not read that file");
      }
    }
    setAttachments(next);
    setMenuOpen(false);
    if (photoRef.current) photoRef.current.value = "";
    if (fileRef.current) fileRef.current.value = "";
  }

  async function send(text?: string) {
    const value = (text ?? goal).trim();
    if (loading || disabled) return;
    if (!value && attachments.length === 0) {
      setError("Type a message or attach a file.");
      return;
    }
    if (value.length > 50000) {
      setError("Message is too long (max 50,000 characters).");
      return;
    }
    setError("");
    setBudgetError(null);
    setLoading(true);
    const prompt = value || "Use the attached files and photos to complete the request.";
    onSubmitting?.(prompt);
    try {
      const payload = attachments.map(({ name, mime, text: body, image_base64 }) => ({
        name, mime, text: body, image_base64,
      }));
      // §19 (C9): persist the submission identity + model + attachment
      // references BEFORE dispatch — a refresh right after an accepted-
      // but-unacknowledged submission recovers the SAME intent.
      setDraft(prompt);   // persists submission_identity via the hook contract
      const res = await submitGoal(prompt, payload, {
        model: model || catalog?.default_model || undefined,
        parent_run_id: parentRunId,
        thread_id: threadId,
        idempotency_key: effectiveSubmissionKey,
      });
      setGoal("");
      clearDraft();   // Accepted messages no longer need a saved draft.
      setAttachments([]);
      setBudgetError(null);
      setAdmissionError(null);
      setError("");
      setSubmissionKey(newSubmissionKey());   // success: next submission is a NEW intent
      if (onRunCreated) {
        onRunCreated({ ...res, goal: prompt });
        setLoading(false);
      } else {
        router.push(`/dashboard/workspace/${res.run_id}`);
      }
    } catch (err: unknown) {
      onSubmitting?.(null);
      // §3.19: admission refusals (rate/capacity/queue/provider/outage) get
      // the precise overload state — the draft is PRESERVED so a retry
      // resubmits the same intent; budget refusals get their own notice.
      const admission = admissionBodyFrom(err);
      if (admission) {
        setAdmissionError(admission);
      } else if (err instanceof ApiError && err.body) {
        setBudgetError(err.body);
      } else {
        const message = err instanceof Error ? err.message : "Failed to start the run";
        // §18.10: an AMBIGUOUS transport failure (timeout) leaves
        // acceptance UNKNOWN — the SAME submission key is kept so a
        // retry resolves the same logical command (one external
        // effect), and the user is told the acceptance state, not just
        // "retry".
        const ambiguous = /timed out|timeout/i.test(message);
        if (ambiguous) {
          setAmbiguousAcceptance({
            key: effectiveSubmissionKey,
            message: ("Your submission may have been accepted — its "
                      + "status is unknown because the response timed "
                      + "out. Retrying reuses the same request identity, "
                      + "so it cannot create a duplicate run."),
          });
        } else {
          setError(message);
        }
      }
      setLoading(false);
    }
  }

  async function handleSubmit(e: FormEvent) {
    e.preventDefault();
    await send();
  }

  return (
    <form onSubmit={handleSubmit} className="chat-composer">
      {loading && !onSubmitting && <p role="status" className="composer-hint">Sending your message…</p>}
      {ambiguousAcceptance && (
        <div role="alert" style={{ padding: 10, marginBottom: 10, borderRadius: 8, background: "var(--bg-tertiary)", fontSize: 13 }}>
          <strong>Acceptance unknown.</strong> {ambiguousAcceptance.message}{" "}
          <button type="button" className="btn btn-ghost btn-sm"
                  onClick={() => setAmbiguousAcceptance(null)}>
            Dismiss
          </button>
        </div>
      )}
      {(models.length > 0 || catalog) && <ModelSelector label="Model for this message"
        value={model} onChange={setModel} models={models} catalog={catalog} allowDefault compact />}

      {budgetError && (
        <div style={{ marginBottom: 12 }}>
          <BudgetBlockedNotice error={budgetError} onRetry={() => { setBudgetError(null); void send(); }} />
        </div>
      )}
      {admissionError && (
        <div style={{ marginBottom: 12 }}>
          <AdmissionNotice
            error={admissionError}
            onRetry={() => { setAdmissionError(null); void send(); }}
            // retry reuses the SAME submission key (§4.4)
          />
        </div>
      )}
      {error && !budgetError && !admissionError && (
        <div style={{
          padding: "10px 14px", marginBottom: 12, background: "var(--error-subtle)",
          borderRadius: 12, color: "var(--error)", fontSize: 13, lineHeight: 1.45,
        }}>
          {error}
        </div>
      )}
      {storageError && <p role="status" className="composer-hint">Your draft could not be saved on this device. Keep this tab open until you send it.</p>}
      {attachments.length > 0 && (
        <div className="attach-chip-row">
          {attachments.map((a) => (
            <span key={a.id} className="attach-chip">
              {a.kind === "photo" ? "Photo" : "File"} · {a.name}
              <button type="button" aria-label={`Remove ${a.name}`} onClick={() => setAttachments((prev) => prev.filter((x) => x.id !== a.id))}>
                ×
              </button>
            </span>
          ))}
        </div>
      )}
      <div className="glass-panel chat-composer-box composer-pill">
        <div className="composer-row">
          <div className="composer-plus-wrap" ref={menuRef}>
            <button
              type="button"
              className="composer-plus"
              title="Add photos, files, or an MCP"
              aria-label="Add photos, files, or an MCP"
              aria-expanded={menuOpen}
              onClick={() => setMenuOpen((v) => !v)}
              disabled={disabled || loading}
            >
              <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                <path d="M12 5v14M5 12h14" />
              </svg>
            </button>
            {menuOpen && (
              <div className="attach-menu" role="menu">
                <button type="button" role="menuitem" onClick={() => photoRef.current?.click()}>
                  Add photos
                </button>
                <button type="button" role="menuitem" onClick={() => fileRef.current?.click()}>
                  Add files
                </button>
                <Link href="/dashboard/integrations/create" role="menuitem" onClick={() => setMenuOpen(false)}>
                  Create an MCP
                </Link>
              </div>
            )}
            <input
              ref={photoRef}
              type="file"
              accept="image/*"
              multiple
              hidden
              onChange={(e) => void addFiles(e.target.files, "photo")}
            />
            <input
              ref={fileRef}
              type="file"
              accept=".txt,.md,.json,.csv,.py,.js,.ts,.tsx,.html,.css,.yml,.yaml,.xml,.log,.sql,text/*,application/json"
              multiple
              hidden
              onChange={(e) => void addFiles(e.target.files, "file")}
            />
          </div>
          <textarea
            ref={inputRef}
            className="input chat-input"
            placeholder={disabled ? "Draft your next message…" : compact ? "Message Sandtable…" : "Ask anything. Sandtable will plan, build missing tools, and do the work."}
            value={goal}
            onChange={(e) => { setGoal(e.target.value); }}
            onKeyDown={(e) => {
              if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
                e.preventDefault();
                void send();
              }
            }}
            disabled={loading}
            aria-label="Message"
            title="Enter to send · Shift+Enter for a new line"
            rows={compact ? 1 : 2}
            maxLength={50000}
          />
          <button type="submit" className="send-orb" disabled={disabled || loading} aria-label="Send">
            {loading ? <span className="spinner" style={{ width: 16, height: 16 }} /> : (
              <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2">
                <path d="M12 19V5M5 12l7-7 7 7" />
              </svg>
            )}
          </button>
        </div>
        {goal.length >= 40000 && (
          <div style={{
            fontSize: 11,
            color: goal.length >= 49000 ? "var(--error)" : goal.length >= 40000 ? "var(--warning)" : "var(--text-tertiary)",
            textAlign: "right",
            padding: "4px 12px 0",
            fontVariantNumeric: "tabular-nums",
            opacity: goal.length < 100 ? 0.5 : 1,
          }}>
            {goal.length.toLocaleString()} / 50,000
          </div>
        )}
      </div>
      {!compact && (
        <div style={{ marginTop: 16, display: "flex", gap: 8, flexWrap: "wrap", justifyContent: "center" }}>
          {SUGGESTIONS.map((s) => (
            <button key={s.label} type="button" className="badge badge-neutral suggestion-chip" onClick={() => { setGoal(s.goal); inputRef.current?.focus(); }}>
              {s.label}
            </button>
          ))}
        </div>
      )}
    </form>
  );
}
