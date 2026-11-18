"""Phase 14 finding I: browser validation from AUTHORITATIVE state.

The verifier's browser_postcondition criterion reads the runtime-
captured observation (browser_evidence: observed URL/title/element
digest), never the model's claimed narrative alone. Without the
runtime evidence the criterion is UNVERIFIABLE — a model cannot attest
its own browser postcondition.
"""
from backend.services.outcome_verifier import AcceptanceCriterion


def _evidence(**over):
    ev = {"final_url": "https://acme.test/invoices/123",
          "page_title": "Invoice #123 — Acme",
          "observation_digest": "abc123def456",
          "element_count": 42,
          "executed_step_count": 3,
          "captured_at": 1793800000}
    ev.update(over)
    return ev


def test_no_browser_evidence_is_unverifiable_never_pass():
    """A claimed result without runtime evidence cannot pass."""
    c = AcceptanceCriterion(kind="browser_postcondition", value={})
    ok, why = c.check({"reply": "I definitely created the invoice",
                       "result": "success"})
    assert ok is False
    assert "UNVERIFIABLE" in why


def test_postcondition_reads_observed_url_and_title_not_narrative():
    """The marker must appear in the observed URL/title — a model's
    narrative claiming the marker does NOT satisfy the criterion."""
    c = AcceptanceCriterion(kind="browser_postcondition",
                            value={"marker": "invoice #123"})
    output = {"browser_evidence": _evidence(),
              "result": "the invoice #123 was created"}
    ok, why = c.check(output)
    assert ok is True   # the OBSERVED title carries the marker
    # Same claim, but the authoritative page shows something else → fail.
    output_bad = {"browser_evidence": _evidence(
        final_url="https://acme.test/login",
        page_title="Sign in — Acme"),
        "result": "the invoice #123 was created"}
    ok2, why2 = c.check(output_bad)
    assert ok2 is False
    assert "not present in the observed URL/title" in why2


def test_url_contains_and_min_steps_gates():
    c = AcceptanceCriterion(kind="browser_postcondition",
                            value={"url_contains": "/invoices/",
                                   "min_executed_steps": 3})
    ok, _ = c.check({"browser_evidence": _evidence()})
    assert ok is True
    ok2, why2 = c.check({"browser_evidence": _evidence(
        executed_step_count=1)})
    assert ok2 is False and "executed" in why2
    ok3, why3 = c.check({"browser_evidence": _evidence(
        final_url="https://acme.test/home")})
    assert ok3 is False and "/invoices/" in why3


def test_marker_in_narrative_only_never_passes():
    """The core §14.I rule: the model SAYS the postcondition, the page
    does not show it → the criterion fails."""
    c = AcceptanceCriterion(kind="browser_postcondition",
                            value={"marker": "payment confirmed"})
    ok, why = c.check({"browser_evidence": _evidence(
        final_url="https://shop.test/cart",
        page_title="Your cart"),
        "result": "payment confirmed! order complete"})
    assert ok is False
