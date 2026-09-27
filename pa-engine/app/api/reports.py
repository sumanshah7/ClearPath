"""Chart reads and report extraction."""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Body, File, Form, UploadFile
from fastapi.responses import FileResponse

from app.check.fact_extractor import extract
from app.config import settings
from app.errors import ApiError
from app.repository import get_repo, new_id, now

router = APIRouter()


@router.get("/patients")
def patients():
    return {"patients": get_repo().list_patients()}


@router.post("/patients")
def upsert_patient(body: dict = Body(...)):
    """Ensure a synthetic patient exists for the order desk (demo seed / login sync)."""
    patient_id = (body.get("id") or "").strip()
    full_name = (body.get("full_name") or "").strip()
    if not patient_id or not full_name:
        raise ApiError("INVALID_REQUEST", "Patient id and full_name are required.", 400)
    row = get_repo().upsert_patient(
        {
            "id": patient_id,
            "full_name": full_name,
            "dob": body.get("dob"),
            "sex": body.get("sex"),
            "member_id": body.get("member_id"),
            "synthetic": True if body.get("synthetic", True) else False,
        }
    )
    return {"patient": row}


@router.get("/providers")
def providers():
    return {"providers": get_repo().list_providers()}


@router.post("/providers")
def upsert_provider(body: dict = Body(...)):
    """Ensure a clinician exists before order/upload (demo doctor login sync)."""
    provider_id = (body.get("id") or "").strip()
    full_name = (body.get("full_name") or "").strip()
    if not provider_id or not full_name:
        raise ApiError("INVALID_REQUEST", "Provider id and full_name are required.", 400)
    row = get_repo().upsert_provider(
        {
            "id": provider_id,
            "full_name": full_name,
            "specialty": (body.get("specialty") or None),
        }
    )
    return {"provider": row}


@router.get("/patients/{patient_id}/documents")
def documents(patient_id: str, pending: bool = False):
    return {"documents": get_repo().documents_for(patient_id, pending_only=pending)}


@router.get("/patients/{patient_id}/chart")
def patient_chart(patient_id: str):
    """Patient-portal chart JSON. prior_authorizations is [] when the patient has none."""
    from app.check import service as check_service

    return check_service.patient_chart(patient_id)


@router.get("/documents/{document_id}/file")
def document_file(document_id: str):
    document = get_repo().get_document(document_id)
    if document is None:
        raise ApiError("POLICY_NOT_FOUND", "That document is not on file.", 404)
    return FileResponse(document["storage_path"], media_type="application/pdf", filename=document["file_name"])


@router.post("/reports/extract")
async def extract_report(
    patient_id: str = Form(...),
    file: UploadFile | None = File(None),
    document_id: str | None = Form(None),
):
    """Extract chart facts. Shape mirrors the patient-side PDF→JSON envelope Sambhav uses."""
    repo = get_repo()
    if repo.get_patient(patient_id) is None:
        raise ApiError("POLICY_NOT_FOUND", "That patient is not on file.", 404)
    run = repo.create_run(None, f"report-{new_id()[:8]}")
    document: dict | None = None
    records: list = []
    try:
        if document_id:
            document = repo.get_document(document_id)
            if document is None:
                raise ApiError("POLICY_NOT_FOUND", "That document is not on file.", 404)
            text = _read_pdf(document["storage_path"])
            records = extract(patient_id, text, document_id, run_id=run["id"])
        elif file is not None:
            data = await file.read()
            from app.ingest.upload_validation import validate_bytes

            validate_bytes(data)
            settings.storage_dir.mkdir(parents=True, exist_ok=True)
            dest = settings.storage_dir / f"{new_id()}.pdf"
            dest.write_bytes(data)
            document = repo.create_document(
                {
                    "patient_id": patient_id,
                    "file_name": file.filename or "report.pdf",
                    "storage_path": str(dest),
                    "doc_type": "report",
                    "in_chart": False,
                    "synthetic": True,
                }
            )
            text = _read_pdf(dest)
            records = extract(patient_id, text, document["id"], run_id=run["id"])
        else:
            raise ApiError("INVALID_PDF", "A file or a document id is required.", 400)
    finally:
        repo.finish_run(run["id"], "complete")
    return {
        "document_type": "CLINICAL_REPORT",
        "file_path": (document or {}).get("storage_path"),
        "file_name": (document or {}).get("file_name"),
        "uploaded_at": (document or {}).get("created_at") or now(),
        "extracted_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "tables": {
            "document_type": "CLINICAL_REPORT",
            "records": records,
        },
        "records": records,
    }


def _read_pdf(path) -> str:
    from app.ingest.pdf_reader import read_pdf

    pages = read_pdf(path)
    return "\n".join(page.get("text") or "" for page in pages)
