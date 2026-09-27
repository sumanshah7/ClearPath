"""Generate 6 UHC Dual Complete patient-report PDFs that match the LIVE policy catalog.

Labels and plan identity must equal accepted listing rows on the live EOC so Order Desk
catalog match succeeds (no “not in live plan catalog” / demo-only plan warnings).

Writes only to local folders (not required on Vercel):
  data/demo/uhc-reports/
  demo-patient-reports/
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas

_HERE = Path(__file__).resolve().parent
_PA_ENGINE = _HERE.parent
_REPO = _PA_ENGINE.parent
if str(_PA_ENGINE) not in sys.path:
    sys.path.insert(0, str(_PA_ENGINE))

from synth.documents import PATIENT_FOOTER  # noqa: E402

OUT_LOCAL = _REPO / "data" / "demo" / "uhc-reports"
OUT_FOLDER = _REPO / "demo-patient-reports"

# Must match policies.insurer / plan_name / plan_year on the live UHC EOC seed.
INSURER = "UnitedHealthcare Community Plan of Ohio"
PLAN_NAME = "Dual Complete OH-S3"
PLAN_YEAR = "2026"

# Exact service_label values from accepted PA-listing rows (policy live catalog).
REPORTS = [
    {
        "file": "01-robert-nguyen-outpatient-diagnostic-tests.pdf",
        "full_name": "Robert Nguyen",
        "dob": "1959-01-18",
        "sex": "male",
        "mrn": "MRN-10774",
        "member_id": "UHC-77421",
        "address": "2300 Summit Blvd, Akron, OH 44308",
        "phone": "555-4101",
        "service_label": "Outpatient diagnostic tests",
        "pa_status": "required",
        "icd10": "M51.16",
        "icd10_display": "Intervertebral disc disorders with radiculopathy, lumbar region",
        "notes": (
            "Chief complaint: progressive low back pain with left leg numbness for 9 weeks. "
            "Neurological symptom documented: numbness in the left foot. "
            "Symptoms present at least 6 weeks. Completed 8 weeks of physical therapy. "
            "No lumbar surgery in the past 6 months. "
            "Ordering clinician requests Outpatient diagnostic tests (advanced imaging / MRI lumbar evaluation) "
            "under Dual Complete OH-S3 benefit Outpatient diagnostic tests."
        ),
    },
    {
        "file": "02-priya-sharma-outpatient-diagnostic-tests-xrays.pdf",
        "full_name": "Priya Sharma",
        "dob": "1971-09-04",
        "sex": "female",
        "mrn": "MRN-10881",
        "member_id": "UHC-88102",
        "address": "64 Cedarbrook Court, Columbus, OH 43215",
        "phone": "555-4102",
        "service_label": "Outpatient diagnostic tests - X-rays",
        "pa_status": "required",
        "icd10": "M54.5",
        "icd10_display": "Low back pain",
        "notes": (
            "Chief complaint: mechanical low back pain. "
            "Ordering: Outpatient diagnostic tests - X-rays (lumbar views) per plan benefit chart."
        ),
    },
    {
        "file": "03-marcus-bennett-chiropractic-services.pdf",
        "full_name": "Marcus Bennett",
        "dob": "1965-12-11",
        "sex": "male",
        "mrn": "MRN-10903",
        "member_id": "UHC-90344",
        "address": "19 Lake Shore Drive, Cleveland, OH 44114",
        "phone": "555-4103",
        "service_label": "Chiropractic services",
        "pa_status": "required",
        "icd10": "M54.12",
        "icd10_display": "Radiculopathy, cervical region",
        "notes": (
            "Neck pain with right-arm radiation for 10 weeks. "
            "Ordering: Chiropractic services for cervical radiculopathy under Dual Complete OH-S3."
        ),
    },
    {
        "file": "04-helen-park-annual-wellness-visit.pdf",
        "full_name": "Helen Park",
        "dob": "1955-04-02",
        "sex": "female",
        "mrn": "MRN-10220",
        "member_id": "UHC-22011",
        "address": "18 Willow Lane, Columbus, OH 43215",
        "phone": "555-4106",
        "service_label": "Annual wellness visit",
        "pa_status": "not_required",
        "icd10": "Z00.00",
        "icd10_display": "Encounter for general adult medical examination without abnormal findings",
        "notes": (
            "Annual wellness visit for preventive care review, medication list, and screening schedule. "
            "Service ordered: Annual wellness visit."
        ),
    },
    {
        "file": "05-linda-okonkwo-ambulance-services.pdf",
        "full_name": "Linda Okonkwo",
        "dob": "1982-06-27",
        "sex": "female",
        "mrn": "MRN-10662",
        "member_id": "UHC-66219",
        "address": "880 Market Street Apt 4B, Toledo, OH 43604",
        "phone": "555-4104",
        "service_label": "Ambulance services",
        "pa_status": "conditional",
        "icd10": "R55",
        "icd10_display": "Syncope and collapse",
        "notes": (
            "Non-emergency transport evaluation after syncope workup. "
            "Ordering: Ambulance services (ground) — PA status conditional per Dual Complete OH-S3 listing."
        ),
    },
    {
        "file": "06-nina-castillo-outpatient-rehabilitation.pdf",
        "full_name": "Nina Castillo",
        "dob": "1988-11-30",
        "sex": "female",
        "mrn": "MRN-10447",
        "member_id": "UHC-44701",
        "address": "55 Prospect Ave, Cincinnati, OH 45202",
        "phone": "555-4108",
        "service_label": "Outpatient rehabilitation services",
        "pa_status": "required",
        "icd10": "M54.16",
        "icd10_display": "Radiculopathy, lumbar region",
        "notes": (
            "Lumbar radiculopathy with functional limits. "
            "Ordering: Outpatient rehabilitation services (physical therapy course) per plan benefit."
        ),
    },
]


def _wrap(text: str, width: int = 95) -> list[str]:
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


def _lines(r: dict) -> list[str]:
    return [
        "ClearPath Spine & Primary Care",
        "Clinical order report (SYNTHETIC DEMO — matches live UHC Dual Complete OH-S3 catalog)",
        "",
        f"Patient name: {r['full_name']}",
        f"Date of birth: {r['dob']}",
        f"Sex: {r['sex']}",
        f"MRN: {r['mrn']}",
        f"Member ID: {r['member_id']}",
        f"Address: {r['address']}",
        f"Phone: {r['phone']}",
        "",
        f"Insurer: {INSURER}",
        f"Plan name: {PLAN_NAME}",
        f"Plan year: {PLAN_YEAR}",
        f"Group number: UHC-OH-S3",
        "",
        "Ordering clinician: Dr. Ana Reyes, MD | Specialty: spine | NPI: 1679651234",
        "Encounter date: 2026-09-26",
        "",
        "Diagnoses (ICD-10):",
        f"  {r['icd10']} ({r['icd10_display']})",
        "",
        "Treatment requested / ordered:",
        # Put the catalog label on lines the extractor looks for.
        f"  Service: {r['service_label']}",
        f"  Service category: {r['service_label']}",
        f"  Expected PA status (listing): {r['pa_status']}",
        "",
        "Clinical notes:",
        r["notes"],
        "",
        "Attestation: Synthetic test data only. Not a real patient. Not real PHI.",
    ]


def write_pdf(path: Path, lines: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    pen = canvas.Canvas(str(path), pagesize=letter)
    y = 740
    for line in lines:
        for chunk in _wrap(line):
            if y < 72:
                pen.setFont("Times-Roman", 8)
                pen.drawString(48, 42, PATIENT_FOOTER)
                pen.showPage()
                y = 740
            font = "Times-Bold" if chunk.startswith("ClearPath") or chunk.startswith("Clinical order") else "Times-Roman"
            pen.setFont(font, 13 if font == "Times-Bold" else 11)
            pen.drawString(48, y, chunk)
            y -= 16
    pen.setFont("Times-Roman", 8)
    pen.drawString(48, 42, PATIENT_FOOTER)
    pen.drawString(48, 28, "ClearPath UHC demo — catalog-aligned")
    pen.showPage()
    pen.save()


def main() -> None:
    OUT_LOCAL.mkdir(parents=True, exist_ok=True)
    OUT_FOLDER.mkdir(parents=True, exist_ok=True)
    # Clear prior mismatched sample names
    for folder in (OUT_LOCAL, OUT_FOLDER):
        for old in folder.glob("*.pdf"):
            old.unlink()

    index = []
    for r in REPORTS:
        path = OUT_LOCAL / r["file"]
        write_pdf(path, _lines(r))
        shutil.copy2(path, OUT_FOLDER / r["file"])
        index.append(
            {
                "file": r["file"],
                "patient": r["full_name"],
                "dob": r["dob"],
                "service_label": r["service_label"],
                "pa_status": r["pa_status"],
                "insurer": INSURER,
                "plan_name": PLAN_NAME,
                "plan_year": PLAN_YEAR,
            }
        )
        print("wrote", r["file"])

    readme = "\n".join(
        [
            "UHC Dual Complete OH-S3 — catalog-aligned synthetic reports",
            "==========================================================",
            "Upload these from Doctor → Upload report.",
            "Service labels match accepted live listing rows on the EOC.",
            "",
            f"Plan: {INSURER} / {PLAN_NAME} / {PLAN_YEAR}",
            "",
            *[
                f"- {row['file']}: {row['patient']} · {row['service_label']} · PA {row['pa_status']}"
                for row in index
            ],
            "",
        ]
    )
    (OUT_FOLDER / "README.txt").write_text(readme)
    (OUT_LOCAL / "README.md").write_text("# " + readme.replace("\n", "\n\n") + "\n")
    (OUT_LOCAL / "index.json").write_text(json.dumps({"reports": index}, indent=2) + "\n")
    print("folder →", OUT_FOLDER)


if __name__ == "__main__":
    main()
