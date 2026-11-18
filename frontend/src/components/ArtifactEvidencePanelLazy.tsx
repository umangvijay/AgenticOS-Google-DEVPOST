"use client";

/**
 * Deep-linkable evidence view (Phase 18 §18.21): mounts the evidence
 * panel for the deliverable named in the URL (?deliverable=...), with
 * generation-guarded loads and the stale-verification display.
 */
import { useSearchParams } from "next/navigation";
import { ArtifactEvidencePanel } from "./ArtifactEvidencePanel";

export function ArtifactEvidencePanelLazy() {
  const params = useSearchParams();
  const deliverableId = params?.get("deliverable") ?? null;
  if (!deliverableId) return null;   // absent when no deliverable is named
  return <ArtifactEvidencePanel deliverableId={deliverableId} />;
}
