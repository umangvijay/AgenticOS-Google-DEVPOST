import { describe, it, expect } from "vitest";
import { scheduleIsEnabled, Schedule } from "../src/lib/api";

/**
 * The audit's Round-3 contract fix: the backend's source of truth is the
 * `status` string; `is_enabled` is derived. Both shapes must agree, and a
 * missing status must default to ACTIVE (not the historical "everything
 * looks Paused" bug).
 */
describe("scheduleIsEnabled", () => {
  const base = {
    schedule_id: "s1", name: "n", goal: "g", cron_expression: "0 9 * * 1",
    timezone: "UTC", created_at: "2026-10-17T00:00:00Z",
  } satisfies Schedule;

  it("prefers the boolean flag when present", () => {
    expect(scheduleIsEnabled({ ...base, status: "PAUSED", is_enabled: true })).toBe(true);
    expect(scheduleIsEnabled({ ...base, status: "ACTIVE", is_enabled: false })).toBe(false);
  });

  it("derives from status when the flag is missing", () => {
    expect(scheduleIsEnabled({ ...base, status: "ACTIVE" })).toBe(true);
    expect(scheduleIsEnabled({ ...base, status: "PAUSED" })).toBe(false);
    expect(scheduleIsEnabled({ ...base, status: "paused" })).toBe(false); // case-insensitive
  });

  it("defaults to ACTIVE when neither is set (historical bug: everything showed Paused)", () => {
    expect(scheduleIsEnabled({ ...base, status: undefined, is_enabled: undefined })).toBe(true);
  });
});
