"""Gate 1 and Gate 2 routes."""

from __future__ import annotations

from fastapi import APIRouter, Body

from app.repository import get_repo, new_id
from app.review.present import present_item
from app.review.review import (
    accept,
    apply_suggestion,
    edit,
    go_live,
    queue,
    reject,
    reset_review_queue_for_demo,
    take_offline,
)

router = APIRouter(prefix="/policies")


@router.post("/reset-review-queue")
def reset_review_queue(body: dict = Body(default={})):
    """Doctor-logout HITL demo: reset Gate-1 review_state only (not live/PA history)."""
    actor = body.get("actor") or "doctor_logout"
    return reset_review_queue_for_demo(actor=str(actor))


@router.get("/{policy_id}/review-queue")
def review_queue(policy_id: str):
    from app.pipeline.cache import get, stage_key

    policy = get_repo().get_policy(policy_id)
    pages = get(stage_key(policy["sha256"], "clean")) if policy else None
    page_rows = (pages or {}).get("pages") if isinstance(pages, dict) else []
    text = {row["page"]: row["text"] for row in page_rows or []}
    items = []
    for item in queue(policy_id):
        data = item["data"]
        shown = present_item(item)
        items.append(
            {
                "id": item["id"],
                "item_key": item["item_key"],
                "item_type": item["item_type"],
                "seq": item["seq"],
                "page": item["page"],
                "page_text": text.get(item["page"], ""),
                "review_state": item["review_state"],
                "judge_verdict": item.get("judge_verdict"),
                "judge_reason": item.get("judge_reason"),
                "question_verdict": item.get("question_verdict"),
                "grounding": item.get("grounding"),
                "edited_by_human": bool(item.get("edited_by_human")),
                "suggestion": item.get("suggestion"),
                "review_note": item.get("review_note"),
                "summary": shown["title"],
                "title": shown["title"],
                "cost_share": shown["cost_share"],
                "pa_required": shown["pa_required"],
                "pa_status": shown.get("pa_status"),
                "marker_used": shown.get("marker_used"),
                "service_codes": shown.get("service_codes") or [],
                "listing_index": shown.get("listing_index"),
                "has_exception": shown["has_exception"],
                "exception": shown["exception"],
                "limits": shown["limits"],
                "evidence": shown["evidence"],
                "why": shown["why"],
                "criteria": shown["criteria"],
                "data": data,
                "original_data": item.get("original_data"),
            }
        )
    return {"items": items}


@router.post("/{policy_id}/items/{item_id}/accept")
def accept_item(policy_id: str, item_id: str, body: dict):
    return accept(policy_id, item_id, body["reviewer"], body.get("note"))


@router.post("/{policy_id}/items/{item_id}/edit")
def edit_item(policy_id: str, item_id: str, body: dict):
    run = get_repo().create_run(policy_id, f"edit-{new_id()[:8]}")
    try:
        return edit(policy_id, item_id, body["reviewer"], body.get("data") or {}, body.get("note"), run["id"])
    finally:
        get_repo().finish_run(run["id"], "complete")


@router.post("/{policy_id}/items/{item_id}/reject")
def reject_item(policy_id: str, item_id: str, body: dict):
    return reject(policy_id, item_id, body["reviewer"], body.get("note") or "")


@router.post("/{policy_id}/items/{item_id}/apply-suggestion")
def suggestion(policy_id: str, item_id: str, body: dict):
    return apply_suggestion(policy_id, item_id, body["reviewer"])


@router.post("/{policy_id}/go-live")
def live(policy_id: str, body: dict):
    return go_live(policy_id, body["reviewer"])


@router.post("/{policy_id}/take-offline")
def offline(policy_id: str, body: dict):
    return take_offline(policy_id, body["reviewer"])


@router.post("/{policy_id}/blocks/{block_id}/go-live")
def block_live(policy_id: str, block_id: str, body: dict):
    return go_live(policy_id, body["reviewer"], block_id=block_id)


@router.post("/{policy_id}/blocks/{block_id}/take-offline")
def block_offline(policy_id: str, block_id: str, body: dict):
    return take_offline(policy_id, body["reviewer"], block_id=block_id)


@router.get("/{policy_id}/questionnaires")
def list_questionnaires(policy_id: str):
    from app.check import service as check_service

    return check_service.list_policy_questionnaires(policy_id)


@router.delete("/{policy_id}/questionnaires/{questionnaire_id}")
def delete_questionnaire(policy_id: str, questionnaire_id: str):
    from app.check import service as check_service

    return check_service.delete_questionnaire(policy_id, questionnaire_id)


@router.post("/{policy_id}/questionnaires/{questionnaire_id}/regenerate")
def regenerate_questionnaire(policy_id: str, questionnaire_id: str, body: dict | None = None):
    from app.check import service as check_service

    body = body or {}
    return check_service.regenerate_questionnaire(
        policy_id,
        questionnaire_id,
        reviewer=body.get("reviewer") or "reviewer",
    )
