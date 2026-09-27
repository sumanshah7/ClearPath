"""PA marker detection: next-line daggers and listing parse."""

from __future__ import annotations

from app.ingest.coverage_lines import coverage_from_line
from app.ingest.pa_markers import (
    annotate_item_from_pages,
    labels_match,
    line_pa_status,
    parse_pa_listing_text,
    windows_by_service_line,
)

DAGGER = "\u2020"
EM = "\u2014"


def test_emdash_double_dagger_on_same_line_is_required():
    status, marker = line_pa_status(f"Cardiac rehabilitation services visit.{EM}{DAGGER}{DAGGER}")
    assert status == "required"
    assert marker


def test_spaced_dagger_definition_form_is_required():
    status, marker = line_pa_status(
        f"are marked by a double dagger ({EM}{DAGGER} {DAGGER}) in the Medical Benefits Chart."
    )
    assert status == "required"
    assert marker


def test_next_line_dagger_associates_with_service_window():
    page = (
        "Cardiac rehabilitation services $0 copayment for each Not covered out-of-\n"
        f"visit.{EM}{DAGGER}{DAGGER}\n"
        "Primary care provider $0 copayment\n"
    )
    rows = windows_by_service_line(page)
    cardiac = next(r for r in rows if "Cardiac rehabilitation" in r["service_line"])
    assert cardiac["pa_status"] == "required"
    assert cardiac["pa_required"] is True


def test_annotate_item_reads_dagger_on_following_line():
    pages = [
        {
            "page": 64,
            "text": (
                "Cardiac rehabilitation services $0 copayment for each Not covered out-of-\n"
                f"visit.{EM}{DAGGER}{DAGGER}\n"
            ),
        }
    ]
    item = {"service_label": "Cardiac rehabilitation services", "page": 64, "data": {}}
    patch = annotate_item_from_pages(item, pages)
    assert patch is not None
    assert patch["pa_required"] is True
    assert patch["pa_status"] == "required"


def test_coverage_from_line_sets_pa_status():
    row = coverage_from_line(f"Outpatient surgery {DAGGER}{DAGGER} $0 copay", 88)
    assert row["pa_required"] is True
    assert row["pa_status"] == "required"


def test_parse_pa_listing_counts_statuses():
    text = """
Full Benefit Category Listing
1 Acupuncture for chronic low back Conditional Referral may be required
pain
2 Ambulance services Conditional Only non-emergency
3 Annual routine physical exam Not required $0
4 Cardiac rehabilitation services Required $0
5 Chiropractic services Required Manual
6 Emergency care Not required No referral
"""
    rows = parse_pa_listing_text(text)
    by = {r["service_label"]: r["pa_status"] for r in rows}
    assert by["Cardiac rehabilitation services"] == "required"
    assert by["Emergency care"] == "not_required"
    assert by["Ambulance services"] == "conditional"
    assert sum(1 for r in rows if r["pa_status"] == "required") >= 2


def test_parse_pa_listing_handles_wrapped_numbers_and_status():
    """pdfplumber often splits '10' and 'Not required' across lines."""
    text = """
Full Benefit Category Listing
# Benefit Category PA Status Notes Cost Share Pg.
1 Acupuncture for chronic low back Conditiona Referral may be required; cost Same as primary 57
pain l depends on PCP vs specialist setting care/specialist visit copay,
in-network only
2 Ambulance services Conditiona Only non-emergency transportation $0 copay per one-way trip 59-6
l may need prior authorization; in-network 0
emergency ambulance never
requires it
3 Annual routine physical exam Not $0 copay, 1 visit/year 60
required
4 Annual wellness visit Not $0 copay (preventive) 60-6
required 1
5 Bone mass measurement Not $0 copay 61
required
6 Breast cancer screening Not $0 copay 61
(mammograms) required
7 Cardiac rehabilitation services Required $0 copay per visit 62
8 Cardiovascular disease risk Not $0 copay, 1 visit/year 62
reduction visit required
9 Cardiovascular disease screening Not $0 copay, once every 5 62
tests required years
1 Cervical and vaginal cancer Not $0 copay 63
0 screening required
1 Chiropractic services Required Manual manipulation for subluxation $0 copay per visit 63-6
1 only 4
"""
    rows = parse_pa_listing_text(text)
    by_idx = {r["listing_index"]: r for r in rows}
    assert by_idx[1]["pa_status"] == "conditional"
    assert "pain" in by_idx[1]["service_label"].lower()
    assert by_idx[3]["pa_status"] == "not_required"
    assert by_idx[6]["pa_status"] == "not_required"
    assert by_idx[7]["pa_status"] == "required"
    assert by_idx[10]["pa_status"] == "not_required"
    assert "Cervical" in by_idx[10]["service_label"]
    assert by_idx[11]["pa_status"] == "required"


def test_parse_fhir_style_listing_true_false_column():
    text = """
insurance.item[] - Benefit Line Items
# productOrService.text authorizatio PA status
1 Acupuncture for chronic low back True conditional CONDITIONAL: Referral may be 57
pain required; cost depends on PCP vs
2 Ambulance services True conditional CONDITIONAL: Only non-emergency 59-6
3 Annual routine physical exam False not-require 60
d
4 Annual wellness visit False not-require 60-6
d 1
5 Bone mass measurement False not-require 61
d
6 Breast cancer screening False not-require 61
(mammograms) d
7 Cardiac rehabilitation services True required 62
"""
    rows = parse_pa_listing_text(text)
    by_idx = {r["listing_index"]: r for r in rows}
    assert by_idx[1]["pa_status"] == "conditional"
    assert by_idx[3]["pa_status"] == "not_required"
    assert by_idx[7]["pa_status"] == "required"


def test_labels_match_fuzzy():
    assert labels_match("Cardiac rehabilitation services", "Cardiac rehabilitation services visit")
    assert labels_match("Outpatient diagnostic tests - X-rays", "Outpatient diagnostic tests - X-rays")
    assert not labels_match("Emergency care", "Hospice care")


def test_listing_match_prefers_discriminating_sibling():
    from app.ingest.pa_markers import best_coverage_for_listing, listing_match_score

    listing = "Outpatient diagnostic tests - X-rays"
    stem = {"id": "1", "service_label": "Outpatient diagnostic tests"}
    xrays = {"id": "2", "service_label": "Outpatient diagnostic tests - X-rays"}
    lab = {"id": "3", "service_label": "Outpatient diagnostic tests - laboratory tests"}
    assert listing_match_score(listing, xrays["service_label"]) > listing_match_score(
        listing, stem["service_label"]
    )
    best = best_coverage_for_listing(listing, [stem, lab, xrays])
    assert best is not None and best["id"] == "2"


def test_fold_closes_open_paren_after_status():
    from app.ingest.pa_markers import _fold_name_continuation

    name = _fold_name_continuation(
        "Immunizations (flu, pneumonia,",
        "COVID-19, Hepatitis B, others) d 2",
    )
    assert "Hepatitis" in name
    assert name.count("(") == name.count(")")


def test_choose_richer_listing_by_name_completeness():
    from app.ingest.pa_markers import choose_richer_listing

    short = [
        {"service_label": "Cardiovascular disease risk", "listing_index": 8},
        {"service_label": "Dental services", "listing_index": 15},
    ]
    fuller = [
        {"service_label": "Cardiovascular disease risk reduction visit", "listing_index": 8},
        {"service_label": "Dental services (Medicare-covered, limited medical circumstances)", "listing_index": 15},
    ]
    assert choose_richer_listing([short, fuller]) is fuller

