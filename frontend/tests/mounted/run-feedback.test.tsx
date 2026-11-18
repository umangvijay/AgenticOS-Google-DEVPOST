import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import RunFeedbackPanel from "@/components/RunFeedbackPanel";
import { apiFetch } from "@/lib/api";

vi.mock("@/lib/api", () => ({ apiFetch: vi.fn() }));
vi.mock("@/lib/auth-context", () => ({ useAuth: () => ({ user: { id: "user-1" } }) }));
const api = vi.mocked(apiFetch);
beforeEach(() => api.mockReset());
afterEach(cleanup);

it("submits the user's actual input, displays triage status, and deletes feedback", async () => {
  let saved = false;
  api.mockImplementation(async (path, options) => {
    if (path === "/feedback" && options?.method === "POST") { saved = true; return { feedback_id: "fb1" }; }
    if (options?.method === "DELETE") return { deleted: "fb1" };
    return { feedback: saved ? [{ id: "fb1", category: "incorrect_result", triage_status: "received" }] : [] };
  });
  render(<RunFeedbackPanel runId="run-1" />);
  fireEvent.click(screen.getByText("Feedback on this run"));
  fireEvent.change(screen.getByLabelText("What happened?"), { target: { value: "The report omitted my last invoice" } });
  fireEvent.change(screen.getByLabelText("What did you expect?"), { target: { value: "Include all three invoices" } });
  fireEvent.click(screen.getByText("Send feedback"));
  await waitFor(() => expect(screen.getByText(/incorrect result — received/)).toBeTruthy());
  const sent = api.mock.calls.find(([path, options]) => path === "/feedback" && options?.method === "POST");
  expect(JSON.parse(sent![1]!.body as string)).toEqual({ run_id: "run-1", helpful: false,
    category: "incorrect_result", comment: "The report omitted my last invoice",
    expected_result: "Include all three invoices", share_diagnostics: false });
  fireEvent.click(screen.getByLabelText("Delete feedback fb1"));
  await waitFor(() => expect(screen.queryByText(/incorrect result — received/)).toBeNull());
});

it("preserves input and shows a backend submission failure", async () => {
  api.mockImplementation(async (_path, options) => {
    if (options?.method === "POST") throw new Error("Feedback store unavailable");
    return { feedback: [] };
  });
  render(<RunFeedbackPanel runId="run-2" />);
  fireEvent.click(screen.getByText("Feedback on this run"));
  fireEvent.change(screen.getByLabelText("What happened?"), { target: { value: "Keep my report" } });
  fireEvent.click(screen.getByText("Send feedback"));
  await waitFor(() => expect(screen.getByRole("alert").textContent).toContain("unavailable"));
  expect((screen.getByLabelText("What happened?") as HTMLTextAreaElement).value).toBe("Keep my report");
  expect(screen.queryByText(/Feedback received/)).toBeNull();
});

it("keeps the workspace usable while feedback history is unavailable", async () => {
  api.mockResolvedValueOnce({ ok: true }).mockResolvedValueOnce({
    feedback: [{ id: "recovered", category: "incorrect_result", triage_status: "received" }],
  });
  render(<RunFeedbackPanel runId="run-history" />);
  fireEvent.click(screen.getByText("Feedback on this run"));
  await waitFor(() => expect(screen.getByRole("alert").textContent).toContain("unavailable"));
  fireEvent.click(screen.getByRole("button", { name: "Refresh status" }));
  await waitFor(() => expect(screen.getByText(/incorrect result — received/)).toBeTruthy());
  expect(screen.queryByRole("alert")).toBeNull();
});
