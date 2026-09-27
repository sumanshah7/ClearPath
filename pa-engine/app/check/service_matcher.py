"""Match an order to live policy items. Never guess when several candidates remain."""

from __future__ import annotations

from app import llm
from app.repository import get_repo


def match(
    order_text: str,
    service_code: str | None,
    drug_name: str | None,
    catalog_item_id: str | None,
    *,
    run_id: str,
    insurer: str | None = None,
    plan_name: str | None = None,
    plan_year: str | None = None,
) -> dict:
    repo = get_repo()
    rules = repo.live_items_for_plan(insurer=insurer, plan_name=plan_name, plan_year=plan_year, item_type="rule")
    coverage = repo.live_items_for_plan(insurer=insurer, plan_name=plan_name, plan_year=plan_year, item_type="coverage")
    if drug_name:
        return _drug(repo, drug_name, insurer=insurer, plan_name=plan_name, plan_year=plan_year)
    if catalog_item_id:
        item = repo.get_item(catalog_item_id)
        if item and item["review_state"] != "rejected":
            policy = repo.get_policy(item["policy_id"])
            if policy and policy.get("status") != "live":
                return {"status": "none", "items": [], "candidates": None}
            if policy and insurer and policy.get("insurer") != insurer:
                return {"status": "none", "items": [], "candidates": None}
            return {"status": "matched", "items": [item], "candidates": None}
    # Exact / near-exact coverage label match before LLM (payer-agnostic, no hardcoding).
    coverage_hit = _coverage_by_label(coverage, order_text)
    if coverage_hit is not None:
        return {"status": "coverage", "items": [coverage_hit], "candidates": None}
    if service_code:
        hits = [item for item in rules if service_code in _codes(item)]
        policies = {item["policy_id"] for item in hits}
        if len(policies) == 1:
            return {"status": "matched", "items": hits, "candidates": None}
        if len(policies) > 1:
            return {"status": "ambiguous", "items": [], "candidates": [_candidate(item) for item in hits]}
        cov = [item for item in coverage if service_code in (item.get("service_codes") or [])]
        if len(cov) == 1:
            return {"status": "coverage", "items": cov, "candidates": None}
        if len(cov) > 1:
            from app.check.coverage_gate import _best_label_match

            best = _best_label_match(cov, order_text)
            if best is not None:
                return {"status": "coverage", "items": [best], "candidates": None}
            return {"status": "ambiguous", "items": [], "candidates": [_candidate(item) for item in cov]}
    candidates = []
    for item in rules + coverage:
        label = item.get("service_label") or item["data"].get("requirement_text") or ""
        applies = item["data"].get("applies_to") or []
        phrase_label = " ".join([label, *applies])
        candidates.append({"id": item["id"], "label": phrase_label, "page": item["page"]})
    if not candidates:
        return {"status": "none", "items": [], "candidates": None}
    ranked = llm.complete(
        "p_svc",
        {"order_text": order_text, "candidates": candidates},
        schema=None,
        stage="service_match",
        run_id=run_id,
    )["data"].get("candidates") or []
    verified = []
    for row in ranked:
        item = repo.get_item(row["item_id"])
        if item is None:
            continue
        phrase = (row.get("phrase") or "").lower()
        label = " ".join(
            [item.get("service_label") or "", *(item["data"].get("applies_to") or [])]
        ).lower()
        if phrase and phrase in label and phrase in order_text.lower():
            verified.append({**_candidate(item), "phrase": phrase, "score": row.get("score") or 0})
    if len(verified) == 1:
        item = repo.get_item(verified[0]["item_id"])
        kind = "coverage" if item["item_type"] == "coverage" else "matched"
        return {"status": kind, "items": [item], "candidates": None}
    if len(verified) > 1:
        top = verified[0]["score"]
        close = [row for row in verified if row["score"] >= top * 0.8]
        if len(close) == 1:
            item = repo.get_item(close[0]["item_id"])
            kind = "coverage" if item["item_type"] == "coverage" else "matched"
            return {"status": kind, "items": [item], "candidates": None}
        return {"status": "ambiguous", "items": [], "candidates": close}
    return {"status": "none", "items": [], "candidates": None}


def _coverage_by_label(coverage: list[dict], order_text: str) -> dict | None:
    from app.check.coverage_gate import _best_label_match
    from app.ingest.pa_markers import labels_match, significant_words

    order = (order_text or "").strip()
    if len(order) < 3:
        return None
    exact = [item for item in coverage if labels_match(order, item.get("service_label") or "")]
    if len(exact) == 1:
        return exact[0]
    if len(exact) > 1:
        return _best_label_match(exact, order)
    order_words = significant_words(order)
    scored = []
    for item in coverage:
        label = item.get("service_label") or ""
        label_words = significant_words(label)
        overlap = len(order_words & label_words)
        codes = [str(c) for c in (item.get("service_codes") or [])]
        code_hit = any(c and c in order for c in codes)
        # Short orders ("MRI lumbar") often share only one strong token with a chart label
        # that also carries a billing code; accept single-token overlap when signal is high.
        if code_hit or overlap >= 2 or (overlap >= 1 and len(order_words) <= 3):
            scored.append(item)
        elif order.lower() in label.lower():
            scored.append(item)
    if len(scored) == 1:
        return scored[0]
    if len(scored) > 1:
        return _best_label_match(scored, order)
    # Last resort: substring containment either way.
    contains = [
        item
        for item in coverage
        if order.lower() in (item.get("service_label") or "").lower()
        or (item.get("service_label") or "").lower() in order.lower()
    ]
    if len(contains) == 1:
        return contains[0]
    if len(contains) > 1:
        return _best_label_match(contains, order)
    return None


def coverage_candidates(
    order_text: str,
    *,
    limit: int = 8,
    insurer: str | None = None,
    plan_name: str | None = None,
    plan_year: str | None = None,
) -> list[dict]:
    """Suggest coverage rows when automatic match fails, so the clinician can choose."""
    from app.ingest.pa_markers import significant_words

    repo = get_repo()
    order_words = significant_words(order_text)
    ranked = []
    for item in repo.live_items_for_plan(
        insurer=insurer, plan_name=plan_name, plan_year=plan_year, item_type="coverage"
    ):
        label = item.get("service_label") or ""
        overlap = len(order_words & significant_words(label))
        if overlap or (order_text or "").lower() in label.lower():
            ranked.append((overlap, len(label), item))
    ranked.sort(key=lambda row: (-row[0], row[1]))
    return [_candidate(item) for _, _, item in ranked[:limit]]


def _drug(
    repo,
    drug_name: str,
    *,
    insurer: str | None = None,
    plan_name: str | None = None,
    plan_year: str | None = None,
) -> dict:
    blocks = repo.live_blocks() + [
        b for p in repo.list_policies(True) for b in repo.blocks_for(p["id"]) if b["status"] != "live"
    ]
    named = [b for b in blocks if drug_name.lower() in b["label"].lower() or b["label"].lower() in drug_name.lower()]
    if insurer or plan_name:
        filtered = []
        for block in named:
            policy = repo.get_policy(block["policy_id"])
            if policy is None:
                continue
            if insurer and policy.get("insurer") != insurer:
                continue
            if plan_name and policy.get("plan_name") != plan_name:
                continue
            if plan_year is not None and plan_year != "" and (policy.get("plan_year") or "") != plan_year:
                continue
            filtered.append(block)
        named = filtered
    live = [b for b in named if b["status"] == "live"]
    pending = [b for b in named if b["status"] != "live"]
    if live:
        items = repo.items_for(live[0]["policy_id"], live[0]["id"])
        items = [i for i in items if i["review_state"] != "rejected"]
        return {"status": "drug", "items": items, "block": live[0], "candidates": None}
    if pending:
        return {"status": "block_not_live", "items": [], "block": pending[0], "candidates": None}
    return {"status": "none", "items": [], "candidates": None}


def _codes(item: dict) -> list[str]:
    data = item.get("data") or {}
    return [*(item.get("service_codes") or []), *(data.get("codes") or []), *(data.get("applies_to") or [])]


def _candidate(item: dict) -> dict:
    return {
        "item_id": item["id"],
        "label": item.get("service_label") or item["data"].get("requirement_text"),
        "page": item["page"],
        "phrase": item.get("service_label") or "",
    }
