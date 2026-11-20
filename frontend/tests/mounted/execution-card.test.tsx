import { afterEach, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import WorkspaceExecutionCard from "@/components/WorkspaceExecutionCard";
import ExecutionTimeline from "@/components/ExecutionTimeline";
import type { WorkflowRun, WorkflowEvent } from "@/lib/api";

afterEach(cleanup);
const run: WorkflowRun = { run_id: "run", workflow_id: "wf", user_id: "u", goal: "Read a report", status: "COMPLETED", created_at: "2026-11-19",
  tasks: [{ task_id: "read", run_id: "run", workflow_id: "wf", agent: "core.chat", status: "COMPLETED", dependencies: [], attempt: 1,
    input_data: { url: "https://example.com/report" }, output_data: { reply: "Here is the report", sources: ["javascript:alert(1)", "https://name:secret@example.com", "https://example.com/report", { url: "https://example.org/source" }] } }] };
it("renders only real outputs and valid deduplicated web sources, with no diagnostic fetch on mount", () => {
  const diagnostics = vi.fn(() => <p>Diagnostics mounted</p>);
  render(<WorkspaceExecutionCard turns={[run]} events={[]} onClose={vi.fn()} onInspectRun={vi.fn()}>{diagnostics}</WorkspaceExecutionCard>);
  expect(screen.getByRole("link", { name: "Assistant response" }).getAttribute("href")).toBe("#result-run-read");
  expect(screen.getAllByRole("link").filter(a => a.getAttribute("href")?.startsWith("https://"))).toHaveLength(2);
  expect(screen.queryByText(/javascript|secret/)).toBeNull();
  expect(diagnostics).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole("button", { name: "Collapse execution card" }));
  expect(screen.getByRole("button", { name: "Expand execution card" }).getAttribute("aria-expanded")).toBe("false");
  expect(screen.queryByRole("link", { name: "Assistant response" })).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: "Expand execution card" }));
  expect(screen.getByRole("link", { name: "Assistant response" })).toBeTruthy();
});
it("does not invent files or sources for a queued run", () => {
  render(<WorkspaceExecutionCard turns={[{ ...run, status: "PENDING", tasks: [] }]} events={[]} onClose={vi.fn()} onInspectRun={vi.fn()} />);
  expect(screen.getByText("No saved outputs yet.")).toBeTruthy();
  expect(screen.getByText("No linked sources.")).toBeTruthy();
  expect(screen.getByRole("status").textContent).toBe("Queued");
});
it("groups task events by run and task, preserving saved state independently of event wording", () => {
  const event: WorkflowEvent = { event_id: "e", run_id: "run", workflow_id: "wf", task_id: "read", type: "TASK_STARTED", summary: "Task read started", timestamp: "invalid", sanitized_metadata: {} };
  const { container } = render(<ExecutionTimeline events={[event, { ...event, event_id: "e2", run_id: "another" }]} tasks={run.tasks} compact />);
  expect(container.querySelectorAll(".execution-step")).toHaveLength(2);
  expect(screen.getByText("Completed")).toBeTruthy();
  expect(container.querySelector("time")).toBeNull();
  expect(container.textContent).not.toContain("Invalid Date");
});
