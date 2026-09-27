"""The only module that sets review states."""

from __future__ import annotations

from app.errors import ApiError
from app.ingest.grounding import check as ground
from app.ingest.question_builder import build as build_questions
from app.pipeline.episodes import EpisodeRecorder
from app.repository import get_repo, now

RISK = {
    "HALLUCINATED": 1,
    "WRONG_VALUE": 2,
    "GROUNDING": 3,
    "QUESTIONS": 4,
    "VAGUE": 5,
    "UNAVAILABLE": 5,
    "ACCURATE": 6,
}

DECIDED = {"accepted", "edited", "rejected"}


def initial_state(grounding: dict, verdict: str, question_verdict: str | None) -> str:
    """auto_approved is routing, not approval."""
    if (
        grounding.get("passed")
        and verdict == "ACCURATE"
        and question_verdict in {None, "complete"}
    ):
        return "auto_approved"
    return "pending_review"


def risk_rank(item: dict) -> tuple[int, int]:
    if not (item.get("grounding") or {}).get("passed", True):
        rank = RISK["GROUNDING"]
    elif item.get("question_verdict") not in {None, "complete"}:
        rank = RISK["QUESTIONS"]
    else:
        rank = RISK.get(item.get("judge_verdict") or "UNAVAILABLE", 5)
    return rank, item.get("seq") or 0


def queue(policy_id: str) -> list[dict]:
    items = get_repo().items_for(policy_id)
    # Rejected rows stay in the DB for audit but are not Gate-1 work.
    open_items = [item for item in items if item.get("review_state") != "rejected"]
    return sorted(open_items, key=risk_rank)


def accept(policy_id: str, item_id: str, reviewer: str, note: str | None = None) -> dict:
    repo = get_repo()
    item = _owned(policy_id, item_id)
    if not (item.get("grounding") or {}).get("passed") and not (note and note.strip()):
        raise ApiError(
            "REASON_REQUIRED",
            "This item failed grounding. Add a note, or edit it, before accepting.",
            400,
        )
    before = item["review_state"]
    updated = repo.write_item(
        item_id,
        {
            "review_state": "accepted",
            "reviewed_by": reviewer,
            "reviewed_at": now(),
            "review_note": note,
        },
        actor=reviewer,
    )
    _log(reviewer, "policy_reviewer", "policy_items", item_id, "accept", {"review_state": before}, {"review_state": "accepted"}, note, policy_id)
    return updated


def edit(policy_id: str, item_id: str, reviewer: str, data: dict, note: str | None, run_id: str) -> dict:
    repo = get_repo()
    item = _owned(policy_id, item_id)
    policy = repo.get_policy(policy_id)
    page_text = _page_text(policy, data.get("policy_page") or data.get("page") or item["page"])
    merged = {**item["data"], **data}
    if item["item_type"] == "rule":
        built = build_questions(merged, run_id=run_id)
        merged["questions"] = _keep_locked_text(item["data"].get("questions") or [], built["questions"])
        merged["pass_condition"] = built["pass_condition"]
        question_verdict = built["question_verdict"]
    else:
        question_verdict = item.get("question_verdict")
    grounding = ground(
        {
            "evidence_text": merged.get("evidence_text"),
            "requirement_text": merged.get("requirement_text") or merged.get("service_label"),
            "codes": merged.get("codes") or merged.get("service_codes") or [],
            "conditions": merged.get("conditions") or [],
        },
        page_text,
    )
    before = {"review_state": item["review_state"], "data": item["data"]}
    updated = repo.write_item(
        item_id,
        {
            "data": merged,
            "page": merged.get("policy_page") or merged.get("page") or item["page"],
            "service_label": merged.get("service_label") or item.get("service_label"),
            "service_codes": merged.get("service_codes") or merged.get("codes") or item.get("service_codes"),
            "grounding": grounding,
            "question_verdict": question_verdict,
            "review_state": "edited",
            "edited_by_human": True,
            "reviewed_by": reviewer,
            "reviewed_at": now(),
            "review_note": note,
            "suggestion": None,
        },
        actor=reviewer,
    )
    _log(reviewer, "policy_reviewer", "policy_items", item_id, "edit", before, {"data": merged}, note, policy_id)
    return updated


def reject(policy_id: str, item_id: str, reviewer: str, note: str) -> dict:
    if not note or not note.strip():
        raise ApiError("REASON_REQUIRED", "A note is required to reject an item.", 400)
    repo = get_repo()
    item = _owned(policy_id, item_id)
    updated = repo.write_item(
        item_id,
        {
            "review_state": "rejected",
            "reviewed_by": reviewer,
            "reviewed_at": now(),
            "review_note": note,
        },
        actor=reviewer,
    )
    _log(reviewer, "policy_reviewer", "policy_items", item_id, "reject", {"review_state": item["review_state"]}, {"review_state": "rejected"}, note, policy_id)
    return updated


def apply_suggestion(policy_id: str, item_id: str, reviewer: str) -> dict:
    repo = get_repo()
    item = _owned(policy_id, item_id)
    suggestion = item.get("suggestion") or {}
    changes = suggestion.get("changes") or {}
    if not changes:
        raise ApiError("POLICY_NOT_FOUND", "This item has no suggestion to apply.", 404)
    changes["edited_by_human"] = True
    changes["review_state"] = "edited"
    changes["suggestion"] = None
    changes["reviewed_by"] = reviewer
    changes["reviewed_at"] = now()
    updated = repo.write_item(item_id, changes, actor=reviewer)
    _log(reviewer, "policy_reviewer", "policy_items", item_id, "apply_suggestion", item["data"], changes.get("data"), None, policy_id)
    return updated


def go_live(policy_id: str, reviewer: str, *, block_id: str | None = None) -> dict:
    from app.fhir.builders import build_for_policy
    from app.fhir.validate import validate_resource

    repo = get_repo()
    policy = repo.get_policy(policy_id)
    if policy is None:
        raise ApiError("POLICY_NOT_FOUND", "No policy with that id.", 404)
    if policy["status"] not in {"draft", "archived", "live"}:
        raise ApiError(
            "INVALID_TRANSITION",
            f"Go live needs a draft or archived policy (currently {policy['status']}).",
            409,
        )
    items = repo.items_for(policy_id, block_id)
    if any(item["review_state"] not in DECIDED for item in items):
        raise ApiError("ITEMS_UNDECIDED", "Every item needs a human decision before go-live.", 409)
    active = [item for item in items if item["review_state"] != "rejected"]
    if not active:
        raise ApiError("ITEMS_UNDECIDED", "At least one item must be accepted or edited.", 409)
    if any(item.get("question_verdict") not in {None, "complete"} and item["item_type"] == "rule" for item in active):
        raise ApiError("QUESTIONS_INCOMPLETE", "Condition coverage is below 100%.", 409)
    resource = build_for_policy(policy, active, block_id=block_id, status="active")
    errors = validate_resource(resource)
    if errors:
        raise ApiError("FHIR_INVALID", "FHIR validation failed: " + "; ".join(errors), 409)
    if block_id:
        repo.update_block(
            block_id,
            {"status": "live", "went_live_by": reviewer, "went_live_at": now(), "fhir_questionnaire": resource},
        )
    history = _append_status_history(policy, "live", reviewer)
    changes = {
        "status": "live",
        "went_live_by": reviewer,
        "went_live_at": now(),
        "status_history": history,
        "validation_report": {
            **(policy.get("validation_report") or {}),
            "fhir": {"valid": True, "errors": []},
        },
    }
    if resource.get("resourceType") == "Questionnaire":
        changes["fhir_questionnaire"] = resource
    else:
        changes["fhir_insurance_plan"] = resource
    updated = repo.update_policy(policy_id, changes, actor=reviewer)
    _log(reviewer, "policy_reviewer", "policies", policy_id, "go_live", {"status": policy["status"]}, {"status": "live"}, None, policy_id)
    _refresh_plan_memory(updated)
    return updated


def reset_review_queue_for_demo(*, actor: str = "doctor_logout") -> dict:
    """HITL demo: put Gate-1 review rows back to pending_review on doctor logout.

    Does not change policy live/archived status, PARequest history, questionnaires,
    or insurer decisions. Leaves mass auto-rejected EOC fragments (no listing_index)
    rejected so the queue stays ~listing-sized rather than hundreds of junk rows.
    """
    repo = get_repo()
    reset_count = 0
    policies_touched: list[str] = []
    for policy in repo.list_policies(include_drafts=True):
        policy_id = policy["id"]
        touched = False
        for item in repo.items_for(policy_id):
            state = item.get("review_state") or ""
            # Only Gate-1 open-queue rows (accepted / edited / pending / auto). Do not
            # resurrect mass auto-rejected EOC fragments or session rejects that were
            # already removed from the work queue — keeps Needs review near listing size.
            if state not in {"accepted", "edited", "pending_review", "auto_approved"}:
                continue
            already_clean = (
                state == "pending_review"
                and not item.get("reviewed_by")
                and not item.get("edited_by_human")
            )
            if already_clean:
                continue
            repo.write_item(
                item["id"],
                {
                    "review_state": "pending_review",
                    "reviewed_by": None,
                    "reviewed_at": None,
                    "review_note": "Reset on doctor logout (HITL demo)",
                    "edited_by_human": 0,
                },
                actor=actor,
            )
            reset_count += 1
            touched = True
        if touched:
            policies_touched.append(policy_id)
    return {"ok": True, "reset_count": reset_count, "policies": policies_touched}


def take_offline(policy_id: str, reviewer: str, *, block_id: str | None = None) -> dict:
    """live -> archived. Keeps extracted rules, FHIR blobs, and past PA decisions."""
    repo = get_repo()
    policy = repo.get_policy(policy_id)
    if policy is None:
        raise ApiError("POLICY_NOT_FOUND", "No policy with that id.", 404)
    if policy["status"] != "live":
        raise ApiError("INVALID_TRANSITION", "Only a live policy can be taken offline.", 409)
    if block_id:
        block = repo.get_block(block_id)
        if block is None or block["policy_id"] != policy_id:
            raise ApiError("POLICY_NOT_FOUND", "No block with that id.", 404)
        repo.update_block(block_id, {"status": "archived"})
    history = _append_status_history(policy, "archived", reviewer)
    updated = repo.update_policy(
        policy_id,
        {"status": "archived", "status_history": history},
        actor=reviewer,
    )
    _log(
        reviewer,
        "policy_reviewer",
        "policies",
        policy_id,
        "take_offline",
        {"status": "live"},
        {"status": "archived"},
        None,
        policy_id,
    )
    return updated


def _append_status_history(policy: dict, status: str, changed_by: str) -> list[dict]:
    history = list(policy.get("status_history") or [])
    history.append({"status": status, "changed_by": changed_by, "timestamp": now()})
    return history


def confirm_role(policy_id: str, reviewer: str, document_role: str) -> dict:
    repo = get_repo()
    policy = repo.get_policy(policy_id)
    if policy is None:
        raise ApiError("POLICY_NOT_FOUND", "No policy with that id.", 404)
    updated = repo.update_policy(
        policy_id,
        {"document_role": document_role, "role_confirmed_by": reviewer, "status": "ingesting"},
        actor=reviewer,
    )
    _log(reviewer, "policy_reviewer", "policies", policy_id, "confirm_role", {"document_role": policy["document_role"]}, {"document_role": document_role}, None, policy_id)
    return updated


def edit_identity(policy_id: str, reviewer: str, insurer: str | None, plan_name: str | None, plan_year: str | None) -> dict:
    repo = get_repo()
    policy = repo.get_policy(policy_id)
    if policy is None:
        raise ApiError("POLICY_NOT_FOUND", "No policy with that id.", 404)
    identity = policy.get("identity_evidence") or {}
    if insurer is not None:
        identity.setdefault("insurer", {})["value"] = insurer
    if plan_name is not None:
        identity.setdefault("plan_name", {})["value"] = plan_name
    if plan_year is not None:
        identity.setdefault("plan_year", {})["value"] = plan_year
    updated = repo.update_policy(
        policy_id,
        {
            "insurer": insurer if insurer is not None else policy.get("insurer"),
            "plan_name": plan_name if plan_name is not None else policy.get("plan_name"),
            "plan_year": plan_year if plan_year is not None else policy.get("plan_year"),
            "identity_evidence": identity,
            "identity_edited_by_human": True,
        },
        actor=reviewer,
    )
    _log(reviewer, "policy_reviewer", "policies", policy_id, "identity_edit", policy.get("identity_evidence"), identity, None, policy_id)
    return updated


def _keep_locked_text(previous: list[dict], rebuilt: list[dict]) -> list[dict]:
    locked = {q["link_id"]: q["text"] for q in previous if q.get("text_edited_by_human")}
    for question in rebuilt:
        if question["link_id"] in locked:
            question["text"] = locked[question["link_id"]]
            question["text_edited_by_human"] = True
    return rebuilt


def _owned(policy_id: str, item_id: str) -> dict:
    item = get_repo().get_item(item_id)
    if item is None or item["policy_id"] != policy_id:
        raise ApiError("POLICY_NOT_FOUND", "That item is not on this policy.", 404)
    return item


def _page_text(policy: dict, page: int) -> str:
    from app.pipeline.cache import get, stage_key

    cached = get(stage_key(policy["sha256"], "pages")) or []
    for entry in cached:
        if entry.get("page") == page:
            return entry.get("text") or ""
    return ""


def _log(actor, role, table, target, action, before, after, note, policy_id: str) -> None:
    repo = get_repo()
    recorder = EpisodeRecorder("policy", policy_id, None, actor=actor)
    episode = recorder.record(f"gate1_{action}" if action != "go_live" else "go_live", f"{actor} {action}", actor=actor)
    repo.add_review_log(
        {
            "episode_id": episode["id"],
            "actor": actor,
            "actor_role": role,
            "target_table": table,
            "target_id": target,
            "action": action,
            "before": before,
            "after": after,
            "note": note,
        }
    )


def reject_answer(pa_id: str, answer_id: str, provider_id: str, reason: str) -> dict:
    repo = get_repo()
    answer, criterion, pa = _answer_context(pa_id, answer_id)
    if not repo.get_provider(provider_id):
        raise ApiError("POLICY_NOT_FOUND", "That clinician is not on file.", 404)
    before = {"value": answer.get("value"), "review_state": answer["review_state"]}
    repo.write_answer(
        answer_id,
        {
            "rejected_ai_value": answer.get("value"),
            "value": None,
            "review_state": "clinician_rejected",
            "edited_by_human": True,
            "reject_reason": reason,
            "answered_by": provider_id,
            "answered_at": now(),
            "fill_method": answer.get("fill_method"),
        },
        actor=provider_id,
    )
    _log_clinician(provider_id, "pa_answers", answer_id, "answer_reject", before, {"review_state": "clinician_rejected"}, reason, pa_id)
    _recalculate(pa, criterion)
    repo.add_event(pa_id, "answer_rejected", "A clinician set an answer aside", "clinician")
    return repo.get_pa(pa_id)


def enter_answer(pa_id: str, answer_id: str, body: dict) -> dict:
    import json as _json

    from app.config import settings

    repo = get_repo()
    answer, criterion, pa = _answer_context(pa_id, answer_id)
    provider_id = body.get("provider_id")
    attestation = (body.get("attestation") or "").strip()
    evidence = (body.get("evidence_text") or "").strip()
    source_kind = (body.get("source_kind") or "chart_document").strip().lower()
    source_id = (body.get("source_document_id") or "").strip()
    if not repo.get_provider(provider_id):
        raise ApiError("POLICY_NOT_FOUND", "That clinician is not on file.", 404)
    if len(attestation) < 10:
        raise ApiError("ATTESTATION_REQUIRED", "An attestation of at least 10 characters is required.", 400)
    if not evidence:
        raise ApiError("ATTESTATION_REQUIRED", "Evidence text is required.", 400)
    if "value" not in body:
        raise ApiError("REASON_REQUIRED", "A value is required.", 400)
    value = _coerce_entered_value(answer.get("answer_type") or "string", body["value"])

    allowed = {"chart_document", "clinician_note", "not_in_chart"}
    if source_kind not in allowed:
        raise ApiError(
            "REASON_REQUIRED",
            "Source must be a chart document, clinician note, or not-documented attestation.",
            400,
        )

    if source_kind == "chart_document":
        if not source_id:
            raise ApiError("ATTESTATION_REQUIRED", "Pick a chart document, or choose another source option.", 400)
        document = repo.get_document(source_id)
        if document is None or document.get("patient_id") != pa["patient_id"]:
            raise ApiError("POLICY_NOT_FOUND", "That source document is not on this patient's chart.", 404)
        if not document.get("in_chart"):
            repo.update_document(source_id, {"in_chart": 1})
    else:
        # H4 still requires a source: create a labeled clinician note (or absence note) on the chart.
        stamp = now()[:10]
        if source_kind == "not_in_chart":
            file_name = f"Not documented — clinician attestation ({stamp}).txt"
            doc_type = "absence_attestation"
            header = "Clinician attestation: finding not documented in the chart."
        else:
            file_name = f"Clinician note ({stamp}).txt"
            doc_type = "clinician_note"
            header = "Clinician chart note entered during prior authorization."
        settings.storage_dir.mkdir(parents=True, exist_ok=True)
        dest = settings.storage_dir / f"{answer_id[:8]}-{stamp}-{doc_type}.txt"
        dest.write_text(
            f"{header}\n\nEvidence:\n{evidence}\n\nAttestation:\n{attestation}\n",
            encoding="utf-8",
        )
        document = repo.create_document(
            {
                "patient_id": pa["patient_id"],
                "file_name": file_name,
                "storage_path": str(dest),
                "doc_type": doc_type,
                "in_chart": 1,
                "synthetic": 1,
            }
        )
        source_id = document["id"]

    record = repo.insert_record(
        {
            "patient_id": pa["patient_id"],
            "resource_type": "DocumentReference",
            "record_kind": source_kind if source_kind != "chart_document" else "clinician_evidence",
            "body": evidence,
            "author_provider_id": provider_id,
            "source_document_id": source_id,
            "start_date": now()[:10],
            "synthetic": 1 if source_kind != "chart_document" else 0,
        }
    )
    repo.write_answer(
        answer_id,
        {
            "value": _json.dumps(value),
            "unit": body.get("unit") or answer.get("unit"),
            "evidence_text": evidence,
            "evidence_record_id": record["id"],
            "source_document_id": source_id,
            "review_state": "clinician_entered",
            "edited_by_human": True,
            "fill_method": "clinician_entered",
            "attestation": attestation,
            "answered_by": provider_id,
            "answered_at": now(),
            "enabled": 1,
        },
        actor=provider_id,
    )
    _log_clinician(provider_id, "pa_answers", answer_id, "answer_enter", {"value": answer.get("value")}, {"value": value, "source_kind": source_kind}, attestation, pa_id)
    _recalculate(pa, criterion)
    repo.add_event(pa_id, "answer_entered", "A clinician entered an answer", "clinician")
    return repo.get_pa(pa_id)


def _coerce_entered_value(answer_type: str, value):
    from app.check.pass_eval import _as_bool
    from app.errors import ApiError

    if answer_type == "boolean":
        coerced = _as_bool(value)
        if not isinstance(coerced, bool):
            raise ApiError("REASON_REQUIRED", "Use Yes or No for this question.", 400)
        return coerced
    return value


def verify_criterion(pa_id: str, criterion_id: str, provider_id: str) -> dict:
    repo = get_repo()
    criterion = repo.get_criterion(criterion_id)
    pa = repo.get_pa(pa_id)
    if criterion is None or pa is None or criterion["pa_request_id"] != pa_id:
        raise ApiError("POLICY_NOT_FOUND", "That rule is not on this request.", 404)
    if criterion["status"] != "met":
        raise ApiError("CRITERION_NOT_MET", "Only a met rule can be verified.", 409)
    if not repo.get_provider(provider_id):
        raise ApiError("POLICY_NOT_FOUND", "That clinician is not on file.", 404)
    repo.update_criterion(criterion_id, {"verified_by": provider_id, "verified_at": now()})
    for answer in repo.answers_for(criterion_id):
        if answer["review_state"] == "ai_filled" and answer.get("enabled"):
            repo.write_answer(answer["id"], {"review_state": "clinician_confirmed"}, actor=provider_id)
    _log_clinician(provider_id, "pa_criteria", criterion_id, "verify", {"verified_by": None}, {"verified_by": provider_id}, None, pa_id)
    repo.add_event(pa_id, "verified", "A clinician verified a rule", "clinician")
    return repo.get_pa(pa_id)


def _answer_context(pa_id: str, answer_id: str):
    repo = get_repo()
    answer = repo.get_answer(answer_id)
    if answer is None:
        raise ApiError("POLICY_NOT_FOUND", "That answer does not exist.", 404)
    criterion = repo.get_criterion(answer["pa_criterion_id"])
    pa = repo.get_pa(pa_id)
    if criterion is None or pa is None or criterion["pa_request_id"] != pa_id:
        raise ApiError("POLICY_NOT_FOUND", "That answer is not on this request.", 404)
    if pa["status"] in {"submitted", "in_review", "approved"}:
        raise ApiError("INVALID_TRANSITION", "Answers are locked after submission.", 409)
    if criterion.get("verified_by"):
        raise ApiError("INVALID_TRANSITION", "This rule is already verified.", 409)
    return answer, criterion, pa


def _recalculate(pa: dict, criterion: dict) -> None:
    from app.check.answer_filler import decode_value
    from app.check.pass_eval import evaluate
    from app.check.scorer import readiness

    repo = get_repo()
    item = repo.get_item(criterion["policy_item_id"]) if criterion.get("policy_item_id") else None
    answers = repo.answers_for(criterion["id"])
    mapped = {}
    for answer in answers:
        mapped[answer["link_id"]] = {
            **answer,
            "value": decode_value(answer.get("value")),
            "enabled": bool(answer.get("enabled", True)),
        }
    judged = evaluate(
        criterion["pass_condition"],
        mapped,
        judge_verdict=(item or {}).get("judge_verdict"),
        uses_llm=any((a.get("fill_method") or "").startswith("llm") for a in answers),
    )
    repo.update_criterion(criterion["id"], {"status": judged["status"], "status_reason": judged["reason"], "verified_by": None, "verified_at": None})
    statuses = [row["status"] if row["id"] != criterion["id"] else judged["status"] for row in repo.criteria_for(pa["id"])]
    score, status, _, _ = readiness(statuses)
    target = "ready_for_review" if status == "ready_for_review" else "needs_info"
    repo.update_pa(pa["id"], {"readiness": score, "status": target})


def _log_clinician(actor, table, target, action, before, after, note, pa_id: str) -> None:
    repo = get_repo()
    recorder = EpisodeRecorder("pa_request", pa_id, None, actor=actor)
    episode = recorder.record(action, f"{actor} {action}", actor=actor)
    repo.add_review_log(
        {
            "episode_id": episode["id"],
            "actor": actor,
            "actor_role": "clinician",
            "target_table": table,
            "target_id": target,
            "action": action,
            "before": before,
            "after": after,
            "note": note,
        }
    )


def _refresh_plan_memory(policy: dict) -> None:
    if not policy.get("insurer") or not policy.get("plan_name"):
        return
    repo = get_repo()
    items = [
        item["data"]
        for item in repo.items_for(policy["id"])
        if item["review_state"] != "rejected"
    ]
    memory = {"items": items, "policy_id": policy["id"]}
    existing = repo._one(
        "plan_memory",
        "select * from plan_memory where insurer = ? and plan_name = ? and plan_year = ?",
        (policy["insurer"], policy["plan_name"], policy.get("plan_year")),
    )
    if existing:
        repo._write(
            "update plan_memory set memory = ?, updated_at = ? where id = ?",
            (__import__("json").dumps(memory), now(), existing["id"]),
        )
    else:
        repo._insert_raw(
            "plan_memory",
            {
                "id": __import__("uuid").uuid4().__str__(),
                "insurer": policy["insurer"],
                "plan_name": policy["plan_name"],
                "plan_year": policy.get("plan_year"),
                "memory": memory,
                "updated_at": now(),
            },
        )
