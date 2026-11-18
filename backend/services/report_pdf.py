"""§28.8 — the workspace report PDF.

Generates a verified, immutable PDF deliverable from verified structured
rows. The publication path REUSES the staged deliverable publisher
(checksum → validate → store → verify → publish) and the exported-file
verifier — a PDF whose parsed content contradicts the verified rows is
never published, and an HTML fallback from the renderer fails the
publication EXPLICITLY instead of shipping the wrong bytes.
"""
from __future__ import annotations

import html
import logging
from typing import Any, Dict, List

logger = logging.getLogger("report_pdf")


class ReportPdfError(ValueError):
    """Publication refused — the renderer or verification failed."""


def report_html(*, title: str, as_of: str, currency: str,
                inclusion_rule: str, rows: List[Dict[str, Any]],
                total: str, source_revision: str) -> str:
    """The report document: table + text summary. Rows are the VERIFIED
    structured rows; everything user-visible is escaped."""
    rows_html = "".join(
        "<tr>"
        f"<td>{html.escape(str(r.get('invoice_id') or ''))}</td>"
        f"<td>{html.escape(str(r.get('customer') or ''))}</td>"
        f"<td>{html.escape(str(r.get('due_date') or ''))}</td>"
        f"<td style='text-align:right'>{html.escape(str(r.get('amount') or ''))}</td>"
        "</tr>"
        for r in rows)
    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>{html.escape(title)}</title>
<style>
  body {{ font-family: Helvetica, Arial, sans-serif; margin: 36px; color: #12202e; }}
  h1 {{ font-size: 20px; margin: 0 0 4px; }}
  .meta {{ font-size: 12px; color: #4a5c6e; margin-bottom: 18px; }}
  table {{ border-collapse: collapse; width: 100%; font-size: 13px; }}
  th, td {{ border: 1px solid #c3cfdb; padding: 7px 10px; text-align: left; }}
  th {{ background: #eef3f8; }}
  .total {{ margin-top: 14px; font-size: 15px; font-weight: 700; }}
  .rule {{ font-size: 12px; color: #4a5c6e; margin-top: 10px; }}
  .rev {{ font-size: 10px; color: #7c8b9c; margin-top: 24px; }}
</style></head><body>
<h1>{html.escape(title)}</h1>
<div class="meta">As of {html.escape(as_of)} &middot; Currency: {html.escape(currency)}</div>
<table>
<thead><tr><th>Invoice</th><th>Customer</th><th>Due date</th><th style="text-align:right">Amount</th></tr></thead>
<tbody>{rows_html or '<tr><td colspan="4">No matching invoices</td></tr>'}</tbody>
</table>
<div class="total">Total overdue: {html.escape(total)}</div>
<div class="rule">Inclusion rule: {html.escape(inclusion_rule)}</div>
<div class="rev">Source revision: {html.escape(source_revision)}</div>
</body></html>"""


def render_report_pdf(html_document: str) -> bytes:
    """Render via WeasyPrint. A renderer that cannot produce PDF (the
    HTML-fallback case) raises — the caller fails the publication
    EXPLICITLY rather than publishing HTML under a .pdf name."""
    try:
        from weasyprint import HTML
    except Exception as exc:  # noqa: BLE001 — an unavailable renderer is a refusal
        raise ReportPdfError(f"PDF renderer unavailable: {exc}") from exc
    import io
    buffer = io.BytesIO()
    HTML(string=html_document).write_pdf(buffer)
    pdf_bytes = buffer.getvalue()
    buffer.close()
    if not pdf_bytes.startswith(b"%PDF"):
        raise ReportPdfError("renderer produced a non-PDF payload")
    return pdf_bytes


async def verify_report_pdf(pdf_bytes: bytes, *, expected_invoice_ids: List[str],
                            expected_total: str) -> Dict[str, Any]:
    """The independent exported-file verification: the parsed PDF must
    name every expected invoice and the exact total."""
    from backend.services.rendered_document_verifier import (
        verify_exported_document)
    expected = list(expected_invoice_ids) + [expected_total]
    return await verify_exported_document(
        content=pdf_bytes, media_type="application/pdf", expected_text=expected)
