"""Fragment coverage labels must not become review rows."""

from __future__ import annotations

from app.ingest.coverage_lines import (
    is_plausible_service_label,
    merge_uncovered,
    uncovered_lines,
)
from app.ingest.pa_markers import windows_by_service_line


def test_page60_wrap_fragments_are_not_plausible():
    junk = [
        "Medicine (ACAOM); and,",
        "· a current, full, active, and",
        "Rico) of the United States, or",
        "District of Columbia.",
        "Auxiliary personnel furnishing",
        "Acupuncture services performed by",
        "· a master’s or doctoral level degree",
        "· Benefit is",
        "Medicine from a school accredited",
        "Physician assistants (PAs), nurse",
        "Covered services include:",
        "Covered services include, but aren’t",
        "We cover medically necessary services",
        "Note: If you’re having surgery in a",
        "· Cancer",
        "· Surgical supplies, such as",
        "Service: Medicine (ACAOM); and, | In-network: Covered",
        "For people with diabetes who have",
        "Mental health services provided by a",
        "Medically-necessary services that",
        "Comprehensive programs of cardiac",
    ]
    for label in junk:
        assert not is_plausible_service_label(label), label


def test_real_chart_services_stay_plausible():
    good = [
        "Acupuncture for chronic low back pain",
        "Emergency care",
        "Inpatient hospital care",
        "Pulmonary rehabilitation services",
        "Annual wellness visit",
        "Durable medical equipment (DME)",
        "· Outpatient diagnostics – X-rays",
        "· Pulmonary rehabilitation services",
        "Ambulance services",
        "Medicare diabetes prevention program (MDPP)",
        "Immunizations",
        "Service: Emergency care | In-network: $0 copay",
        "· Diagnostic non-laboratory tests",
        "· Outpatient dialysis treatments",
    ]
    for label in good:
        assert is_plausible_service_label(label), label


def test_uncovered_skips_eligibility_wraps_keeps_cost_rows():
    page = "\n".join(
        [
            "Acupuncture for chronic low back pain $0 copayment",
            "· a master’s or doctoral level degree in Oriental",
            "Medicine (ACAOM); and,",
            "· a current, full, active, and unrestricted license",
            "Rico) of the United States, or",
            "District of Columbia.",
            "Auxiliary personnel furnishing",
            "Emergency care $0 copay per visit",
        ]
    )
    missing = uncovered_lines(page, [])
    blob = "\n".join(missing)
    assert "Emergency care" in blob
    assert "Acupuncture for chronic low back pain" in blob
    assert "ACAOM" not in blob
    assert "District of Columbia" not in blob
    assert "Auxiliary personnel" not in blob
    assert "Rico)" not in blob


def test_merge_uncovered_drops_fragment_lines():
    lines = [
        "Medicine (ACAOM); and,",
        "Emergency care $0 copay per visit",
        "Rico) of the United States, or",
    ]
    rows = merge_uncovered(lines, [], page=60, existing_keys=set())
    labels = [row["service_label"] for row in rows]
    assert any(label.startswith("Emergency care") for label in labels)
    assert all("ACAOM" not in label for label in labels)
    assert all("Rico)" not in label for label in labels)


def test_windows_do_not_borrow_next_bullet_marker():
    dagger = "\u2020"
    em = "\u2014"
    text = "\n".join(
        [
            "· Surgical supplies, such as",
            f"· Splints, casts, and other devices medical supply.{em}{dagger}{dagger}",
        ]
    )
    rows = windows_by_service_line(text)
    surgical = next((r for r in rows if "Surgical supplies" in r["service_line"]), None)
    splints = next((r for r in rows if "Splints" in r["service_line"]), None)
    assert splints is not None and splints["pa_required"] is True
    if surgical is not None:
        assert surgical["pa_required"] is False
