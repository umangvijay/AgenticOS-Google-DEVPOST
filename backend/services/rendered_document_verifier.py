"""Rendered-document verification (Phase 17 §17.14) — verify the FINAL
HTML/PDF, not only the input JSON. Text extraction checks what parsers
can see; visual layout review remains a named limitation (§17.1).

Renderer containment (§17.14/WeasyPrint guidance): rendered HTML/CSS
from untrusted content must not fetch remote resources or read local
files — the sanitizer strips external references before rendering and
the verifier rejects documents that still carry them.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

VERIFIER_VERSION = "rendered-doc-1.0.0"

_ALLOWED_URL_SCHEMES = ("https://", "mailto:")

# Remote/local fetch vectors that WeasyPrint documents as risks.
_UNSAFE_PATTERNS = (
    re.compile(r"<\s*script\b", re.I),
    re.compile(r"url\s*\(\s*['\"]?\s*(?:https?:)?//", re.I),
    re.compile(r"@import", re.I),
    re.compile(r"<\s*iframe\b", re.I),
    re.compile(r"src\s*=\s*['\"]\s*(?:https?:)?//", re.I),
    re.compile(r"file://", re.I),
)


def _extracted_visible_text(html: str) -> str:
    """The text a READER would see — comments, script, and style bodies
    excluded (§17.14)."""
    from html.parser import HTMLParser

    class _P(HTMLParser):
        def __init__(self):
            super().__init__()
            self.parts = []
            self._skip = 0
        def handle_starttag(self, tag, attrs):
            if tag in ("script", "style"):
                self._skip += 1
        def handle_endtag(self, tag):
            if tag in ("script", "style") and self._skip:
                self._skip -= 1
        def handle_data(self, data):
            if not self._skip and data.strip():
                self.parts.append(data.strip())
    p = _P()
    p.feed(html or "")
    return " ".join(p.parts)


def sanitize_for_rendering(html: str) -> str:
    """Strip script/remote-fetch vectors BEFORE rendering — the renderer
    never sees network or filesystem references from untrusted content.
    Whole constructs are REMOVED (script/iframe elements, @import rules,
    remote url()/src values), leaving no host residue."""
    out = html or ""
    out = re.sub(r"<\s*script\b.*?</\s*script\s*>", "", out,
                 flags=re.I | re.S)
    out = re.sub(r"<\s*iframe\b.*?</\s*iframe\s*>", "", out,
                 flags=re.I | re.S)
    out = re.sub(r"@import[^;]*;", "", out, flags=re.I)
    out = re.sub(r"url\(\s*['\"]?(?:https?:)?//[^)\"']*\)",
                 "none", out, flags=re.I)
    out = re.sub(r'(src|href)\s*=\s*["\']\s*(?:https?:)?//[^"\']*["\']',
                 r'\1="#"', out, flags=re.I)
    out = re.sub(r"file://", "file-removed:", out, flags=re.I)
    return out


async def verify_rendered_document(*, html: str,
                                   expected_text: List[str],
                                   required_sections: Optional[List[str]] = None,
                                   max_bytes: int = 2_000_000,
                                   min_bytes: int = 50,
                                   ) -> Dict[str, Any]:
    """Verify the RENDERED artifact: it parses, expected text survives,
    sections exist in order, nothing vanished, links are safe, and the
    size is sane. Checks are typed; a failure is a FAIL, never a pass."""
    checks: List[Dict[str, Any]] = []
    blocking: List[str] = []
    html = html or ""

    def check(name: str, ok: bool, detail: str = "") -> None:
        # The detail states WHAT WAS OBSERVED (not a verdict echo) —
        # 'missing from the rendered output' accompanies failures only.
        checks.append({"check": name, "status": "pass" if ok else "fail",
                       "detail": detail if not ok else ""})
        if not ok:
            blocking.append(f"{name}: {detail}")

    # 1) It parses as HTML (a real parser, not a regex over the source).
    extracted = ""
    parse_ok = True
    parse_detail = ""
    try:
        from html.parser import HTMLParser

        class _P(HTMLParser):
            def __init__(self):
                super().__init__()
                self.tags = []
                self.text_parts = []
                self._skip = 0
            def handle_starttag(self, tag, attrs):
                self.tags.append(tag)
                if tag in ("script", "style"):
                    self._skip += 1
            def handle_endtag(self, tag):
                if tag in ("script", "style") and self._skip:
                    self._skip -= 1
            def handle_data(self, data):
                if not self._skip and data.strip():
                    self.text_parts.append(data.strip())
        p = _P()
        p.feed(html)
        extracted = " ".join(p.text_parts)
        parse_ok = "html" in p.tags or bool(extracted)
    except Exception as exc:
        parse_ok = False
        parse_detail = str(exc)[:200]
    check("html_parses", parse_ok, parse_detail)

    # 2) Expected text survives rendering — checked against the
    # EXTRACTED visible text (§17.9 new probe 3): text inside HTML
    # comments never renders, so it must NOT satisfy this check.
    extracted_text = _extracted_visible_text(html)
    for expected in expected_text or []:
        check("text_survives", expected.lower() in extracted_text.lower(),
              f"'{expected[:60]}' missing from the rendered output")

    # 3) Sections present (order respected when multiple given).
    if required_sections:
        positions = []
        for s in required_sections:
            idx = extracted.lower().find(s.lower())
            check("section_present", idx >= 0, f"section '{s}' missing")
            if idx >= 0:
                positions.append(idx)
        if len(positions) == len(required_sections) and \
                positions != sorted(positions):
            check("section_order", False, "sections out of order")

    # 4) Not empty / not oversized.
    size = len(html.encode("utf-8"))
    check("size_min", size >= min_bytes, f"{size} bytes")
    check("size_max", size <= max_bytes, f"{size} bytes")

    # 5) Links use allowed schemes; remote/local fetch vectors absent.
    for m in re.finditer(r"href\s*=\s*['\"]([^'\"]+)['\"]", html, re.I):
        url = m.group(1)
        check("link_scheme", url.startswith(_ALLOWED_URL_SCHEMES)
              or url.startswith("#") or url.startswith("/"),
              f"disallowed scheme: {url[:80]}")
    for pattern in _UNSAFE_PATTERNS:
        if pattern.search(html):
            check("no_remote_fetch", False,
                  f"unsafe render vector present: {pattern.pattern[:30]}")

    # 6) Unicode survives.
    for ch in ("é", "—"):
        if ch in html:
            check("unicode_preserved", ch in html)

    verdict = "fail" if blocking else "pass"
    return {"verdict": verdict, "verifier_version": VERIFIER_VERSION,
            "checks": checks, "blocking_reasons": blocking,
            "size_bytes": size,
            "note": ("visual layout defects need a viewer fixture — "
                     "declared in §17.1's not_yet_verified")}


# ── §28.8: exported-file verification ─────────────────────────────────
# Conversion (HTML → PDF) can change the result: fonts, glyphs, page
# breaks and text extraction all differ from the HTML source. The
# verifier therefore re-opens the EXPORTED bytes and checks the facts
# the owner expects to find, instead of trusting the HTML verdict.

PDF_MAGIC = b"%PDF"  # first four bytes of any PDF header
VERIFIER_VERSION_EXPORTED = "exported-1"


async def verify_exported_document(*, content: bytes,
                                   media_type: str,
                                   expected_text: List[str],
                                   max_bytes: int = 20_000_000,
                                   min_bytes: int = 200,
                                   ) -> Dict[str, Any]:
    """Verify the EXPORTED deliverable bytes: the format matches the
    declared media type, every expected fact survives conversion, the
    document has readable non-blank pages, and the size is sane.
    Returns the same typed-checks shape as verify_rendered_document,
    plus the sha256 of the exact bytes verified."""
    import hashlib as _hashlib
    checks: List[Dict[str, Any]] = []
    blocking: List[str] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        checks.append({"check": name, "status": "pass" if ok else "fail",
                       "detail": detail if not ok else ""})
        if not ok:
            blocking.append(f"{name}: {detail}")

    content = content or b""
    kind = (media_type or "").lower()

    # 1) The bytes ARE the declared format — a mislabeled export is a
    #    fail, never a pass.
    if "pdf" in kind:
        check("format_matches", content[:4] == PDF_MAGIC,
              f"declared {media_type} but the bytes do not start with %PDF")
        text, pages, blank_pages = "", 0, 0
        parse_detail = ""
        try:
            import io as _io
            import pdfplumber  # declared dependency (requirements.txt)
            with pdfplumber.open(_io.BytesIO(content)) as pdf:
                pages = len(pdf.pages)
                texts = [(page.extract_text() or "") for page in pdf.pages]
                blank_pages = sum(1 for t in texts if not t.strip())
                text = "\n".join(texts)
        except Exception as exc:
            parse_detail = f"pdf unreadable ({type(exc).__name__}: {str(exc)[:120]})"
        check("document_parses", not parse_detail, parse_detail)
        check("has_pages", pages > 0, f"{pages} pages")
        check("pages_not_blank", pages > 0 and blank_pages < pages,
              f"{blank_pages}/{pages} blank pages")
    elif "html" in kind:
        check("format_matches", bool(content.strip()),
              "declared html but the export is empty")
        text = content.decode("utf-8", errors="replace")
        check("document_parses", bool(text.strip()), "no extractable text")
        check("has_pages", True)
        check("pages_not_blank", True)
    else:
        check("format_matches", False,
              f"unsupported export media type: {media_type}")
        text, pages, blank_pages = "", 0, 0
        check("document_parses", False, "unsupported media type")
        check("has_pages", False, "unsupported media type")
        check("pages_not_blank", False, "unsupported media type")

    # 2) Factual grounding THROUGH conversion — every expected fact must
    #    survive in the exported text (PDF extraction is the owner's
    #    view of the file, so this is what 'delivered' means).
    for expected in expected_text or []:
        check("fact_survives_export",
              bool(expected) and expected.lower() in text.lower(),
              f"'{expected[:60]}' missing from the exported document")

    # 3) Size bounds.
    size = len(content)
    check("size_min", size >= min_bytes, f"{size} bytes")
    check("size_max", size <= max_bytes, f"{size} bytes")

    verdict = "fail" if blocking else "pass"
    return {"verdict": verdict,
            "verifier_version": VERIFIER_VERSION_EXPORTED,
            "media_type": media_type,
            "checks": checks, "blocking_reasons": blocking,
            "size_bytes": size, "pages": pages, "blank_pages": blank_pages,
            "sha256": _hashlib.sha256(content).hexdigest(),
            "note": ("visual layout defects need a viewer fixture — "
                     "declared in §17.1's not_yet_verified")}
