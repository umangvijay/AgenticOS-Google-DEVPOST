"""
AgentOS — credential envelope migration (Phase 30 / 30.7 step 3)

Bounded, resumable backfill of locally-encrypted credentials into the
tenant/purpose-bound ``env1`` envelope. Readers were deployed first
(``decrypt_credential_payload`` reads every generation), so migrating a
record is safe at any time.

Per bounded page:
  - already env1        → counted, untouched (a replayed page is a no-op)
  - managed reference   → counted as ``managed`` (remote custody already
                          version-qualified or pinned at read time)
  - legacy vault value  → decrypted, re-sealed, verified for decrypt
                          EQUIVALENCE in memory, stored under
                          compare-and-set on the record's current value
Classification is separate for conflicts (concurrent rotation), key
unavailability and corrupt records. Only counts and record references
are emitted — never plaintext, never ciphertext.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from backend.security.key_envelope import (
    is_envelope, seal_for_credential, open_envelope,
)
from backend.security.secrets_vault import secrets_vault

logger = logging.getLogger(__name__)


@dataclass
class MigrationCheckpoint:
    """Resumable progress marker: owner + key offset into the listing."""
    owner_id: str
    next_offset: int = 0
    pages_done: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {"owner_id": self.owner_id, "next_offset": self.next_offset,
                "pages_done": self.pages_done}

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "MigrationCheckpoint":
        return cls(owner_id=data["owner_id"],
                   next_offset=int(data.get("next_offset", 0)),
                   pages_done=int(data.get("pages_done", 0)))


@dataclass
class PageReport:
    counts: Dict[str, int] = field(default_factory=lambda: {
        "scanned": 0, "already_envelope": 0, "migrated": 0,
        "managed": 0, "conflict": 0, "key_unavailable": 0, "corrupt": 0,
    })
    records: List[str] = field(default_factory=list)  # "owner:key#outcome"
    checkpoint: Optional[MigrationCheckpoint] = None
    done: bool = False


def _classify(decrypt_error: Exception) -> str:
    message = str(decrypt_error).lower()
    if "temporarily" in message or "unavailable" in message or "not initialized" in message:
        return "key_unavailable"
    return "corrupt"


async def backfill_owner_credentials(
    secrets_repo, owner_id: str, *,
    page_size: int = 25,
    checkpoint: Optional[MigrationCheckpoint] = None,
) -> PageReport:
    """Migrate at most ``page_size`` legacy records for one owner.

    Returns a report with the next checkpoint. Callers loop until
    ``report.done`` — restarting mid-page is safe (completed pages are
    idempotent under replay)."""
    report = PageReport()
    offset = checkpoint.next_offset if checkpoint else 0
    keys = await secrets_repo.list_secret_keys(owner_id)
    page = [k for k in keys if k.startswith("cred:")][offset:offset + page_size]
    report.checkpoint = MigrationCheckpoint(owner_id=owner_id)

    for key in page:
        report.counts["scanned"] += 1
        record_ref = f"{owner_id}:{key}"
        try:
            stored = await secrets_repo.get_secret(owner_id, key)
        except Exception as exc:
            report.counts["key_unavailable"] += 1
            report.records.append(f"{record_ref}#key_unavailable")
            logger.info("Envelope backfill read failed for %s (%s)", record_ref, type(exc).__name__)
            continue
        if not stored:
            continue
        if is_envelope(stored):
            report.counts["already_envelope"] += 1
            report.records.append(f"{record_ref}#already_envelope")
            continue
        if stored.startswith("gcp://"):
            report.counts["managed"] += 1
            report.records.append(f"{record_ref}#managed")
            continue
        name = key[len("cred:"):]
        try:
            plaintext = secrets_vault.decrypt(stored)
        except Exception as exc:
            report.counts[_classify(exc)] += 1
            report.records.append(f"{record_ref}#{_classify(exc)}")
            continue
        try:
            sealed = seal_for_credential(plaintext, owner_id=owner_id, name=name)
            # Decrypt EQUIVALENCE check in memory before the write — the
            # envelope must read back exactly the legacy plaintext.
            roundtrip = open_envelope(
                sealed, tenant_id=owner_id, credential_id=f"cred:{name}",
                purpose="credential-store",
            )
            if roundtrip != plaintext:
                report.counts["corrupt"] += 1
                report.records.append(f"{record_ref}#equivalence_failed")
                continue
        except Exception as exc:
            report.counts[_classify(exc)] += 1
            report.records.append(f"{record_ref}#{_classify(exc)}")
            continue
        cas = getattr(secrets_repo, "store_secret_if_unchanged", None)
        try:
            if cas is not None:
                written = await cas(owner_id, key, sealed, stored)
            else:
                await secrets_repo.store_secret(owner_id, key, sealed)
                reread = await secrets_repo.get_secret(owner_id, key)
                written = reread == sealed  # a concurrent rotation would differ
        except Exception as exc:
            report.counts[_classify(exc)] += 1
            report.records.append(f"{record_ref}#{_classify(exc)}")
            continue
        if written:
            report.counts["migrated"] += 1
            report.records.append(f"{record_ref}#migrated")
        else:
            # Concurrent write (rotation) between read and CAS — the
            # record is left for the next sweep; never overwritten.
            report.counts["conflict"] += 1
            report.records.append(f"{record_ref}#conflict")

    migrated_any = offset + page_size
    report.checkpoint.next_offset = migrated_any
    report.checkpoint.pages_done = (checkpoint.pages_done if checkpoint else 0) + 1
    report.done = migrated_any >= len(keys)
    logger.info(
        "Envelope backfill page owner=%s scanned=%d migrated=%d conflict=%d "
        "corrupt=%d unavailable=%d done=%s",
        owner_id, report.counts["scanned"], report.counts["migrated"],
        report.counts["conflict"], report.counts["corrupt"],
        report.counts["key_unavailable"], report.done,
    )
    return report
