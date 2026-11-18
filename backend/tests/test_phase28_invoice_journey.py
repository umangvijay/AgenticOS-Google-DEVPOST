"""Phase 28 §28.4–28.8 — the invoice-report acceptance journey.

A controlled ledger whose data CHANGES between runs; a verified report
(table, text, PDF) whose independently parsed PDF must match the source
data exactly. Boundaries covered:

- pagination with one qualifying invoice on page two,
- paid and due-today exclusions, declared as-of date, currency, total,
- one TRANSIENT page-two failure (retried within budget) and one
  PERMANENT revocation (refused, no partial report),
- a source change between runs (the second report differs),
- published PDF served through TWO authenticated actions (inline preview
  and attachment download) with IDENTICAL bytes,
- foreign-account denial on both actions.

A supplied compute fixture or a manually completed child cannot close
this journey — the verification reads the REAL published bytes against
the independent fixture reader's expected values.
"""
import asyncio
import json
import uuid

import pytest
import pytest_asyncio
from aiohttp import web

from backend.services.report_pdf import (
    ReportPdfError, render_report_pdf, report_html, verify_report_pdf)


# ── the controlled ledger ─────────────────────────────────────────────

class LedgerState:
    """The ledger dataset: explicit paid/due-today/overdue rows across
    two pages, with a mutable overdue amount on page two and failure
    injection switches the journey can toggle between runs."""

    def __init__(self):
        self.page2_amount = 815.25      # the CHANGE between runs
        self.fail_page2_once = False
        self.revoked = False
        self.requests: list[str] = []

    def rows(self):
        return [
            # page 1
            {"invoice_id": "INV-1001", "customer": "Northwind", "amount": 120.50,
             "due_date": "2026-11-01", "status": "unpaid", "page": 1},
            {"invoice_id": "INV-1002", "customer": "Acme Corp", "amount": 79.50,
             "due_date": "2026-11-05", "status": "unpaid", "page": 1},
            {"invoice_id": "INV-1003", "customer": "Initech", "amount": 300.00,
             "due_date": "2026-10-20", "status": "paid", "page": 1},
            {"invoice_id": "INV-1004", "customer": "Umbrella", "amount": 42.00,
             "due_date": "2026-11-13", "status": "unpaid", "page": 1},  # due TODAY
            # page 2
            {"invoice_id": "INV-2001", "customer": "Globex", "amount": self.page2_amount,
             "due_date": "2026-11-02", "status": "unpaid", "page": 2},
        ]


@pytest.fixture
def ledger():
    state = LedgerState()
    hit_counter = {"page2": 0}

    async def entries(request):
        page = int(request.rel_url.query.get("page", "1"))
        state.requests.append(f"page{page}")
        if request.headers.get("Authorization") != "Bearer scoped-ledger-token":
            return web.json_response({"error": "revoked or missing credential"}, status=401)
        if state.revoked:
            return web.json_response({"error": "credential revoked"}, status=403)
        if page == 2:
            hit_counter["page2"] += 1
            if state.fail_page2_once and hit_counter["page2"] == 1:
                return web.json_response({"error": "transient upstream failure"},
                                         status=503)
        rows = [r for r in state.rows() if r["page"] == page]
        return web.json_response({"page": page, "page_size": 4,
                                  "has_more": page < 2, "entries": rows})

    app = web.Application()
    app.router.add_get("/entries", entries)
    return state, app


@pytest_asyncio.fixture
async def ledger_site(ledger):
    state, app = ledger
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    yield {"base": f"http://127.0.0.1:{port}", "state": state}
    await runner.cleanup()


# ── the read-aggregate-report service (the vertical slice) ────────────

async def _read_page(base: str, page: int, token: str, *, attempts_left: int = 3):
    """The bounded read through outbound controls: transient failures
    retry with backoff; a permanent refusal is never retried into a
    partial report."""
    from backend.services.http_pool import get_client
    client = await get_client()
    for attempt in range(attempts_left):
        response = await client.request(
            method="GET", url=f"{base}/entries?page={page}",
            headers={"Authorization": f"Bearer {token}"}, timeout=10.0)
        if response.status_code == 200:
            return response.json()
        if response.status_code in (502, 503, 504):
            await asyncio.sleep(0.05 * (attempt + 1))  # bounded backoff
            continue
        raise PermissionError(f"ledger refused page {page}: HTTP {response.status_code}")
    raise TimeoutError(f"ledger page {page} stayed unavailable after {attempts_left} attempts")


async def build_overdue_report(base: str, token: str, *, as_of: str,
                               currency: str = "USD") -> dict:
    """Read ALL pages, apply the declared overdue rule, and return the
    verified rows + total. The inclusion rule: unpaid AND due before
    as-of (due-today is NOT overdue)."""
    page = 1
    entries: list[dict] = []
    while True:
        body = await _read_page(base, page, token)
        entries.extend(body["entries"])
        if not body.get("has_more"):
            break
        page += 1
    overdue = [e for e in entries
               if e["status"] == "unpaid" and e["due_date"] < as_of]
    overdue.sort(key=lambda e: e["invoice_id"])
    total = round(sum(e["amount"] for e in overdue), 2)
    return {"as_of": as_of, "currency": currency,
            "rows": [{"invoice_id": e["invoice_id"], "customer": e["customer"],
                      "due_date": e["due_date"],
                      "amount": f"{e['amount']:.2f}"} for e in overdue],
            "total": f"{total:.2f}"}


# ── the journey ───────────────────────────────────────────────────────

EXPECTED_PAGE1_IDS = ["INV-1001", "INV-1002"]


@pytest.mark.asyncio
async def test_invoice_report_matches_changing_source_data(ledger_site):
    base, state = ledger_site["base"], ledger_site["state"]

    # RUN 1 — page 2 amount 815.25 → expected overdue rows and total.
    report = await build_overdue_report(base, "scoped-ledger-token",
                                        as_of="2026-11-13")
    ids = [r["invoice_id"] for r in report["rows"]]
    assert ids == EXPECTED_PAGE1_IDS + ["INV-2001"], report["rows"]
    assert report["total"] == "1015.25"
    # The paid overdue invoice and the due-today invoice are excluded.
    assert "INV-1003" not in ids and "INV-1004" not in ids
    # Pagination actually crossed both pages.
    assert "page1" in state.requests and "page2" in state.requests

    # RUN 2 — the ledger CHANGES: page-two amount moves. The new report
    # must reflect the new data (an independently known total).
    state.page2_amount = 905.75
    report2 = await build_overdue_report(base, "scoped-ledger-token",
                                         as_of="2026-11-13")
    assert report2["total"] == "1105.75"
    assert [r["invoice_id"] for r in report2["rows"]] == \
        EXPECTED_PAGE1_IDS + ["INV-2001"]


@pytest.mark.asyncio
async def test_transient_page2_failure_retries_and_completes(ledger_site):
    base, state = ledger_site["base"], ledger_site["state"]
    state.fail_page2_once = True
    report = await build_overdue_report(base, "scoped-ledger-token",
                                        as_of="2026-11-13")
    assert report["total"] == "1015.25", \
        "the transient failure retried and the run completed with the full data"


@pytest.mark.asyncio
async def test_permanent_revocation_never_yields_a_partial_report(ledger_site):
    base, state = ledger_site["base"], ledger_site["state"]
    state.revoked = True
    with pytest.raises(PermissionError):
        await build_overdue_report(base, "scoped-ledger-token",
                                   as_of="2026-11-13")
    # No report object was produced at all — nothing to publish.


# ── publish + serve the verified PDF ──────────────────────────────────

def _publish_files(report: dict, source_revision: str):
    rows = report["rows"]
    title = "Overdue invoice report"
    doc_html = report_html(
        title=title, as_of=report["as_of"], currency=report["currency"],
        inclusion_rule="unpaid invoices due before the as-of date "
                       "(due-today excluded)",
        rows=rows, total=report["total"], source_revision=source_revision)
    pdf = render_report_pdf(doc_html)
    table_json = json.dumps(report, indent=2)
    text = (f"Overdue invoices as of {report['as_of']} "
            f"({report['currency']}): "
            + ", ".join(f"{r['invoice_id']} {r['amount']}" for r in rows)
            + f". Total {report['total']}.")
    files = [
        {"path": "report.pdf", "content": pdf},
        {"path": "report.json", "content": table_json},
        {"path": "report.md", "content": text},
    ]
    return pdf, files


@pytest.mark.asyncio
async def test_verified_pdf_served_identically_through_both_actions(
        ledger_site, tmp_path):
    from backend.services.artifact_builder import save_user_files
    base, state = ledger_site["base"], ledger_site["state"]
    owner = "owner-" + uuid.uuid4().hex

    # Publish from the verified report: parse-check the PDF BEFORE the
    # bytes are allowed to become a deliverable.
    report = await build_overdue_report(base, "scoped-ledger-token",
                                        as_of="2026-11-13")
    pdf, files = _publish_files(report, source_revision="ledger-snap-1")
    verification = await verify_report_pdf(
        pdf, expected_invoice_ids=[r["invoice_id"] for r in report["rows"]],
        expected_total=report["total"])
    assert verification["verdict"] == "pass", verification["blocking_reasons"]
    assert verification["pages"] >= 1 and verification["blank_pages"] == 0

    # The artifact store holds the published bytes; the durable staged
    # publication (candidate → verify → publish) is exercised by the
    # retained Phase 17/20 suites with a wired repo — here the contract
    # under test is the BYTE identity of the two authorized actions.
    metadata = save_user_files(owner, "invoice-report", files)
    artifact_id = metadata["artifact_id"]

    # The stored bytes hash back to the verified PDF (binary-safe write).
    from backend.services.artifact_builder import load_artifact
    manifest = load_artifact(owner, artifact_id)
    stored_sha = manifest["checksums"]["report.pdf"]
    assert stored_sha == hashlib_sha256(pdf)

    # BOTH authenticated actions serve IDENTICAL bytes.
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient
    from backend.api.routers.artifacts import router
    from backend.api.dependencies.auth import AuthenticatedUser, get_current_user

    def as_user(uid):
        return AuthenticatedUser(uid, f"{uid}@example.test", uid, "user", "local")

    app = FastAPI()
    app.include_router(router)

    async with AsyncClient(transport=ASGITransport(app=app),
                           base_url="http://fixture") as client:
        app.dependency_overrides[get_current_user] = lambda: as_user(owner)
        preview = await client.get(
            f"/artifacts/{artifact_id}/files/report.pdf?disposition=preview")
        download = await client.get(
            f"/artifacts/{artifact_id}/files/report.pdf?disposition=download")
        assert preview.status_code == 200 and download.status_code == 200
        # Identical immutable bytes on both actions.
        assert preview.content == download.content == pdf
        assert preview.headers["content-type"] == "application/pdf"
        assert preview.headers["content-disposition"].startswith("inline")
        assert download.headers["content-disposition"].startswith("attachment")
        assert "no-store" in preview.headers["cache-control"]
        assert "no-store" in download.headers["cache-control"]

        # A FOREIGN account is denied on both actions.
        app.dependency_overrides[get_current_user] = lambda: as_user(
            "stranger-" + uuid.uuid4().hex)
        foreign_preview = await client.get(
            f"/artifacts/{artifact_id}/files/report.pdf?disposition=preview")
        foreign_download = await client.get(
            f"/artifacts/{artifact_id}/files/report.pdf?disposition=download")
        assert foreign_preview.status_code == 404
        assert foreign_download.status_code == 404


def hashlib_sha256(content: bytes) -> str:
    import hashlib
    return hashlib.sha256(content).hexdigest()


@pytest.mark.asyncio
async def test_html_fallback_fails_publication_explicitly():
    """The renderer must never be allowed to publish HTML under a .pdf
    name — the fallback is an explicit refusal."""
    # A renderer that produced HTML bytes fails the exported verification
    # (format mismatch) — the publication refuses instead of shipping them.
    from backend.services.report_pdf import verify_report_pdf
    html_bytes = b"<html><body>not a pdf</body></html>"
    result = await verify_report_pdf(
        html_bytes, expected_invoice_ids=["INV-1001"], expected_total="1.00")
    assert result["verdict"] == "fail"
    assert any("format_matches" in b or "document_parses" in b
               for b in result["blocking_reasons"])
    # The real renderer's own guard: verify_report_pdf on a genuine PDF
    # that is MISSING an expected invoice still fails (the parsed content
    # must match the verified rows, not merely parse).
    report = {"rows": [{"invoice_id": "INV-1001", "customer": "A",
                        "due_date": "2026-11-01", "amount": "120.50"}],
              "total": "120.50", "as_of": "2026-11-13", "currency": "USD"}
    pdf, _ = _publish_files(report, source_revision="snap-x")
    wrong = await verify_report_pdf(
        pdf, expected_invoice_ids=["INV-1001", "INV-9999"],
        expected_total="120.50")
    assert wrong["verdict"] == "fail"
