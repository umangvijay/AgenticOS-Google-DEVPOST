/**
 * Sandtable — Typed API Client (PRODUCTION)
 *
 * No hardcoded tokens. Uses real JWT from auth state.
 * Auto-refreshes expired tokens. All endpoints typed.
 */

import { fetchEventSource } from "@microsoft/fetch-event-source";
import { readSession, refreshSession } from "./auth-session";

import {
  RequestOptions, combineSignals, throwIfAborted, OperationDeadline,
} from "@/lib/request-policy";

export const API_BASE =
  process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000/api/v1";

// ── Token management ─────────────────────────────────────────────

/** Error carrying the HTTP status and (when structured) the backend body
 * (§2.15, §3.19) — budget rejections with code/available/required surface to
 * BudgetBlockedNotice; admission rejections with code/retry_after_seconds
 * surface to AdmissionNotice; status lets UIs detect access loss (401/403/404). */
export class ApiError extends Error {
  status?: number;
  body?: BudgetErrorBody | AdmissionErrorBody;
  constructor(message: string, body?: BudgetErrorBody | AdmissionErrorBody, status?: number) {
    super(message);
    this.name = "ApiError";
    this.body = body;
    this.status = status;
  }
}

function getAuthData(): { accessToken: string | null; refreshToken: string | null } {
  const session = readSession();
  return { accessToken: session?.accessToken || null, refreshToken: session?.refreshToken || null };
}

function getToken(): string | null {
  return getAuthData().accessToken;
}

function authHeaders(): Record<string, string> {
  const token = getToken();
  return token ? { Authorization: `Bearer ${token}` } : {};
}

const CSRF_STORAGE_KEY = "agentos_csrf_token";

function readStoredCsrf(): string {
  if (typeof window === "undefined") return "";
  try {
    return sessionStorage.getItem(CSRF_STORAGE_KEY) || "";
  } catch {
    return "";
  }
}

function writeStoredCsrf(token: string) {
  try {
    sessionStorage.setItem(CSRF_STORAGE_KEY, token);
  } catch {
    /* private mode / disabled storage */
  }
}

function csrfHeaders(): Record<string, string> {
  const token = readStoredCsrf();
  return token ? { "X-CSRF-Token": token } : {};
}

async function ensureCsrf(deadline?: OperationDeadline,
                          callerSignal?: AbortSignal): Promise<void> {
  // §19 (C6): CSRF preparation previously had NO timeout — it now
  // shares the caller's deadline and respects caller cancellation.
  if (typeof window === "undefined") return;
  if (readStoredCsrf()) return;
  throwIfAborted(callerSignal);
  const remaining = (deadline?.remainingMs(8000)) ?? 8000;
  // A deadline-exhausted prep returns immediately; the main fetch
  // surfaces the deadline error.
  if (remaining <= 0) return;
  const res = await fetch(`${API_BASE.replace("/api/v1", "")}/api/v1/csrf-token`, {
    credentials: "include",
    signal: combineSignals(callerSignal, AbortSignal.timeout(remaining)),
  });
  if (!res.ok) return;
  const data = (await res.json().catch(() => ({}))) as { csrf_token?: unknown };
  const token = typeof data.csrf_token === "string" ? data.csrf_token : "";
  if (token) writeStoredCsrf(token);
}

// ── Generic fetcher with auth ────────────────────────────────────

function formatApiError(errData: unknown, status: number): string {
  const detail = (errData as { detail?: unknown } | null)?.detail;
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) {
    return detail
      .map((item) => {
        if (typeof item === "string") return item;
        if (item && typeof item === "object" && "msg" in item) return String((item as { msg: unknown }).msg);
        return JSON.stringify(item);
      })
      .join("; ");
  }
  if (detail && typeof detail === "object") return JSON.stringify(detail);
  return `API error: ${status}`;
}

function rewriteFetchError(err: unknown): Error {
  if (err instanceof TypeError) {
    return new Error(
      "Could not reach the API (network or CORS). If Cloud Run is waking up, wait a few seconds and retry."
    );
  }
  return err instanceof Error ? err : new Error(String(err));
}

/** §18.5: standardized request behavior — every API call carries a
  timeout (30s default; SSE/binary paths use their own transports) and
  the server's X-Request-ID surfaces on errors for support correlation. */
const DEFAULT_REQUEST_TIMEOUT_MS = 30_000;

export async function apiFetch<T>(
  path: string,
  options: RequestOptions = {}
): Promise<T> {
  // §18.9 (finding F2/probe 4): the CALLER's signal is COMBINED with the
  // timeout — never overwritten. An already-aborted request never
  // reaches the transport. One total deadline spans CSRF/refresh/retry.
  const { signal: callerSignal, timeoutMs: callerTimeout, deadlineMs,
    requestId, idempotencyKey, ...restOptions } = options as RequestOptions;
  const requestTimeoutMs = callerTimeout ?? DEFAULT_REQUEST_TIMEOUT_MS;
  throwIfAborted(callerSignal);
  const deadline = new OperationDeadline(deadlineMs);
  const headers: Record<string, string> = {
    "Content-Type": "application/json",
    ...authHeaders(),
    ...csrfHeaders(),
    ...(restOptions.headers as Record<string, string> || {}),
  };
  if (requestId) headers["X-Request-ID"] = requestId;
  if (idempotencyKey) headers["Idempotency-Key"] = idempotencyKey;

  let res: Response;
  try {
    res = await fetch(`${API_BASE}${path}`, {
      ...restOptions,
      headers,
      credentials: "include",
      signal: combineSignals(callerSignal,
        AbortSignal.timeout(deadline.remainingMs(requestTimeoutMs))),
    });
  } catch (err) {
    if (err instanceof DOMException && err.name === "TimeoutError") {
      throw new Error("The request timed out. Check your connection and retry.");
    }
    throw rewriteFetchError(err);
  }

  if (res.status === 401) {
    const failedToken = headers.Authorization?.replace(/^Bearer /, "") || null;
    const accessToken = await refreshSession(failedToken);
    if (!accessToken) throw new Error("Your session has expired. Please sign in again.");
    headers.Authorization = `Bearer ${accessToken}`;
    try {
      // The deadline persists across the authorized retry (§18.9).
      res = await fetch(`${API_BASE}${path}`, {
        ...restOptions, headers, credentials: "include",
        signal: combineSignals(callerSignal,
          AbortSignal.timeout(deadline.remainingMs(requestTimeoutMs))),
      });
    } catch (err) { throw rewriteFetchError(err); }
  }

  if (res.status === 403) {
    const errData = await res.clone().json().catch(() => ({}));
    const detail = formatApiError(errData, 403);
    if (/csrf/i.test(detail)) {
      writeStoredCsrf("");
      await ensureCsrf(deadline, callerSignal);
  // §19 (C6): cancelled callers stop before dispatch.
  throwIfAborted(callerSignal);
      const retryHeaders: Record<string, string> = {
        ...headers,
        ...csrfHeaders(),
      };
      try {
        res = await fetch(`${API_BASE}${path}`, {
          ...options,
          headers: retryHeaders,
          credentials: "include",
        });
      } catch (err) {
        throw rewriteFetchError(err);
      }
    }
  }

  if (res.status === 204) return {} as T;

  if (!res.ok) {
    const errData = await res.json().catch(() => ({}));
    const detail = (errData as { detail?: unknown } | null)?.detail;
    if (detail && typeof detail === "object" && !Array.isArray(detail)
        && "code" in (detail as Record<string, unknown>)) {
      const body = detail as BudgetErrorBody & AdmissionErrorBody;
      throw new ApiError(body.message || body.code, body, res.status);
    }
    throw new ApiError(formatApiError(errData, res.status), undefined, res.status);
  }

  return res.json();
}

// ── Interfaces ───────────────────────────────────────────────────

export interface Task {
  task_id: string;
  workflow_id: string;
  run_id: string;
  agent: string;
  tool?: string;
  title?: string;
  status: string;
  input_data: Record<string, unknown>;
  output_data?: Record<string, unknown>;
  dependencies: string[];
  started_at?: string;
  completed_at?: string;
  error?: string;
  error_type?: string;
  attempt: number;
}

export interface WorkflowRun {
  run_id: string;
  workflow_id: string;
  user_id: string;
  goal: string;
  status: string;
  tasks: Task[];
  created_at: string;
  parent_run_id?: string | null;
  thread_id?: string | null;
}

export interface WorkflowEvent {
  event_id: string;
  timestamp: string;
  type: string;
  workflow_id: string;
  run_id: string;
  task_id?: string;
  status?: string;
  summary: string;
  sanitized_metadata: Record<string, unknown>;
}

export interface Integration {
  mcp_id: string;
  name: string;
  description?: string;
  trust_tier: string;
  transport?: string;
  is_enabled: boolean;
  health?: string;
  tool_count: number;
  validated_tool_count?: number;
  circuit_breaker: {
    state: string;
    failure_count: number;
    total_successes: number;
    total_failures: number;
    retry_in_seconds?: number;
  };
  created_at?: string;
}

export interface IntegrationTool {
  tool_name?: string;
  name?: string;
  description?: string;
  inputSchema?: Record<string, unknown>;
}

export interface Notification {
  id: string;
  type: string;
  title: string;
  body: string;
  is_read: boolean;
  metadata: Record<string, unknown>;
  created_at: string;
}

export interface Schedule {
  schedule_id: string;
  name: string;
  goal: string;
  cron_expression: string;
  timezone: string;
  /** Backend source of truth: "ACTIVE" | "PAUSED" */
  status?: string;
  /** Derived flag (backend also sends it); undefined falls back to status */
  is_enabled?: boolean;
  next_run_at?: string;
  created_at: string;
}

export function scheduleIsEnabled(schedule: Schedule): boolean {
  if (typeof schedule.is_enabled === "boolean") return schedule.is_enabled;
  return (schedule.status || "ACTIVE").toUpperCase() === "ACTIVE";
}

export interface ApprovalRequest {
  approval_id: string;
  workflow_id: string;
  run_id: string;
  task_id: string;
  user_id: string;
  status: string;
  requested_at: string;
  expires_at: string;
  risk_level: string;
  reason: string;
  tool_name: string;
  tool_arguments: Record<string, unknown>;
}

export interface UserSettings {
  theme: string;
  autonomy_level: number;
  default_model: string;
  notifications_enabled: boolean;
  daily_token_limit: number;
  auto_approve_low_risk: boolean;
  webhook_url?: string;
}

export interface GoalAttachment {
  name: string;
  mime: string;
  text?: string;
  image_base64?: string;
}

// ── Workflows ────────────────────────────────────────────────────

export async function submitGoal(
  goal: string,
  attachments: GoalAttachment[] = [],
  opts: { parent_run_id?: string; thread_id?: string; model?: string;
          idempotency_key?: string } = {},
) {
  // C16 (§5.0 Batch E): ONE stable submission identity per intended
  // submission — the caller REUSES the key across timeout/disconnection
  // retries so the server dedups, and issues a NEW key for an intentional
  // new submission.
  return apiFetch<{ run_id: string; workflow_id: string; status: string;
                    task_count: number; thread_id?: string;
                    command_id?: string; deduplicated?: boolean }>(
    "/workflows",
    {
      method: "POST",
      headers: opts.idempotency_key
        ? { "Idempotency-Key": opts.idempotency_key }
        : undefined,
      body: JSON.stringify({
        goal,
        attachments,
        model: opts.model || undefined,
        parent_run_id: opts.parent_run_id || undefined,
        thread_id: opts.thread_id || undefined,
      }),
    }
  );
}

// ── Effects & reconciliation API (Phase 5 §5.19) ────────────────────

export interface EffectSummary {
  effect_id: string;
  operation: string;
  state: string;
  state_version: number;
  target: string;
  run_id?: string;
  task_id?: string;
  terminal: boolean;
  permitted_actions: {
    reconcile: boolean;
    retry: boolean;
    compensate: boolean;
    close_unresolved: boolean;
  };
  receipts?: {
    provider_status: string;
    provider_operation_id?: string;
    detail?: Record<string, unknown>;
  }[];
}

/** Effects a run caused (safe summaries; evidence bodies stay redacted). */
export async function getEffectsByRun(runId: string): Promise<{
  run_id: string; effects: EffectSummary[]; count: number;
}> {
  return apiFetch(`/effects/by-run/${encodeURIComponent(runId)}`);
}

/** Request ONE bounded evidence investigation (§5.10) — never a guess. */
export async function reconcileEffect(effectId: string): Promise<Record<string, unknown>> {
  return apiFetch(`/effects/${encodeURIComponent(effectId)}/reconcile`, {
    method: "POST",
  });
}

/** Authorized, evidence-backed conditional resolution (§5.11). */
export async function resolveEffect(
  effectId: string,
  body: { resolution: string; expected_version: number;
          evidence_references: string[]; reason: string },
): Promise<Record<string, unknown>> {
  return apiFetch(`/effects/${encodeURIComponent(effectId)}/resolve`, {
    method: "POST", body: JSON.stringify(body),
  });
}

/** A fresh client submission identity (§4.4: a deliberate new submission
 * gets a new key). */
export function newSubmissionKey(): string {
  if (typeof crypto !== "undefined" && "randomUUID" in crypto) {
    return `sub-${crypto.randomUUID()}`;
  }
  return `sub-${Date.now()}-${Math.random().toString(36).slice(2)}`;
}

export async function getWorkflowThread(runId: string) {
  return apiFetch<{ thread_id: string; workflows: WorkflowRun[] }>(`/workflows/${runId}/thread`);
}

export async function listWorkflows(limit = 50, offset = 0) {
  return apiFetch<{ workflows: WorkflowRun[]; count: number }>(
    `/workflows?limit=${limit}&offset=${offset}`
  );
}

export async function getWorkflow(runId: string) {
  return apiFetch<WorkflowRun>(`/workflows/${runId}`);
}

export async function cancelWorkflow(runId: string) {
  return apiFetch<{ run_id: string; status: string }>(
    `/workflows/${runId}/cancel`,
    { method: "POST" }
  );
}

export async function retryWorkflow(runId: string) {
  return apiFetch<{ run_id: string; status: string; retried_tasks: number }>(
    `/workflows/${runId}/retry`,
    { method: "POST" }
  );
}

export async function resumeWorkflow(runId: string) {
  return apiFetch<{ run_id: string; status: string; resumed_tasks: number }>(
    `/workflows/${runId}/resume`,
    { method: "POST" }
  );
}

export function subscribeWorkflowEvents(
  runId: string,
  onMessage: (event: WorkflowEvent) => void,
  onOpen?: () => void,
  onError?: (err: unknown) => void
) {
  const controller = new AbortController();
  // §18.16: the dedup window is BOUNDED — an ever-growing Set would
  // accumulate unbounded memory over indefinite sessions. A FIFO ring
  // of recent IDs keeps duplicate suppression within the reconnect
  // window, which is where duplicates occur.
  const seen = new Set<string>();
  const seenOrder: string[] = [];
  const SEEN_MAX = 2000;
  const rememberSeen = (id: string) => {
    if (seen.has(id)) return;
    seen.add(id);
    seenOrder.push(id);
    if (seenOrder.length > SEEN_MAX) {
      const oldest = seenOrder.shift();
      if (oldest !== undefined) seen.delete(oldest);
    }
  };

  void ensureCsrf();
  fetchEventSource(`${API_BASE}/workflows/${runId}/events`, {
    headers: { ...authHeaders(), ...csrfHeaders() },
    credentials: "include",
    signal: controller.signal,
    onopen: async (res) => {
      if (res.ok && res.status === 200) {
        if (onOpen) onOpen();
      } else if (res.status >= 400 && res.status < 500 && res.status !== 429) {
        throw new Error(`SSE failed: ${res.status}`);
      }
    },
    onmessage: (msg) => {
      if (msg.event === "error") {
        let text = msg.data || "Event stream failed";
        try {
          const parsed = JSON.parse(msg.data) as { error?: string };
          if (parsed.error) text = parsed.error;
        } catch { /* keep raw */ }
        if (onError) onError(new Error(text));
        return;
      }
      if (msg.data) {
        try {
          const data = JSON.parse(msg.data) as WorkflowEvent;
          if (data.event_id) {
            if (seen.has(data.event_id)) return;
            rememberSeen(data.event_id);
          }
          onMessage(data);
        } catch (e) {
          console.error("Failed to parse SSE event", e);
        }
      }
    },
    onerror: (err) => {
      if (onError) onError(err);
      // §3.19: jittered reconnect — returning a delay controls the retry
      // backoff and keeps many clients from resynchronizing into a wave.
      return jitteredDelay(2000);
    },
  });

  return () => controller.abort();
}

// ── Integrations ─────────────────────────────────────────────────

export interface AgentTool {
  agent_tool_name: string;
  name: string;
  mcp_name?: string;
  description?: string;
  input_schema?: Record<string, unknown>;
}

export interface AgentPlugin {
  id: string;
  state: string;
  installed_by: string;
  manifest: {
    plugin_id: string;
    name: string;
    description: string;
    required_tools: string[];
    agents: { agent_id: string; display_name: string; instructions: string; allowed_tools: string[] }[];
  };
}

export async function listAgentPlugins() {
  return apiFetch<{ plugins: AgentPlugin[] }>("/agents/plugins");
}

export async function listAgentTools() {
  return apiFetch<{ tools: AgentTool[] }>("/agents/tools");
}

export async function installAgentPlugin(manifest: Record<string, unknown>) {
  return apiFetch<{ plugin: AgentPlugin }>("/agents/plugins", {
    method: "POST", body: JSON.stringify({ manifest }),
  });
}

export async function setAgentPluginEnabled(pluginId: string, enabled: boolean, requiredTools: string[] = []) {
  return apiFetch<{ status: string; plugin_id: string }>(`/agents/plugins/${encodeURIComponent(pluginId)}/${enabled ? "enable" : "disable"}`, {
    method: "POST", ...(enabled ? { body: JSON.stringify({ required_tools: requiredTools }) } : {}),
  });
}

export async function listIntegrations() {
  return apiFetch<{ integrations: Integration[]; count: number }>("/integrations");
}

export async function getIntegration(mcpId: string) {
  return apiFetch<Integration & { tools: IntegrationTool[];
                                  operation_eligibility?: OperationEligibility[] }>(
    `/integrations/${mcpId}`);
}

export async function buildIntegrationFromURL(url: string, name?: string, method?: "url" | "website") {
  return apiFetch<{ status: string; message: string; build_id?: string; mcp_id?: string }>(
    "/integrations/build-from-url",
    { method: "POST", body: JSON.stringify({ url, name, method }) }
  );
}

export async function buildIntegrationFromWebsite(url: string, name?: string, notes?: string) {
  return apiFetch<{ status: string; message: string; build_id?: string; mcp_id?: string }>(
    "/integrations/build-from-website",
    { method: "POST", body: JSON.stringify({ url, name, notes }) }
  );
}

export async function buildIntegrationFromSpec(spec: string, name?: string) {
  return apiFetch<{ status: string; message: string; build_id?: string; mcp_id?: string }>(
    "/integrations/build",
    { method: "POST", body: JSON.stringify({ spec, name }) }
  );
}

// §27.6: the DECLARED supported-app catalog — a declaration is never
// execution proof; per-operation readiness comes from build records.
export interface DeclaredApp {
  app_id: string;
  version: string;
  name: string;
  publisher: string;
  auth_method: string;
  documented_operations: string[];
  tested_operations: string[];
  qualification: string;
  description: string;
}

export async function listDeclaredApps() {
  return apiFetch<{ apps: DeclaredApp[] }>("/integrations/apps/catalog");
}

export async function installDeclaredApp(appId: string, baseUrl?: string) {
  return apiFetch<{ status: string; message: string; build_id: string; app_id: string; tested_operations: string[] }>(
    `/integrations/apps/${encodeURIComponent(appId)}/install`,
    { method: "POST", body: JSON.stringify({ base_url: baseUrl ?? null }) }
  );
}

export async function buildIntegrationFromPrompt(prompt: string, name?: string) {
  return apiFetch<{ status: string; message: string; build_id?: string; mcp_id?: string }>(
    "/integrations/build-from-prompt",
    { method: "POST", body: JSON.stringify({ prompt, name }) }
  );
}

export async function getIntegrationBuild(buildId: string, signal?: AbortSignal) {
  return apiFetch<{
    build_id: string;
    status: string;
    stage: string;
    logs: string[];
    mcp_id?: string;
    tools?: unknown[];
    error?: string;
    message?: string;
  }>(`/integrations/builds/${buildId}`, { signal });
}

export async function testIntegration(mcpId: string) {
  return apiFetch<{ status: string; probe?: { http_status?: number; reason?: string } }>(`/integrations/${mcpId}/test`, { method: "POST" });
}

export async function enableIntegration(mcpId: string) {
  return apiFetch(`/integrations/${mcpId}/enable`, { method: "POST" });
}

export async function disableIntegration(mcpId: string) {
  return apiFetch(`/integrations/${mcpId}/disable`, { method: "POST" });
}

export async function deleteIntegration(mcpId: string) {
  return apiFetch(`/integrations/${mcpId}`, { method: "DELETE" });
}

// ── Notifications ────────────────────────────────────────────────

export async function listNotifications(unreadOnly = false, limit = 50) {
  return apiFetch<{ notifications: Notification[]; unread_count: number }>(
    `/notifications?unread_only=${unreadOnly}&limit=${limit}`
  );
}

export async function getUnreadCount() {
  return apiFetch<{ unread_count: number }>("/notifications/unread");
}

export async function markNotificationRead(id: string) {
  return apiFetch(`/notifications/${id}/read`, { method: "POST" });
}

export async function markAllNotificationsRead() {
  return apiFetch("/notifications/read-all", { method: "POST" });
}

// ── Schedules ────────────────────────────────────────────────────

export async function listSchedules() {
  return apiFetch<{ schedules: Schedule[]; count: number }>("/schedules");
}

export async function createSchedule(data: {
  name: string;
  goal: string;
  cron_expression: string;
  timezone?: string;
  is_enabled?: boolean;
}) {
  return apiFetch<Schedule>("/schedules", {
    method: "POST",
    body: JSON.stringify(data),
  });
}

export async function deleteSchedule(id: string) {
  return apiFetch(`/schedules/${id}`, { method: "DELETE" });
}

export async function runScheduleNow(id: string) {
  return apiFetch(`/schedules/${id}/run`, { method: "POST" });
}

export async function toggleSchedulePause(id: string) {
  return apiFetch(`/schedules/${id}/pause`, { method: "POST" });
}

// ── Settings ─────────────────────────────────────────────────────

export interface ModelCatalogEntry {
  id: string;
  name: string;
  family: string;
  lifecycle: string;
  selectable: boolean;
  access: string;
  reason: string;
  note: string;
  retirement_date: string;
  documentation: string;
}

export interface ModelCatalog {
  models: ModelCatalogEntry[];
  provider: string;
  default_model: string;
  documentation_checked: string;
  auth_mode: string;
}

export async function getSettings() {
  return apiFetch<{ settings: UserSettings; available_models?: string[]; model_catalog?: ModelCatalog }>("/settings");
}

export async function updateSettings(data: Partial<UserSettings>) {
  return apiFetch<{ settings: UserSettings }>("/settings", {
    method: "PUT",
    body: JSON.stringify(data),
  });
}

// ── Memory ───────────────────────────────────────────────────────

export async function addMemory(content: string, memory_type = "semantic") {
  return apiFetch<{ memory_id: string }>("/memory", {
    method: "POST",
    body: JSON.stringify({ content, memory_type }),
  });
}

export async function searchMemory(query: string, limit = 10) {
  return apiFetch<{ results: unknown[] }>("/memory/search", {
    method: "POST",
    body: JSON.stringify({ query, limit }),
  });
}

// ── Approvals ────────────────────────────────────────────────────

export async function listApprovals() {
  return apiFetch<{ approvals: ApprovalRequest[]; count: number }>("/approvals");
}

export async function resolveApproval(approvalId: string, action: "approve" | "reject") {
  return apiFetch(`/approvals/${approvalId}/${action}`, { method: "POST" });
}

// ── Health ───────────────────────────────────────────────────────

export async function healthCheck() {
  const res = await fetch(`${API_BASE.replace("/api/v1", "")}/health`);
  return res.json();
}

// ── Credentials ──────────────────────────────────────────────────

export async function listCredentials() {
  return apiFetch<{ credentials: string[]; count: number }>("/credentials");
}

export async function storeCredential(name: string, values: Record<string, string>) {
  return apiFetch<{ name: string; fields: string[]; stored: boolean }>("/credentials", {
    method: "POST",
    body: JSON.stringify({ name, values }),
  });
}

export async function deleteCredential(name: string) {
  return apiFetch(`/credentials/${encodeURIComponent(name)}`, { method: "DELETE" });
}

// ── Capabilities ─────────────────────────────────────────────────

export async function checkSiteHealth(url: string) {
  return apiFetch<Record<string, unknown>>("/capabilities/site-health", {
    method: "POST",
    body: JSON.stringify({ url }),
  });
}

export async function debugSource(source: string, language = "python", error_message = "", goal = "") {
  return apiFetch<Record<string, unknown>>("/capabilities/debug", {
    method: "POST",
    body: JSON.stringify({ source, language, error_message, goal }),
  });
}

export async function pingGemini(candidateKey?: string) {
  return apiFetch<{ ok: boolean; reply: string; using_user_key: boolean }>("/capabilities/ping", {
    method: "POST",
    body: JSON.stringify(candidateKey ? { candidate_key: candidateKey } : {}),
  });
}

export async function validateCredential(name: string, values: Record<string, string>) {
  return apiFetch<{ valid: boolean; error?: string }>("/credentials/validate", {
    method: "POST",
    body: JSON.stringify({ name, values }),
  });
}

export interface GenerateProgress { job_id: string; status: string; stage?: string; completed_files?: number; total_files?: number }

export async function generateProject(brief: string, kind = "website", name = "", scale = "standard", onProgress?: (progress: GenerateProgress) => void) {
  const started = await apiFetch<{ job_id: string; status: string }>("/capabilities/generate", {
    method: "POST",
    body: JSON.stringify({ brief, kind, name, scale }),
  });
  const jobId = started.job_id;
  onProgress?.(started);
  const controller = new AbortController();
  void fetchEventSource(`${API_BASE}/capabilities/generate/${jobId}/events`, {
    headers: authHeaders(), credentials: "include", signal: controller.signal,
    onmessage(event) {
      if (event.event === "progress") {
        try { onProgress?.(JSON.parse(event.data)); } catch { /* polling recovers */ }
      }
    },
    onerror() { controller.abort(); },
  }).catch(() => {});
  type Job = GenerateProgress & {
    error?: string | null;
    result?: {
      artifact_id: string;
      name: string;
      summary: string;
      files: string[];
      entrypoint: string;
      kind: string;
      scale?: string;
    } | null;
  };
  try {
    const deadline = Date.now() + 30 * 60 * 1000;
    let readFailures = 0;
    while (Date.now() < deadline) {
      let job: Job;
      try {
        job = await apiFetch<Job>(`/capabilities/generate/${jobId}`);
        readFailures = 0;
      } catch (err) {
        if (!(err instanceof ApiError) || !err.status || err.status < 500 || ++readFailures > 3) throw err;
        await new Promise(resolve => setTimeout(resolve, 1000 * readFailures));
        continue;
      }
      onProgress?.(job);
      if (job.status === "completed" && job.result) return job.result;
      if (job.status === "failed") throw new Error(job.error || "Generation failed");
      await new Promise(resolve => setTimeout(resolve, 2500));
    }
    throw new Error(`Couldn’t confirm the result of job ${jobId}. Check its saved status before starting another job.`);
  } finally { controller.abort(); }
}

export async function listArtifacts() {
  return apiFetch<{ artifacts: unknown[]; count: number }>("/artifacts");
}

export interface ModelAssetRequest {
  kind: string;                 // box | sphere | cylinder | assembly
  size?: number[];
  segments?: number;
  parts?: Array<Record<string, unknown>>;
  name?: string;
}

export interface ModelAssetResult {
  artifact_id: string;
  name: string;
  files: string[];
  model?: { vertices: number; faces: number; sha256: string; format: string; generation_ms: number };
}

// §27.15: a REAL modeling job — never a website-generation fallback.
export async function generateModelAsset(request: ModelAssetRequest) {
  return apiFetch<ModelAssetResult>("/modeling/assets", {
    method: "POST",
    body: JSON.stringify(request),
  });
}

export function artifactFileUrl(artifactId: string, filePath: string) {
  return `${API_BASE}/artifacts/${artifactId}/files/${filePath}`;
}

export async function fetchArtifactText(artifactId: string, filePath: string) {
  const res = await fetch(`${API_BASE}/artifacts/${artifactId}/files/${encodeURIComponent(filePath).replace(/%2F/g, "/")}`, {
    headers: { ...authHeaders() },
    credentials: "include",
  });
  if (!res.ok) {
    throw new Error("Could not load generated file");
  }
  return res.text();
}

// ── Resume ───────────────────────────────────────────────────────

export async function createResumeFromText(profile_text: string, job_description = "", tailor = false) {
  return apiFetch<Record<string, unknown>>("/resume/create", {
    method: "POST",
    body: JSON.stringify({ profile_text, job_description, tailor }),
  });
}

export async function tailorResume(master_resume: Record<string, unknown>, job_description: string) {
  return apiFetch<{ status: string; tailored_resume: Record<string, unknown> }>("/resume/tailor", {
    method: "POST",
    body: JSON.stringify({ master_resume, job_description, target_job_id: "jd" }),
  });
}

export async function renderResumeHtml(resume: Record<string, unknown>) {
  return apiFetch<{ html: string; pdf_error?: string }>("/resume/render", {
    method: "POST",
    body: JSON.stringify({ resume, format: "html" }),
  });
}

export async function scanResumeFile(file: File, jobDescription: string) {
  await ensureCsrf();
  const form = new FormData();
  form.append("file", file);
  form.append("jobDescription", jobDescription);
  const headers: Record<string, string> = { ...authHeaders(), ...csrfHeaders() };
  delete (headers as { "Content-Type"?: string })["Content-Type"];
  const res = await fetch(`${API_BASE}/resume/scan`, {
    method: "POST",
    headers,
    body: form,
    credentials: "include",
  });
  if (!res.ok) {
    const errData = await res.json().catch(() => ({}));
    throw new Error((errData as { detail?: string }).detail || `API error: ${res.status}`);
  }
  return res.json();
}

// ── Context usage ────────────────────────────────────────────────

export interface ContextCategory {
  id: string;
  label: string;
  tokens: number;
}

export interface ContextUsage {
  window_tokens: number;
  context_estimate: number;
  remaining_tokens?: number;
  percent: number;
  model: string;
  tool_count: number;
  categories: ContextCategory[];
  budget_endpoint?: string;
  note?: string;
}

export async function getContextUsage() {
  return apiFetch<ContextUsage>("/usage/context");
}

// ── Authoritative budget (Phase 2, §2.15/§2.16) ──────────────────
// Distinct from the context estimate above: this is the spending ledger.
// `uncertain_reserved` is a SUBSET of `reserved`, not an extra subtraction.

export interface ProviderRateLimit {
  provider: string;
  source: "provider_header" | "provider_receipt" | "provider_error";
  scope?: string | null;
  limit?: number | null;
  remaining?: number | null;
  reset_at?: string | null;
  window?: string | null;
  observed_at?: string | null;
}

export interface BudgetSnapshot {
  scope: string;
  subject_id: string;
  unit: string;
  period: string;
  period_start?: string | null;
  period_end?: string | null;
  limit: number | null;
  consumed: number | null;
  reserved: number | null;
  uncertain_reserved: number | null;
  available: number | null;
  accounting_status: "healthy" | "degraded" | "unavailable";
  policy_version?: string | null;
  provider_rate_limit?: ProviderRateLimit | null;
  as_of?: string | null;
  message?: string;
}

export interface BudgetCheckResult {
  admitted: boolean;
  snapshot: BudgetSnapshot;
}

export interface BudgetErrorBody {
  code: string;
  unit?: string;
  retryable_now?: boolean;
  message?: string;
  available?: number;
  required?: number;
}

// ── Admission overload contract (Phase 3 §3.19) ─────────────────────

export interface AdmissionErrorBody {
  code: string;                 // RATE_LIMITED | TENANT_CAPACITY_EXHAUSTED | QUEUE_FULL |
                                // PROVIDER_THROTTLED | ADMISSION_UNAVAILABLE | POLICY_INVALID
  message?: string;
  group?: string;               // policy group that produced the decision
  retry_after_seconds?: number;
  accepted?: boolean;           // always false on these bodies
  enforcement?: string;         // shared | local_only | local_emergency | disabled_by_policy
  decision_id?: string;         // correlate with support/logs
  resource?: string;            // capacity resource class, when applicable
}

export const ADMISSION_CODES = new Set([
  "RATE_LIMITED",
  "TENANT_CAPACITY_EXHAUSTED",
  "QUEUE_FULL",
  "PROVIDER_THROTTLED",
  "ADMISSION_UNAVAILABLE",
  "POLICY_INVALID",
]);

/** True when an ApiError body is an admission decision (not a budget one). */
export function isAdmissionError(err: unknown): err is ApiError & { body: AdmissionErrorBody } {
  return err instanceof ApiError
    && !!err.body
    && typeof (err.body as { code?: unknown }).code === "string"
    && ADMISSION_CODES.has((err.body as { code: string }).code);
}

// ── Connector eligibility (Phase 6 §6.12/§6.17) ─────────────────────

export interface OperationEligibility {
  tool_name: string;
  allowed: boolean;
  reason_codes: string[];
  provenance: string;      // official_spec | extracted_docs | inferred_description | unknown
  operation_digest: string;
}

/** §6.17: operation-level truth from the ONE authoritative evaluator —
 * eligible count, blocked count, and per-operation reasons. */
export function eligibilitySummary(detail: { operation_eligibility?: OperationEligibility[] }): {
  eligible: number; blocked: number; total: number;
} {
  const list = detail.operation_eligibility || [];
  return {
    eligible: list.filter(e => e.allowed).length,
    blocked: list.filter(e => !e.allowed).length,
    total: list.length,
  };
}

export function eligibilityReasonLabel(code: string): string {
  const labels: Record<string, string> = {
    CONNECTOR_DISABLED: "Connector is disabled",
    REVISION_NOT_ACTIVE: "Revision is not active",
    REVISION_REVOKED: "Revision was revoked",
    UNKNOWN_PROVENANCE: "Definition origin is unknown — review required",
    REVIEW_REQUIRED: "Inferred definition needs review",
    UNSUPPORTED_OPERATION: "Operation uses unsupported constructs",
    CREDENTIAL_MISSING: "Credential is missing",
    CREDENTIAL_CONTEXT_CHANGED: "Credential context changed — revalidate",
    SCOPE_MISSING: "Missing required scopes",
    VALIDATION_MISSING: "No validation evidence for the current context",
    VALIDATION_STALE: "Validation evidence is stale — revalidate",
    RUNTIME_CHANGED: "Runtime changed since validation",
    WRITE_VALIDATION_REQUIRED: "Write validation profile required before writes",
  };
  return labels[code] ?? code;
}

// ── Recovery episodes (Phase 11 §11.17) ─────────────────────────────

export interface RecoveryEpisodeSummary {
  episode_id: string;
  state: string;
  task_id?: string;
  failure_category?: string;
  failure_code?: string;
  proposal_explanation?: string;
  policy_result?: string;
  policy_reason?: string;
  attempt_count?: number;
  verification_verdict?: string;
}

export async function getRecoveryEpisodes(runId: string): Promise<{
  run_id: string; episodes: RecoveryEpisodeSummary[]; count: number;
}> {
  return apiFetch(`/recovery/runs/${encodeURIComponent(runId)}/episodes`);
}

// ── Command lifecycle API (Phase 4 §4.19/§4.25) ─────────────────────

export interface CommandReceipt {
  command_id: string;
  run_id?: string;
  build_id?: string;
  state: string;          // ACCEPTED | QUEUED | RUNNING | WAITING |
                          // CANCEL_REQUESTED | SUCCEEDED | FAILED | CANCELLED | EXPIRED
  state_version?: number;
  waiting_reason?: string;
  terminal: boolean;
  terminal_reason?: string;
  cancel_requested_at?: number;
  cancel_acknowledged_at?: number;
  result_reference?: string;
  created_at?: number;
  updated_at?: number;
  status_url?: string;
  accepted?: boolean;
  deduplicated?: boolean;
  message?: string;
}

/** Resolve a run's command receipt (C15/C16) — the workspace page knows
 * the run id, so lifecycle states and cancellation are reachable without
 * having held the command id client-side. */
export async function getCommandByRun(runId: string): Promise<CommandReceipt> {
  const receipt = await apiFetch<CommandReceipt>(`/commands/by-run/${encodeURIComponent(runId)}`);
  if (!receipt?.command_id || !receipt.state) throw new Error("Command status is unavailable.");
  return receipt;
}

/** Resolve UNKNOWN acceptance after a transport timeout: GET the durable
 * state instead of blindly resubmitting (§4.19). */
export async function getCommand(commandId: string): Promise<CommandReceipt> {
  return apiFetch<CommandReceipt>(`/commands/${encodeURIComponent(commandId)}`);
}

/** Record cancellation INTENT (§4.15) — the response distinguishes
 * "requested" from "already finished honestly"; effects are not undone. */
export async function cancelCommand(commandId: string): Promise<{
  command_id: string; state: string; terminal: boolean; message: string;
}> {
  return apiFetch(`/commands/${encodeURIComponent(commandId)}/cancel`, {
    method: "POST",
  });
}

/** Authorized retry of a FAILED command (§4.19) — a new scoped key under
 * the same intent, through the same admission gates. */
export async function retryCommand(commandId: string): Promise<CommandReceipt> {
  return apiFetch<CommandReceipt>(`/commands/${encodeURIComponent(commandId)}/retry`, {
    method: "POST",
  });
}

/** Authorized command event history (§4.19/§4.21). */
export async function getCommandEvents(commandId: string): Promise<{
  command_id: string;
  events: { event_id: string; event_type: string; aggregate_version: number;
            payload: Record<string, unknown>; created_at: number;
            published_at: number | null }[];
  count: number;
}> {
  return apiFetch(`/commands/${encodeURIComponent(commandId)}/events`);
}

/** Parse an admission body out of any error (tolerates string details). */
export function admissionBodyFrom(err: unknown): AdmissionErrorBody | null {
  if (isAdmissionError(err)) return err.body;
  if (err instanceof ApiError && err.body) {
    const candidate = err.body as { code?: string };
    if (candidate.code && ADMISSION_CODES.has(candidate.code)) {
      return err.body as AdmissionErrorBody;
    }
  }
  return null;
}

/** Jittered reconnect/poll delay (§3.19): base ±25%, so many clients never
 * synchronize their retries into a new wave. Returns milliseconds. */
export function jitteredDelay(baseMs: number): number {
  return Math.max(250, Math.round(baseMs * (0.75 + Math.random() * 0.5)));
}

export async function getBudgetSnapshot() {
  return apiFetch<BudgetSnapshot>("/usage/budget");
}

export async function checkBudgetAdmission(requiredUnits: number) {
  return apiFetch<BudgetCheckResult>("/usage/budget/check", {
    method: "POST",
    body: JSON.stringify({ required_units: requiredUnits }),
  });
}


export async function listAdminUsers() {
  return apiFetch<{ users: Array<{ user_id: string; email?: string; name?: string; role: string; is_active: boolean; runs_total?: number; runs_5h?: number; last_run_at?: string }>; count: number; window_hours: number }>(
    "/admin/users"
  );
}

export async function setUserActive(userId: string, active: boolean) {
  return apiFetch<{ user_id: string; is_active: boolean }>(`/admin/users/${userId}/${active ? "enable" : "disable"}`, { method: "POST" });
}

export async function adminStats() {
  return apiFetch<{ users_total: number; users_active_5h: number; runs_total: number; integrations: number; window_hours: number }>("/admin/stats");
}

export interface AdminAuditEvent {
  id?: number;
  event_type: string;
  actor_id: string;
  actor_type: string;
  resource_id: string;
  workflow_id?: string;
  run_id?: string;
  details?: Record<string, unknown>;
  timestamp: string;
}

export async function adminAudit(limit = 100, eventType?: string) {
  const params = new URLSearchParams({ limit: String(limit) });
  if (eventType) params.set("event_type", eventType);
  return apiFetch<{ events: AdminAuditEvent[]; count: number }>(`/admin/audit?${params.toString()}`);
}

export async function setUserRole(userId: string, role: string) {
  return apiFetch<{ user_id: string; role: string }>(`/admin/users/${userId}/role`, { method: "POST", body: JSON.stringify({ role }) });
}


export async function deleteAnyUser(userId: string) {
  return apiFetch<{ deleted: boolean }>(`/admin/users/${userId}`, { method: "DELETE" });
}

export async function setUserLimits(userId: string, dailyTokenLimit?: number, windowHours?: number) {
  return apiFetch<{ user_id: string }>(`/admin/users/${userId}/limits`, {
    method: "PUT",
    body: JSON.stringify({ daily_token_limit: dailyTokenLimit ?? null, window_hours: windowHours ?? null }),
  });
}


export async function attachIntegrationKey(mcpId: string, apiKey: string) {
  return apiFetch(`/integrations/${mcpId}/credentials`, {
    method: "POST", body: JSON.stringify({api_key: apiKey}),
  });
}


export function subscribeBuildEvents(buildId: string, onUpdate: (build: Awaited<ReturnType<typeof getIntegrationBuild>>) => void) {
  const controller = new AbortController();
  void fetchEventSource(`${API_BASE}/integrations/builds/${buildId}/events`, {
    headers: authHeaders(), credentials: "include", signal: controller.signal,
    onmessage(message) {
      if (message.event !== "build") return;
      try { onUpdate(JSON.parse(message.data)); } catch { /* polling remains available */ }
    },
    onerror() { controller.abort(); },
  }).catch(() => { /* polling remains available */ });
  return () => controller.abort();
}


export async function downloadIntegrationServer(mcpId: string) {
  const response = await fetch(`${API_BASE}/integrations/${mcpId}/export`, {headers: authHeaders(), credentials: "include"});
  if (!response.ok) throw new Error("Server export is available for HTTP API integrations only.");
  const url = URL.createObjectURL(await response.blob());
  const link = document.createElement("a"); link.href = url; link.download = "generated-mcp.zip";
  link.click(); setTimeout(() => URL.revokeObjectURL(url), 1000);
}

export interface ConnectorRevision {
  revision_id: string;
  created_at: string;
  tool_count: number;
  version?: string;
}
export async function listConnectorRevisions(mcpId: string): Promise<{ revisions: ConnectorRevision[] }> {
  return apiFetch(`/integrations/${encodeURIComponent(mcpId)}/revisions`);
}
export async function checkpointConnector(mcpId: string) {
  return apiFetch(`/integrations/${encodeURIComponent(mcpId)}/revisions`, { method: "POST" });
}
export async function restoreConnectorRevision(mcpId: string, revisionId: string): Promise<{ mcp_id: string }> {
  return apiFetch(`/integrations/${encodeURIComponent(mcpId)}/revisions/${encodeURIComponent(revisionId)}/restore`, { method: "POST" });
}

export interface CostTrackingEstimate {
  mode: string;
  note: string;
  estimated_tokens: number;
  estimated_cost_usd: number;
  usd_per_1m_tokens: number;
}

export interface ModelLeaderboardRow {
  model: string;
  attempts: number;
  successes: number;
  failures: number;
  ewma_latency_ms: number;
  success_rate: number | null;
  score: number;
}

export interface OperationsSummary {
  sample_size: number;
  sample_limit: number;
  completion_rate: number | null;
  median_completion_seconds: number | null;
  status_counts: Record<string, number>;
  failed_tasks_by_agent: Record<string, number>;
  attention: { run_id: string; status: string; reason: string; age_seconds: number | null }[];
  generated_at: string;
  cost_tracking: CostTrackingEstimate;
}

export async function getModelLeaderboard() {
  return apiFetch<{ policy: string; models: ModelLeaderboardRow[]; note: string }>("/usage/models");
}
export async function getOperationsSummary(): Promise<OperationsSummary> {
  return apiFetch("/usage/operations");
}

// ── API keys (programmatic access) ─────────────────────────────

export interface ApiKeyRow {
  id: string;
  name: string;
  prefix: string;
  created_at: string;
  last_used_at?: string | null;
  revoked_at?: string | null;
  is_active?: boolean;
}

export async function createApiKey(name: string) {
  return apiFetch<{ id: string; name: string; key: string; prefix: string; warning: string }>(
    "/auth/api-keys",
    { method: "POST", body: JSON.stringify({ name }) },
  );
}

export async function listApiKeys() {
  return apiFetch<{ keys: ApiKeyRow[]; count: number }>("/auth/api-keys");
}

export async function revokeApiKey(id: string) {
  return apiFetch<{ id: string; revoked: boolean }>(`/auth/api-keys/${id}`, { method: "DELETE" });
}

// ── Shared integration catalog ─────────────────────────────────

export interface CatalogEntry {
  mcp_id: string;
  name: string;
  description?: string;
  source_type: string;
  trust_tier: string;
  spec_version?: string;
  updated_at?: string;
  tools: { name?: string; description?: string }[];
  tool_count: number;
}

export async function listCatalog() {
  return apiFetch<{ integrations: CatalogEntry[]; count: number }>("/integrations/catalog");
}

export async function installFromCatalog(mcpId: string) {
  return apiFetch<{ mcp_id: string; installed: boolean; tools?: number; name?: string; message?: string }>(
    `/integrations/${mcpId}/install`,
    { method: "POST" },
  );
}

// ── Organizations (RBAC foundation) ───────────────────────────

export interface OrgRow {
  org_id: string;
  name: string;
  owner_id: string;
  created_at: string;
  role?: string;
}

export interface OrgMember {
  user_id: string;
  role: string;
  added_at?: string;
  email?: string;
  name?: string;
}

export interface OrgRunRow {
  run_id: string;
  goal: string;
  status: string;
  created_at: string;
  member_email?: string;
  user_id: string;
  tasks: { task_id: string; title?: string; agent: string; status: string; error?: string }[];
}

export async function createOrg(name: string) {
  return apiFetch<OrgRow & { role: string }>("/orgs", { method: "POST", body: JSON.stringify({ name }) });
}

export async function listMyOrgs() {
  return apiFetch<{ orgs: OrgRow[]; count: number }>("/orgs");
}

export async function getOrg(orgId: string) {
  return apiFetch<OrgRow & { members: OrgMember[] }>("/orgs/" + orgId);
}

export async function addOrgMember(orgId: string, email: string, role: string) {
  return apiFetch<{ org_id: string; user_id: string; role: string }>(
    `/orgs/${orgId}/members`, { method: "POST", body: JSON.stringify({ email, role }) });
}

export async function inviteOrgMember(orgId: string, email: string, role: string) {
  return apiFetch<{ invitation_id: string; org_id: string; email: string; role: string; expires_at: string; token: string }>(
    `/orgs/${orgId}/invitations`, { method: "POST", body: JSON.stringify({ email, role }) });
}

export async function listOrgInvitations(orgId: string) {
  return apiFetch<{ org_id: string; invitations: Array<{ invitation_id: string; email: string; role: string; status: string; expires_at: string }> }>(
    `/orgs/${orgId}/invitations`);
}

export async function revokeOrgInvitation(orgId: string, invitationId: string) {
  return apiFetch<{ org_id: string; invitation_id: string; revoked: boolean }>(
    `/orgs/${orgId}/invitations/${encodeURIComponent(invitationId)}`, { method: "DELETE" });
}

export async function acceptOrgInvitation(token: string) {
  return apiFetch<{ org_id: string; role: string; accepted: boolean }>(
    "/orgs/invitations/accept", { method: "POST", body: JSON.stringify({ token }) });
}

export async function removeOrgMember(orgId: string, userId: string) {
  return apiFetch<{ org_id: string; user_id: string; removed: boolean }>(
    `/orgs/${orgId}/members/${encodeURIComponent(userId)}`, { method: "DELETE" });
}

export async function setOrgMemberRole(orgId: string, userId: string, role: string) {
  return apiFetch<{ org_id: string; user_id: string; role: string }>(
    `/orgs/${orgId}/members/${encodeURIComponent(userId)}/role`, { method: "POST", body: JSON.stringify({ role }) });
}

export async function deleteOrg(orgId: string) {
  return apiFetch<{ org_id: string; deleted: boolean }>(`/orgs/${orgId}`, { method: "DELETE" });
}

export async function getOrgRuns(orgId: string, limit = 20) {
  return apiFetch<{ org_id: string; runs: OrgRunRow[]; count: number }>(`/orgs/${orgId}/runs?limit=${limit}`);
}


// ── Evaluation records API (Phase 12) ────────────────────────────────

/** A durable, evidence-based evaluation of one run. Verification comes
 * from the acceptance criteria + effect ledger (interaction ≠ business
 * verified); the scorer version and fixture hash travel with every record
 * so historical scores cannot be silently rewritten. */
export interface EvaluationRecord {
  evaluation_id: string;
  run_id: string;
  workflow_id: string | null;
  agent_name: string | null;
  model: string | null;
  success: boolean;
  total_tasks: number | null;
  completed_tasks: number | null;
  failed_tasks: number | null;
  needs_review_tasks: string[] | null;
  unverified_effects: string[] | null;
  partially_applied_effects: string[] | null;
  unattributed_effects: string[] | null;
  evidence_gaps?: string[] | null;
  measurement_quality?: Record<string, string> | null;
  recovery_attempts: number | null;
  successful_recoveries: number | null;
  tool_calls: number | null;
  unknown_tool_call_rate: number | null;
  invalid_argument_rate: number | null;
  tool_schema_violation_rate: number | null;
  latency_ms: number | null;
  token_usage_total: number | null;
  uncertain_effects: string[];
  failed_effects: string[];
  confirmed_effects: string[];
  task_verifications: { task_id: string; task_status: string;
                        verifier_verdict: string; criterion_count: number;
                        effect_count: number }[];
  fixture_hash: string | null;
  evaluator_version: string | null;
  created_at_epoch: number | null;
}

export async function getEvaluationsForRun(runId: string): Promise<{
  run_id: string; evaluations: EvaluationRecord[]; count: number;
}> {
  return apiFetch(`/evaluations/runs/${encodeURIComponent(runId)}`);
}

export async function getEvaluation(evaluationId: string): Promise<EvaluationRecord> {
  return apiFetch(`/evaluations/${encodeURIComponent(evaluationId)}`);
}

/** Aggregate metrics across recorded evaluations. Admin-only. */
export async function getEvaluationSummary(): Promise<{
  total: number; success_rate: number; avg_latency_ms: number;
  total_tokens: number; total_recoveries: number;
}> {
  return apiFetch("/evaluations/summary");
}

// ── Experiments API (Phase 13: controlled self-improvement) ──────────

export interface ExperimentThresholds {
  success_min: number;
  regression_max: number;
  regression_metrics: string[];
  cost_max_usd: number | null;
  max_cohort_fraction: number;
  max_cohort_size: number;
  critical_constraints: string[];
}

export interface ExperimentRecord {
  experiment_id: string;
  kind: string;
  target: string;
  hypothesis: string;
  expected_benefit: string;
  baseline_ref: string;
  candidate_ref: string;
  candidate_diff: string;
  dataset_ref: string;
  dataset_hash: string;
  grading_hash: string;
  thresholds: ExperimentThresholds;
  state: string;
  state_version: number;
  results: { baseline?: Record<string, number>;
             candidate?: Record<string, number> } | null;
  proposed_by: string;
  evaluated_by: string;
  approved_by: string;
  activated_by: string;
  rolled_back_by: string;
  cohort_size: number | null;
  promoted_dataset_hash: string | null;
  promoted_results: Record<string, unknown> | null;
  promoted_actor: string | null;
  deployment_version: string | null;
  rollback_reason: string | null;
  created_at: number;
  updated_at: number;
}

export async function listExperiments(): Promise<{ experiments: ExperimentRecord[]; count: number }> {
  return apiFetch("/experiments");
}

export async function getExperiment(experimentId: string): Promise<ExperimentRecord> {
  return apiFetch(`/experiments/${encodeURIComponent(experimentId)}`);
}

export async function startExperimentShadow(experimentId: string): Promise<ExperimentRecord> {
  return apiFetch(`/experiments/${encodeURIComponent(experimentId)}/shadow`, { method: "POST" });
}

export async function startExperimentCanary(experimentId: string, cohortSize: number): Promise<ExperimentRecord> {
  return apiFetch(`/experiments/${encodeURIComponent(experimentId)}/canary`, {
    method: "POST", body: JSON.stringify({ cohort_size: cohortSize }),
  });
}

export interface ActivationReceipt {
  experiment_id: string;
  prompt_id: string;
  candidate_version_id: string;
  state: "pending" | "effective" | "failed";
  active_version_id?: string | null;
  error?: string | null;
}

export async function promoteExperiment(experimentId: string, deploymentVersion: string,
                                        totalPopulation?: number): Promise<ExperimentRecord & {
  activation_receipt?: ActivationReceipt; reconciled?: boolean;
}> {
  const body: Record<string, unknown> = { deployment_version: deploymentVersion };
  if (typeof totalPopulation === "number") body.total_population = totalPopulation;
  return apiFetch(`/experiments/${encodeURIComponent(experimentId)}/promote`, {
    method: "POST",
    body: JSON.stringify(body),
  });
}

export async function rollbackExperiment(experimentId: string, reason: string): Promise<ExperimentRecord> {
  return apiFetch(`/experiments/${encodeURIComponent(experimentId)}/rollback`, {
    method: "POST", body: JSON.stringify({ reason }),
  });
}

// ── Phase 14: contextual routing telemetry ───────────────────────────

export interface RoutingDriftEntry {
  context_key: string;
  recent_mean_reward: number;
  long_run_mean_reward: number;
  recent_attempts: number;
  drift_detected: boolean;
}

export async function getRoutingDrift(): Promise<{
  contexts: RoutingDriftEntry[]; drift_detected: boolean;
}> {
  return apiFetch("/usage/routing/drift");
}

export interface RoutingHealth {
  total_decisions: number;
  with_feedback: number;
  missing_feedback: number;
  distinct_actions: number;
}

// §14.34: the decision-log health — coverage of verified feedback over
// the logged selections (missing feedback is EXPLICIT, never dropped).
export async function getRoutingHealth(): Promise<RoutingHealth> {
  return apiFetch("/usage/routing/health");
}

// ── Phase 15 §15.25: the multi-agent workspace ───────────────────────

export interface DelegationRow {
  contract_id: string;
  parent_id: string;
  role: string;
  title: string;
  responsibility: string;
  goal: string;
  state: string;
  budget_units: number;
  spent_units: number;
  deadline_at: number;
  depth: number;
  result_keys: string[];
  error: string | null;
}

export async function getDelegations(runId: string): Promise<{
  delegations: DelegationRow[]; available: boolean; run_id: string;
}> {
  return apiFetch(`/workflows/${encodeURIComponent(runId)}/delegations`);
}

export async function cancelDelegation(runId: string, contractId: string): Promise<{
  contract_id: string; state: string; children_cancelled: number;
}> {
  return apiFetch(`/workflows/${encodeURIComponent(runId)}/delegations/${encodeURIComponent(contractId)}/cancel`, {
    method: "POST",
  });
}

// ── Phase 16 §16.27: the memory workspace ────────────────────────────

export interface MemoryRecord {
  id: string;
  content: string;
  memory_class: string;
  scope: string;
  verification_status: string;
  sensitivity: string;
  source: string;
  created_at: string;
  subject_key?: string | null;
  superseded_by?: string | null;
  deletion_state?: string;
}

export async function listMemories(limit = 100): Promise<{
  memories: MemoryRecord[]; total?: number;
}> {
  return apiFetch(`/memory?limit=${limit}`);
}

export async function forgetMemory(memoryId: string): Promise<unknown> {
  return apiFetch(`/memory/${encodeURIComponent(memoryId)}/forget`, {
    method: "POST",
  });
}

export async function correctMemory(memoryId: string, content: string): Promise<unknown> {
  return apiFetch(`/memory/${encodeURIComponent(memoryId)}/correct`, {
    method: "POST", body: JSON.stringify({ content }),
  });
}

export async function exportMemories(): Promise<unknown> {
  return apiFetch(`/memory/export`);
}

// ── Phase 17 §17.21: the artifact evidence API ───────────────────────

export interface DeliverableRecord {
  deliverable_id: string;
  deliverable_key: string;
  revision: number;
  parent_revision: number | null;
  deliverable_type: string;
  lifecycle_state: string;
  file_manifest: Array<{ path: string; sha256?: string; byte_length?: number }>;
  content_checksum: string;
  verification: {
    verdict?: string; verifier_version?: string;
    checks?: Array<{ check: string; status: string; detail?: string }>;
    blocking_reasons?: string[];
    unsupported_claims?: Array<{ field: string; reason: string }>;
    stale?: boolean; stale_reason?: string; stale_since?: number;
    reviewed_claims?: Array<{ field: string; action: string; revision: number }>;
  };
  lineage: { source_memory_ids?: string[]; generator_version?: string };
  updated_at: number;
}

export async function getDeliverable(deliverableId: string): Promise<DeliverableRecord> {
  return apiFetch(`/deliverables/${encodeURIComponent(deliverableId)}`);
}

export async function listDeliverables(): Promise<{ deliverables: DeliverableRecord[] }> {
  return apiFetch(`/deliverables`);
}

// §17.22: field-level review actions, tied to the EXACT revision so a
// delayed response cannot approve a newer draft accidentally.
export async function reviewDeliverableClaim(deliverableId: string, revision: number,
                                             action: "confirm" | "remove" | "edit",
                                             payload: { field: string; value?: string }): Promise<unknown> {
  return apiFetch(`/deliverables/${encodeURIComponent(deliverableId)}/review`, {
    method: "POST",
    body: JSON.stringify({ revision, action, ...payload }),
  });
}

// ── §28.7: chat link previews ──────────────────────────────────────────
// Hovering a chat link fetches BOUNDED ANONYMOUS metadata only — the
// preview route creates no browser session, no model usage, no build and
// no tool execution.

export interface LinkPreview {
  requested_url: string;
  display_url: string;
  final_host: string;
  state: "loading" | "ready" | "limited" | "unavailable";
  title?: string | null;
  description?: string | null;
  favicon_url?: string | null;
  thumbnail_url?: string | null;
  content_type?: string | null;
  expires_at?: number;
  private?: boolean;
}

export async function getLinkPreview(url: string, opts?: { signal?: AbortSignal }) {
  return apiFetch<LinkPreview>("/link-previews", {
    method: "POST",
    body: JSON.stringify({ url, private: false }),
    signal: opts?.signal,
  });
}

export async function getPrivateLinkPreview(url: string, opts?: { signal?: AbortSignal }) {
  return apiFetch<LinkPreview>("/link-previews", {
    method: "POST",
    body: JSON.stringify({ url, private: true }),
    signal: opts?.signal,
  });
}

/** §28.8: the dock's authenticated FILE fetch — raw bytes (never
 * JSON-parsed), bearer headers, caller-provided abort. Returns the Blob
 * for object-URL previews. */
export async function fetchArtifactBlob(
  artifactId: string, filePath: string, opts?: { signal?: AbortSignal },
): Promise<Blob> {
  const response = await fetch(
    `${API_BASE}/artifacts/${artifactId}/files/${filePath}?disposition=preview`,
    { headers: authHeaders(), credentials: "include", signal: opts?.signal });
  if (!response.ok) {
    const error = new ApiError(`artifact fetch failed: ${response.status}`);
    error.status = response.status;
    throw error;
  }
  return response.blob();
}
