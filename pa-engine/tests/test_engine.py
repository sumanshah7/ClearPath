import json
from datetime import date
from pathlib import Path

import pytest

from app.check.dates import at_least_ago, today, within
from app.check.insurer import ALLOWED
from app.check.pass_eval import evaluate
from app.check.quote import verify
from app.errors import ApiError
from app.fhir.builders import questionnaire, questionnaire_response
from app.fhir.validate import validate_resource
from app.ingest.grounding import check
from app.ingest.upload_validation import validate_bytes
from app.pipeline.cache import get, put
from app.pipeline.context_builder import clean
from app.pipeline.episodes import EpisodeRecorder
from app.repository import SCHEMA, get_repo, new_id


def test_upload_validation_rejects_non_pdf():
    with pytest.raises(ApiError) as caught:
        validate_bytes(b"not a pdf")
    assert caught.value.code == "INVALID_PDF"


def test_quote_exact_passes_and_paraphrase_fails():
    original = "Completed 7 weeks of physical therapy for lumbar radiculopathy."
    assert verify(original, original)
    assert not verify("Completed seven weeks of therapy.", original)
    assert not verify("short", original)


def test_pass_eval_tri_state_and_threshold():
    missing = evaluate(
        {"all": [{"q": "weeks", "op": "gte", "value": 6, "unit": "weeks", "fail": "Completed: {value} weeks; policy requires 6", "missing": "No documentation found that treatment lasted long enough"}]},
        {"weeks": {"value": None, "enabled": True}},
    )
    assert missing["status"] == "missing"
    assert missing["state"] is None
    short = evaluate(
        {"all": [{"q": "weeks", "op": "gte", "value": 6, "unit": "weeks", "fail": "Completed: {value} weeks; policy requires 6"}]},
        {"weeks": {"value": 3, "enabled": True}},
    )
    assert short["status"] == "missing"
    assert "3" in short["reason"]
    met = evaluate(
        {"all": [{"q": "weeks", "op": "gte", "value": 6, "unit": "weeks", "fail": "x"}]},
        {"weeks": {"value": 7, "enabled": True}},
    )
    assert met["status"] == "met"


def test_dates_use_demo_clock():
    assert today() == date(2026, 9, 27)
    assert within(date(2026, 9, 20), 6, "months")
    assert at_least_ago(date(2026, 7, 5), 6, "weeks")


def test_grounding_rejects_changed_number_and_missing_code():
    page = "4.1 Diagnosis of lumbar radiculopathy (M54.16) is documented."
    bad_number = check(
        {"evidence_text": page, "requirement_text": "Diagnosis requires 12 weeks.", "codes": ["M54.16"], "conditions": []},
        page,
    )
    assert not bad_number["passed"]
    bad_code = check(
        {"evidence_text": page, "requirement_text": "Diagnosis is documented.", "codes": ["Z99.9"], "conditions": []},
        page,
    )
    assert not bad_code["passed"]
    ok = check(
        {"evidence_text": page, "requirement_text": "Diagnosis of lumbar radiculopathy (M54.16) is documented.", "codes": ["M54.16"], "conditions": []},
        page,
    )
    assert ok["passed"]


def test_context_builder_strips_repeated_edges_and_keeps_protected_lines():
    pages = [
        {"page": 1, "text": "Running header\nCoverage criteria unless contraindicated\nPrinted page 1"},
        {"page": 2, "text": "Running header\nAnother rule line\nPrinted page 2"},
    ]
    cleaned, report = clean(pages)
    assert "Running header" not in cleaned[0]["text"]
    assert "unless" in cleaned[0]["text"]
    assert report["removed_lines"] >= 2


def test_locks_keep_human_edits():
    repo = get_repo()
    policy = repo.create_policy(
        {
            "document_role": "clinical_policy",
            "file_name": "t.pdf",
            "storage_path": "/tmp/t.pdf",
            "sha256": new_id(),
            "status": "draft",
        }
    )
    item = repo.insert_item(
        {
            "policy_id": policy["id"],
            "item_type": "rule",
            "item_key": "weeks",
            "seq": 1,
            "data": {"requirement_text": "6 weeks"},
            "original_data": {"requirement_text": "4 weeks"},
            "page": 2,
            "review_state": "edited",
            "edited_by_human": True,
        }
    )
    locked = repo.write_item(item["id"], {"data": {"requirement_text": "4 weeks"}}, actor="engine")
    assert locked["lock_preserved"] is True
    assert locked["data"]["requirement_text"] == "6 weeks"
    assert locked["suggestion"]["changes"]["data"]["requirement_text"] == "4 weeks"
    sibling = repo.insert_item(
        {
            "policy_id": policy["id"],
            "item_type": "rule",
            "item_key": "other",
            "seq": 2,
            "data": {"requirement_text": "old"},
            "original_data": {"requirement_text": "old"},
            "page": 3,
            "review_state": "pending_review",
            "edited_by_human": False,
        }
    )
    updated = repo.write_item(sibling["id"], {"data": {"requirement_text": "new"}}, actor="engine")
    assert updated["data"]["requirement_text"] == "new"


def test_episodes_are_append_only_and_ordered():
    repo = get_repo()
    recorder = EpisodeRecorder("policy", new_id(), new_id())
    recorder.record("upload", "Received a document")
    rows = repo._all("episodes", "select * from episodes order by seq")
    assert rows[-2]["status"] == "started"
    assert rows[-1]["status"] == "completed"
    assert rows[-1]["seq"] > rows[-2]["seq"]


def test_same_pdf_bytes_hit_the_fingerprint_cache_even_under_a_new_name(tmp_path, monkeypatch):
    from app.pipeline import pipeline_runner

    monkeypatch.setenv("STORAGE_DIR", str(tmp_path))
    # Force settings.storage_dir to re-read — Settings uses a property.
    pdf = b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n" + b"x" * 200
    first = {
        "id": new_id(),
        "document_role": "benefit_summary",
        "source_kind": "published",
        "source_url": "https://example.test/a.pdf",
        "downloaded_at": "2026-09-26",
        "file_name": "plan-a.pdf",
        "storage_path": str(tmp_path / "a.pdf"),
        "sha256": __import__("hashlib").sha256(pdf).hexdigest(),
        "status": "draft",
        "ingestion_state": {},
        "insurer": "Demo Plan",
        "plan_name": "Open Access",
        "plan_year": "2026",
        "identity_evidence": {},
        "sections": {},
        "validation_report": {},
    }
    Path(first["storage_path"]).write_bytes(pdf)
    repo = get_repo()
    repo.create_policy(first)
    again = pipeline_runner.ingest_upload(pdf, "renamed-copy.pdf", "benefit_summary", None, None)
    assert again["cached"] is True
    assert again["id"] == first["id"]
    assert "fingerprint" in (again.get("cache_message") or "").lower()
    assert repo.get_policy(first["id"])["file_name"] == "renamed-copy.pdf"


def test_new_upload_returns_immediately_and_runs_in_the_background(tmp_path, monkeypatch):
    import time

    from app.pipeline import pipeline_runner

    monkeypatch.setenv("STORAGE_DIR", str(tmp_path))
    pdf = b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n" + b"y" * 200
    started = {"count": 0}

    def slow_run(policy_id: str):
        started["count"] += 1
        time.sleep(0.4)
        get_repo().update_policy(policy_id, {"status": "draft"}, actor="engine")
        return {"id": policy_id, "cached": False, "status": "draft"}

    monkeypatch.setattr(pipeline_runner, "run", slow_run)
    t0 = time.perf_counter()
    body = pipeline_runner.ingest_upload(pdf, "async.pdf", "clinical_policy", None, None)
    elapsed = time.perf_counter() - t0
    assert elapsed < 0.25
    assert body["accepted"] is True
    assert body["status"] == "ingesting"
    assert body["cached"] is False
    deadline = time.time() + 2
    while time.time() < deadline and get_repo().get_policy(body["id"])["status"] == "ingesting":
        time.sleep(0.05)
    assert get_repo().get_policy(body["id"])["status"] == "draft"
    assert started["count"] == 1


def test_pages_preview_returns_cached_pages_before_extract_finishes(tmp_path, monkeypatch):
    from app.pipeline import cache as artifact_cache
    from app.pipeline.progress import ingestion_steps, pages_preview

    monkeypatch.setenv("STORAGE_DIR", str(tmp_path))
    policy_id = new_id()
    digest = "abc123preview"
    repo = get_repo()
    repo.create_policy(
        {
            "id": policy_id,
            "document_role": "clinical_policy",
            "source_kind": "published",
            "source_url": None,
            "downloaded_at": None,
            "file_name": "preview.pdf",
            "storage_path": str(tmp_path / "preview.pdf"),
            "sha256": digest,
            "status": "ingesting",
            "ingestion_state": {},
        }
    )
    artifact_cache.put(
        artifact_cache.stage_key(digest, "pages"),
        "stage",
        digest,
        [{"page": 1, "text": "Northwind Mutual lumbar policy page one.", "grid_rows": []}],
        None,
    )
    preview = pages_preview(policy_id)
    assert preview["page_count"] == 1
    assert preview["pages"] == []
    steps = ingestion_steps(policy_id)
    assert any(step["label"] == "Extracting pages" for step in steps)


def test_delete_policy_removes_row_storage_and_cache(tmp_path, monkeypatch):
    from app.pipeline import cache as artifact_cache

    monkeypatch.setenv("STORAGE_DIR", str(tmp_path))
    pdf = tmp_path / "gone.pdf"
    pdf.write_bytes(b"%PDF-1.4\ntrailer\n%%EOF\n" + b"z" * 200)
    policy_id = new_id()
    digest = "deletefingerprint01"
    repo = get_repo()
    repo.create_policy(
        {
            "id": policy_id,
            "document_role": "benefit_summary",
            "source_kind": "published",
            "source_url": None,
            "downloaded_at": None,
            "file_name": "gone.pdf",
            "storage_path": str(pdf),
            "sha256": digest,
            "status": "draft",
            "ingestion_state": {},
        }
    )
    artifact_cache.put(artifact_cache.stage_key(digest, "pages"), "stage", digest, [{"page": 1, "text": "x"}], None)
    result = repo.delete_policy(policy_id)
    assert result["deleted"] is True
    assert repo.get_policy(policy_id) is None
    assert not pdf.exists()
    assert artifact_cache.get(artifact_cache.stage_key(digest, "pages")) is None


def test_cache_hides_temporary_rows():
    key = "stage:" + new_id()
    put(key, "stage", "abc", {"ok": True}, None)
    assert get(key) == {"ok": True}
    assert get(key + ":tmp") is None


def test_demo_mode_cache_miss_does_not_call_a_provider(monkeypatch):
    monkeypatch.setenv("DEMO_MODE", "true")
    from app.llm import complete

    with pytest.raises(ApiError) as caught:
        complete("p2", {"item": {}, "page_text": "x"}, schema=None, stage="judge", run_id=new_id(), provider_kind="judge")
    assert caught.value.code == "DEMO_CACHE_MISS"


def test_guardrails_have_no_denial_status():
    assert "denied" not in SCHEMA.lower()
    assert ALLOWED == {"info_requested", "approved"}
    insurer = Path(__file__).resolve().parents[1] / "app/check/insurer.py"
    assert "denied" not in insurer.read_text().lower()


def test_fhir_questionnaire_validates():
    policy = {"id": "abc", "sha256": "1234567890abcdef", "file_name": "policy.pdf", "insurer": "Plan", "document_role": "clinical_policy", "source_url": "https://example.test"}
    items = [
        {
            "item_type": "rule",
            "review_state": "accepted",
            "page": 2,
            "data": {
                "criterion_key": "symptom",
                "requirement_text": "A symptom is documented.",
                "questions": [
                    {
                        "link_id": "symptom.documented",
                        "text": "Is this requirement documented in the chart?",
                        "answer_type": "boolean",
                        "enable_when": None,
                    }
                ],
            },
        }
    ]
    resource = questionnaire(policy, items, status="active")
    assert validate_resource(resource) == []
    response = questionnaire_response(
        {"patient": {"full_name": "Synthetic Patient"}, "updated_at": "2026-09-27T00:00:00+00:00"},
        [
            {
                "criterion_key": "symptom",
                "verified_by": "x",
                "questions": [
                    {
                        "link_id": "symptom.documented",
                        "answer_type": "boolean",
                        "enabled": True,
                        "value": json.dumps(True),
                        "evidence_text": "A symptom is documented in the note.",
                    }
                ],
            }
        ],
        policy,
        completed=True,
    )
    assert validate_resource(response) == []


def test_memory_keeps_a_marker_string_only_when_it_is_on_the_page():
    from app.pipeline.working_memory import apply_updates, empty

    memory = apply_updates(
        empty(),
        {"markers": ["2: May require prior authorization."], "definitions": ["not on the page"]},
        {11: "2: May require prior authorization. Copay applies."},
    )
    assert memory["markers"][0]["page"] == 11
    assert memory["definitions"] == []


def test_extract_batches_are_ten_pages_unless_the_token_budget_splits_them(monkeypatch):
    from app.pipeline.pipeline_runner import _groups

    monkeypatch.setenv("CONTEXT_GROUP_PAGES", "10")
    pages = [{"page": i, "text": "authorization required"} for i in range(1, 26)]
    groups = _groups(pages)
    assert [len(group) for group in groups] == [10, 10, 5]
    huge = [{"page": 1, "text": "word " * 8000}, {"page": 2, "text": "word " * 8000}]
    assert [len(group) for group in _groups(huge)] == [1, 1]


def test_missed_benefit_lines_are_kept_and_a_pasted_question_is_not():
    from app.ingest.coverage_lines import merge_uncovered, uncovered_lines
    from app.ingest.question_builder import accept_phrasing

    page = "\n".join(
        [
            "Emergency care $0 copay per visit",
            "Depending on Specialists $0 copay or 20% coinsurance",
            "This is a summary of what we cover and what you pay",
            "Inpatient hospital care $0 copay per stay",
        ]
    )
    saved = [{"service_label": "Inpatient hospital care", "data": {"evidence_text": "Inpatient hospital care $0 copay per stay"}}]
    missing = uncovered_lines(page, saved)
    assert any(line.startswith("Emergency care") for line in missing)
    assert any("Specialists $" in line for line in missing)
    assert all(line in page for line in missing)
    assert all("Inpatient hospital" not in line for line in missing)
    rows = merge_uncovered(missing, [], page=5, existing_keys=set())
    assert {row["evidence_text"] for row in rows} == set(missing)
    assert rows[1]["service_label"].startswith("Specialists")

    pasted = accept_phrasing(
        "Is the following documented: A neurological symptom is documented in the clinical note?",
        {"value": None},
        "A neurological symptom is documented in the clinical note.",
    )
    assert pasted is None


def test_question_prompt_replaces_a_pasted_sentence(monkeypatch):
    from app.ingest.question_builder import build

    def fake(prompt, user, **kwargs):
        if prompt == "p5":
            assert "requirement_text" in user
            assert user["slots"][0]["condition_text"]
            link = user["slots"][0]["link_id"]
            return {"data": {"texts": {link: "Is a neurological symptom documented in the chart?"}}}
        return {"data": {"verdict": "complete", "reason": "Each condition is a question."}}

    monkeypatch.setattr("app.ingest.question_builder.llm.complete", fake)
    rule = {
        "criterion_key": "symptom_note",
        "criterion_type": "clinical_note",
        "requirement_text": "A neurological symptom is documented in the clinical note.",
        "conditions": [
            {
                "condition_key": "documented",
                "text": "A neurological symptom is documented in the clinical note.",
                "kind": "requirement",
                "value": None,
                "unit": None,
            }
        ],
    }
    built = build(rule, run_id="run")
    assert built["questions"][0]["text"] == "Is a neurological symptom documented in the chart?"


def test_a_benefit_chart_becomes_one_row_per_service():
    from app.ingest.table_grid import rows_from_words

    words = [
        {"text": "In-network", "x0": 220, "top": 10},
        {"text": "Out-of-network", "x0": 380, "top": 10},
        {"text": "Emergency", "x0": 40, "top": 40},
        {"text": "care", "x0": 110, "top": 40},
        {"text": "$0", "x0": 220, "top": 40},
        {"text": "copay", "x0": 250, "top": 40},
        {"text": "40%", "x0": 380, "top": 40},
        {"text": "coinsurance", "x0": 420, "top": 40},
        {"text": "Specialists", "x0": 40, "top": 70},
        {"text": "Depending", "x0": 40, "top": 92},
        {"text": "on", "x0": 110, "top": 92},
        {"text": "$20", "x0": 220, "top": 92},
        {"text": "copay", "x0": 250, "top": 92},
    ]
    services = [row["service"] for row in rows_from_words(words, 520)]
    assert "Emergency care" in services
    assert "Specialists" in services


def test_a_thin_benefit_batch_is_split_and_a_full_page_is_not():
    from app.pipeline.pipeline_runner import _benefit_batch_is_thin

    pages = [{"page": i, "text": "Primary care $0 copay\nSpecialist 20% coinsurance\nCovered dental"} for i in range(1, 5)]
    assert _benefit_batch_is_thin("benefit_summary", pages, [{"service_label": "Ambulance"}])
    assert not _benefit_batch_is_thin("benefit_summary", pages[:1], [])
    assert not _benefit_batch_is_thin("clinical_policy", pages, [])


def test_no_hardcoding():
    banned = ["aetna", "cigna", "humana", "unitedhealthcare", "blue cross", "kaiser", "anthem", "72148", "northwind", "maria rodriguez"]
    root = Path("app")
    for path in root.rglob("*.py"):
        text = path.read_text().lower()
        for word in banned:
            assert word not in text, f"{word} found in {path}"
