"""Detect prior-authorization markers on benefit-chart pages.

EOCs often put the marker on a later line of the same row (for example
visit followed by an em-dash and two dagger characters). The dagger may also
be spaced. This module never hardcodes a payer name.
"""

from __future__ import annotations

import re

# Double-dagger as printed in many Medicare EOCs: em-dash + dagger dagger, sometimes spaced.
_DAGGER = "\u2020"  # dagger
_DOUBLE = "\u2021"  # double dagger
_EM = "\u2014"  # em dash

_MARKER_RE = re.compile(
    rf"(?:{_EM}|-)?\s*(?:{_DOUBLE}{_DOUBLE}|{_DOUBLE}|{_DAGGER}\s*{_DAGGER}|{_DAGGER}{_DAGGER})"
)
_AUTH_WORDS = (
    "prior authorization",
    "preauthorization",
    "precertification",
    "prior approval",
    "authorization required",
    "referral required",
)
_CONDITIONAL_WORDS = (
    "referral may be required",
    "may need prior authorization",
    "may require prior authorization",
    "prior authorization may be",
    "conditional",
)


def marker_in_text(text: str) -> tuple[bool, str | None]:
    """Return whether a PA marker appears, and the matched token."""
    if not text:
        return False, None
    match = _MARKER_RE.search(text)
    if match:
        return True, match.group(0).replace(" ", "")
    for token in (_DOUBLE + _DOUBLE, _DOUBLE, _DAGGER + _DAGGER, _EM + _DAGGER + _DAGGER):
        if token in text:
            return True, token
    return False, None


def line_pa_status(text: str) -> tuple[str, str | None]:
    """Classify a text window: required | not_required | conditional."""
    low = (text or "").lower()
    hit, marker = marker_in_text(text or "")
    # Explicit negatives win over a bare "prior authorization" phrase.
    if re.search(
        r"\bno (?:referral(?:/approval)?|prior authorization|preauthorization|precertification)\b"
        r"|\bnot require(?:d)? prior authorization\b"
        r"|\bprior authorization (?:is )?not required\b"
        r"|\bwithout prior authorization\b",
        low,
    ):
        if any(word in low for word in _CONDITIONAL_WORDS):
            return "conditional", marker
        return "not_required", None
    if any(word in low for word in _CONDITIONAL_WORDS):
        return "conditional", marker
    if hit or any(word in low for word in _AUTH_WORDS):
        return "required", marker
    return "not_required", None


def windows_by_service_line(page_text: str, *, look_ahead: int = 4) -> list[dict]:
    """Pair a service-looking line with the next few lines (where markers often sit)."""
    lines = [re.sub(r"\s+", " ", line).strip() for line in (page_text or "").splitlines()]
    lines = [line for line in lines if line]
    rows: list[dict] = []
    for index, line in enumerate(lines):
        if not re.match(r"[A-Z\u00b7\u2022\*]", line):
            continue
        if len(line) < 8 or len(line) > 160:
            continue
        if line.lower().startswith(("depending", "medicaid", "your ", "the ", "this ", "see ")):
            continue
        # Eligibility / wrap bullets are not chart service rows (e.g. "· a current, full, active, and").
        bare = re.sub(r"^[\u00b7\u2022\*\-–—]+\s*", "", line).strip()
        if re.match(
            r"^(?:a|an|the|of|to|for|with|by|from|that|which|who|when|where|if|as|and|or)\b",
            bare,
            re.I,
        ):
            continue
        if re.search(r"(?:,|;|\band\b|\bor\b)\s*$", bare, re.I) and "$" not in bare:
            continue
        if re.match(r"^[A-Za-z]{0,16}\)", bare):
            continue
        # Legend / list-intro lines must not own a dagger from later rows.
        if re.match(
            r"^(?:covered services include|covered services that|\*?\s*covered services that do not|"
            r"we cover|note:|for the purpose of|service to have)\b",
            bare,
            re.I,
        ):
            continue
        if _MARKER_RE.search(line) and len(re.findall(r"[A-Za-z]{4,}", line)) < 2:
            continue
        if re.match(r"^\$|\bnot covered\b", line, re.I):
            continue
        chunk = [line]
        for nxt in lines[index + 1 : index + look_ahead + 1]:
            # Next bullet / starred service row owns its own marker — do not borrow.
            if re.match(r"^[\u00b7\u2022\*]", nxt) and len(nxt) >= 8:
                break
            if re.match(r"[A-Z]", nxt) and len(nxt) >= 12:
                words = re.findall(r"[A-Za-z]{4,}", nxt)
                # New capitalized service row stops the window (do not borrow its marker).
                if len(words) >= 2 and not re.match(
                    r"^(visit|network|copayment|coinsurance|medicare-covered)\b", nxt, re.I
                ):
                    break
            chunk.append(nxt)
        window = "\n".join(chunk)
        status, marker = line_pa_status(window)
        rows.append(
            {
                "service_line": line,
                "window": window,
                "pa_status": status,
                "pa_required": status in {"required", "conditional"},
                "marker_used": marker,
            }
        )
    return rows


def annotate_item_from_pages(item: dict, pages: list[dict]) -> dict | None:
    """Return data patches for pa_required / pa_status when the EOC page shows a marker."""
    from app.ingest.coverage_lines import is_plausible_service_label

    label = (item.get("service_label") or (item.get("data") or {}).get("service_label") or "").strip()
    if len(label) < 4 or not is_plausible_service_label(label):
        return {
            "pa_required": False,
            "pa_status": "not_required",
            "marker_used": None,
        }
    page_no = int(item.get("page") or (item.get("data") or {}).get("page") or 0)
    page_by = {int(p["page"]): p for p in pages}
    candidates = []
    if page_no in page_by:
        candidates.append(page_by[page_no])
    for delta in (-1, 1, 2):
        other = page_by.get(page_no + delta)
        if other:
            candidates.append(other)
    label_low = label.lower()
    best = None
    for page in candidates:
        text = page.get("text") or ""
        if label_low not in text.lower() and label_low[:24] not in text.lower():
            continue
        for row in windows_by_service_line(text):
            service = row["service_line"].lower()
            if not (label_low in service or service in label_low or label_low[:20] in service):
                continue
            if best is None or (row["pa_status"] == "required" and best["pa_status"] != "required"):
                best = {**row, "page": page["page"]}
            if row["pa_status"] == "required":
                break
        if best and best["pa_status"] == "required":
            break
    if best is None:
        page = page_by.get(page_no)
        if page is None:
            return None
        words = {
            w
            for w in re.findall(r"[a-z]{4,}", label_low)
            if w not in {"with", "from", "that", "this", "services", "service"}
        }
        for row in windows_by_service_line(page.get("text") or ""):
            service_words = set(re.findall(r"[a-z]{4,}", row["service_line"].lower()))
            if words and len(words & service_words) >= max(1, min(2, len(words) // 2)):
                best = {**row, "page": page_no}
                break
    if best is None:
        return None
    return {
        "pa_required": best["pa_required"],
        "pa_status": best["pa_status"],
        "marker_used": best.get("marker_used"),
    }


def _listing_body_lines(text: str) -> list[str]:
    raw = (text or "").replace("\r\n", "\n")
    raw = re.sub(r"Conditiona\s*\n\s*l\b", "Conditional", raw, flags=re.I)
    raw = re.sub(r"not-require\s*\n\s*d\b", "not-required", raw, flags=re.I)
    raw = re.sub(r"Not\s*\n\s*required\b", "Not required", raw, flags=re.I)
    lines = [re.sub(r"\s+", " ", line).strip() for line in raw.splitlines()]
    lines = [line for line in lines if line]
    start = 0
    for index, line in enumerate(lines):
        low = line.lower()
        if "full benefit category" in low or "insurance.item" in low:
            start = index + 1
    while start < len(lines) and (
        lines[start].startswith("#")
        or lines[start].lower().startswith(("legend", "or network", "each row"))
    ):
        start += 1
    return lines[start:]


def _starts_listing_entry(line: str, number: int, lines: list[str], index: int) -> str | None:
    """Return match kind if this line begins listing row `number`."""
    if re.match(rf"^{number}\s+[A-Z\"\u201c\(]", line):
        return "full"
    if 10 <= number <= 99:
        tens, ones = divmod(number, 10)
        nxt = lines[index + 1] if index + 1 < len(lines) else ""
        if re.fullmatch(rf"{tens}", line) and (
            re.fullmatch(rf"{ones}", nxt) or re.match(rf"^{ones}\s+", nxt)
        ):
            return "split_digit"
        if re.match(rf"^{tens}\s+[A-Z\"\u201c\(]", line) and (
            re.fullmatch(rf"{ones}", nxt) or re.match(rf"^{ones}\s+", nxt)
        ):
            return "split_name"
    return None


def _scrub_listing_rest(after: str) -> str:
    """Drop cost-share / page-ref noise so name wraps can be found."""
    rest = after or ""
    for _ in range(8):
        nxt = re.sub(r"^[\s\d\-–—/,.]+", "", rest)
        nxt = re.sub(
            r"^(?:\$[^\w]*\d*[^\w]*|copay(?:ment)?s?\b[^A-Za-z]*|coinsurance\b[^A-Za-z]*|"
            r"cost[\s-]*share\b[^A-Za-z]*|also subject to\b.{0,80}?|"
            r"for certain drugs\b[^A-Za-z]*|separate administration\b[^A-Za-z]*|"
            r"pg\.?\b[^A-Za-z]*)",
            "",
            nxt,
            flags=re.I,
        )
        if nxt == rest:
            break
        rest = nxt
    return rest.strip()


def _fold_name_continuation(name: str, after: str) -> str:
    """Append wrapped name fragments that appear after the status token."""
    name = (name or "").strip()
    rest = _scrub_listing_rest(after)
    # Close an open parenthesis that wrapped past the status column.
    open_parens = name.count("(") - name.count(")")
    if open_parens > 0:
        for match in re.finditer(r"([^)]{1,100}\))", rest):
            frag = match.group(1).strip()
            if re.search(r"\$|\bcopay\b|\brequired\b|\bsubject to\b|\bpg\b", frag, re.I):
                continue
            if not re.search(r"[A-Za-z]{3,}", frag):
                continue
            name = (name + " " + frag).strip()
            rest = _scrub_listing_rest(rest[match.end() :])
            break
    dangling = bool(
        re.search(r"(?:,|;|\band\b|\bor\b|\bof\b|\bfor\b|\bwith\b|\bby\b|\bto\b)\s*$", name, re.I)
        or name.rstrip().endswith(("-", "–", "—"))
    )
    # Hyphen subtype that landed after the status: "… services -" + "PCP office visit"
    if name.rstrip().endswith(("-", "–", "—")):
        cont = re.match(
            r"([A-Za-z][A-Za-z0-9\-/]*(?:\s+[A-Za-z][A-Za-z0-9\-/]*){0,10})",
            rest,
        )
        if cont:
            frag = cont.group(1).strip()
            frag = re.sub(r"\s+[dl]$", "", frag, flags=re.I).strip()
            if len(frag) >= 3 and frag[0].isupper() and not frag.lower().startswith(
                ("depends", "referral", "may ", "only", "needed", "monitoring", "by ", "care")
            ):
                return (name.rstrip() + " " + frag).strip()
        return name.rstrip(" -\u2013\u2014").strip()
    # Lowercase wrap after a dangling connector: "devices and" + "related supplies"
    if dangling:
        cont = re.match(
            r"([a-z][a-z0-9\-/]*(?:\s+[A-Za-z][A-Za-z0-9\-/]*){0,8})",
            rest,
        )
        if cont:
            frag = cont.group(1).strip()
            frag = re.sub(r"\s+[dl]$", "", frag, flags=re.I).strip()
            if len(frag) >= 3 and not frag.lower().startswith(
                (
                    "referral",
                    "depends",
                    "only",
                    "may ",
                    "needed",
                    "paid",
                    "also",
                    "some ",
                    "must ",
                    "applies",
                    "manual",
                    "preventive",
                    "general",
                    "monthly",
                    "no ",
                    "waived",
                    "provided",
                    "combined",
                    "ssbci",
                    "conditional",
                    "true",
                    "false",
                    "within",
                    "cost",
                    "copay",
                    "home",
                    "injectables",
                )
            ):
                return (name + " " + frag).strip()
    cont = re.match(
        r"([A-Za-z][A-Za-z\-]*(?:\s+\([^)]+\)|\s+[A-Za-z][A-Za-z0-9\-/]*){0,10})",
        rest,
    )
    if not cont:
        return name
    frag = cont.group(1).strip()
    if frag.lower() in {"d", "l"} or len(frag) < 3:
        return name
    frag = re.sub(r"\s+[dl]$", "", frag, flags=re.I).strip()
    skip_prefixes = (
        "referral",
        "depends",
        "only",
        "may ",
        "needed",
        "paid",
        "also",
        "some ",
        "must ",
        "applies",
        "manual",
        "preventive",
        "general",
        "monthly",
        "no ",
        "waived",
        "provided",
        "combined",
        "ssbci",
        "conditional",
        "true",
        "false",
        "consultation",
        "within",
        "cost",
        "copay",
    )
    if not frag or frag.lower().startswith(skip_prefixes):
        return name
    if frag.lower() in {"conditional", "required", "not-required", "not-require"}:
        return name
    return (name + " " + frag).strip()


def _status_from_listing_block(block: str) -> tuple[str | None, str | None]:
    """Return (status, name) from one joined listing entry."""
    m = re.search(
        r"\b(True|False)\s+(required|not-required|not-require|conditional)\b",
        block,
        re.I,
    )
    if m:
        token = m.group(2).lower()
        if token.startswith("not"):
            status = "not_required"
        elif token.startswith("cond"):
            status = "conditional"
        else:
            status = "required"
        name = _fold_name_continuation(block[: m.start()], block[m.end() :])
        return status, name

    had_conditiona = bool(re.search(r"\bConditiona\b", block, re.I))
    block2 = re.sub(r"\bConditiona\b", "Conditional", block, flags=re.I)
    if re.search(r"\bConditional\b", block2, re.I):
        name = re.split(r"\bConditional\b", block2, maxsplit=1, flags=re.I)[0]
        rest = re.split(r"\bConditional\b", block2, maxsplit=1, flags=re.I)[1]
        # Only fold "word l" when the PDF split Conditional across lines (Conditiona / l).
        # Otherwise "monitoring l" / "by l" from notes falsely become the subtype.
        if had_conditiona:
            skip = {
                "may",
                "need",
                "depends",
                "developing",
                "administration",
                "required",
                "only",
                "never",
                "needed",
                "monitoring",
                "by",
                "care",
                "hospital",
                "outpatient",
            }
            cont = re.search(r"\b([a-z][a-z\-]+)\s+l\b", rest)
            if cont and cont.group(1) not in skip:
                name = (name + " " + cont.group(1)).strip()
        open_parens = name.count("(") - name.count(")")
        if open_parens > 0:
            closer = re.search(r"([^)]{1,120}\))", rest)
            if closer:
                name = (name + " " + closer.group(1)).strip()
        name = _fold_name_continuation(name, rest)
        return "conditional", name

    if re.search(r"\bNot\s+required\b", block2, re.I):
        parts = re.split(r"\bNot\s+required\b", block2, maxsplit=1, flags=re.I)
        name = _fold_name_continuation(parts[0], parts[1] if len(parts) > 1 else "")
        return "not_required", name

    if re.search(r"\bNot\b", block2):
        m_not = re.search(r"\bNot\b", block2)
        assert m_not is not None
        for m_req in re.finditer(r"\brequired\b", block2, re.I):
            if m_req.start() <= m_not.end():
                continue
            before = block2[m_not.end() : m_req.start()]
            if re.search(r"\bmay\s+be\b", before, re.I):
                continue
            words = re.findall(r"[A-Za-z]{3,}", before)
            if len(words) <= 16:
                name = block2[: m_not.start()]
                # Paren wrap often sits between Not and required.
                open_parens = name.count("(") - name.count(")")
                if open_parens > 0:
                    hay = before + " " + block2[m_req.end() :]
                    for match in re.finditer(r"([^)]{1,100}\))", hay):
                        frag = match.group(1).strip()
                        if re.search(r"\$|\bcopay\b|\brequired\b|\bsubject to\b|\bpg\b", frag, re.I):
                            continue
                        if not re.search(r"[A-Za-z]{3,}", frag):
                            continue
                        name = (name + " " + frag).strip()
                        break
                name = _fold_name_continuation(name, before + " " + block2[m_req.end() :])
                return "not_required", name

    for m_req in re.finditer(r"\bRequired\b", block2):
        prefix = block2[max(0, m_req.start() - 12) : m_req.start()].lower()
        if "may be" in prefix or prefix.rstrip().endswith("not"):
            continue
        name = _fold_name_continuation(block2[: m_req.start()], block2[m_req.end() :])
        return "required", name
    return None, None


def parse_pa_listing_text(text: str) -> list[dict]:
    """Parse a PA requirements listing (category + Required/Not required/Conditional)."""
    lines = _listing_body_lines(text)
    expected = 1
    index = 0
    found: list[tuple[int, list[str]]] = []
    current: list[str] = []
    while index < len(lines) and expected <= 100:
        kind = _starts_listing_entry(lines[index], expected, lines, index)
        if kind:
            if current:
                found.append((expected - 1, current))
            if kind.startswith("split"):
                current = [lines[index], lines[index + 1]]
                index += 2
            else:
                current = [lines[index]]
                index += 1
            expected += 1
            continue
        if current:
            current.append(lines[index])
        index += 1
    if current:
        found.append((expected - 1, current))

    rows: list[dict] = []
    for number, block_lines in found:
        block = " ".join(block_lines)
        block = re.sub(rf"^{number}\s+", "", block)
        if number >= 10:
            tens, ones = divmod(number, 10)
            block = re.sub(rf"^{tens}\s+{ones}\s+", "", block)
            block = re.sub(rf"^{tens}\s+", "", block)
            if re.match(rf"^{ones}\s+", block):
                block = re.sub(rf"^{ones}\s+", "", block, count=1)
        status, name = _status_from_listing_block(block)
        if status is None or name is None:
            continue
        name = re.sub(r"\s+", " ", name).strip(" -\u2013\u2014|,")
        name = re.sub(r"^\d+\s+", "", name)
        name = re.sub(r"\s+[dl]$", "", name, flags=re.I).strip()
        # Status tokens must not remain as a fake subtype after a dash.
        name = re.sub(
            r"\s*[-–—]?\s*(?:conditional|conditiona|required|not[\s-]*required|not-require)\s*$",
            "",
            name,
            flags=re.I,
        ).strip(" -\u2013\u2014|,")
        # Drop a leftover single leading digit from split entry numbers (e.g. "3 Immunizations").
        name = re.sub(r"^\d\s+(?=[A-Z\"\u201c])", "", name).strip()
        if len(name) < 4:
            continue
        rows.append(
            {
                "service_label": name[:160],
                "pa_status": status,
                "pa_required": status in {"required", "conditional"},
                "listing_index": number,
            }
        )
    return rows


def significant_words(label: str) -> set[str]:
    stop = {
        "with",
        "from",
        "that",
        "this",
        "services",
        "service",
        "care",
        "and",
        "for",
        "the",
        "incl",
        "including",
        "related",
        "other",
        "etc",
        "without",
        "contrast",
        "views",
        "view",
    }
    text = (label or "").lower()
    # Keep imaging stems intact ("x-ray" / "x-rays" → xray) so order text matches EOC labels.
    text = re.sub(r"\bx[\s\-]?rays?\b", "xray", text)
    text = re.sub(r"\b(ct|mri|pet)[\s\-]?scans?\b", r"\1", text)
    # 3+ letters so short clinical tokens (mri, snf, pet) still match benefit labels.
    return {w for w in re.findall(r"[a-z]{3,}", text) if w not in stop}


def labels_match(a: str, b: str) -> bool:
    """True when two service labels refer to the same benefit row."""
    ka = re.sub(r"[^a-z0-9]+", "", (a or "").lower())
    kb = re.sub(r"[^a-z0-9]+", "", (b or "").lower())
    if not ka or not kb:
        return False
    if ka == kb:
        return True
    shorter, longer = (ka, kb) if len(ka) <= len(kb) else (kb, ka)
    if len(shorter) >= 16 and shorter in longer and len(shorter) >= int(0.6 * len(longer)):
        return True
    wa, wb = significant_words(a), significant_words(b)
    if not wa or not wb:
        return False
    overlap = len(wa & wb)
    if overlap < 2:
        return False
    return overlap / len(wa | wb) > 0.5


def listing_match_score(listing_label: str, coverage_label: str) -> float:
    """Higher is better. Used to pick among several fuzzy hits (no service catalog)."""
    a = (listing_label or "").strip()
    b = (coverage_label or "").strip()
    if not a or not b:
        return -1.0
    try:
        from app.ingest.coverage_lines import is_plausible_service_label

        if not is_plausible_service_label(b):
            return -1.0
    except Exception:
        pass
    ka = re.sub(r"[^a-z0-9]+", "", a.lower())
    kb = re.sub(r"[^a-z0-9]+", "", b.lower())
    if ka == kb:
        return 100.0
    wa, wb = significant_words(a), significant_words(b)
    if not wa or not wb:
        return -1.0
    overlap = wa & wb
    if len(overlap) < 2 and ka not in kb and kb not in ka:
        return -1.0
    union = wa | wb
    jaccard = len(overlap) / len(union)
    # Penalize short stems that absorb a longer sibling family name.
    coverage_ratio = len(overlap) / max(len(wa), len(wb))
    size_penalty = abs(len(wa) - len(wb)) * 0.08
    # Reward discriminators present in both (tokens unique to neither side alone).
    score = (jaccard * 40.0) + (coverage_ratio * 40.0) - size_penalty
    if ka in kb or kb in ka:
        shorter, longer = (ka, kb) if len(ka) <= len(kb) else (kb, ka)
        if len(shorter) >= 12:
            score += 8.0 * (len(shorter) / len(longer))
    # Prefer closer raw lengths for subtype rows ("… - X-rays" vs bare stem).
    score -= abs(len(a) - len(b)) * 0.02
    return score


def best_coverage_for_listing(
    listing_label: str,
    candidates: list[dict],
    *,
    claimed: set[str] | None = None,
    floor: float = 28.0,
) -> dict | None:
    """Pick the best unmatched coverage row for a listing label, or None if ambiguous/weak."""
    claimed = claimed or set()
    scored: list[tuple[float, dict]] = []
    for item in candidates:
        if not item or item.get("id") in claimed:
            continue
        label = item.get("service_label") or (item.get("data") or {}).get("service_label") or ""
        score = listing_match_score(listing_label, label)
        if score < floor:
            continue
        # Soft boost when the item already carries a listing_index (revive path).
        if (item.get("data") or {}).get("listing_index") is not None:
            score += 1.0
        scored.append((score, item))
    if not scored:
        return None
    scored.sort(key=lambda row: (-row[0], len(row[1].get("service_label") or "")))
    best_score, best = scored[0]
    # Near-tie with a different word set → refuse and let insert create a distinct row.
    if len(scored) > 1 and best_score - scored[1][0] < 3.0:
        w_best = significant_words(best.get("service_label") or "")
        w_second = significant_words(scored[1][1].get("service_label") or "")
        if w_best != w_second:
            return None
    return best


def listing_parse_quality(rows: list[dict]) -> tuple[int, float, int]:
    """Rank a listing parse: more rows, longer mean names, fewer truncated open-parens."""
    if not rows:
        return (0, 0.0, 0)
    labels = [str(r.get("service_label") or "") for r in rows]
    mean_len = sum(len(label) for label in labels) / len(labels)
    open_trunc = sum(1 for label in labels if label.count("(") > label.count(")"))
    return (len(rows), mean_len, -open_trunc)


def choose_richer_listing(parses: list[list[dict]]) -> list[dict]:
    """Prefer the parse with more complete service names (structural, not payer-specific)."""
    ranked = sorted(
        (listing_parse_quality(rows), rows) for rows in parses if rows
    )
    if not ranked:
        return []
    return ranked[-1][1]
