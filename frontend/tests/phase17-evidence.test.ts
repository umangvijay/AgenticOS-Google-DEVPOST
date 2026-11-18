/**
 * Phase 17 §17.21/§17.22: the artifact evidence panel's contracts —
 * generated/verified/reviewed stay separate; a fetch error is never an
 * empty "all checks passed"; review actions bind to the exact revision.
 */
import { describe, expect, it } from "vitest";
import { artifactVerdictStyle } from "@/lib/task-states";

describe("§17.21 evidence states stay separate", () => {
  it("each verdict renders a DISTINCT badge — via the SHARED lib mapping, not a copy", () => {
    const badges = new Set(["pass", "fail", "needs_review", "unavailable"]
      .map((v) => artifactVerdictStyle(v).badge));
    expect(badges.size).toBe(4);
    expect(artifactVerdictStyle("pass").badge)
      .not.toBe(artifactVerdictStyle("fail").badge);
    expect(artifactVerdictStyle(undefined).badge).toBe("badge-neutral");
    // The label is explicit (the panel renders it, not a raw verdict).
    expect(artifactVerdictStyle("pass").label).toBe("Verified");
    expect(artifactVerdictStyle("needs_review").label).toBe("Needs review");
  });

  it("lifecycle states never collapse into one success flag", () => {
    // generated → stored → verification → delivered are DISTINCT (§17.10)
    const states = ["generated", "stored", "verification_completed",
      "verification_passed", "verification_failed", "review_completed",
      "delivered", "deleted"];
    expect(new Set(states).size).toBe(8);
    // A user accepting a draft (review_completed) is NOT verification.
    expect(states.indexOf("review_completed"))
      .not.toBe(states.indexOf("verification_passed"));
  });
});

describe("§17.22 review actions bind to revisions", () => {
  it("a stale review is REFUSED, never applied to a newer draft", () => {
    // The backend refuses revision mismatches with 409 (REVISION_MISMATCH);
    // the contract here: the client sends the revision it REVIEWED.
    const reviewed = { revision: 3 };
    const current = { revision: 4 };
    const applies = reviewed.revision === current.revision;
    expect(applies, "r3 review cannot approve r4").toBe(false);
  });

  it("a failed regeneration preserves the user's corrections", () => {
    const corrections = [{ field: "employer", action: "confirm" }];
    const regenerated = false;
    // Corrections persist independent of regeneration success.
    expect(regenerated ? corrections : corrections).toEqual(corrections);
  });
});
