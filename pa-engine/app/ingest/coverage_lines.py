"""Find benefit-chart lines the extractor did not return.

Labels come from the document line. No service list is stored here.
"""

from __future__ import annotations

import re

from app.ingest.pa_markers import line_pa_status

_STRONG = re.compile(
    r"\bcopay\b|\bcoinsurance\b|\bnot covered\b|\bcovered\b.+\bcovered\b|\ballowance\b",
    re.I,
)
_COST_NEAR = re.compile(
    r"\$|\bcopay(?:ment)?\b|\bcoinsurance\b|\bnot covered\b|\byou pay\b|\ballowance\b|\bcovered\b",
    re.I,
)
_NOISE = re.compile(
    r"^(?:depending on(?: your [\w\s]+ eligibility,?)?\s+|your\s+|medicaid may\s+)",
    re.I,
)
_SKIP_HEAD = {
    "may",
    "will",
    "your",
    "the",
    "this",
    "medicaid",
    "medicare",
    "deductible",
    "generic",
    "additional",
    "benefits",
    "sharing",
    "drugs",
    "rates",
    "rays",
    "eligibility",
    "respite",
}
_HEAD = re.compile(r"\$|\bcopay\b|\bcoinsurance\b|\bnot covered\b|\bcovered\b", re.I)
_BULLET = re.compile(r"^[\u00b7\u2022\*\-–—]+\s*")
_SERVICE_HINT = re.compile(
    r"\b("
    r"care|services?|visits?|therapy|therapies|treatment|treatments|"
    r"imaging|hospital|surgery|surgeries|exam(?:ination)?s?|screening(?:s)?|"
    r"equipment|drugs?|medications?|ambulance|dialysis|prosthet\w*|orthotic\w*|"
    r"rehab(?:ilitation)?|vaccine(?:s)?|shots?|lab(?:oratory)?|"
    r"mammogram(?:s)?|colonoscopy|sigmoidoscopy|mri|ct|x-?rays?|ultrasound|"
    r"infusion|hospice|chiropractic|podiatry|dental|vision|hearing|fitness|"
    r"transportation|transplant|chemotherapy|radiation|provider|physician|"
    r"specialists?|inpatient|outpatient|wellness|snf|dme|"
    r"acupuncture|cardiac|pulmonary|emergency|urgent|observation|"
    r"diagnostics?|supplies|devices?|aids?|membership|program|"
    r"immunizations?|antigens?|otc|credit|meal(?:s)?|"
    r"gym|opioids?|substance|mental|behavioral"
    r")\b",
    re.I,
)
# Soft clinical tokens that alone do not make a chart service (disease list bullets).
_DISEASE_ONLY = re.compile(
    r"^(?:cancer|dementia|stroke|stretcher|hiv/?aids|hepatitis\s*[abc]?|"
    r"diabetes|obesity|hypertension|hyperlipidemia)\.?$",
    re.I,
)
_DANGLING_END = re.compile(
    r"(?:,|;|:|\(|\band\b|\bor\b|\bby\b|\bfor\b|\bfrom\b|\bwith\b|\bof\b|\bto\b|"
    r"\bthat\b|\bby a\b|\bof cardiac\b|"
    r"\bincluding\b|\bfurnishing\b|\bperformed by\b|\bprovided by\b|"
    r"\baccording to\b|\bas follows\b|\bsuch as\b|\bwho have\b|\bor signs\b|"
    r"\bevery\b|\busing\b|\band the\b|\bmay be\b|\bif you\b|"
    r"\byour\b|\byou.?re\b|\baren.?t\b|\binclude\b)\s*$",
    re.I,
)
_LEAD_SKIP = re.compile(
    r"^(?:a|an|the|of|to|for|with|by|from|that|which|who|when|where|if|as|and|or|"
    r"in|on|at|you|your|these|this|other|any|some|like|not|meet|contact|under|"
    r"needed|furnished|be|are|use|consist|receive|up to|first|referrals?|"
    r"generally|however|please|see|note|examples?|chapter|section|page|"
    r"depending|medicaid|we|includes?|you.?re|you.?ll|even|visit the|"
    r"members? of|comprehensive programs?|unitedhealthcare|"
    r"routine dental benefits|benefit guidelines|a \d+-day|"
    r"a home-delivered|doesn.?t include|medicare doesn.?t)\b",
    re.I,
)
_HARD_REJECT = re.compile(
    r"^(?:we cover|covered services include|covered services that|"
    r"doesn.?t include|medicare doesn.?t|if you|when you|with this|"
    r"for (?:the |services|drugs|all |assistance|people|new|preventive)|"
    r"note:|visit the|please |benefit guidelines|includes inpatient|"
    r"routine .+ you |for the purpose of|members of our plan|"
    r"service to have|service for more|outpatient diagnostic outpatient|"
    r"physician/practitioner|even if you|states, even if|"
    r"cgms from|rehabilitat(?:ion)? facilities|"
    r"unitedhealthcare hearing are not|"
    r"\*?\s*covered services that do not)\b",
    re.I,
)
_GEO_ONLY = re.compile(
    r"^(?:District of Columbia|Puerto Rico|United States|Virgin Islands|Guam|"
    r"American Samoa|Northern Mariana Islands)\.?$",
    re.I,
)
_PAREN_FRAG = re.compile(r"^[A-Za-z]{0,16}\)")
_CREDENTIAL = re.compile(
    r"\b(?:master.?s|doctoral|degree|license[sd]?|accredited|certif\w*|acaom)\b",
    re.I,
)
_NARRATIVE = re.compile(
    r"\b(?:in terms of|you pay|you will|you can|you have|you get|you were|"
    r"call customer|end of this|what you pay|covered service\s*\||"
    r"there is no|doesn.?t lead|born between|another country|"
    r"your doctor has|by phone, internet|monthly credit is|"
    r"change after you|paper copy sent|coordinated care plan must|"
    r"prior hospital stay is not|available if you live|"
    r"you.?ll get a credit|don.?t agree|used up your inpatient|"
    r"unrelated to your|admitted to a hospice|"
    r"not mainly for your convenience|furnished by a provider qualified|"
    r"provider access fees|trips are curb-to-curb|"
    r"these services will be|outpatient diagnostic outpatient)\b",
    re.I,
)


def _line_requires_pa(text: str) -> tuple[bool, str | None]:
    status, marker = line_pa_status(text)
    return status in {"required", "conditional"}, marker


def strip_bullet(text: str) -> str:
    return _BULLET.sub("", " ".join((text or "").split())).strip()


def normalize_service_label(text: str) -> str:
    """Strip bullets, footnotes, and marker noise from a stored service label."""
    label = strip_bullet(text)
    label = re.sub(r"\s*Provided by:.*$", "", label, flags=re.I).strip()
    label = re.sub(r"\s*[\.]*\s*[—\-–]*\s*[†‡\*]+\s*$", "", label).strip()
    label = re.sub(r"\s+", " ", label).strip(" .,:;|-")
    return label[:120]


def is_plausible_service_label(text: str) -> bool:
    """True when text can stand alone as a benefit-chart service name.

    Rejects wrapped eligibility bullets, mid-sentence fragments, geography-only
    lines, and dangling connectors that recovery used to promote to coverage rows.
    """
    raw = " ".join((text or "").split()).strip()
    if not raw:
        return False
    # Grid rows: only the service-name cell must be plausible.
    if raw.lower().startswith("service:"):
        name = raw.split("|", 1)[0]
        name = re.sub(r"^service:\s*", "", name, flags=re.I).strip()
        return is_plausible_service_label(name)

    label = normalize_service_label(raw)
    # Chart lines often append cost share — evaluate the service-name head only.
    cost_split = re.compile(r"\$|\bcopay(?:ment)?\b|\bcoinsurance\b|\bnot covered\b|\byou pay\b", re.I)
    if cost_split.search(label):
        label = cost_split.split(label, maxsplit=1)[0].strip(" .,:;|-")
        label = normalize_service_label(label)
    if len(label) < 3:
        return False
    if re.match(r"^[a-z]", label):
        return False
    if _PAREN_FRAG.match(label):
        return False
    if _DANGLING_END.search(label):
        return False
    if _HARD_REJECT.match(label):
        return False
    # Truncated provider lists: "Physician assistants (PAs), nurse"
    if re.search(
        r",\s+(?:nurse|nurses|doctor|doctors|provider|providers|practitioner|"
        r"practitioners|therapist|therapists|specialist|specialists)\s*$",
        label,
        re.I,
    ):
        return False
    if _GEO_ONLY.match(label):
        return False
    if _DISEASE_ONLY.match(label):
        return False
    if re.match(r"^Benefit is\b", label, re.I) and len(label) < 48:
        return False
    # Note: must not use \b after the colon (colon is non-word).
    if re.match(r"^(?:Chapter|Section|Page|See\b|Note:|Examples?\s+include|Please\s+refer)\b", label, re.I):
        return False
    if re.match(r"^Note:", label, re.I):
        return False
    if _NARRATIVE.search(label):
        return False
    if re.match(
        r"^(?:You (?:can|have|get|were|are|will)|These (?:services|screenings|drugs)|"
        r"OTC catalog|Details of|Observation services are|SET (?:is|may)|"
        r"MDPP services are|Original Medicare|Provider Requirements|"
        r"Medicare Beneficiary|Grace Period|Covered service|A plan|Some Medicare|"
        r"Home care|Specialist)\.?$",
        label,
        re.I,
    ):
        return False
    if _CREDENTIAL.search(label) and not _SERVICE_HINT.search(label):
        return False
    has_hint = bool(_SERVICE_HINT.search(label))
    # Lead-skip words need a real chart shape (category dash or short named benefit).
    if _LEAD_SKIP.match(label):
        if not has_hint:
            return False
        # "For people with diabetes…" / "We cover medically necessary services"
        if re.match(r"^(?:for|we|if|when|with|includes?|not|any|some)\b", label, re.I):
            return False
    if re.match(r"^Medicare\b", label, re.I) and not has_hint:
        return False
    words = re.findall(r"[A-Za-z]{3,}", label)
    if len(words) < 1:
        return False
    # Cost / column mash stuck into the *name* (after head split above).
    if re.search(r"\$|\bcopay(?:ment)?\b|\bcoinsurance\b|\bmonthly credit\b", label, re.I):
        return False
    if has_hint:
        # Still reject sentence-like titles that merely contain a hint word.
        if re.search(r"\b(?:must provide|are not|include, but|that do not|who have)\b", label, re.I):
            return False
        if len(label) > 90 and not re.search(r"[–—\-]", label):
            return False
        return True
    # Short Title-ish / chart names without a hint word: Immunizations, Bone mass measurement.
    tokens = re.findall(r"[A-Za-z][A-Za-z\-]{1,}", label)
    if 1 <= len(tokens) <= 6 and tokens[0][0].isupper():
        if not re.search(
            r"\b(?:you|your|these|this|when|where|which|that|who|most|all|we|if)\b",
            label,
            re.I,
        ):
            if not re.search(r"(?:Chek|Contour|Accu)\b", label):
                if not _DISEASE_ONLY.match(label) and not re.search(r"[,:;]$", label):
                    return len(label) <= 64
    return False


def candidate_lines(text: str) -> list[str]:
    found: list[str] = []
    seen: set[str] = set()
    for raw in (text or "").splitlines():
        original = " ".join(raw.split())
        line = _NOISE.sub("", original).strip()
        if len(line) < 18 or len(line) > 240:
            continue
        if not re.match(r"[A-Z]", line):
            continue
        if not _STRONG.search(line):
            continue
        if re.search(r"\bmay have\b|\byou pay\b|\byou will\b|\bin this stage\b|\bthe plan pays\b", line, re.I):
            continue
        if "." in line[:12]:
            continue
        head = _HEAD.split(line, maxsplit=1)[0]
        words = re.findall(r"[A-Za-z][A-Za-z\-]{3,}", head)
        if words and all(word.lower().startswith("medicare") or word.lower() == "covered" for word in words):
            continue
        if not words or words[0].lower() in _SKIP_HEAD:
            continue
        if len(words) == 1 and words[0].lower() in {"covered", "medicare-covered", "outpatient"}:
            continue
        if not is_plausible_service_label(service_label_from_line(original)):
            continue
        key = original.lower()
        if key in seen:
            continue
        seen.add(key)
        found.append(original)
    return found


def uncovered_lines(text: str, items: list[dict]) -> list[str]:
    from app.ingest.pa_markers import windows_by_service_line

    labels = []
    evidence = []
    for item in items:
        if item.get("review_state") == "rejected":
            continue
        label = (item.get("service_label") or "").strip().lower()
        if len(label) >= 6:
            labels.append(label)
        data = item.get("data") if isinstance(item.get("data"), dict) else {}
        quote = (data.get("evidence_text") or item.get("evidence_text") or "").strip().lower()
        if quote:
            evidence.append(quote)
    missing = []
    seen: set[str] = set()
    # Prefer service-line windows so a dagger on the next line still marks the row,
    # but only when the line is a plausible chart service (not eligibility wrap text).
    window_rows = windows_by_service_line(text)
    window_lines: list[str] = []
    for row in window_rows:
        line = row["service_line"]
        if not is_plausible_service_label(line):
            continue
        # Narrative wraps without cost signals on the row are not missing chart services.
        if not _COST_NEAR.search(row.get("window") or line) and not _SERVICE_HINT.search(strip_bullet(line)):
            continue
        if not _COST_NEAR.search(row.get("window") or line) and _LEAD_SKIP.match(strip_bullet(line)):
            continue
        window_lines.append(line)
    for line in window_lines + candidate_lines(text):
        low = line.lower()
        if low in seen:
            continue
        seen.add(low)
        if any(quote and (quote in low or low in quote) for quote in evidence):
            continue
        if any(label in low for label in labels):
            continue
        label = service_label_from_line(line)
        if not is_plausible_service_label(label):
            continue
        missing.append(line)
    return missing


def service_label_from_line(line: str) -> str:
    head = _HEAD.split(_NOISE.sub("", line).strip(), maxsplit=1)[0]
    head = normalize_service_label(head)
    if len(head) < 3:
        head = normalize_service_label(" ".join(line.split()[:6]))
    return head[:80]


def clean_label(model_label: str | None, line: str) -> str:
    parsed = service_label_from_line(line)
    label = normalize_service_label(str(model_label or ""))
    if not label or "$" in label or label.lower().startswith("depending"):
        return parsed
    if not is_plausible_service_label(label) and is_plausible_service_label(parsed):
        return parsed
    return label


def coverage_from_line(line: str, page: int, *, window: str | None = None) -> dict:
    """Build a coverage row. Prefer a multi-line window so next-line daggers count."""
    status, marker = line_pa_status(window or line)
    return {
        "service_label": service_label_from_line(line),
        "service_codes": [],
        "pa_required": status in {"required", "conditional"},
        "pa_status": status,
        "page": page,
        "evidence_text": (window or line).splitlines()[0][:240] if window else line,
        "marker_used": marker,
        "reference": None,
    }


def merge_uncovered(lines: list[str], model_items: list, page: int, existing_keys: set[str]) -> list[dict]:
    """One coverage row per missed line. The model row is used only when its quote is that line."""
    from app.ingest.pa_markers import line_pa_status, windows_by_service_line

    by_line = {line: line for line in lines}
    # Map service line -> full window text for next-line marker detection.
    window_for: dict[str, str] = {}
    for row in windows_by_service_line("\n".join(lines)):
        window_for[row["service_line"]] = row["window"]
    chosen: dict[str, dict] = {}
    for item in model_items:
        if not isinstance(item, dict):
            continue
        evidence = " ".join(str(item.get("evidence_text") or "").split())
        if evidence not in by_line:
            continue
        row = {**item, "page": page, "evidence_text": evidence}
        row["service_label"] = clean_label(row.get("service_label"), evidence)
        if not is_plausible_service_label(row.get("service_label") or ""):
            continue
        chosen[evidence] = row
    rows = []
    seen = set(existing_keys)
    for line in lines:
        window = window_for.get(line)
        row = chosen.get(line) or coverage_from_line(line, page, window=window)
        label = str(row.get("service_label") or "")
        if not is_plausible_service_label(label):
            continue
        if chosen.get(line) and window:
            status, marker = line_pa_status(window)
            row = {
                **row,
                "pa_required": status in {"required", "conditional"},
                "pa_status": status,
                "marker_used": marker or row.get("marker_used"),
            }
        key = re.sub(r"[^a-z0-9]+", "_", label.lower()).strip("_")[:48] or "coverage"
        if key in seen:
            continue
        seen.add(key)
        rows.append(row)
    return rows
