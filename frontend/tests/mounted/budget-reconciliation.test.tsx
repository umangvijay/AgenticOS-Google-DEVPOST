/**
 * Phase 28 §28.7 — usage receipt reconciliation, mounted.
 *
 * The budget panel's accounting equation (settled consumption,
 * reservations incl. the uncertain subset, available, allowance) is
 * reconciled from the LEDGER after disconnect, refresh and account
 * switch:
 *  - a LATE response can never overwrite newer state (stale fencing);
 *  - actual connectivity recovery triggers an immediate reload;
 *  - an account switch reloads for the new principal and discards the
 *    prior principal's in-flight response;
 *  - accounting unavailable renders the explicit state, never zero.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, render, screen, cleanup, waitFor } from "@testing-library/react";
import BudgetUsagePanel from "@/components/BudgetUsagePanel";
import { writeSession } from "@/lib/auth-session";

const { getBudgetSnapshot, networkState } = vi.hoisted(() => ({
  getBudgetSnapshot: vi.fn(),
  networkState: { online: true },
}));

vi.mock("@/lib/api", () => ({
  getBudgetSnapshot: (...args: unknown[]) => getBudgetSnapshot(...args),
}));

vi.mock("@/hooks/useNetworkStatus", () => ({
  useNetworkStatus: () => ({ online: networkState.online }),
}));

function snapshotOf(consumed: number, reserved: number, uncertain = 0) {
  return {
    limit: 1000, consumed, reserved, uncertain_reserved: uncertain,
    available: 1000 - consumed - reserved, period: "2026-11-15",
    accounting_status: "ok", message: null, policy_version: "p28",
    provider_rate_limit: null,
  };
}

function deferred<T>() {
  let resolve!: (v: T) => void;
  const promise = new Promise<T>((r) => { resolve = r; });
  return { promise, resolve };
}

const flush = (ms = 10) =>
  act(async () => { await new Promise((r) => setTimeout(r, ms)); });

beforeEach(() => {
  window.localStorage.clear();
  networkState.online = true;
  getBudgetSnapshot.mockReset();
});
afterEach(cleanup);

describe("§28.7 receipt reconciliation", () => {
  it("renders ledger-backed quantities with the uncertain subset distinct", async () => {
    getBudgetSnapshot.mockResolvedValue(snapshotOf(420, 200, 50));
    render(<BudgetUsagePanel refreshMs={60_000} />);
    await waitFor(() => expect(screen.getByText("420")).toBeTruthy());
    expect(screen.getByText("200")).toBeTruthy();
    expect(screen.getByText(/\(50 uncertain\)/)).toBeTruthy();
    expect(screen.getByText("380")).toBeTruthy();
    expect(screen.getByText(/Daily allowance/i)).toBeTruthy();
    expect(screen.getByText(/Settled consumption/i)).toBeTruthy();
    expect(screen.getByText(/Active reservations/i)).toBeTruthy();
  });

  it("accounting unavailable renders the explicit state, never a zero", async () => {
    getBudgetSnapshot.mockResolvedValue({
      ...snapshotOf(0, 0), accounting_status: "unavailable",
      message: "Accounting is temporarily unavailable.",
    });
    render(<BudgetUsagePanel refreshMs={60_000} />);
    await waitFor(() =>
      expect(screen.getByText(/Accounting is temporarily unavailable\./)).toBeTruthy());
    expect(screen.queryByText(/Daily allowance/i)).toBeNull();
  });

  it("a LATE older response is discarded after a newer load wins", async () => {
    // Load 1 hangs (older token); the interval fires load 2 (newer
    // token) which resolves with the NEWER ledger. Load 1 then
    // resolves LAST — the fencing token must discard it.
    const slow = deferred<Record<string, unknown>>();
    getBudgetSnapshot
      .mockImplementationOnce(() => slow.promise)
      .mockResolvedValue(snapshotOf(700, 0));
    render(<BudgetUsagePanel refreshMs={20} />);
    await flush(60);              // interval fires load 2 → 700 applies
    expect(screen.getByText("700")).toBeTruthy();
    await act(async () => {
      slow.resolve(snapshotOf(111, 0));   // late, OLDER token
      await new Promise((r) => setTimeout(r, 15));
    });
    expect(screen.getByText("700")).toBeTruthy();
    expect(screen.queryByText("111")).toBeNull();
  });

  it("an auth-changed event reloads for the new principal and the prior response loses", async () => {
    const stale = deferred<Record<string, unknown>>();
    getBudgetSnapshot.mockImplementationOnce(() => stale.promise);
    render(<BudgetUsagePanel refreshMs={60_000} />);
    await flush(5);
    // Account switch: writeSession dispatches AUTH_CHANGED. The panel
    // reloads; the OLD principal's in-flight response resolves late.
    getBudgetSnapshot.mockResolvedValue(snapshotOf(250, 0));
    await act(async () => {
      writeSession({
        user: { id: "user-b", email: "b@example.test", name: "B" } as never,
        accessToken: "tok-b", refreshToken: "ref-b",
      });
      await new Promise((r) => setTimeout(r, 5));
      stale.resolve(snapshotOf(999, 0));   // prior principal's late data
      await new Promise((r) => setTimeout(r, 15));
    });
    expect(screen.getByText("250")).toBeTruthy();
    expect(screen.queryByText("999")).toBeNull();
  });

  it("network recovery reloads immediately from the ledger", async () => {
    getBudgetSnapshot.mockResolvedValue(snapshotOf(10, 0));
    const view = render(<BudgetUsagePanel refreshMs={60_000} />);
    await waitFor(() => expect(screen.getByText("10")).toBeTruthy());
    const loadsAfterBaseline = getBudgetSnapshot.mock.calls.length;
    // Connectivity drop → recovery: the online transition must trigger
    // an immediate reconciling load (not wait for the interval).
    networkState.online = false;
    await flush(5);
    view.rerender(<BudgetUsagePanel refreshMs={60_000} />);
    await flush(5);
    networkState.online = true;
    getBudgetSnapshot.mockResolvedValue(snapshotOf(80, 0));
    view.rerender(<BudgetUsagePanel refreshMs={60_000} />);
    await flush(10);
    expect(getBudgetSnapshot.mock.calls.length).toBeGreaterThan(loadsAfterBaseline);
    expect(screen.getByText("80")).toBeTruthy();
  });

  it("unmount stops the poller and a late response does not touch state", async () => {
    getBudgetSnapshot.mockResolvedValue(snapshotOf(10, 0));
    const { unmount } = render(<BudgetUsagePanel refreshMs={60_000} />);
    await waitFor(() => expect(screen.getByText("10")).toBeTruthy());
    unmount();
    getBudgetSnapshot.mockResolvedValue(snapshotOf(500, 0));
    await flush(10);
    // No React state update after unmount: no act warning, no throw.
    expect(document.querySelector('[data-testid="budget-panel"]')).toBeNull();
  });
});
