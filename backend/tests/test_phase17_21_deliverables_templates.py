"""Phase 17 + 21 acceptance tests: deliverable provenance and product
expansion (workflow templates + dry runs)."""
import pytest
import pytest_asyncio

from backend.repositories.sqlite.database import DatabaseManager
from backend.services.provenance import (
    ProvenanceReport, deliverable_checksum, track_provenance,
)
from backend.services.workflow_templates import (
    WorkflowTemplateService, dry_run_workflow,
)

RESUME_SOURCE = """
John Doe — Senior Engineer at Acme Corp, 2019-2024.
BSc Computer Science, MIT, 2015.
Led the migration of the billing pipeline to Go.
Skills: Python, Go, Kubernetes.
"""

RESUME_DATA = {
    "basics": {"name": "John Doe", "title": "Senior Engineer"},
    "experience": [{
        "company": "Acme Corp",
        "period": "2019-2024",
        "achievements": ["Led the migration of the billing pipeline to Go"],
    }],
    "education": [{"school": "MIT", "degree": "BSc Computer Science",
                   "graduation": "2015"}],
    "skills": ["Python", "Go", "Kubernetes", "Quantum Blockchain"],
}


# ── Phase 17 §17.3: factual provenance ─────────────────────────────────

def test_supported_facts_cite_their_sources():
    report = track_provenance(RESUME_DATA, {"resume.txt": RESUME_SOURCE})
    by_path = {f.field_path: f for f in report.facts}
    acme = by_path["experience[0].company"]
    assert acme.supported is True
    assert "resume.txt" in acme.source_refs
    assert acme.claim_type == "employer"
    mit = by_path["education[0].school"]
    assert mit.supported is True and mit.claim_type == "qualification"


def test_unsupported_fact_is_flagged_not_silent():
    """§17.3 acceptance: 'Quantum Blockchain' appears NOWHERE in the
    source — the fabricated skill is flagged."""
    report = track_provenance(RESUME_DATA, {"resume.txt": RESUME_SOURCE})
    flagged = [f for f in report.unsupported]
    assert any("Quantum Blockchain" in f.value for f in flagged), \
        "an added factual claim must be flagged"
    assert all(f.claim_type == "skill" for f in flagged)


def test_rephrasing_of_supported_content_stays_supported():
    """§12.27: rephrasing (same underlying source content) is allowed;
    ADDING a claim is not — the report distinguishes them."""
    rephrased = {"basics": {"summary":
                   "John Doe: Senior Engineer at Acme Corp since 2019"}}
    report = track_provenance(rephrased, {"resume.txt": RESUME_SOURCE})
    summary = report.facts[0]
    assert summary.supported is True


def test_missing_source_flags_everything():
    report = track_provenance(RESUME_DATA, {})
    assert len(report.unsupported) == len(report.facts)


def test_source_revisions_recorded():
    report = track_provenance(RESUME_DATA, {"resume.txt": RESUME_SOURCE})
    assert report.source_revisions and report.source_revisions[0].startswith("resume.txt:")


def test_checksum_is_content_addressed():
    a = deliverable_checksum({"x": 1})
    b = deliverable_checksum({"x": 1})
    c = deliverable_checksum({"x": 2})
    assert a == b and a != c


# ── Phase 21 §21.1: versioned workflow templates ───────────────────────

@pytest_asyncio.fixture
async def templates(tmp_path):
    db = DatabaseManager(str(tmp_path / "templates.db"))
    await db.initialize()
    svc = WorkflowTemplateService(db)
    try:
        yield svc
    finally:
        await db.close()


def _definition(agent="core.search"):
    return {"tasks": [
        {"task_id": "t1", "agent": agent, "input_data": {"q": "{{query}}"},
         "dependencies": []},
        {"task_id": "t2", "agent": "core.email",
         "input_data": {"to": "{{recipient}}"}, "dependencies": ["t1"]},
    ]}


@pytest.mark.asyncio
async def test_template_creation_and_versioning(templates):
    created = await templates.create_template(
        tenant_id="u1", name="Weekly report",
        definition=_definition(), required_capabilities=["email.send"],
        config_schema={"required": ["query", "recipient"]},
        created_by="u1")
    assert created["version"] == 1

    # Preview: actions + permissions BEFORE creating a run (§21.1).
    preview = dry_run_workflow(created["definition"])
    assert preview["dry_run"] is True
    effects = [e["effect"] for e in preview["external_effects"]]
    assert any("email" in e for e in effects), \
        "the preview names the external effect"

    # Editing creates a NEW VERSION; the old version stays readable.
    updated = await templates.update_template(
        created["template_id"],
        definition=_definition(agent="core.http"), updated_by="u1")
    assert updated["version"] == 2
    old = await templates.get_template(created["template_id"], version=1)
    assert old["definition"]["tasks"][0]["agent"] == "core.search", \
        "v1 is immutable — existing runs keep their meaning"
    assert updated["definition"]["tasks"][0]["agent"] == "core.http"


@pytest.mark.asyncio
async def test_template_list_shows_latest_version_only(templates):
    t = await templates.create_template(
        tenant_id="u2", name="n", definition=_definition(),
        required_capabilities=[], config_schema={}, created_by="u2")
    await templates.update_template(t["template_id"],
                                    definition=_definition(agent="core.http"),
                                    updated_by="u2")
    rows = await templates.list_templates("u2")
    assert len(rows) == 1
    assert rows[0]["version"] == 2


# ── Phase 21 §21.2: the dry run ────────────────────────────────────────

def test_dry_run_names_external_effects_and_estimate():
    proposal = dry_run_workflow(_definition())
    assert proposal["dry_run"] is True
    assert proposal["task_count"] == 2
    ids = [e["task_id"] for e in proposal["external_effects"]]
    assert "t2" in ids, "the email task would write externally"
    est = proposal["cost_estimate"]
    assert est["kind"] == "ESTIMATE"
    assert est["max"] > est["min"] > 0
    assert est["uncertainty"], "uncertainty is explicit"


def test_dry_run_has_no_side_effects():
    """§21.2: the proposal never executes — same input twice yields the
    same pure plan and touches nothing."""
    d = _definition()
    a = dry_run_workflow(d)
    b = dry_run_workflow(d)
    assert a == b


def test_dry_run_read_only_agents_have_no_effects():
    d = {"tasks": [{"task_id": "t1", "agent": "core.search",
                    "input_data": {}, "dependencies": []}]}
    proposal = dry_run_workflow(d)
    assert proposal["external_effects"] == []
