"""Order check: match, coverage gate, answers, evaluation. Routers call this and nothing else."""

from __future__ import annotations

import json

from app.check import coverage_gate, service_matcher
from app.check.answer_filler import decode_value, fill
from app.check.insurer import approve as insurer_approve_action
from app.check.insurer import request_info as insurer_request_info_action
from app.check.insurer import schedule
from app.check.owner import find as find_owner
from app.check.packet import build as build_packet
from app.check.pass_eval import evaluate
from app.check.scorer import readiness
from app.errors import ApiError
from app.llm import reset_steps
from app.pipeline.episodes import EpisodeRecorder
from app.repository import get_repo, new_id, now
from app.review import review as gates

TRANSITIONS = {
    "draft": {"matching", "checking", "not_required"},
    "matching": {"checking", "not_required"},
    "checking": {"not_required", "needs_info", "ready_for_review"},
    "needs_info": {"ready_for_review", "needs_info"},
    "ready_for_review": {"needs_info", "submitted", "ready_for_review"},
    "submitted": {"in_review"},
    "in_review": {"approved", "info_requested"},
    "info_requested": {"needs_info", "ready_for_review", "submitted"},
    "not_required": set(),
    "approved": set(),
}


def list_recent(*, limit: int = 50, queue: str | None = None) -> dict:
    repo = get_repo()
    rows = []
    for pa in repo.list_pas(limit=limit):
        if queue == "insurer" and pa["status"] not in {
            "submitted",
            "in_review",
            "info_requested",
            "approved",
        }:
            continue
        if queue == "doctor" and pa["status"] in {"submitted", "in_review", "approved"}:
            # Still list them; doctor needs post-submit visibility. No filter.
            pass
        patient = repo.get_patient(pa["patient_id"]) or {}
        rows.append(
            {
                "id": pa["id"],
                "status": pa["status"],
                "order_text": pa.get("order_text"),
                "service_code": pa.get("service_code"),
                "service_category": pa.get("service_category"),
                "insurer": pa.get("insurer"),
                "plan_name": pa.get("plan_name"),
                "plan_year": pa.get("plan_year"),
                "updated_at": pa.get("updated_at"),
                "submitted_at": pa.get("submitted_at"),
                "patient": {"id": patient.get("id"), "full_name": patient.get("full_name")},
            }
        )
    return {"requests": rows}


def insurer_approve(pa_id: str, *, note: str | None = None) -> dict:
    pa = _pa(pa_id)
    if pa["status"] not in {"submitted", "in_review"}:
        raise ApiError(
            "INVALID_TRANSITION",
            "Only a request in review can be approved.",
            409,
        )
    insurer_approve_action(pa_id, note=note)
    return present(pa_id)


def insurer_request_info(pa_id: str, *, note: str | None = None) -> dict:
    pa = _pa(pa_id)
    if pa["status"] not in {"submitted", "in_review"}:
        raise ApiError(
            "INVALID_TRANSITION",
            "Additional evidence can only be requested while the request is in review.",
            409,
        )
    insurer_request_info_action(pa_id, note=note)
    return present(pa_id)


def patient_chart(patient_id: str) -> dict:
    """Chart envelope for the patient portal. prior_authorizations is [] when none exist."""
    repo = get_repo()
    patient = repo.get_patient(patient_id)
    if patient is None:
        raise ApiError("POLICY_NOT_FOUND", "That patient is not on file.", 404)
    provider_rows = repo.list_providers()
    documents = repo.documents_for(patient_id)
    records = repo.records_for(patient_id)
    pas = [pa for pa in repo.list_pas(limit=200) if pa["patient_id"] == patient_id]
    name_parts = (patient.get("full_name") or "").split(None, 1)
    first = name_parts[0] if name_parts else ""
    last = name_parts[1] if len(name_parts) > 1 else ""
    providers_out = []
    for provider in provider_rows:
        providers_out.append(
            {
                "npi": provider.get("npi") or provider["id"][:10],
                "first_name": (provider.get("full_name") or "").split(None, 1)[0],
                "last_name": (
                    (provider.get("full_name") or "").split(None, 1)[1]
                    if " " in (provider.get("full_name") or "")
                    else ""
                ),
                "specialty": provider.get("specialty"),
                "tax_id": None,
                "clinic_name": None,
            }
        )
    prior = [_prior_authorization_row(pa, repo) for pa in pas]
    return {
        "patient": {
            "first_name": first,
            "last_name": last,
            "dob": patient.get("dob"),
            "sex": patient.get("sex"),
            "phone": None,
            "address": None,
        },
        "clinics": [],
        "providers": providers_out,
        "facilities": [],
        "payers": [],
        "patient_insurance": {
            "member_id": patient.get("member_id"),
            "group_number": None,
            "effective_date": None,
            "termination_date": None,
            "payer_code": None,
        },
        "icd10_codes": [],
        "cpt_codes": [],
        "document_types": sorted({(d.get("doc_type") or "REPORT").upper() for d in documents}),
        "diagnoses": [],
        "allergies": [],
        "medications": [],
        "surgical_history": [],
        "family_history": [],
        "social_history": {},
        "lab_panels": [],
        "lab_test_catalog": [],
        "lab_results": [],
        "imaging_reports": [],
        "previous_treatments": [],
        "clinical_records": [
            {
                "record_kind": r.get("record_kind"),
                "body": r.get("body"),
                "start_date": r.get("start_date"),
                "code": r.get("code"),
            }
            for r in records
        ],
        "prior_authorizations": prior,
    }


def _prior_authorization_row(pa: dict, repo) -> dict:
    patient = repo.get_patient(pa["patient_id"]) or {}
    provider = repo.get_provider(pa["ordering_provider_id"]) or {}
    events = repo.events_for(pa["id"])
    documents = repo.documents_for(pa["patient_id"])
    status_map = {
        "submitted": "SUBMITTED",
        "in_review": "UNDER_REVIEW",
        "info_requested": "INFO_REQUESTED",
        "approved": "APPROVED",
        "ready_for_review": "READY",
        "needs_info": "NEEDS_INFO",
        "not_required": "NOT_REQUIRED",
        "matching": "MATCHING",
        "checking": "CHECKING",
    }
    current = status_map.get(pa["status"], (pa["status"] or "").upper())
    history = []
    for event in events:
        history.append(
            {
                "status": status_map.get(event.get("event_type"), str(event.get("event_type") or "").upper()),
                "status_date": event.get("created_at"),
                "notes": event.get("message"),
            }
        )
    if not history and pa.get("submitted_at"):
        history.append(
            {
                "status": "SUBMITTED",
                "status_date": pa.get("submitted_at"),
                "notes": "Initial submission received",
            }
        )
    docs = []
    for document in documents:
        docs.append(
            {
                "type_name": (document.get("doc_type") or "REPORT").upper(),
                "file_path": document.get("storage_path"),
                "uploaded_date": document.get("created_at"),
                "extracted_at": document.get("created_at"),
            }
        )
    packet = pa.get("submission_packet") or {}
    letter = None
    for attachment in packet.get("attachments") or []:
        if "necessity" in str(attachment.get("file_name") or "").lower():
            letter = {
                "provider_npi": provider.get("npi") or provider.get("id"),
                "letter_date": pa.get("submitted_at"),
                "letter_text": None,
            }
            break
    return {
        "member_id": patient.get("member_id"),
        "requesting_provider_npi": provider.get("npi") or provider.get("id"),
        "icd10_code": None,
        "cpt_code": pa.get("service_code"),
        "request_type": "INITIAL",
        "urgency": "STANDARD",
        "units_requested": 1,
        "requested_date": (pa.get("created_at") or "")[:10] or None,
        "requested_start_date": None,
        "current_status": current,
        "order_text": pa.get("order_text"),
        "pa_request_id": pa["id"],
        "insurer": pa.get("insurer"),
        "plan_name": pa.get("plan_name"),
        "documents": docs,
        "status_history": history,
        "medical_necessity_letter": letter,
        "appeals": [],
        "cost_share": None,
    }


def confirm_draft(pa_id: str, body: dict | None = None) -> dict:
    """Doctor confirms pre-filled ingest fields, then runs the existing coverage/check engine."""
    body = body or {}
    repo = get_repo()
    pa = _pa(pa_id)
    if pa["status"] not in {"draft", "matching"}:
        raise ApiError("INVALID_TRANSITION", "Only a draft or unmatched request can be confirmed.", 409)

    insurer = body.get("insurer") or pa["insurer"]
    plan_name = body.get("plan_name") or pa["plan_name"]
    plan_year = body.get("plan_year") if body.get("plan_year") is not None else pa.get("plan_year")
    order_text = body.get("order_text") or body.get("service_category") or pa["order_text"]
    service_code = body.get("service_code") if "service_code" in body else pa.get("service_code")
    service_category = body.get("service_category") or pa.get("service_category") or order_text
    insurance_plan_id = body.get("insurance_plan_id") or pa.get("insurance_plan_id")

    # Resolve opaque / missing plan ids to a live benefit summary so coverage matching works.
    policy = repo.get_policy(insurance_plan_id) if insurance_plan_id else None
    if policy is not None and policy.get("status") != "live":
        policy = None
        insurance_plan_id = None
    if policy is None:
        live_benefits = [
            row
            for row in repo.list_policies(False)
            if row.get("document_role") == "benefit_summary" and row.get("status") == "live"
        ]
        if insurer:
            matched = [
                row
                for row in live_benefits
                if (row.get("insurer") or "").lower() == insurer.lower()
                and (not plan_name or (row.get("plan_name") or "").lower() == plan_name.lower())
            ]
            if not matched:
                matched = [row for row in live_benefits if (row.get("insurer") or "").lower() == insurer.lower()]
            if matched:
                policy = matched[0]
                insurance_plan_id = policy["id"]
            elif not live_benefits:
                raise ApiError(
                    "NO_ACTIVE_POLICY",
                    f"No active policy for this plan ({insurer}"
                    + (f" / {plan_name}" if plan_name else "")
                    + "). Go live on a matching Evidence of Coverage in the Policy library.",
                    409,
                )
        elif live_benefits:
            policy = live_benefits[0]
            insurance_plan_id = policy["id"]
    if policy:
        insurer = policy.get("insurer") or insurer
        plan_name = policy.get("plan_name") or plan_name
        plan_year = policy.get("plan_year") or plan_year

    # Keep draft until check succeeds; avoid a stuck matching shell with empty candidates.
    repo.update_pa(
        pa_id,
        {
            "insurer": insurer,
            "plan_name": plan_name,
            "plan_year": plan_year or "",
            "order_text": order_text,
            "service_code": service_code,
            "service_category": service_category,
            "insurance_plan_id": insurance_plan_id,
            "benefit_summary_id": (policy or {}).get("id"),
        },
    )
    repo.add_event(pa_id, "matched", "Doctor confirmed ingested order; running PA determination", "clinician")
    reset_steps()
    run = repo.create_run(None, f"confirm-{pa_id[:8]}")
    pa = repo.get_pa(pa_id)
    try:
        result = _check(
            pa,
            {
                "order_text": order_text,
                "service_code": service_code,
                "drug_name": pa.get("drug_name"),
                "insurer": insurer,
                "plan_name": plan_name,
                "plan_year": plan_year,
            },
            run["id"],
            EpisodeRecorder("pa_request", pa_id, run["id"]),
        )
    finally:
        repo.finish_run(run["id"], "complete")
    if result.get("status") not in {"matching", "not_required", "draft"}:
        try:
            _snapshot_questionnaire(pa_id)
        except Exception:
            pass
    return present(pa_id) if result.get("id") else result


def get_questionnaire(pa_id: str) -> dict:
    """Return stored FHIR Questionnaire for this PA (build+store if missing)."""
    repo = get_repo()
    pa = _pa(pa_id)
    if pa["status"] == "not_required":
        raise ApiError("INVALID_TRANSITION", "No questionnaire: prior authorization is not required.", 409)
    if pa["status"] == "draft":
        raise ApiError("INVALID_TRANSITION", "Confirm the draft order before loading the questionnaire.", 409)
    if pa.get("questionnaire_id"):
        stored = repo.get_fhir_questionnaire(pa["questionnaire_id"])
        if stored:
            return stored["resource"]
    return _snapshot_questionnaire(pa_id)


def list_policy_questionnaires(policy_id: str) -> dict:
    """List generated FHIR Questionnaire rows for a policy (insurance_plan_id)."""
    repo = get_repo()
    policy = repo.get_policy(policy_id)
    if policy is None:
        raise ApiError("POLICY_NOT_FOUND", "No policy with that id.", 404)
    rows = []
    for row in repo.list_fhir_questionnaires_for_plan(policy_id):
        response_count = repo.count_questionnaire_responses(row["id"])
        resource = row.get("resource") or {}
        rows.append(
            {
                "id": row["id"],
                "insurance_plan_id": row.get("insurance_plan_id"),
                "service_category": row.get("service_category"),
                "created_at": row.get("created_at"),
                "title": resource.get("title") if isinstance(resource, dict) else None,
                "item_count": len(resource.get("item") or []) if isinstance(resource, dict) else 0,
                "response_count": response_count,
                "can_delete": response_count == 0,
                "delete_blocked_reason": (
                    None
                    if response_count == 0
                    else "responses already submitted against this questionnaire"
                ),
            }
        )
    embedded = policy.get("fhir_questionnaire")
    return {
        "policy_id": policy_id,
        "policy_status": policy.get("status"),
        "embedded_questionnaire": bool(embedded),
        "questionnaires": rows,
    }


def delete_questionnaire(policy_id: str, questionnaire_id: str) -> dict:
    """Delete a generated questionnaire only when no QuestionnaireResponse exists."""
    repo = get_repo()
    policy = repo.get_policy(policy_id)
    if policy is None:
        raise ApiError("POLICY_NOT_FOUND", "No policy with that id.", 404)
    stored = repo.get_fhir_questionnaire(questionnaire_id)
    if stored is None or stored.get("insurance_plan_id") != policy_id:
        raise ApiError("POLICY_NOT_FOUND", "No questionnaire with that id for this policy.", 404)
    response_count = repo.count_questionnaire_responses(questionnaire_id)
    if response_count > 0:
        raise ApiError(
            "QUESTIONNAIRE_HAS_RESPONSES",
            "responses already submitted against this questionnaire",
            409,
            responses=response_count,
        )
    repo.delete_fhir_questionnaire(questionnaire_id)
    # Clear embedded copy on the policy when it matches this resource id.
    embedded = policy.get("fhir_questionnaire") or {}
    if isinstance(embedded, dict) and embedded.get("id") == questionnaire_id:
        repo.update_policy(policy_id, {"fhir_questionnaire": None}, actor="engine")
    return {"deleted": True, "id": questionnaire_id}


def regenerate_questionnaire(policy_id: str, questionnaire_id: str, *, reviewer: str = "reviewer") -> dict:
    """Delete (if eligible) and create a fresh questionnaire from the live policy's rules."""
    repo = get_repo()
    policy = repo.get_policy(policy_id)
    if policy is None:
        raise ApiError("POLICY_NOT_FOUND", "No policy with that id.", 404)
    if policy.get("status") != "live":
        raise ApiError(
            "NO_ACTIVE_POLICY",
            "Regenerate needs the policy to be live. Go live first, then regenerate.",
            409,
        )
    delete_questionnaire(policy_id, questionnaire_id)

    from app.fhir.builders import questionnaire as build_q
    from app.fhir.validate import validate_resource

    items = [
        i
        for i in repo.items_for(policy_id)
        if i["review_state"] in {"accepted", "edited"} and i["item_type"] == "rule"
    ]
    if not items:
        raise ApiError("ITEMS_UNDECIDED", "No accepted rules to build a questionnaire from.", 409)
    resource = build_q(policy, items, status="active")
    resource["id"] = new_id()
    errors = validate_resource(resource)
    if errors:
        raise ApiError("FHIR_INVALID", "FHIR validation failed: " + "; ".join(errors), 409)
    stored = repo.save_fhir_questionnaire(
        {
            "insurance_plan_id": policy_id,
            "service_category": (items[0].get("data") or {}).get("applies_to", [None])[0]
            if items
            else None,
            "resource": resource,
        }
    )
    repo.update_policy(policy_id, {"fhir_questionnaire": resource}, actor=reviewer)
    return {
        "deleted": questionnaire_id,
        "questionnaire": {
            "id": stored["id"],
            "insurance_plan_id": policy_id,
            "service_category": stored.get("service_category"),
            "created_at": stored.get("created_at"),
            "title": resource.get("title"),
            "item_count": len(resource.get("item") or []),
            "response_count": 0,
            "can_delete": True,
            "delete_blocked_reason": None,
            "resource": resource,
        },
    }


def submit_questionnaire_response(pa_id: str, body: dict) -> dict:
    """Accept a FHIR QuestionnaireResponse and store it keyed from PARequest (G1-safe)."""
    repo = get_repo()
    pa = _pa(pa_id)
    if pa["status"] in {"draft", "not_required", "approved"}:
        raise ApiError("INVALID_TRANSITION", "Questionnaire answers cannot be submitted in this state.", 409)

    resource = body.get("resource") or body
    if resource.get("resourceType") != "QuestionnaireResponse":
        raise ApiError("INVALID_PDF", "Body must be a FHIR QuestionnaireResponse.", 400)

    if not pa.get("questionnaire_id"):
        _snapshot_questionnaire(pa_id)
        pa = repo.get_pa(pa_id)

    # Soft-apply boolean/string values onto unanswered enabled answers when present.
    by_link: dict = {}
    for item in resource.get("item") or []:
        _collect_qr_answers(item, by_link)
    for criterion in repo.criteria_for(pa_id):
        if criterion.get("verified_by"):
            continue
        for answer in repo.answers_for(criterion["id"]):
            if answer["link_id"] not in by_link:
                continue
            if answer.get("edited_by_human"):
                continue
            value = by_link[answer["link_id"]]
            repo.write_answer(
                answer["id"],
                {
                    "value": json.dumps(value),
                    "review_state": "ai_filled" if value is not None else "unanswered",
                    "fill_method": answer.get("fill_method") or "llm_quote",
                    "enabled": 1,
                },
                actor="engine",
            )

    stored = repo.save_fhir_qr(
        {
            "pa_request_id": pa_id,
            "questionnaire_id": pa.get("questionnaire_id"),
            "resource": resource,
        }
    )
    repo.update_pa(pa_id, {"questionnaire_response_id": stored["id"]})
    repo.add_event(pa_id, "checked", "QuestionnaireResponse stored", "clinician")
    return present(pa_id)


def reviewer_decision(pa_id: str, body: dict) -> dict:
    """approve | request_info only (G1 — deny is not implemented)."""
    action = (body.get("action") or "").strip().lower()
    note = body.get("note")
    if action == "approve":
        return insurer_approve(pa_id, note=note)
    if action in {"request_info", "request_additional_information", "additional_info"}:
        return insurer_request_info(pa_id, note=note)
    if action == "deny":
        raise ApiError(
            "INVALID_TRANSITION",
            "Denial is not allowed (constitution G1). Use request_info or approve.",
            409,
        )
    raise ApiError("INVALID_PDF", "action must be approve or request_info.", 400)


def list_patient_pa_requests(patient_id: str) -> dict:
    from app.boundary.export_pa import buildOutputForTeammate

    repo = get_repo()
    if repo.get_patient(patient_id) is None:
        raise ApiError("POLICY_NOT_FOUND", "That patient is not on file.", 404)
    requests = [buildOutputForTeammate(pa["id"]) for pa in repo.list_pas_for_patient(patient_id)]
    return {"patient_id": patient_id, "pa_requests": requests}


def reconfirm_pa_required(pa_id: str) -> dict:
    """Insurer-side re-check of coverage rules before decision."""
    from app.boundary.pa_rules import lookup_requirement

    pa = _pa(pa_id)
    rule = lookup_requirement(
        insurance_plan_id=pa.get("insurance_plan_id") or pa.get("benefit_summary_id"),
        service_category=pa.get("service_category") or pa.get("order_text") or "",
        service_code=pa.get("service_code"),
    )
    return rule


def _snapshot_questionnaire(pa_id: str) -> dict:
    from app.fhir.builders import questionnaire as build_q

    repo = get_repo()
    pa = repo.get_pa(pa_id)
    if pa is None:
        raise ApiError("POLICY_NOT_FOUND", "No request with that id.", 404)
    if pa.get("questionnaire_id"):
        stored = repo.get_fhir_questionnaire(pa["questionnaire_id"])
        if stored:
            return stored["resource"]

    policy = repo.get_policy(pa["criteria_policy_id"]) if pa.get("criteria_policy_id") else None
    items = []
    if policy:
        items = [i for i in repo.items_for(policy["id"]) if i["review_state"] != "rejected" and i["item_type"] == "rule"]
        # Prefer order-scoped: only items tied to current criteria keys.
        keys = {c["criterion_key"] for c in repo.criteria_for(pa_id)}
        scoped = [i for i in items if (i.get("data") or {}).get("criterion_key") in keys]
        if scoped:
            items = scoped
    if not policy:
        # Synthetic questionnaire from current criteria questions.
        resource = _questionnaire_from_criteria(pa)
    else:
        resource = build_q(policy, items, status="active")
        resource["id"] = new_id()
        resource["useContext"] = [
            {
                "code": {"code": "insurance_plan_id"},
                "valueString": pa.get("insurance_plan_id") or policy["id"],
            },
            {
                "code": {"code": "service_category"},
                "valueString": pa.get("service_category") or pa.get("order_text") or "",
            },
        ]

    stored = repo.save_fhir_questionnaire(
        {
            "insurance_plan_id": pa.get("insurance_plan_id") or (policy or {}).get("id"),
            "service_category": pa.get("service_category") or pa.get("order_text"),
            "resource": resource,
        }
    )
    repo.update_pa(pa_id, {"questionnaire_id": stored["id"]})
    return resource


def _questionnaire_from_criteria(pa: dict) -> dict:
    repo = get_repo()
    items = []
    for criterion in repo.criteria_for(pa["id"]):
        group = {
            "linkId": criterion["criterion_key"],
            "text": criterion["requirement_text"],
            "type": "group",
            "item": [],
        }
        for answer in repo.answers_for(criterion["id"]):
            group["item"].append(
                {
                    "linkId": answer["link_id"],
                    "text": answer["question_text"],
                    "type": {"boolean": "boolean", "string": "string", "quantity": "integer", "date": "date", "coding": "choice"}.get(
                        answer["answer_type"], "string"
                    ),
                    "required": True,
                }
            )
        items.append(group)
    return {
        "resourceType": "Questionnaire",
        "id": new_id(),
        "status": "active",
        "title": f"PA questionnaire for {pa.get('service_category') or pa.get('order_text')}",
        "useContext": [
            {"code": {"code": "insurance_plan_id"}, "valueString": pa.get("insurance_plan_id") or ""},
            {"code": {"code": "service_category"}, "valueString": pa.get("service_category") or pa.get("order_text") or ""},
        ],
        "item": items,
    }


def _collect_qr_answers(item: dict, out: dict) -> None:
    link = item.get("linkId")
    answers = item.get("answer") or []
    if link and answers:
        ans = answers[0]
        if "valueBoolean" in ans:
            out[link] = ans["valueBoolean"]
        elif "valueString" in ans:
            out[link] = ans["valueString"]
        elif "valueInteger" in ans:
            out[link] = ans["valueInteger"]
        elif "valueDate" in ans:
            out[link] = ans["valueDate"]
        elif "valueCoding" in ans:
            out[link] = ans["valueCoding"]
    for nested in item.get("item") or []:
        _collect_qr_answers(nested, out)


def check(payload: dict) -> dict:
    reset_steps()
    repo = get_repo()
    patient = repo.get_patient(payload["patient_id"])
    provider = repo.get_provider(payload["ordering_provider_id"])
    if patient is None or provider is None:
        raise ApiError("POLICY_NOT_FOUND", "The patient or ordering clinician is not in the chart.", 404)
    if not patient.get("synthetic"):
        raise ApiError("INVALID_PDF", "Only synthetic patients can be checked.", 400)
    run = repo.create_run(None, f"pa-{payload['patient_id'][:8]}-{new_id()[:8]}")
    pa = repo.create_pa(
        {
            "patient_id": patient["id"],
            "ordering_provider_id": provider["id"],
            "insurer": payload["insurer"],
            "plan_name": payload["plan_name"],
            "plan_year": payload.get("plan_year") or "",
            "order_text": payload["order_text"],
            "service_code": payload.get("service_code"),
            "drug_name": payload.get("drug_name"),
            "status": "matching",
            "readiness": 0,
            "source_upload_id": payload.get("source_upload_id"),
            "source_document_reference": payload.get("source_document_reference"),
            "service_category": payload.get("service_category") or payload.get("order_text"),
        }
    )
    recorder = EpisodeRecorder("pa_request", pa["id"], run["id"])
    recorder.record("created", "Request opened")
    repo.add_event(pa["id"], "created", "Request opened", "engine")
    try:
        result = _check(pa, payload, run["id"], recorder)
    finally:
        repo.finish_run(run["id"], "complete")
    try:
        _snapshot_questionnaire(pa["id"])
    except Exception:
        pass
    return result


def choose_match(pa_id: str, item_id: str) -> dict:
    repo = get_repo()
    pa = _pa(pa_id)
    if pa["status"] != "matching":
        raise ApiError("INVALID_TRANSITION", "A match can only be chosen while the request is matching.", 409)
    item = repo.get_item(item_id)
    if item is None:
        raise ApiError("POLICY_NOT_FOUND", "That policy item does not exist.", 404)
    status = "coverage" if item["item_type"] == "coverage" else "matched"
    return _check(
        pa,
        {
            "order_text": pa["order_text"],
            "service_code": pa.get("service_code"),
            "drug_name": pa.get("drug_name"),
            "insurer": pa["insurer"],
            "plan_name": pa["plan_name"],
            "plan_year": pa["plan_year"],
        },
        new_id(),
        EpisodeRecorder("pa_request", pa_id, None),
        preset={"status": status, "items": [item], "candidates": None},
    )


def recheck(pa_id: str) -> dict:
    pa = _pa(pa_id)
    if pa["status"] in {"submitted", "in_review", "approved"}:
        raise ApiError("INVALID_TRANSITION", "This request can no longer be re-checked.", 409)
    reset_steps()
    run = get_repo().create_run(None, f"recheck-{pa_id[:8]}")
    try:
        return _evaluate_existing(pa, run["id"], EpisodeRecorder("pa_request", pa_id, run["id"]), event="rechecked")
    finally:
        get_repo().finish_run(run["id"], "complete")


def reject_answer(pa_id: str, answer_id: str, provider_id: str, reason: str) -> dict:
    if not reason or len(reason.strip()) < 5:
        raise ApiError("REASON_REQUIRED", "A reason of at least 5 characters is required.", 400)
    return gates.reject_answer(pa_id, answer_id, provider_id, reason.strip())


def enter_answer(pa_id: str, answer_id: str, body: dict) -> dict:
    return gates.enter_answer(pa_id, answer_id, body)


def verify(pa_id: str, criterion_id: str, provider_id: str) -> dict:
    return gates.verify_criterion(pa_id, criterion_id, provider_id)


def submit(pa_id: str, provider_id: str) -> dict:
    repo = get_repo()
    pa = _pa(pa_id)
    view = present(pa_id)
    if pa["status"] == "not_required":
        raise ApiError(
            "PA_NOT_REQUIRED",
            "Prior authorization is not required for this order. No packet can be submitted.",
            409,
        )
    if pa["status"] in {"approved", "submitted", "in_review"}:
        raise ApiError(
            "INVALID_TRANSITION",
            f"This request is already {pa['status'].replace('_', ' ')}.",
            409,
        )
    if not view["can_submit"]:
        raise ApiError("NOT_ALL_VERIFIED", "Every rule must be verified before submission.", 409)
    _move(pa, "submitted")
    criteria = _tree(pa_id)
    packet = build_packet(repo.get_pa(pa_id), [c for c in criteria], preview=False)
    packet["packet_version"] = (pa.get("packet_version") or 0) + 1
    repo.update_pa(
        pa_id,
        {
            "status": "submitted",
            "submission_packet": packet,
            "packet_version": packet["packet_version"],
            "submitted_at": packet["submitted_at"],
        },
    )
    recorder = EpisodeRecorder("pa_request", pa_id, None, actor=provider_id)
    episode = recorder.record("submit", f"Packet v{packet['packet_version']} submitted", actor=provider_id)
    repo.add_event(pa_id, "submitted", "Packet submitted", "clinician", episode["id"])
    repo.add_review_log(
        {
            "episode_id": episode["id"],
            "actor": provider_id,
            "actor_role": "clinician",
            "target_table": "pa_requests",
            "target_id": pa_id,
            "action": "submit",
            "before": {"status": pa["status"]},
            "after": {"status": "submitted"},
            "note": None,
        }
    )
    # Persist QuestionnaireResponse snapshot at submit for insurer + export.
    try:
        from app.fhir.builders import questionnaire_response as build_qr

        criteria = _tree(pa_id)
        policy = repo.get_policy(pa["criteria_policy_id"]) if pa.get("criteria_policy_id") else None
        patient = repo.get_patient(pa["patient_id"])
        resource = build_qr(
            {**repo.get_pa(pa_id), "patient": patient},
            criteria,
            policy or {"id": "none", "sha256": "0"},
            completed=True,
        )
        if not pa.get("questionnaire_id"):
            _snapshot_questionnaire(pa_id)
            pa = repo.get_pa(pa_id)
        stored = repo.save_fhir_qr(
            {
                "pa_request_id": pa_id,
                "questionnaire_id": pa.get("questionnaire_id"),
                "resource": resource,
            }
        )
        repo.update_pa(pa_id, {"questionnaire_response_id": stored["id"]})
    except Exception:
        pass
    schedule(pa_id)
    return present(pa_id)


def preview(pa_id: str) -> dict:
    pa = _pa(pa_id)
    if pa["status"] == "not_required":
        raise ApiError(
            "PA_NOT_REQUIRED",
            "Prior authorization is not required for this order. There is no submission packet to preview.",
            409,
        )
    if pa.get("submission_packet") and pa["status"] in {"submitted", "in_review", "approved", "info_requested"}:
        return pa["submission_packet"]
    view = present(pa_id)
    if not view["can_submit"]:
        raise ApiError("NOT_ALL_VERIFIED", "Preview is available once every rule is verified.", 409)
    criteria = _tree(pa_id)
    return build_packet(pa, criteria, preview=True)


def _check(pa, payload, run_id, recorder, preset=None) -> dict:
    repo = get_repo()
    insurer = payload.get("insurer") or pa.get("insurer")
    plan_name = payload.get("plan_name") or pa.get("plan_name")
    plan_year = payload.get("plan_year") if payload.get("plan_year") is not None else pa.get("plan_year")
    matched = preset or service_matcher.match(
        payload["order_text"],
        payload.get("service_code"),
        payload.get("drug_name"),
        payload.get("catalog_item_id"),
        run_id=run_id,
        insurer=insurer,
        plan_name=plan_name,
        plan_year=plan_year,
    )
    if matched["status"] == "block_not_live":
        raise ApiError("BLOCK_NOT_LIVE", "That drug block has not been through review.", 409)
    if matched["status"] == "ambiguous":
        repo.update_pa(pa["id"], {"status": "matching", "match_candidates": matched["candidates"]})
        recorder.record("match", "More than one policy item matches. A clinician chooses.")
        repo.add_event(pa["id"], "matched", "Waiting for a clinician to choose the policy item", "engine")
        return present(pa["id"])
    if matched["status"] == "none":
        # Offer coverage suggestions instead of hard-failing (ingest orders are often short).
        suggestions = service_matcher.coverage_candidates(
            payload["order_text"],
            insurer=insurer,
            plan_name=plan_name,
            plan_year=plan_year,
        )
        if suggestions:
            repo.update_pa(pa["id"], {"status": "matching", "match_candidates": suggestions})
            recorder.record("match", "No exact match; clinician chooses from coverage suggestions.")
            repo.add_event(pa["id"], "matched", "Choose the benefit row that matches this order", "engine")
            return present(pa["id"])
        raise ApiError(
            "POLICY_NOT_FOUND",
            f"No active policy for this plan ({insurer or 'the selected'}"
            + (f" / {plan_name}" if plan_name else "")
            + "). Go live on a matching policy in the Policy library, or pick a different plan.",
            404,
        )

    gate = coverage_gate.evaluate(
        matched,
        payload.get("service_code"),
        payload["order_text"],
        insurer=insurer,
        plan_name=plan_name,
        plan_year=plan_year,
    )
    recorder.record("coverage_gate", gate["note"] or "Coverage gate finished")
    changes = {
        "coverage_note": gate["note"],
        "benefit_summary_id": (gate.get("policy") or {}).get("id") if gate.get("policy") else (gate["item"]["policy_id"] if gate.get("item") else None),
        "coverage_item_id": gate["item"]["id"] if gate.get("item") else None,
    }
    if gate["outcome"] == "not_required":
        _move(pa, "not_required")
        repo.update_pa(pa["id"], {**changes, "status": "not_required", "readiness": 1})
        repo.add_event(pa["id"], "not_required", gate["note"] or "Prior authorization is not required", "engine")
        recorder.record("not_required", "Prior authorization is not required")
        return present(pa["id"])

    items = _criteria_items_for_order(matched, gate, payload["order_text"], payload.get("service_code"))
    if matched["status"] == "drug":
        changes["criteria_block_id"] = matched["block"]["id"]
        changes["criteria_policy_id"] = matched["block"]["policy_id"]
    elif items:
        changes["criteria_policy_id"] = items[0].get("policy_id") or (gate.get("item") or {}).get("policy_id")
    elif gate.get("item"):
        changes["criteria_policy_id"] = gate["item"]["policy_id"]
    repo.update_pa(pa["id"], {**changes, "status": "checking"})
    _move(pa, "checking", current="matching")
    criteria = _build_criteria(pa["id"], items, run_id, coverage_item=gate.get("item"), order_text=payload["order_text"])
    _save_and_score(pa["id"], criteria, recorder, "checked")
    try:
        _snapshot_questionnaire(pa["id"])
    except Exception:
        pass
    return present(pa["id"])


def _evaluate_existing(pa, run_id, recorder, event: str) -> dict:
    repo = get_repo()
    patient = repo.get_patient(pa["patient_id"])
    records = repo.records_for(pa["patient_id"])
    criteria = []
    for current in repo.criteria_for(pa["id"]):
        if current.get("verified_by"):
            answers = repo.answers_for(current["id"])
            criteria.append({**current, "answers": answers})
            continue
        item = repo.get_item(current["policy_item_id"]) if current.get("policy_item_id") else None
        if item is None:
            # Insurer-request (and other non-policy) criteria: re-score answers after doctor evidence.
            answers = repo.answers_for(current["id"])
            judged = evaluate(
                current.get("pass_condition"),
                _answer_map(answers),
                judge_verdict=None,
                uses_llm=False,
            )
            criteria.append(
                {
                    **{k: current[k] for k in current if k not in {"id", "verified_by", "verified_at"}},
                    "status": judged["status"],
                    "status_reason": judged["reason"],
                    "answers": answers,
                }
            )
            continue
        fresh = fill(item, records, patient, run_id=run_id)
        previous = {a["link_id"]: a for a in repo.answers_for(current["id"])}
        merged = []
        for answer in fresh:
            old = previous.get(answer["link_id"])
            if old and (old["edited_by_human"] or (old.get("value") is not None and answer.get("value") is None)):
                if old["edited_by_human"] or old.get("review_state") in {"ai_filled", "clinician_confirmed"}:
                    if old["edited_by_human"]:
                        merged.append({**old, "id": None})
                        continue
            if old and old.get("value") is not None and not old["edited_by_human"] and answer.get("value") is None:
                merged.append({k: v for k, v in old.items() if k != "id"})
                continue
            if old and old.get("edited_by_human"):
                merged.append({k: v for k, v in old.items() if k != "id"})
                continue
            answer.pop("conflict", None)
            merged.append(answer)
        judged = evaluate(
            current["pass_condition"],
            _answer_map(merged),
            judge_verdict=item.get("judge_verdict"),
            uses_llm=any(a.get("fill_method") in {"llm_quote", "llm_quote_value"} for a in merged),
        )
        owner, reason = (None, None)
        if judged["status"] == "missing":
            providers = {p["id"]: p for p in repo.list_providers()}
            owner, reason = find_owner(current["requirement_text"], records, providers)
        criteria.append(
            {
                **{k: current[k] for k in current if k not in {"id", "verified_by", "verified_at"}},
                "status": judged["status"],
                "status_reason": judged["reason"],
                "likely_owner_provider_id": owner["id"] if owner else None,
                "likely_owner_reason": reason,
                "answers": merged,
            }
        )
    _save_and_score(pa["id"], criteria, recorder, event)
    return present(pa["id"])


def _criteria_items_for_order(matched: dict, gate: dict, order_text: str, service_code: str | None) -> list[dict]:
    """Only the clinical rules (or one coverage necessity row) for this order — never the full EOC chart."""
    repo = get_repo()
    raw = list(matched.get("items") or [])
    rules = [item for item in raw if item.get("item_type") == "rule"]
    coverage = gate.get("item")
    if matched.get("status") == "drug":
        return rules or raw
    scoped = _filter_rules_for_order(rules, order_text, service_code, coverage)
    if scoped:
        return scoped
    # Live clinical rules that name this service, even when the matcher returned coverage only.
    live_rules = [
        item
        for item in repo.live_items("rule")
        if _rule_applies_to_order(item, order_text, service_code, coverage)
    ]
    if live_rules:
        return live_rules
    # Coverage says PA required but no clinical questionnaire exists yet: one order-scoped criterion.
    if coverage is not None:
        return [{"_synthetic_from_coverage": True, **coverage}]
    return []


def _filter_rules_for_order(
    rules: list[dict], order_text: str, service_code: str | None, coverage: dict | None
) -> list[dict]:
    if not rules:
        return []
    matched = [rule for rule in rules if _rule_applies_to_order(rule, order_text, service_code, coverage)]
    return matched or rules


def _rule_applies_to_order(rule: dict, order_text: str, service_code: str | None, coverage: dict | None) -> bool:
    from app.check.service_matcher import _codes
    from app.ingest.pa_markers import significant_words

    codes = _codes(rule)
    if service_code and service_code in codes:
        return True
    data = rule.get("data") or {}
    applies = " ".join(str(x) for x in (data.get("applies_to") or []))
    blob = " ".join(
        [
            rule.get("service_label") or "",
            data.get("requirement_text") or "",
            applies,
        ]
    ).lower()
    order_words = significant_words(order_text or "")
    cov_words = significant_words((coverage or {}).get("service_label") or "")
    rule_words = significant_words(blob)
    if order_words and len(order_words & rule_words) >= max(1, min(2, len(order_words) // 2)):
        return True
    if cov_words and len(cov_words & rule_words) >= max(1, min(2, len(cov_words) // 2)):
        return True
    order_low = (order_text or "").lower()
    if any(token and len(token) > 4 and token in blob for token in order_low.split()):
        return True
    return False


def _build_criteria(
    pa_id: str,
    items: list[dict],
    run_id: str,
    *,
    coverage_item: dict | None = None,
    order_text: str = "",
) -> list[dict]:
    repo = get_repo()
    pa = repo.get_pa(pa_id)
    patient = repo.get_patient(pa["patient_id"])
    records = repo.records_for(pa["patient_id"])
    providers = {p["id"]: p for p in repo.list_providers()}
    criteria = []
    seq = 0
    for item in sorted(
        [row for row in items if not row.get("_synthetic_from_coverage") and row.get("item_type") == "rule"],
        key=lambda row: row.get("seq") or 0,
    ):
        seq += 1
        data = item["data"]
        answers = fill(item, records, patient, run_id=run_id)
        conflict = any(a.pop("conflict", None) for a in answers)
        judged = evaluate(
            data.get("pass_condition"),
            _answer_map(answers),
            judge_verdict=item.get("judge_verdict"),
            uses_llm=any((a.get("fill_method") or "").startswith("llm") for a in answers),
        )
        if conflict:
            judged = {"status": "unclear", "reason": "Records conflict for this answer."}
        owner, owner_reason = (None, None)
        if judged["status"] == "missing":
            owner, owner_reason = find_owner(data["requirement_text"], records, providers)
        criteria.append(
            {
                "policy_item_id": item["id"],
                "seq": seq,
                "criterion_key": data["criterion_key"],
                "requirement_text": data["requirement_text"],
                "criterion_type": data["criterion_type"],
                "policy_page": item["page"],
                "origin": "policy",
                "pass_condition": data.get("pass_condition") or {},
                "status": judged["status"],
                "status_reason": judged["reason"],
                "likely_owner_provider_id": owner["id"] if owner else None,
                "likely_owner_reason": owner_reason,
                "answers": answers,
            }
        )
    # One order-scoped medical-necessity criterion when only the benefit chart says PA is required.
    synthetics = [row for row in items if row.get("_synthetic_from_coverage")]
    source = synthetics[0] if synthetics else (coverage_item if not criteria else None)
    if not criteria and source is not None:
        seq += 1
        criteria.append(_synthetic_coverage_criterion(source, seq, records, patient, providers, order_text))
    return criteria


def _synthetic_coverage_criterion(
    coverage: dict,
    seq: int,
    records: list,
    patient: dict | None,
    providers: dict,
    order_text: str,
) -> dict:
    from app.ingest.pa_markers import significant_words

    label = coverage.get("service_label") or order_text or "this service"
    page = coverage.get("page") or 0
    link_id = "medical_necessity"

    def overlaps(text: str, body: str) -> bool:
        wa, wb = significant_words(text or ""), significant_words(body or "")
        return bool(wa and wb and len(wa & wb) >= max(1, min(2, len(wa) // 2)))

    evidence = None
    value = None
    for record in records:
        body = (record.get("body") or "") + " " + str(record.get("value") or "")
        if overlaps(label, body) or overlaps(order_text, body):
            evidence = body.strip()[:240]
            value = True
            break
    judged_status = "met" if value is True else "missing"
    owner, owner_reason = (None, None)
    if judged_status == "missing":
        owner, owner_reason = find_owner(f"medical necessity for {label}", records, providers)
    return {
        "policy_item_id": coverage.get("id"),
        "seq": seq,
        "criterion_key": f"coverage_pa_{(coverage.get('id') or new_id())[:8]}",
        "requirement_text": (
            f"Document medical necessity for {label}. "
            "The plan benefit chart requires prior authorization for this ordered service."
        ),
        "criterion_type": "clinical_note",
        "policy_page": page,
        "origin": "policy",
        "pass_condition": {
            "all": [
                {
                    "q": link_id,
                    "op": "eq",
                    "value": True,
                    "missing": f"No documentation found that medical necessity for {label} is supported.",
                }
            ]
        },
        "status": judged_status,
        "status_reason": None if value else f"No documentation found that medical necessity for {label} is supported.",
        "likely_owner_provider_id": owner["id"] if owner else None,
        "likely_owner_reason": owner_reason,
        "answers": [
            {
                "link_id": link_id,
                "seq": 1,
                "question_text": f"Is medical necessity for {label} documented in the chart?",
                "answer_type": "boolean",
                "enabled": True,
                "value": value,
                "unit": None,
                "fill_method": "llm_quote" if evidence else None,
                "evidence_text": evidence,
                "evidence_record_id": None,
                "review_state": "ai_filled" if value is not None else "unanswered",
                "edited_by_human": False,
                "rejected_ai_value": None,
                "reject_reason": None,
                "attestation": None,
                "answered_by": None,
                "answered_at": None,
            }
        ],
    }


def _save_and_score(pa_id: str, criteria: list[dict], recorder: EpisodeRecorder, event: str) -> None:
    repo = get_repo()
    repo.save_criteria_tree(pa_id, criteria)
    saved = repo.criteria_for(pa_id)
    score, status, met, total = readiness([c["status"] for c in saved])
    pa = repo.get_pa(pa_id)
    if pa["status"] == "not_required":
        return
    if status == "ready_for_review" and pa["status"] in {"needs_info", "checking", "ready_for_review", "info_requested"}:
        target = "ready_for_review"
    elif status == "needs_info":
        target = "needs_info"
    else:
        target = status
    if pa["status"] != target:
        _move(pa, target)
    repo.update_pa(pa_id, {"status": target, "readiness": score})
    message = f"{met} of {total} criteria met"
    recorder.record("evaluate", message)
    repo.add_event(pa_id, event if event in {
        "created", "matched", "not_required", "checked", "rechecked", "answer_rejected", "answer_entered",
        "verified", "submitted", "in_review", "info_requested", "criteria_added", "approved",
    } else "checked", message, "engine")


def _answer_map(answers: list[dict]) -> dict:
    mapped = {}
    for answer in answers:
        value = decode_value(answer.get("value"))
        mapped[answer["link_id"]] = {**answer, "value": value, "enabled": bool(answer.get("enabled", True))}
    return mapped


def _move(pa: dict, target: str, current: str | None = None) -> None:
    state = current or pa["status"]
    if target == state:
        return
    allowed = TRANSITIONS.get(state, set())
    if target not in allowed and not (state == "checking" and target in {"needs_info", "ready_for_review", "not_required"}):
        if target in TRANSITIONS.get(state, set()):
            return
        # checking is set just before scoring; allow the score transition from checking.
        if not (state in {"matching", "checking", "needs_info", "ready_for_review"} and target in {"checking", "needs_info", "ready_for_review", "not_required", "submitted"}):
            raise ApiError("INVALID_TRANSITION", f"Cannot move from {state} to {target}.", 409)


def _pa(pa_id: str) -> dict:
    pa = get_repo().get_pa(pa_id)
    if pa is None:
        raise ApiError("POLICY_NOT_FOUND", "No request with that id.", 404)
    return pa


def _tree(pa_id: str) -> list[dict]:
    repo = get_repo()
    rows = []
    for criterion in repo.criteria_for(pa_id):
        rows.append({**criterion, "questions": repo.answers_for(criterion["id"])})
    return rows


def present(pa_id: str) -> dict:
    repo = get_repo()
    pa = _pa(pa_id)
    # Re-score open rules so Yes/No text answers (e.g. exclusion "No") update status.
    if pa.get("status") in {"needs_info", "ready_for_review", "info_requested"}:
        from app.review.review import _recalculate

        for criterion in repo.criteria_for(pa["id"]):
            if criterion.get("verified_by"):
                continue
            _recalculate(pa, criterion)
        pa = _pa(pa_id)
    patient = repo.get_patient(pa["patient_id"])
    provider = repo.get_provider(pa["ordering_provider_id"])
    criteria = []
    met = 0
    for criterion in repo.criteria_for(pa["id"]):
        if criterion["status"] == "met":
            met += 1
        item = repo.get_item(criterion["policy_item_id"]) if criterion.get("policy_item_id") else None
        owner = repo.get_provider(criterion["likely_owner_provider_id"]) if criterion.get("likely_owner_provider_id") else None
        verifier = repo.get_provider(criterion["verified_by"]) if criterion.get("verified_by") else None
        questions = []
        for answer in repo.answers_for(criterion["id"]):
            source = None
            if answer.get("evidence_record_id"):
                record = repo._one("clinical_records", "select * from clinical_records where id = ?", (answer["evidence_record_id"],))
                if record:
                    author = repo.get_provider(record["author_provider_id"]) if record.get("author_provider_id") else None
                    doc = repo.get_document(record["source_document_id"]) if record.get("source_document_id") else None
                    source = {
                        "author": author["full_name"] if author else None,
                        "date": record.get("start_date"),
                        "document_id": record.get("source_document_id"),
                        "file_name": doc["file_name"] if doc else None,
                        "record_id": record["id"],
                    }
            if source is None and answer.get("source_document_id"):
                doc = repo.get_document(answer["source_document_id"])
                who = repo.get_provider(answer["answered_by"]) if answer.get("answered_by") else None
                source = {
                    "author": who["full_name"] if who else None,
                    "date": (answer.get("answered_at") or "")[:10] or None,
                    "document_id": answer.get("source_document_id"),
                    "file_name": doc["file_name"] if doc else None,
                    "record_id": None,
                }
            questions.append(
                {
                    "id": answer["id"],
                    "link_id": answer["link_id"],
                    "text": answer["question_text"],
                    "answer_type": answer["answer_type"],
                    "enabled": bool(answer.get("enabled")),
                    "value": decode_value(answer.get("value")),
                    "unit": answer.get("unit"),
                    "fill_method": answer.get("fill_method"),
                    "review_state": answer["review_state"],
                    "evidence_text": answer.get("evidence_text"),
                    "evidence_source": source,
                    "edited_by_human": bool(answer.get("edited_by_human")),
                    "rejected_ai_value": decode_value(answer.get("rejected_ai_value")),
                    "reject_reason": answer.get("reject_reason"),
                    "attestation": answer.get("attestation"),
                }
            )
        criteria.append(
            {
                "id": criterion["id"],
                "criterion_key": criterion["criterion_key"],
                "seq": criterion["seq"],
                "requirement_text": criterion["requirement_text"],
                "criterion_type": criterion["criterion_type"],
                "policy_page": criterion["policy_page"],
                "origin": criterion["origin"],
                "status": criterion["status"],
                "status_reason": criterion.get("status_reason"),
                "judge_verdict": item.get("judge_verdict") if item else None,
                "review_state_of_rule": item.get("review_state") if item else None,
                "likely_owner": (
                    {"provider_id": owner["id"], "full_name": owner["full_name"], "reason": criterion.get("likely_owner_reason")}
                    if owner
                    else None
                ),
                "verified_by": (
                    {"provider_id": verifier["id"], "full_name": verifier["full_name"], "at": criterion.get("verified_at")}
                    if verifier
                    else None
                ),
                "questions": questions,
            }
        )
    total = len(criteria)
    all_verified = total > 0 and all(c["verified_by"] and c["status"] == "met" for c in criteria)
    policy = repo.get_policy(pa["criteria_policy_id"]) if pa.get("criteria_policy_id") else None
    coverage_item = repo.get_item(pa["coverage_item_id"]) if pa.get("coverage_item_id") else None
    coverage = _coverage_view(pa, coverage_item)
    qr = None
    if pa.get("questionnaire_response_id"):
        stored_qr = repo.get_fhir_qr(pa["questionnaire_response_id"])
        if stored_qr:
            qr = stored_qr["resource"]
    q_form = None
    if pa.get("questionnaire_id"):
        stored_q = repo.get_fhir_questionnaire(pa["questionnaire_id"])
        if stored_q:
            q_form = stored_q["resource"]
    return {
        "id": pa["id"],
        "status": pa["status"],
        "readiness": pa["readiness"],
        "met_count": met,
        "total_count": total,
        "can_submit": bool(all_verified and pa["status"] == "ready_for_review"),
        "order_text": pa["order_text"],
        "service_code": pa.get("service_code"),
        "service_category": pa.get("service_category") or pa.get("order_text"),
        "drug_name": pa.get("drug_name"),
        "insurance_plan_id": pa.get("insurance_plan_id") or pa.get("benefit_summary_id"),
        "insurer": pa.get("insurer"),
        "plan_name": pa.get("plan_name"),
        "plan_year": pa.get("plan_year"),
        "source_document_reference": pa.get("source_document_reference"),
        "patient": {"id": patient["id"], "full_name": patient["full_name"], "synthetic": bool(patient.get("synthetic", True)), "dob": patient.get("dob")},
        "ordering_provider": {"id": provider["id"], "full_name": provider["full_name"], "specialty": provider.get("specialty")},
        "coverage": coverage,
        "pa_determination": _pa_determination(pa, coverage),
        "questionnaire": q_form,
        "questionnaire_response": qr,
        "criteria_source": (
            {
                "policy_id": policy["id"],
                "title": policy.get("file_name"),
                "source_kind": policy["source_kind"],
                "source_url": policy.get("source_url"),
                "insurer": policy.get("insurer"),
            }
            if policy
            else None
        ),
        "match_candidates": pa.get("match_candidates"),
        "criteria": criteria,
        "events": repo.events_for(pa["id"]),
        "episodes": repo.episodes_for("pa_request", pa["id"]),
    }


def _pa_determination(pa: dict, coverage: dict | None) -> dict:
    if pa["status"] == "draft":
        return {
            "requirement": "pending_confirm",
            "label": "Confirm the order to determine whether prior authorization is required",
            "pa_required": None,
        }
    if pa["status"] == "not_required" or (coverage and coverage.get("pa_required") is False):
        service = (coverage or {}).get("service_label") or pa.get("service_category") or pa.get("order_text")
        return {
            "requirement": "not_required",
            "label": "Prior authorization is not required for this treatment under this plan",
            "pa_required": False,
            "page": (coverage or {}).get("page"),
            "evidence_text": (coverage or {}).get("evidence_text") or pa.get("coverage_note"),
            "service_label": service,
            "document_url": (coverage or {}).get("document_url"),
        }
    status = (coverage or {}).get("pa_status") or "required"
    if status == "conditional":
        label = "Prior authorization may be required (conditional) for this treatment"
    else:
        label = "Prior authorization is required for this treatment under this plan"
    return {
        "requirement": status if status in {"required", "conditional", "not_required"} else "required",
        "label": label,
        "pa_required": True,
        "page": (coverage or {}).get("page"),
        "evidence_text": (coverage or {}).get("evidence_text"),
    }


def _coverage_view(pa: dict, item: dict | None) -> dict | None:
    if pa["status"] == "not_required" or item:
        data = (item or {}).get("data") or {}
        pa_status = data.get("pa_status")
        if not pa_status:
            pa_status = "not_required" if pa["status"] == "not_required" else ("required" if data.get("pa_required", True) else "not_required")
        return {
            "pa_required": bool(pa_status != "not_required" and pa["status"] != "not_required"),
            "pa_status": pa_status,
            "service_label": data.get("service_label") or item.get("service_label") if item else None,
            "page": item["page"] if item else None,
            "evidence_text": data.get("evidence_text") or pa.get("coverage_note"),
            "document_url": f"/policies/{item['policy_id']}/file" if item else None,
        }
    if pa.get("coverage_note"):
        return {
            "pa_required": True,
            "pa_status": "required",
            "service_label": pa.get("service_category") or pa.get("order_text"),
            "page": None,
            "evidence_text": pa["coverage_note"],
            "document_url": None,
        }
    return None
