/**
 * Phase 20 §20.16: BrowserSessionPanel mounted tests — the viewer/
 * takeover surface renders REAL server state, an ownership refusal is
 * an explicit message (never silent control), and action evidence shows
 * failed actions with their real errors.
 */
import { beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, fireEvent, waitFor, cleanup } from "@testing-library/react";
import { BrowserSessionPanel } from "@/components/BrowserSessionPanel";

vi.mock("@/lib/api", () => ({
  apiFetch: vi.fn(),
}));
vi.mock("@/lib/motion", () => ({
  decorativeMotionAllowed: () => false,
}));

import { afterEach } from "vitest";
import { apiFetch } from "@/lib/api";
const mockApi = apiFetch as unknown as ReturnType<typeof vi.fn>;

const CONTROLLED = {
  session_id: "bs-1",
  owner_user_id: "u1",
  controller: "user:u1",
  controller_epoch: "epoch-abc",
  url: "http://127.0.0.1:8081/form",
  role: "controller" as const,
  history: [
    { action: "goto", target: "http://127.0.0.1:8081/", status: "executed",
      executed: true, detail: "navigated", error: null },
    { action: "click", target: "#missing", status: "failed",
      executed: true, detail: "", error: "Timeout 10000ms exceeded" },
  ],
};

beforeEach(() => {
  mockApi.mockReset();
  mockApi.mockResolvedValue(null);
});
afterEach(cleanup);

describe("§20.16 BrowserSessionPanel mounted", () => {
  it("renders role, current url, and action evidence with real errors", async () => {
    mockApi.mockResolvedValueOnce(CONTROLLED);
    render(<BrowserSessionPanel sessionId="bs-1" />);
    await waitFor(() => screen.getByText("Browser session"));
    expect(screen.getByText("controller")).toBeTruthy();
    expect(screen.getByText(/On: http:\/\/127.0.0.1:8081\/form/)).toBeTruthy();
    expect(screen.getByText("goto")).toBeTruthy();
    expect(screen.getByText("click")).toBeTruthy();
    expect(screen.getByText(/Error: Timeout 10000ms exceeded/),
      "the FAILED action shows its REAL error — never rewritten to success").toBeTruthy();
    expect(screen.getAllByText("executed").length).toBeGreaterThan(0);
  });

  it("renders an explicit refusal when the backend denies control", async () => {
    mockApi.mockResolvedValueOnce({ ...CONTROLLED, role: "observer",
                                    controller: "agent" });
    mockApi.mockRejectedValueOnce(
      new Error("403: user u2 does not control session bs-2"));
    render(<BrowserSessionPanel sessionId="bs-2" />);
    await waitFor(() => screen.getByText("Take control"));
    fireEvent.click(screen.getByText("Take control"));
    await waitFor(() =>
      expect(screen.getByText(/do not control this session/i)).toBeTruthy(),
      { timeout: 3000 });
    // The refusal NEVER renders as control.
    expect(screen.queryByText("controller")).toBeNull();
  });

  it("observer role offers takeover and posts to the takeover endpoint", async () => {
    mockApi.mockResolvedValueOnce({ ...CONTROLLED, role: "observer",
                                    controller: "agent" });
    mockApi.mockResolvedValueOnce({ ok: true });           // takeover POST
    mockApi.mockResolvedValueOnce({ ...CONTROLLED });       // reload
    render(<BrowserSessionPanel sessionId="bs-3" />);
    await waitFor(() => screen.getByText("Take control"));
    fireEvent.click(screen.getByText("Take control"));
    await waitFor(() => expect(mockApi).toHaveBeenCalledWith(
      "/browser/sessions/bs-3/takeover", { method: "POST" }));
    const [path, options] = mockApi.mock.calls[1];
    expect(path).toBe("/browser/sessions/bs-3/takeover");
    expect(options.method).toBe("POST");
    await waitFor(() => screen.getByText("controller"));
  });

  it("viewer role states the read-only contract explicitly", async () => {
    mockApi.mockResolvedValueOnce({ ...CONTROLLED, role: "viewer" });
    render(<BrowserSessionPanel sessionId="bs-4" />);
    await waitFor(() => screen.getByText("viewer"));
    expect(screen.getByText(/viewing read-only/i)).toBeTruthy();
    expect(screen.queryByText("Take control")).toBeNull();
  });
});
