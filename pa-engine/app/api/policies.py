"""Policy library routes. No business logic."""

from __future__ import annotations

from fastapi import APIRouter, File, Form, UploadFile
from fastapi.responses import FileResponse, PlainTextResponse

from app.ingest.catalog_resolve import service_codes_for
from app.pipeline import pipeline_runner
from app.pipeline.pipeline_runner import upload_response
from app.repository import get_repo, payer_label
from app.review.audit_export import render
from app.review.review import confirm_role, edit_identity

router = APIRouter(prefix="/policies")


@router.post("")
async def upload(
    file: UploadFile = File(...),
    document_role: str = Form(...),
    source_url: str | None = Form(None),
    downloaded_at: str | None = Form(None),
    source_kind: str = Form("published"),
):
    data = await file.read()
    return pipeline_runner.ingest_upload(
        data,
        file.filename or "policy.pdf",
        document_role,
        source_url,
        downloaded_at,
        source_kind,
    )


@router.get("")
def list_policies(include_drafts: bool = False):
    repo = get_repo()
    policies = repo.list_policies(include_drafts)
    return {
        "policies": [_summary(p) for p in policies],
        "insurers": _insurers(policies if not include_drafts else repo.list_policies(False)),
        "services": _services(),
        "drugs": _drugs(),
    }


@router.get("/{policy_id}")
def get_policy(policy_id: str):
    from app.errors import ApiError

    policy = get_repo().get_policy(policy_id)
    if policy is None:
        raise ApiError("POLICY_NOT_FOUND", "No policy with that id.", 404)
    body = pipeline_runner.upload_response(policy, cached=False)
    body["file_name"] = policy["file_name"]
    body["source_url"] = policy.get("source_url")
    body["context_report"] = policy.get("context_report")
    body["format_hints"] = policy.get("format_hints")
    body["went_live_by"] = policy.get("went_live_by")
    body["went_live_at"] = policy.get("went_live_at")
    body["status_history"] = policy.get("status_history") or []
    body["episodes"] = get_repo().episodes_for("policy", policy_id)
    body["blocks"] = get_repo().blocks_for(policy_id)
    body["ingestion_running"] = pipeline_runner.ingestion_running(policy_id)
    from app.pipeline.progress import ingestion_steps, pages_preview

    body["ingestion_steps"] = ingestion_steps(policy_id)
    preview = pages_preview(policy_id)
    body["page_count"] = preview["page_count"]
    body["pages_ready"] = preview["page_count"] > 0
    return body


@router.get("/{policy_id}/pages")
def policy_pages(policy_id: str):
    """Page previews as soon as pdfplumber finishes (overview Job Progress pattern)."""
    from app.pipeline.progress import pages_preview

    return pages_preview(policy_id)


@router.delete("/{policy_id}")
def delete_policy(policy_id: str):
    import sqlite3

    from app.errors import ApiError

    repo = get_repo()
    if repo.get_policy(policy_id) is None:
        raise ApiError("POLICY_NOT_FOUND", "No policy with that id.", 404)
    # Stop a background read if one is still attached to this id.
    try:
        from app.pipeline import pipeline_runner

        with pipeline_runner._inflight_lock:
            pipeline_runner._inflight.pop(policy_id, None)
    except Exception:
        pass
    try:
        return repo.delete_policy(policy_id)
    except sqlite3.OperationalError as exc:
        raise ApiError("ENGINE_BUSY", f"Could not delete that policy right now: {exc}", 503) from exc
    except Exception as exc:
        raise ApiError("INVALID_PDF", f"Could not delete that policy: {exc}", 400) from exc


@router.get("/{policy_id}/file")
def policy_file(policy_id: str):
    from app.errors import ApiError

    policy = get_repo().get_policy(policy_id)
    if policy is None:
        raise ApiError("POLICY_NOT_FOUND", "No policy with that id.", 404)
    return FileResponse(policy["storage_path"], media_type="application/pdf", filename=policy["file_name"])


@router.post("/{policy_id}/confirm-role")
def confirm(policy_id: str, body: dict):
    """Confirm document role, then continue ingestion in the background (EOCs can be long)."""
    updated = confirm_role(policy_id, body["reviewer"], body["document_role"])
    pipeline_runner.start_ingestion(policy_id)
    return upload_response(updated, cached=False)


@router.patch("/{policy_id}/identity")
def identity(policy_id: str, body: dict):
    return edit_identity(policy_id, body["reviewer"], body.get("insurer"), body.get("plan_name"), body.get("plan_year"))


@router.post("/{policy_id}/apply-coverage")
def apply_coverage(policy_id: str, body: dict):
    """Attach a structured benefit-chart extract (all PA rows) and rebuild InsurancePlan."""
    from app.ingest.coverage_seed import apply_categories

    categories = body.get("categories") or []
    return apply_categories(policy_id, categories, reviewer=body.get("reviewer") or "engine")


@router.post("/{policy_id}/apply-service-codes")
def apply_service_codes(policy_id: str):
    """Attach example CPT/HCPCS/CDT codes from the UHC OH-S3 2026 service-code reference."""
    from app.ingest.service_code_reference import apply_service_code_reference

    return apply_service_code_reference(policy_id)


@router.post("/{policy_id}/reprocess")
def reprocess(policy_id: str, full: bool = False):
    return pipeline_runner.reprocess(policy_id, full=full)


@router.post("/{policy_id}/finalize")
def finalize(policy_id: str):
    """Build draft FHIR from items already extracted (resume after crash near the end)."""
    return pipeline_runner.finalize_draft(policy_id)


@router.post("/{policy_id}/reject-fragments")
def reject_fragments(policy_id: str):
    """Drop wrapped eligibility / mid-sentence shards that are not chart services, then rebuild draft FHIR."""
    from app.pipeline.episodes import EpisodeRecorder
    from app.pipeline.pipeline_runner import _finish_draft, empty, upload_response
    from app.repository import get_repo

    repo = get_repo()
    policy = repo.get_policy(policy_id)
    if policy is None:
        from app.errors import ApiError

        raise ApiError("POLICY_NOT_FOUND", "No policy with that id.", 404)
    run = repo.create_run(policy_id, "reject-fragments")
    recorder = EpisodeRecorder("policy", policy_id, run["id"])
    try:
        before = sum(1 for i in repo.items_for(policy_id) if i.get("review_state") == "rejected")
        _finish_draft(policy, repo.items_for(policy_id), policy.get("working_memory") or empty(), recorder)
        after = sum(1 for i in repo.items_for(policy_id) if i.get("review_state") == "rejected")
        repo.finish_run(run["id"], "complete")
    except Exception as exc:
        repo.finish_run(run["id"], "failed", str(exc)[:500])
        raise
    return {
        "rejected": max(0, after - before),
        "rejected_total": after,
        "policy": upload_response(repo.get_policy(policy_id), cached=False),
    }


@router.post("/{policy_id}/reconcile-pa")
async def reconcile_pa(
    policy_id: str,
    listing: UploadFile | None = File(None),
    listing_alt: UploadFile | None = File(None),
):
    """Re-read EOC dagger markers (and optional PA listing PDF) onto coverage rows.

    When two listing PDFs are uploaded, the parse with more complete service names wins
    (row count, mean label length, fewer truncated open-parens) — not a payer-specific rule.
    """
    from app.ingest.pa_markers import choose_richer_listing, parse_pa_listing_text
    from app.ingest.pdf_reader import read_pdf
    from app.ingest.upload_validation import validate_bytes
    from app.config import settings
    from app.repository import new_id

    texts_and_rows: list[tuple[str, list[dict]]] = []
    for upload in (listing, listing_alt):
        if upload is None:
            continue
        data = await upload.read()
        validate_bytes(data)
        settings.storage_dir.mkdir(parents=True, exist_ok=True)
        dest = settings.storage_dir / f"{new_id()}-pa-listing.pdf"
        dest.write_bytes(data)
        pages = read_pdf(dest)
        text = "\n".join(p.get("text") or "" for p in pages)
        rows = parse_pa_listing_text(text)
        if rows:
            texts_and_rows.append((text, rows))

    listing_text = None
    if len(texts_and_rows) == 1:
        listing_text = texts_and_rows[0][0]
    elif len(texts_and_rows) >= 2:
        winner = choose_richer_listing([rows for _text, rows in texts_and_rows])
        for text, rows in texts_and_rows:
            if rows is winner or (
                len(rows) == len(winner)
                and sum(len(r.get("service_label") or "") for r in rows)
                == sum(len(r.get("service_label") or "") for r in winner)
            ):
                listing_text = text
                break
        if listing_text is None:
            listing_text = texts_and_rows[0][0]

    return pipeline_runner.reconcile_pa_flags(policy_id, listing_text=listing_text)


@router.get("/{policy_id}/blocks")
def blocks(policy_id: str):
    return {"blocks": get_repo().blocks_for(policy_id)}


@router.post("/{policy_id}/blocks/{block_id}/extract")
def extract_block(policy_id: str, block_id: str):
    return pipeline_runner.extract_block(policy_id, block_id)


@router.get("/{policy_id}/audit.md")
def audit(policy_id: str):
    return PlainTextResponse(render(policy_id), media_type="text/markdown")


def _summary(policy: dict) -> dict:
    from app.pipeline import pipeline_runner

    items = get_repo().items_for(policy["id"])
    running = False
    try:
        running = pipeline_runner.ingestion_running(policy["id"]) or policy["status"] == "ingesting"
    except Exception:
        running = policy["status"] == "ingesting"
    current_step = None
    if running:
        try:
            from app.pipeline.progress import current_step_label

            current_step = current_step_label(policy["id"])
        except Exception:
            current_step = "Extracting pages"
    return {
        "id": policy["id"],
        "file_name": policy["file_name"],
        "document_role": policy["document_role"],
        "source_kind": policy["source_kind"],
        "source_url": policy.get("source_url"),
        "insurer": policy.get("insurer"),
        "plan_name": policy.get("plan_name"),
        "plan_year": policy.get("plan_year"),
        "status": policy["status"],
        "item_count": sum(1 for item in items if item["review_state"] != "rejected"),
        "pending": sum(1 for item in items if item["review_state"] in {"auto_approved", "pending_review"}),
        "fhir_valid": ((policy.get("validation_report") or {}).get("fhir") or {}).get("valid"),
        "ingestion_running": running,
        "current_step": current_step,
    }


def _insurers(policies: list[dict]) -> list[dict]:
    """Live plans for the doctor insurance picker. EOCs without insurer still appear."""
    grouped: dict[tuple, dict] = {}
    for policy in policies:
        if policy["status"] != "live":
            continue
        if policy.get("document_role") not in {"benefit_summary", "clinical_policy", "drug_criteria"}:
            continue
        insurer = payer_label(policy)
        plan_name = policy.get("plan_name") or policy.get("file_name") or "Plan"
        plan_year = policy.get("plan_year") or ""
        if not insurer:
            continue
        key = (insurer, plan_name, plan_year)
        grouped.setdefault(
            key,
            {
                "insurer": insurer,
                "plan_name": plan_name,
                "plan_year": plan_year,
                "benefit_summary_id": None,
                "policy_ids": [],
            },
        )
        grouped[key]["policy_ids"].append(policy["id"])
        if policy["document_role"] == "benefit_summary":
            grouped[key]["benefit_summary_id"] = policy["id"]
            grouped[key]["insurer"] = insurer
            grouped[key]["plan_name"] = plan_name
            grouped[key]["plan_year"] = plan_year
    return list(grouped.values())


def _payer_label(policy: dict) -> str | None:
    return payer_label(policy)


def _services() -> list[dict]:
    repo = get_repo()
    rows = []
    for item in repo.live_items():
        policy = repo.get_policy(item["policy_id"]) or {}
        data = item["data"]
        base = {
            "item_id": item["id"],
            "policy_id": item["policy_id"],
            "insurer": payer_label(policy),
            "plan_name": policy.get("plan_name") or policy.get("file_name"),
            "plan_year": policy.get("plan_year") or "",
        }
        if item["item_type"] == "coverage":
            label = data.get("service_label")
            rows.append(
                {
                    **base,
                    "label": label,
                    "codes": _service_codes_for(label, data.get("service_codes") or []),
                    "kind": "coverage",
                }
            )
        else:
            for label in data.get("applies_to") or []:
                rows.append(
                    {
                        **base,
                        "label": label,
                        "codes": _service_codes_for(label, data.get("codes") or []),
                        "kind": "rule",
                    }
                )
    return rows


def _service_codes_for(label: str | None, codes: list) -> list[str]:
    """Prefer CPT/HCPCS/CDT-looking codes; pull from label when the row only has ICD or nothing."""
    return service_codes_for(label, codes)


def _drugs() -> list[dict]:
    return [
        {
            "block_id": block["id"],
            "policy_id": block["policy_id"],
            "label": block["label"],
            "status": block["status"],
        }
        for block in get_repo().live_blocks()
    ]
