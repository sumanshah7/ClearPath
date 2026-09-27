"""Doctor patient-report uploads that prefill the existing Order Desk."""

from __future__ import annotations

from fastapi import APIRouter, File, Form, UploadFile

from app.boundary.export_pa import EXPORT_STATUS, buildOutputForTeammate
from app.config import settings
from app.errors import ApiError
from app.ingest.catalog_resolve import resolveOrderToCatalog
from app.ingest.report_extract import extractReportToJson
from app.repository import get_repo, new_id, now

router = APIRouter(tags=["uploads"])


@router.post("/uploads/patient-report")
async def upload_patient_report(
    file: UploadFile = File(...),
    provider_id: str | None = Form(None),
):
    """Store the file, extract JSON in-house, return fields for Order Desk prefill."""
    repo = get_repo()
    data = await file.read()
    if not data:
        raise ApiError("INVALID_PDF", "The uploaded file is empty.", 400)

    settings.storage_dir.mkdir(parents=True, exist_ok=True)
    upload_id = new_id()
    file_name = file.filename or "patient-report.pdf"
    dest = settings.storage_dir / f"upload-{upload_id[:8]}-{file_name.replace('/', '_')}"
    dest.write_bytes(data)

    result = extractReportToJson(file_bytes=data, file_name=file_name, storage_path=dest)
    extracted = result["extracted"]
    status = result["extraction_status"]

    patient_id = None
    patient_in = extracted.get("patient") or {}
    name = (patient_in.get("name") or "").strip()
    dob = (patient_in.get("dob") or "").strip() or None
    if name:
        existing = repo.find_patient_by_name_dob(name, dob)
        if existing:
            patient_id = existing["id"]
        else:
            patient_id = str(patient_in.get("id") or new_id())
            repo.upsert_patient(
                {
                    "id": patient_id,
                    "full_name": name,
                    "dob": dob,
                    "member_id": patient_in.get("id") or patient_id[:12],
                    "synthetic": True,
                }
            )

    uploaded_by = provider_id if provider_id and repo.get_provider(provider_id) else None
    # Same validation for chart notes — unsigned provider ids trip FOREIGN KEY (500).
    author_provider_id = uploaded_by

    if patient_id:
        # Keep original file on the chart for audit / answer sources.
        repo.create_document(
            {
                "patient_id": patient_id,
                "file_name": file_name,
                "storage_path": str(dest),
                "doc_type": "report",
                "in_chart": 1,
                "synthetic": 1,
            }
        )
        notes = ((extracted.get("treatment_requested") or {}).get("clinical_notes") or "").strip()
        if notes:
            try:
                repo.insert_record(
                    {
                        "patient_id": patient_id,
                        "resource_type": "DocumentReference",
                        "record_kind": "uploaded_report_notes",
                        "display": "Uploaded report extract",
                        "body": notes,
                        "author_provider_id": author_provider_id,
                        "synthetic": 1,
                        "start_date": now()[:10],
                    }
                )
            except Exception:
                # Notes are helpful but must not fail the upload (F1).
                pass

    row = repo.create_uploaded_report(
        {
            "id": upload_id,
            "patient_id": patient_id,
            "uploaded_by": uploaded_by,
            "file_name": file_name,
            "file_reference": str(dest.name),
            "storage_path": str(dest),
            "extracted_json": extracted,
            "extraction_status": status,
        }
    )

    treatment = extracted.get("treatment_requested") or {}
    resolved = _resolve_to_catalog(treatment)

    return {
        "upload_id": row["id"],
        "extraction_status": status,
        "extracted": extracted,
        "catalog_match": resolved,
        "order_desk": {
            "patient_id": patient_id or "",
            **_order_desk_service(treatment, resolved),
            "clinical_notes": (treatment.get("clinical_notes") or "")[:500],
            "source_document_reference": extracted.get("source_document_reference") or file_name,
            "source_upload_id": row["id"],
        },
        "uploaded_at": row.get("uploaded_at"),
    }


@router.get("/uploads/{upload_id}")
def get_upload(upload_id: str):
    repo = get_repo()
    row = repo.get_uploaded_report(upload_id)
    if row is None:
        raise ApiError("POLICY_NOT_FOUND", "That upload is not on file.", 404)
    extracted = row.get("extracted_json") or {}
    if isinstance(extracted, str):
        import json

        extracted = json.loads(extracted)
    treatment = extracted.get("treatment_requested") or {}
    resolved = _resolve_to_catalog(treatment)
    return {
        "upload_id": row["id"],
        "extraction_status": row.get("extraction_status"),
        "extracted": extracted,
        "patient_id": row.get("patient_id"),
        "file_name": row.get("file_name"),
        "uploaded_at": row.get("uploaded_at"),
        "catalog_match": resolved,
        "order_desk": {
            "patient_id": row.get("patient_id") or "",
            **_order_desk_service(treatment, resolved),
            "clinical_notes": (treatment.get("clinical_notes") or "")[:500],
            "source_document_reference": extracted.get("source_document_reference") or row.get("file_name"),
            "source_upload_id": row["id"],
        },
    }


@router.get("/patients/{patient_id}/pa-status")
def patient_pa_status(patient_id: str):
    """Patient-facing status feed. Reads the same PA events as the clinician/insurer flow."""
    repo = get_repo()
    patient = repo.get_patient(patient_id)
    if patient is None:
        raise ApiError("POLICY_NOT_FOUND", "That patient is not on file.", 404)
    pas = [pa for pa in repo.list_pas(limit=200) if pa["patient_id"] == patient_id]
    requests = []
    for pa in pas:
        export = buildOutputForTeammate(pa["id"])
        requests.append(
            {
                "pa_request_id": pa["id"],
                "treatment": pa.get("service_category") or pa.get("order_text"),
                "service_code": pa.get("service_code"),
                "insurer": pa.get("insurer"),
                "plan_name": pa.get("plan_name"),
                "plan_year": pa.get("plan_year"),
                "status": export.get("status") or EXPORT_STATUS.get(pa["status"], pa["status"]),
                "internal_status": pa["status"],
                "decided_at": export.get("decided_at"),
                "reviewer_note": export.get("reviewer_note"),
                "history": export.get("history") or [],
                "updated_at": pa.get("updated_at") or pa.get("created_at"),
                "source_upload_id": pa.get("source_upload_id"),
            }
        )
    requests.sort(key=lambda row: row.get("updated_at") or "", reverse=True)
    return {
        "patient": {
            "id": patient["id"],
            "full_name": patient.get("full_name"),
            "dob": patient.get("dob"),
            "member_id": patient.get("member_id"),
        },
        "requests": requests,
    }


def _resolve_to_catalog(treatment: dict) -> dict:
    """Map the extracted service onto a live catalog row. A failure here must not lose the upload (F1)."""
    order_text = (treatment.get("service_category") or "").strip()
    service_code = _prefer_cpt(treatment.get("diagnosis_codes") or []) or ""
    try:
        return resolveOrderToCatalog(order_text, service_code)
    except Exception:
        return {
            "status": "none",
            "order_text": order_text,
            "service_code": service_code,
            "insurer": None,
            "plan_name": None,
            "plan_year": None,
            "candidates": [],
        }


def _order_desk_service(treatment: dict, resolved: dict) -> dict:
    """Prefill values Order Desk can select: catalog label and plan when resolved, report wording otherwise."""
    order_text = (treatment.get("service_category") or "").strip()
    service_code = _prefer_cpt(treatment.get("diagnosis_codes") or []) or ""
    return {
        "order_text": resolved.get("order_text") or order_text,
        "service_code": resolved.get("service_code") or service_code,
        "insurer": resolved.get("insurer") or "",
        "plan_name": resolved.get("plan_name") or "",
        "plan_year": resolved.get("plan_year") or "",
        "catalog_status": resolved.get("status") or "none",
    }


def _prefer_cpt(codes: list) -> str | None:
    for code in codes:
        c = str(code).strip()
        if c.isdigit() and len(c) == 5:
            return c
    for code in codes:
        c = str(code).strip()
        if c:
            return c
    return None
