"""Generate 6 synthetic UHC Dual Complete patient-report PDFs for Order Desk upload demos.

Writes to:
  data/demo/uhc-reports/          (local seed)
  web/public/demo/uhc-reports/    (downloadable on the deployed Next app)

Usage (from repo root, with venv that has reportlab):
  python -m pa-engine.synth.uhc_patient_reports
or:
  cd pa-engine && python -m synth.uhc_patient_reports
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas

# Allow `python synth/uhc_patient_reports.py` from pa-engine/
_HERE = Path(__file__).resolve().parent
_PA_ENGINE = _HERE.parent
_REPO = _PA_ENGINE.parent
if str(_PA_ENGINE) not in sys.path:
    sys.path.insert(0, str(_PA_ENGINE))

from synth.documents import PATIENT_FOOTER  # noqa: E402

DEMO_JSON_DIR = _REPO / "data" / "demo" / "uhc-patients"
OUT_LOCAL = _REPO / "data" / "demo" / "uhc-reports"
OUT_WEB = _REPO / "web" / "public" / "demo" / "uhc-reports"

# Six demo charts: mix of PA required / not required / conditional.
SELECTED = [
    "patient-01-robert-nguyen.json",  # MRI lumbar — PA required
    "patient-02-priya-sharma.json",  # X-ray lumbar — no PA
    "patient-03-marcus-bennett.json",  # MRI cervical — PA required
    "patient-04-linda-okonkwo.json",  # PT eval — conditional
    "patient-06-helen-park.json",  # Office visit — no PA
    "patient-08-nina-castillo.json",  # MRI brain — PA required
]

CLINICAL_BLURBS = {
    "patient-01-robert-nguyen.json": (
        "Chief complaint: progressive low back pain with left leg numbness for 9 weeks. "
        "Neurological symptom documented: numbness in the left foot and lateral calf. "
        "Symptoms have been present for at least 6 weeks. "
        "Completed 8 weeks of supervised physical therapy for lumbar radiculopathy; last session 2026-09-12. "
        "No lumbar surgery in the past 6 months. "
        "Conservative care has not adequately controlled symptoms. "
        "Ordering: MRI lumbar spine without contrast (CPT 72148) to evaluate disc herniation."
    ),
    "patient-02-priya-sharma.json": (
        "Chief complaint: mechanical low back pain after yard work. "
        "Pain localized to the lumbar region without radicular symptoms. "
        "No red-flag neurological deficits on exam. "
        "Ordering: X-ray lumbar spine 2 or 3 views (CPT 72100) for initial evaluation."
    ),
    "patient-03-marcus-bennett.json": (
        "Chief complaint: neck pain with radiation into the right arm for 10 weeks. "
        "Neurological symptom documented: right hand tingling and reduced grip strength. "
        "Symptoms have been present for at least 6 weeks. "
        "Completed physical therapy for cervical radiculopathy without adequate relief. "
        "No cervical surgery in the past 6 months. "
        "Ordering: MRI cervical spine without contrast (CPT 72141)."
    ),
    "patient-04-linda-okonkwo.json": (
        "Chief complaint: lumbar radiculopathy with intermittent left leg pain. "
        "Patient has not yet completed a formal course of physical therapy. "
        "Ordering: Physical therapy evaluation (CPT 97161) to begin conservative care."
    ),
    "patient-06-helen-park.json": (
        "Chief complaint: chronic low back pain, stable. "
        "Follow-up visit for medication review and activity counseling. "
        "No new imaging ordered today. "
        "Service: Office visit - established patient (CPT 99213)."
    ),
    "patient-08-nina-castillo.json": (
        "Chief complaint: recurrent migraine with increasing frequency over 3 months. "
        "Neurological exam non-focal; no prior brain imaging this year. "
        "Failed trial of preventive therapy; headache diary reviewed. "
        "Ordering: MRI brain without contrast (CPT 70551) to exclude secondary causes."
    ),
}


def _slug(name: str) -> str:
    return name.lower().replace(" ", "-")


def _report_lines(patient: dict, blurb: str) -> list[str]:
    order = patient.get("suggested_order") or {}
    insurance = patient.get("insurance") or {}
    addr = patient.get("address") or {}
    dx = patient.get("diagnoses") or []
    dx_line = ", ".join(f"{d.get('code')} ({d.get('display')})" for d in dx) or "See notes"
    address_line = ", ".join(
        p
        for p in [
            addr.get("line1"),
            addr.get("city"),
            f"{addr.get('state') or ''} {addr.get('postal_code') or ''}".strip(),
        ]
        if p
    )
    return [
        "ClearPath Spine & Primary Care",
        "Clinical progress / order report (SYNTHETIC DEMO)",
        "UnitedHealthcare Dual Complete OH-S3 — demo chart",
        "",
        f"Patient name: {patient.get('full_name')}",
        f"Date of birth: {patient.get('dob')}",
        f"Sex: {patient.get('sex')}",
        f"MRN: {patient.get('mrn')}",
        f"Member ID: {patient.get('member_id')}",
        f"Address: {address_line}",
        f"Phone: {patient.get('phone')}",
        "",
        f"Insurer: {insurance.get('insurer')}",
        f"Plan name: {insurance.get('plan_name')}",
        f"Plan year: {insurance.get('plan_year')}",
        f"Group number: {insurance.get('group_number')}",
        "",
        "Ordering clinician: Dr. Ana Reyes, MD | Specialty: spine | NPI: 1679651234",
        "Encounter date: 2026-09-26",
        "",
        "Diagnoses (ICD-10):",
        f"  {dx_line}",
        "",
        "Treatment requested / ordered:",
        f"  Service: {order.get('order_text')}",
        f"  Service category: {order.get('service_category') or order.get('order_text')}",
        f"  CPT / service code: {order.get('service_code')}",
        f"  Expected PA status (demo key): {order.get('pa_status')}",
        "",
        "Clinical notes:",
        blurb,
        "",
        "Attestation: Synthetic test data only. Not a real patient. Not real PHI.",
    ]


def write_report_pdf(path: Path, lines: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    pen = canvas.Canvas(str(path), pagesize=letter)
    y = 740
    for line in lines:
        # Soft-wrap long clinical notes
        chunks = _wrap(line, 95) if len(line) > 95 else [line]
        for chunk in chunks:
            if y < 72:
                pen.setFont("Times-Roman", 8)
                pen.drawString(48, 42, PATIENT_FOOTER)
                pen.showPage()
                y = 740
            pen.setFont("Times-Bold" if chunk and not chunk.startswith(" ") and chunk.endswith(":") else "Times-Roman", 11)
            if chunk.startswith("ClearPath") or chunk.startswith("Clinical progress"):
                pen.setFont("Times-Bold", 13)
            pen.drawString(48, y, chunk)
            y -= 16
    pen.setFont("Times-Roman", 8)
    pen.drawString(48, 42, PATIENT_FOOTER)
    pen.drawString(48, 28, "Printed page 1 — ClearPath UHC demo report")
    pen.showPage()
    pen.save()


def _wrap(text: str, width: int) -> list[str]:
    words = text.split()
    rows: list[str] = []
    cur: list[str] = []
    for word in words:
        trial = (" ".join(cur + [word])).strip()
        if len(trial) <= width:
            cur.append(word)
        else:
            if cur:
                rows.append(" ".join(cur))
            cur = [word]
    if cur:
        rows.append(" ".join(cur))
    return rows or [""]


def main() -> None:
    OUT_LOCAL.mkdir(parents=True, exist_ok=True)
    OUT_WEB.mkdir(parents=True, exist_ok=True)
    index: list[dict] = []
    for fname in SELECTED:
        src = DEMO_JSON_DIR / fname
        patient = json.loads(src.read_text())
        order = patient.get("suggested_order") or {}
        code = order.get("service_code") or "na"
        out_name = f"{fname.replace('.json', '')}-{code}-report.pdf"
        lines = _report_lines(patient, CLINICAL_BLURBS[fname])
        local_path = OUT_LOCAL / out_name
        write_report_pdf(local_path, lines)
        web_path = OUT_WEB / out_name
        shutil.copy2(local_path, web_path)
        index.append(
            {
                "file": out_name,
                "patient": patient.get("full_name"),
                "dob": patient.get("dob"),
                "order": order.get("order_text"),
                "service_code": code,
                "pa_status": order.get("pa_status"),
                "download_path": f"/demo/uhc-reports/{out_name}",
                "local_path": str(local_path.relative_to(_REPO)),
            }
        )
        print("wrote", out_name)

    readme = OUT_LOCAL / "README.md"
    readme.write_text(
        "\n".join(
            [
                "# UHC Dual Complete — synthetic patient reports (upload demos)",
                "",
                "Six PDF clinical reports for Doctor → Upload report.",
                "Synthetic only — not real PHI.",
                "",
                "| File | Patient | Order | CPT | PA (demo) |",
                "|---|---|---|---|---|",
                *[
                    f"| `{r['file']}` | {r['patient']} | {r['order']} | {r['service_code']} | {r['pa_status']} |"
                    for r in index
                ],
                "",
                "On the live site the same files are served from `/demo/uhc-reports/`.",
                "",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    shutil.copy2(readme, OUT_WEB / "README.md")
    (OUT_LOCAL / "index.json").write_text(json.dumps({"reports": index}, indent=2) + "\n")
    shutil.copy2(OUT_LOCAL / "index.json", OUT_WEB / "index.json")
    print("done →", OUT_LOCAL)
    print("done →", OUT_WEB)


if __name__ == "__main__":
    main()
