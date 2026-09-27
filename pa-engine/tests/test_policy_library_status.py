"""Reversible Go Live / Take Offline and questionnaire delete-regenerate."""

from __future__ import annotations

import hashlib

import pytest

from app.errors import ApiError
from app.repository import get_repo, new_id
from app.review.review import accept, go_live, take_offline
from app.check import service as check_service


def _pdf(tag: bytes) -> bytes:
    # Minimal valid-enough PDF bytes for storage path; not parsed in these tests.
    return b"%PDF-1.4\n" + tag + b"\n%%EOF\n"


def _seed_policy(tmp_path, *, role="clinical_policy", status="draft"):
    repo = get_repo()
    pdf = tmp_path / f"{role}.pdf"
    pdf.write_bytes(_pdf(role.encode()))
    policy = repo.create_policy(
        {
            "id": new_id(),
            "document_role": role,
            "source_kind": "fictional_fallback",
            "file_name": pdf.name,
            "storage_path": str(pdf),
            "sha256": hashlib.sha256(pdf.read_bytes()).hexdigest(),
            "status": status,
            "insurer": "Northwind Mutual",
            "plan_name": "Open Access PPO",
            "plan_year": "2026",
            "ingestion_state": {},
            "status_history": [],
            "validation_report": {"fhir": {"valid": True}, "questions": {"condition_coverage": 1}},
        }
    )
    item = repo.insert_item(
        {
            "id": new_id(),
            "policy_id": policy["id"],
            "item_type": "rule",
            "item_key": "age",
            "seq": 1,
            "service_label": "Age",
            "service_codes": [],
            "data": {
                "criterion_key": "age",
                "requirement_text": "18 or older",
                "criterion_type": "age",
                "policy_page": 1,
                "applies_to": ["MRI lumbar"],
                "conditions": [],
                "questions": [
                    {
                        "link_id": "age_years",
                        "text": "Age?",
                        "answer_type": "quantity",
                        "fill_method": "date_math",
                        "covers": [],
                    }
                ],
                "pass_condition": {"all": [{"gte": ["age_years", 18]}]},
                "evidence_text": "18 or older",
            },
            "original_data": {},
            "page": 1,
            "grounding": {"passed": True},
            "judge_verdict": "ACCURATE",
            "judge_reason": "ok",
            "question_verdict": "complete",
            "review_state": "auto_approved",
        }
    )
    return policy, item


def test_go_live_take_offline_go_live_keeps_items(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "status.db"))
    monkeypatch.setenv("STORAGE_DIR", str(tmp_path))
    # Force new repo handle for this db path.
    import app.repository as repo_mod

    repo_mod._repo = None
    repo_mod._repo_path = None

    policy, item = _seed_policy(tmp_path)
    accept(policy["id"], item["id"], "Suman", "ok")

    live = go_live(policy["id"], "Suman")
    assert live["status"] == "live"
    assert live["status_history"][-1]["status"] == "live"
    assert live["status_history"][-1]["changed_by"] == "Suman"
    assert len(get_repo().items_for(policy["id"])) == 1
    assert len(get_repo().list_policies(False)) == 1

    offline = take_offline(policy["id"], "Suman")
    assert offline["status"] == "archived"
    assert offline["status_history"][-1]["status"] == "archived"
    assert len(offline["status_history"]) == 2
    assert len(get_repo().items_for(policy["id"])) == 1
    assert get_repo().list_policies(False) == []

    again = go_live(policy["id"], "Alex")
    assert again["status"] == "live"
    assert [h["status"] for h in again["status_history"]] == ["live", "archived", "live"]
    assert again["status_history"][-1]["changed_by"] == "Alex"
    assert len(get_repo().items_for(policy["id"])) == 1


def test_take_offline_rejects_non_live(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "offline.db"))
    monkeypatch.setenv("STORAGE_DIR", str(tmp_path))
    import app.repository as repo_mod

    repo_mod._repo = None
    repo_mod._repo_path = None

    policy, _ = _seed_policy(tmp_path)
    with pytest.raises(ApiError) as exc:
        take_offline(policy["id"], "Suman")
    assert exc.value.code == "INVALID_TRANSITION"


def test_questionnaire_delete_blocked_when_response_exists(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "q.db"))
    monkeypatch.setenv("STORAGE_DIR", str(tmp_path))
    import app.repository as repo_mod

    repo_mod._repo = None
    repo_mod._repo_path = None

    policy, item = _seed_policy(tmp_path)
    accept(policy["id"], item["id"], "Suman", "ok")
    go_live(policy["id"], "Suman")
    repo = get_repo()

    patient = repo.upsert_patient(
        {"id": new_id(), "full_name": "Test Patient", "synthetic": True}
    )
    provider = repo.upsert_provider(
        {"id": new_id(), "full_name": "Dr Test", "specialty": "Spine"}
    )
    pa = repo.create_pa(
        {
            "id": new_id(),
            "patient_id": patient["id"],
            "ordering_provider_id": provider["id"],
            "insurer": "Northwind Mutual",
            "plan_name": "Open Access PPO",
            "plan_year": "2026",
            "order_text": "MRI lumbar",
            "status": "needs_info",
        }
    )

    stored = repo.save_fhir_questionnaire(
        {
            "insurance_plan_id": policy["id"],
            "service_category": "MRI lumbar",
            "resource": {"resourceType": "Questionnaire", "id": "q1", "status": "active", "item": []},
        }
    )
    listed = check_service.list_policy_questionnaires(policy["id"])
    assert listed["questionnaires"][0]["can_delete"] is True

    repo.save_fhir_qr(
        {
            "pa_request_id": pa["id"],
            "questionnaire_id": stored["id"],
            "resource": {"resourceType": "QuestionnaireResponse", "status": "completed"},
        }
    )
    listed2 = check_service.list_policy_questionnaires(policy["id"])
    assert listed2["questionnaires"][0]["can_delete"] is False
    assert "responses already submitted" in (listed2["questionnaires"][0]["delete_blocked_reason"] or "")

    with pytest.raises(ApiError) as exc:
        check_service.delete_questionnaire(policy["id"], stored["id"])
    assert exc.value.code == "QUESTIONNAIRE_HAS_RESPONSES"


def test_questionnaire_delete_and_regenerate(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "regen.db"))
    monkeypatch.setenv("STORAGE_DIR", str(tmp_path))
    import app.repository as repo_mod

    repo_mod._repo = None
    repo_mod._repo_path = None

    policy, item = _seed_policy(tmp_path)
    accept(policy["id"], item["id"], "Suman", "ok")
    go_live(policy["id"], "Suman")

    stored = get_repo().save_fhir_questionnaire(
        {
            "insurance_plan_id": policy["id"],
            "service_category": "MRI lumbar",
            "resource": {
                "resourceType": "Questionnaire",
                "id": "old-q",
                "status": "active",
                "title": "Old",
                "item": [{"linkId": "g1"}],
            },
        }
    )
    check_service.delete_questionnaire(policy["id"], stored["id"])
    assert get_repo().get_fhir_questionnaire(stored["id"]) is None

    stored2 = get_repo().save_fhir_questionnaire(
        {
            "insurance_plan_id": policy["id"],
            "service_category": "MRI lumbar",
            "resource": {
                "resourceType": "Questionnaire",
                "id": "old-q2",
                "status": "active",
                "title": "Old2",
                "item": [{"linkId": "g1"}],
            },
        }
    )
    result = check_service.regenerate_questionnaire(policy["id"], stored2["id"], reviewer="Suman")
    assert result["deleted"] == stored2["id"]
    assert result["questionnaire"]["id"] != stored2["id"]
    assert get_repo().get_fhir_questionnaire(stored2["id"]) is None
    assert get_repo().get_fhir_questionnaire(result["questionnaire"]["id"]) is not None
