"""The only module that sequences model calls (F4). Extraction is strictly sequential."""

from __future__ import annotations

import hashlib
import logging
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from app.config import settings
from app.errors import ApiError
from app.fhir.builders import build_for_policy
from app.fhir.validate import validate_resource
from app.ingest import cascade, pdf_reader, precheck, upload_validation
from app.ingest.coverage_lines import (
    is_plausible_service_label,
    merge_uncovered,
    normalize_service_label,
    service_label_from_line,
    uncovered_lines,
)
from app.ingest.scorecard import benefit_score
from app.ingest.table_grid import rows_from_words
from app.ingest.grounding import check as ground
from app.ingest.judge import judge_item
from app.ingest.question_builder import build as build_questions
from app.llm import complete, reset_steps
from app.pipeline import cache
from app.pipeline.context_builder import clean
from app.pipeline.episodes import EpisodeRecorder
from app.pipeline.working_memory import apply_updates, empty, merge_open_item, remember_items, render
from app.repository import get_repo, new_id, now
from app.review.review import initial_state

ROLES = {"benefit_summary", "clinical_policy", "drug_criteria"}
_log = logging.getLogger(__name__)
_inflight: dict[str, threading.Thread] = {}
_inflight_lock = threading.Lock()


def ingest_upload(
    data: bytes,
    file_name: str,
    document_role: str,
    source_url: str | None,
    downloaded_at: str | None,
    source_kind: str = "published",
) -> dict:
    if document_role not in ROLES:
        raise ApiError("INVALID_PDF", "document_role is not a known role.", 400)
    upload_validation.validate_bytes(data)
    digest = hashlib.sha256(data).hexdigest()
    repo = get_repo()
    existing = repo.policy_by_sha(digest)
    if existing:
        return _from_fingerprint_cache(existing, digest, file_name, source_url, source_kind)
    settings.storage_dir.mkdir(parents=True, exist_ok=True)
    path = settings.storage_dir / f"{digest}.pdf"
    path.write_bytes(data)
    policy = repo.create_policy(
        {
            "id": new_id(),
            "document_role": document_role,
            "source_kind": source_kind,
            "source_url": source_url,
            "downloaded_at": downloaded_at,
            "file_name": file_name,
            "storage_path": str(path),
            "sha256": digest,
            "status": "ingesting",
            "ingestion_state": {},
        }
    )
    # Return immediately so the UI can poll the episode timeline (no browser timeout).
    start_ingestion(policy["id"])
    body = upload_response(repo.get_policy(policy["id"]), cached=False)
    body["accepted"] = True
    body["message"] = "Document accepted. Reading pages and extracting rules in the background."
    body["sha256"] = digest
    return body


def start_ingestion(policy_id: str) -> bool:
    """Start a background read if one is not already running. Returns True when newly started."""
    with _inflight_lock:
        current = _inflight.get(policy_id)
        if current is not None and current.is_alive():
            return False
        thread = threading.Thread(
            target=_run_safe,
            args=(policy_id,),
            daemon=False,
            name=f"ingest-{policy_id[:8]}",
        )
        _inflight[policy_id] = thread
        thread.start()
        return True


def ingestion_running(policy_id: str) -> bool:
    with _inflight_lock:
        current = _inflight.get(policy_id)
        return current is not None and current.is_alive()


def wait_for_ingestion(policy_id: str, timeout: float = 300.0) -> dict:
    """Block until a background read finishes (used by demo bootstrap and tests)."""
    import time

    deadline = time.time() + timeout
    while time.time() < deadline:
        if not ingestion_running(policy_id):
            policy = get_repo().get_policy(policy_id)
            if policy is None:
                raise ApiError("POLICY_NOT_FOUND", "No policy with that id.", 404)
            if policy["status"] != "ingesting":
                return upload_response(policy, cached=False)
        time.sleep(0.05)
    raise ApiError("RUN_LIMIT", "Ingestion did not finish before the wait limit.", 504)


def _run_safe(policy_id: str) -> None:
    try:
        run(policy_id)
    except Exception as exc:
        _log.exception("Background ingestion failed for %s: %s", policy_id, exc)
        try:
            repo = get_repo()
            policy = repo.get_policy(policy_id)
            if policy and policy.get("status") == "ingesting":
                repo.update_policy(policy_id, {"status": "failed"}, actor="engine")
            for row in repo._conn.execute(
                "select id from ingestion_runs where policy_id = ? and status = 'running' order by rowid desc limit 1",
                (policy_id,),
            ):
                repo.finish_run(row[0], "failed", str(exc)[:500])
        except Exception:
            _log.exception("Failed to mark policy %s failed after ingest error", policy_id)
    except BaseException as exc:
        _log.exception("Background ingestion aborted for %s: %s", policy_id, exc)
        try:
            repo = get_repo()
            if (repo.get_policy(policy_id) or {}).get("status") == "ingesting":
                repo.update_policy(policy_id, {"status": "failed"}, actor="engine")
        except Exception:
            pass
        raise
    finally:
        with _inflight_lock:
            current = _inflight.get(policy_id)
            if current is threading.current_thread():
                _inflight.pop(policy_id, None)
        # Orphan guard: thread ended while status still ingesting (e.g. hard kill mid-call).
        try:
            repo = get_repo()
            policy = repo.get_policy(policy_id)
            if policy and policy.get("status") == "ingesting" and not ingestion_running(policy_id):
                # Only mark failed if no newer thread took over.
                with _inflight_lock:
                    alive = _inflight.get(policy_id)
                    if alive is None or not alive.is_alive():
                        repo.update_policy(policy_id, {"status": "failed"}, actor="engine")
                        for row in repo._conn.execute(
                            "select id from ingestion_runs where policy_id = ? and status = 'running' order by rowid desc limit 1",
                            (policy_id,),
                        ):
                            repo.finish_run(row[0], "failed", "Ingestion thread exited before draft.")
        except Exception:
            pass


def _from_fingerprint_cache(
    existing: dict,
    digest: str,
    file_name: str,
    source_url: str | None,
    source_kind: str,
) -> dict:
    """Same PDF bytes always load the stored policy. File name does not matter (K1)."""
    repo = get_repo()
    if existing["status"] == "failed":
        repo.update_policy(existing["id"], {"status": "ingesting"}, actor="engine")
        start_ingestion(existing["id"])
        body = upload_response(repo.get_policy(existing["id"]), cached=False)
        body["accepted"] = True
        body["message"] = (
            f"Same fingerprint {digest[:12]} failed earlier. Restarting the read in the background."
        )
        body["sha256"] = digest
        return body
    if existing["status"] == "ingesting":
        # Resume if the process died; otherwise the UI keeps watching the live run.
        start_ingestion(existing["id"])
        body = upload_response(repo.get_policy(existing["id"]), cached=False)
        body["accepted"] = True
        body["cached"] = False
        body["cache_message"] = (
            f"Same fingerprint {digest[:12]} is already being read. Open the timeline to watch progress."
        )
        body["sha256"] = digest
        return body
    changes: dict = {}
    if file_name and file_name != existing.get("file_name"):
        changes["file_name"] = file_name
    if source_url and source_url != existing.get("source_url"):
        changes["source_url"] = source_url
    if source_kind and source_kind != existing.get("source_kind"):
        changes["source_kind"] = source_kind
    if changes:
        repo.update_policy(existing["id"], changes, actor="engine")
    recorder = EpisodeRecorder("policy", existing["id"], None)
    recorder.record(
        "cache",
        f"Same document fingerprint {digest[:12]} — opened from cache without re-extracting",
        status="cache_hit",
        detail={"sha256": digest, "file_name": file_name or existing.get("file_name")},
    )
    body = upload_response(repo.get_policy(existing["id"]), cached=True)
    body["cache_message"] = (
        f"This PDF was already read (fingerprint {digest[:12]}). "
        "Loaded the saved plan and rules. No new model calls."
    )
    body["sha256"] = digest
    return body


def run(policy_id: str) -> dict:
    reset_steps()
    repo = get_repo()
    policy = repo.get_policy(policy_id)
    if policy is None:
        raise ApiError("POLICY_NOT_FOUND", "No policy with that id.", 404)
    plan_key = policy["sha256"][:16]
    ingestion = repo.create_run(policy_id, plan_key)
    recorder = EpisodeRecorder("policy", policy_id, ingestion["id"])
    try:
        result = _run(policy, ingestion["id"], recorder)
        repo.finish_run(ingestion["id"], "complete")
        return result
    except ApiError as exc:
        status = "partial" if exc.code in {"ROLE_UNCONFIRMED", "RUN_LIMIT"} else "failed"
        repo.finish_run(ingestion["id"], status, exc.code)
        if exc.code == "ROLE_UNCONFIRMED":
            raise
        repo.update_policy(policy_id, {"status": "failed"}, actor="engine")
        raise
    except Exception as exc:
        repo.finish_run(ingestion["id"], "failed", str(exc))
        repo.update_policy(policy_id, {"status": "failed"}, actor="engine")
        raise


def _run(policy: dict, run_id: str, recorder: EpisodeRecorder) -> dict:
    repo = get_repo()
    digest = policy["sha256"]
    recorder.record("upload", f"Received {policy['document_role']}, sha256 {digest[:12]}")

    def read_pages():
        hit = cache.get(cache.stage_key(digest, "pages"))
        if hit is not None:
            return {"pages": hit, "cache_hit": True, "detail": {"pages": len(hit)}}
        pages = pdf_reader.read_pdf(policy["storage_path"])
        upload_validation.require_text_layer(pages)
        cache.put(cache.stage_key(digest, "pages"), "stage", digest, pages, None)
        return {"pages": pages, "cache_hit": False, "detail": {"pages": len(pages)}}

    read = recorder.step("pages", "Reading pages", read_pages, done=lambda v: f"Read {len(v['pages'])} pages")
    pages = read["pages"]
    repo.update_policy(policy["id"], {"page_count": len(pages)}, actor="engine")

    def do_clean():
        hit = cache.get(cache.stage_key(digest, "clean"))
        if hit is not None:
            return {**hit, "cache_hit": True}
        cleaned, report = clean(pages)
        payload = {"pages": cleaned, "report": report}
        cache.put(cache.stage_key(digest, "clean"), "stage", digest, payload, None)
        return {**payload, "cache_hit": False, "detail": report}

    cleaned_result = recorder.step(
        "clean",
        "Cleaning pages",
        do_clean,
        done=lambda v: f"Tokens {v['report']['tokens_before']} to {v['report']['tokens_after']}",
    )
    cleaned = cleaned_result["pages"]
    context_report = cleaned_result["report"]

    hints = precheck.precheck(cleaned, policy["document_role"])
    recorder.record(
        "precheck",
        f"Role hint {hints['role_hint']}"
        + (" matches declared role" if not hints["mismatch"] else " differs from declared role"),
        detail=hints,
    )
    if hints["mismatch"] and not policy.get("role_confirmed_by"):
        repo.update_policy(
            policy["id"],
            {"status": "paused", "role_hint": hints["role_hint"], "format_hints": hints, "context_report": context_report},
            actor="engine",
        )
        raise ApiError(
            "ROLE_UNCONFIRMED",
            "The pre-check role hint does not match the declared role. Confirm the document type to continue.",
            409,
            policy_id=policy["id"],
        )

    identity = _identity(cleaned, run_id, recorder, digest)
    sections = cascade.locate(cleaned, policy["document_role"], hints)
    recorder.record(
        "cascade",
        f"Tier {sections['tier_used']} located pages {sections['pages']}",
        detail=sections,
    )
    repo.update_policy(
        policy["id"],
        {
            "insurer": (identity.get("insurer") or {}).get("value"),
            "plan_name": (identity.get("plan_name") or {}).get("value"),
            "plan_year": (identity.get("plan_year") or {}).get("value"),
            "identity_evidence": identity,
            "role_hint": hints["role_hint"],
            "format_hints": hints,
            "sections": sections,
            "possibly_truncated": sections["possibly_truncated"],
            "context_report": context_report,
        },
        actor="engine",
    )
    policy = repo.get_policy(policy["id"])

    if policy["document_role"] == "drug_criteria":
        _index_blocks(policy, cleaned, sections, recorder)
        repo.update_policy(policy["id"], {"status": "draft", "working_memory": empty()}, actor="engine")
        return upload_response(repo.get_policy(policy["id"]), cached=False)

    page_by_num = {p["page"]: p for p in cleaned}
    selected = [page_by_num[n] for n in sections["pages"] if n in page_by_num] or cleaned
    # One page per EOC extract call — dense benefit charts time out when batched (was 2).
    batch = 1 if policy["document_role"] == "benefit_summary" else None
    groups = _groups(selected, batch)
    memory = empty()
    extracted: list[dict] = []
    prompt = "p0" if policy["document_role"] == "benefit_summary" else "p1"
    for index, group in enumerate(groups, start=1):
        def extract(group=group, index=index, memory=memory):
            pages_key = ",".join(str(p["page"]) for p in group)
            hit_key = cache.stage_key(digest, "extract", f"{index}:{pages_key}")
            hit = cache.get(hit_key)
            if hit is not None:
                return {**hit, "cache_hit": True}
            table_rows = [row["line"] for page in group for row in (page.get("grid_rows") or [])]
            user = {
                "pages": [{"page": p["page"], "text": p["text"], "role": "target" if p.get("citable", True) else "context"} for p in group],
                "memory": render(memory),
                "items_found": memory.get("items_found") or [],
                "service_rows_on_these_pages": len(table_rows) or _service_row_signals(group),
                "table_rows": table_rows,
            }
            data = complete(prompt, user, schema=None, stage="extract", run_id=run_id, group_no=index)["data"]
            payload = {"data": data, "fingerprint": hit_key}
            cache.put(hit_key, "stage", digest, payload, None)
            return payload

        try:
            result = recorder.step(
                "extract",
                f"Extracting group {index} of {len(groups)}",
                extract,
                group_no=index,
                done=lambda v, index=index: f"Extracted group {index}",
            )
            # Prefer the primary extract. Re-split only when clearly thin and not a cache hit.
            coverage = (result.get("data") or {}).get("coverage") or (result.get("data") or {}).get("criteria") or []
            if result.get("cache_hit") or not _batch_needs_split(
                policy["document_role"], group, coverage if isinstance(coverage, list) else []
            ):
                pieces = [(result["data"], group)]
            else:
                pieces = _complete_batches(
                    policy["document_role"], group, result["data"], prompt, run_id, index, memory, digest
                )
            for data, piece in pieces:
                fresh = data.get("criteria") or data.get("coverage") or []
                if not isinstance(fresh, list):
                    fresh = []
                fresh = [item for item in fresh if isinstance(item, dict)]
                fresh = merge_open_item(memory, fresh)
                extracted.extend(fresh)
                texts = {p["page"]: p["text"] for p in piece}
                memory = apply_updates(memory, data.get("memory_updates"), texts)
                memory = remember_items(memory, [_key(item) for item in fresh])
            repo.update_policy(
                policy["id"],
                {"working_memory": memory, "ingestion_state": {"group": index, "groups": len(groups)}},
                actor="engine",
            )
            # Checkpoint after every group so a killed thread does not lose page work.
            if extracted:
                _persist_items(policy, extracted, page_by_num, run_id, recorder)
                extracted = []
        except Exception as exc:
            # One bad/timed-out group must not wipe the rest of an EOC chart (overview resume).
            recorder.record(
                "extract",
                f"Skipped group {index} after extract error: {exc}",
                status="failed",
                group_no=index,
                detail={"error": str(exc)},
            )
            continue

    if extracted:
        _persist_items(policy, extracted, page_by_num, run_id, recorder)
    saved = [row for row in repo.items_for(policy["id"]) if row]
    if policy["document_role"] == "benefit_summary":
        try:
            saved.extend(_recover_missing_coverage(policy, selected, page_by_num, run_id, recorder, digest))
            _apply_grid(policy, selected)
            _fill_grid_gaps(policy, selected, run_id, recorder)
            _recheck_unsure(policy, selected, run_id, recorder)
            saved = [row for row in repo.items_for(policy["id"]) if row]
        except Exception as exc:
            recorder.record(
                "extract",
                f"Benefit chart fill/recheck skipped after error: {exc}",
                status="failed",
                detail={"error": str(exc)},
            )
            saved = [row for row in repo.items_for(policy["id"]) if row] or saved
    if policy["document_role"] == "benefit_summary":
        try:
            reconcile_pa_flags(policy["id"])
            saved = [row for row in repo.items_for(policy["id"]) if row] or saved
        except Exception as exc:
            recorder.record("extract", f"PA marker reconcile skipped: {exc}", status="failed", detail={"error": str(exc)})
    _finish_draft(policy, saved, memory, recorder)
    return upload_response(repo.get_policy(policy["id"]), cached=False)


def extract_block(policy_id: str, block_id: str) -> dict:
    reset_steps()
    repo = get_repo()
    policy = repo.get_policy(policy_id)
    block = repo.get_block(block_id)
    if not policy or not block or block["policy_id"] != policy_id:
        raise ApiError("POLICY_NOT_FOUND", "That block is not on this policy.", 404)
    run = repo.create_run(policy_id, policy["sha256"][:16] + block["block_key"])
    recorder = EpisodeRecorder("block", block_id, run["id"])
    pages = cache.get(cache.stage_key(policy["sha256"], "clean"))
    page_rows = pages["pages"] if isinstance(pages, dict) else pdf_reader.read_pdf(policy["storage_path"])
    chosen = [p for p in page_rows if block["start_page"] <= p["page"] <= block["end_page"]]
    repo.update_block(block_id, {"status": "extracting"})
    data = complete(
        "p1d",
        {"block_label": block["label"], "pages": [{"page": p["page"], "text": p["text"], "role": "target"} for p in chosen]},
        schema=None,
        stage="extract",
        run_id=run["id"],
    )["data"]
    page_by_num = {p["page"]: p for p in chosen}
    repo.delete_unlocked_items(policy_id, block_id)
    saved = _persist_items(policy, data.get("criteria") or [], page_by_num, run["id"], recorder, block_id=block_id)
    repo.update_block(block_id, {"status": "draft"})
    repo.finish_run(run["id"], "complete")
    _finish_draft(policy, repo.items_for(policy_id), empty(), recorder)
    return upload_response(repo.get_policy(policy_id), cached=False)


def _identity(pages, run_id, recorder, digest) -> dict:
    def load():
        hit = cache.get(cache.stage_key(digest, "identity"))
        if hit is not None:
            return {"identity": hit, "cache_hit": True}
        # Prefer a human-confirmed identity when the model path is unavailable (e.g. 403).
        repo = get_repo()
        # digest is sha256 of the policy being read; find by cache key context via run.
        first = pages[:5]
        data = complete(
            "p_id",
            {"pages": [{"page": p["page"], "text": p["text"]} for p in first]},
            schema=None,
            stage="identity",
            run_id=run_id,
        )["data"]
        blob = "\n".join(p["text"] for p in first)
        for field in ("insurer", "plan_name", "plan_year", "document_title"):
            entry = data.get(field) or {}
            if not isinstance(entry, dict):
                data[field] = {"value": None, "evidence": None, "page": None}
                continue
            evidence = entry.get("evidence")
            if evidence and evidence not in blob:
                data[field] = {"value": None, "evidence": None, "page": None}
        cache.put(cache.stage_key(digest, "identity"), "stage", digest, data, None)
        return {"identity": data, "cache_hit": False}

    # If the reviewer already set identity, do not call the model.
    try:
        from app.repository import get_repo as _get_repo

        # run_id is tied to a policy via ingestion_runs
        run = _get_repo()._one("ingestion_runs", "select * from ingestion_runs where id = ?", (run_id,))
        if run and run.get("policy_id"):
            policy = _get_repo().get_policy(run["policy_id"])
            if policy and policy.get("identity_edited_by_human") and (
                policy.get("insurer") or (policy.get("identity_evidence") or {}).get("insurer")
            ):
                evidence = policy.get("identity_evidence") or {}
                identity = {
                    "insurer": evidence.get("insurer")
                    or {"value": policy.get("insurer"), "evidence": "human edit", "page": None},
                    "plan_name": evidence.get("plan_name")
                    or {"value": policy.get("plan_name"), "evidence": "human edit", "page": None},
                    "plan_year": evidence.get("plan_year")
                    or {"value": policy.get("plan_year"), "evidence": "human edit", "page": None},
                }
                recorder.record("identity", "Using reviewer-confirmed plan identity", status="completed")
                return identity
    except Exception:
        pass

    result = recorder.step("identity", "Reading plan identity", load, done=lambda v: "Identity read from the document")
    return result["identity"]


def _index_blocks(policy, pages, sections, recorder) -> None:
    repo = get_repo()
    current = None
    found = []
    for page in pages:
        if page["page"] not in sections["pages"] and sections["pages"]:
            continue
        for line in (page.get("text") or "").splitlines():
            if line.strip().lower().startswith("drug:"):
                if current:
                    found.append(current)
                label = line.split(":", 1)[1].strip()
                current = {
                    "policy_id": policy["id"],
                    "block_key": re.sub(r"[^a-z0-9]+", "_", label.lower()).strip("_") or "block",
                    "label": label,
                    "start_page": page["page"],
                    "end_page": page["page"],
                    "tier_used": sections["tier_used"],
                    "status": "indexed",
                }
        if current:
            current["end_page"] = page["page"]
    if current:
        found.append(current)
    if not found:
        found.append(
            {
                "policy_id": policy["id"],
                "block_key": "document",
                "label": policy.get("file_name") or "Drug criteria",
                "start_page": pages[0]["page"],
                "end_page": pages[-1]["page"],
                "tier_used": sections["tier_used"],
                "status": "indexed",
            }
        )
    existing = {block["block_key"] for block in repo.blocks_for(policy["id"])}
    for block in found:
        if block["block_key"] in existing:
            continue
        repo.create_block(block)
    recorder.record("index_blocks", f"Indexed {len(found)} drug blocks without a model call", detail={"count": len(found)})


def _persist_items(policy, extracted, page_by_num, run_id, recorder, block_id: str | None = None) -> list[dict]:
    repo = get_repo()
    saved = []
    seen = set()
    prepared = []
    for seq, raw in enumerate(extracted, start=1):
        try:
            item = _normalize(raw, policy["document_role"])
        except Exception:
            continue
        key = item["item_key"]
        page = item["page"]
        page_text = (page_by_num.get(page) or {}).get("text") or ""
        if item["item_type"] == "coverage":
            evidence = (item["payload"].get("evidence_text") or "").strip()
            label = (item.get("service_label") or "").strip()
            if key in seen or not evidence or evidence not in page_text:
                continue
            # Drop wrapped eligibility / mid-sentence shards the model or recover pass invents.
            if not is_plausible_service_label(label) and not is_plausible_service_label(evidence):
                continue
        elif key in seen:
            key = f"{key}_{seq}"
            item["item_key"] = key
        seen.add(key)
        prepared.append({"item": item, "page_text": page_text, "key": key, "page": page, "seq": seq})
    # Keep stable sequence across incremental checkpoints.
    base = len(repo.items_for(policy["id"], block_id))
    for offset, row in enumerate(prepared):
        if not next((i for i in repo.items_for(policy["id"], block_id) if i["item_key"] == row["key"]), None):
            row["seq"] = base + offset + 1
    assessed = _assess_together(prepared, run_id)
    for row, (grounding, verdict, question_verdict, data) in zip(prepared, assessed):
        item = row["item"]
        key = row["key"]
        page = row["page"]
        seq = row["seq"]
        state = initial_state(grounding, verdict["verdict"], question_verdict)
        existing = next((row for row in repo.items_for(policy["id"], block_id) if row["item_key"] == key), None)
        fields = {
            "data": data,
            "page": page,
            "service_label": item.get("service_label"),
            "service_codes": item.get("service_codes") or [],
            "grounding": grounding,
            "judge_verdict": verdict["verdict"] if verdict["verdict"] != "UNAVAILABLE" else "UNAVAILABLE",
            "judge_reason": verdict.get("reason"),
            "question_verdict": question_verdict,
            "review_state": state,
        }
        if existing and existing["edited_by_human"]:
            repo.write_item(existing["id"], fields, actor="engine")
            saved.append(repo.get_item(existing["id"]))
        elif existing:
            saved.append(repo.write_item(existing["id"], fields, actor="engine"))
        else:
            saved.append(
                repo.insert_item(
                    {
                        "policy_id": policy["id"],
                        "block_id": block_id,
                        "item_type": item["item_type"],
                        "item_key": key,
                        "seq": seq,
                        "service_label": item.get("service_label"),
                        "service_codes": item.get("service_codes") or [],
                        "data": data,
                        "original_data": item["payload"],
                        "page": page,
                        "grounding": grounding,
                        "judge_verdict": fields["judge_verdict"],
                        "judge_reason": verdict.get("reason"),
                        "question_verdict": question_verdict,
                        "review_state": state,
                        "edited_by_human": False,
                    }
                )
            )
    recorder.record(
        "judge",
        "Judged extracted items",
        detail={"count": len(saved)},
    )
    return [row for row in saved if row]


def _assess_together(prepared: list[dict], run_id: str) -> list[tuple]:
    """Judge rows together, then keep document order (F4)."""
    if not prepared:
        return []

    def assess(row: dict) -> tuple:
        item = row["item"]
        page_text = row["page_text"]
        try:
            grounding = ground(item["payload"], page_text)
            verdict = judge_item(item["payload"], page_text, run_id=run_id)
        except Exception:
            grounding = {"passed": False, "failures": ["The judge or grounding step failed for this item."]}
            verdict = {"verdict": "UNAVAILABLE", "reason": "Provider failure. A person still decides."}
        question_verdict = None
        data = item["payload"]
        if item["item_type"] == "rule":
            try:
                built = build_questions(data, run_id=run_id)
                data = {**data, "questions": built["questions"], "pass_condition": built["pass_condition"]}
                question_verdict = built["question_verdict"]
            except Exception:
                question_verdict = "unavailable"
        return grounding, verdict, question_verdict, data

    return [assess(row) for row in prepared]


def _row_is_sure(item: dict) -> bool:
    grounding = item.get("grounding") or {}
    return bool(grounding.get("passed")) and item.get("judge_verdict") == "ACCURATE" and item.get("question_verdict") in {None, "complete"}


def _apply_grid(policy, pages) -> None:
    """Add labeled chart rows to the page text so a quote can cite the row."""
    # Prefer pages already in memory / clean cache — do not re-read a 250+ page PDF here.
    by_num = {page["page"]: page for page in pages}
    missing = [n for n in by_num if not (by_num[n].get("words") or by_num[n].get("grid_rows"))]
    if missing:
        try:
            fresh = {page["page"]: page for page in pdf_reader.read_pdf(policy["storage_path"])}
        except Exception:
            fresh = {}
    else:
        fresh = {}
    for page in pages:
        source = fresh.get(page["page"]) or page
        if page.get("grid_rows"):
            grid = page["grid_rows"]
        else:
            grid = rows_from_words(source.get("words") or [], float(source.get("width") or 612))
        page["grid_rows"] = grid
        text = page.get("text") or ""
        extra = [row["line"] for row in grid if row["line"] not in text]
        if extra:
            page["text"] = (text + "\n" + "\n".join(extra)).strip()


def _fill_grid_gaps(policy, pages, run_id, recorder) -> None:
    """One model call per page for chart rows that are not saved yet."""
    repo = get_repo()
    for page in pages:
        grid = page.get("grid_rows") or []
        if not grid:
            continue
        items = repo.items_for(policy["id"])
        missing = [row for row in grid if not _grid_saved(row["service"], items)]
        if not missing:
            continue
        lines = [row["line"] for row in missing]

        def load(page=page, lines=lines):
            data = complete(
                "p0",
                {
                    "pages": [{"page": page["page"], "text": page.get("text") or "", "role": "target"}],
                    "table_rows": lines,
                    "service_rows_on_these_pages": len(lines),
                    "items_found": [],
                    "memory": {},
                },
                schema=None,
                stage="extract",
                run_id=run_id,
                group_no=page["page"],
            )["data"]
            return {"data": data}

        result = recorder.step(
            "extract",
            f"Reading {len(lines)} chart rows on page {page['page']}",
            load,
            group_no=page["page"],
            done=lambda v, page=page: f"Read chart rows on page {page['page']}",
        )
        model_items = (result.get("data") or {}).get("coverage") or []
        existing = {row["item_key"] for row in repo.items_for(policy["id"])}
        fresh = merge_uncovered(lines, model_items if isinstance(model_items, list) else [], page["page"], existing)
        if fresh:
            _persist_items(policy, fresh, {page["page"]: page}, run_id, recorder)


def _recheck_unsure(policy, pages, run_id, recorder) -> int:
    """Send only the flagged rows back, together, with the chart row when we have one."""
    repo = get_repo()
    page_by_num = {page["page"]: page for page in pages}
    unsure = [
        item
        for item in repo.items_for(policy["id"])
        if item and not item.get("edited_by_human") and not _row_is_sure(item)
    ]
    if not unsure:
        recorder.record("recheck", "No flagged rows to recheck", detail={"count": 0})
        return 0

    def correct(item: dict) -> tuple[dict, dict | None]:
        page = page_by_num.get(item.get("page")) or {}
        grid = _matching_grid(item.get("service_label") or "", page.get("grid_rows") or [])
        try:
            data = complete(
                "p_recheck",
                {
                    "service_label": item.get("service_label"),
                    "judge_verdict": item.get("judge_verdict"),
                    "judge_reason": item.get("judge_reason"),
                    "page": item.get("page"),
                    "page_text": (page.get("text") or "")[:8000],
                    "table_row": grid["line"] if grid else None,
                },
                schema=None,
                stage="recheck",
                run_id=run_id,
                item_key=item.get("item_key"),
            )["data"]
        except Exception:
            return item, None
        corrected = data.get("corrected") if isinstance(data, dict) else None
        if not isinstance(corrected, dict):
            return item, None
        evidence = " ".join(str(corrected.get("evidence_text") or "").split())
        if grid and evidence != grid["line"]:
            evidence = grid["line"]
            corrected = {**corrected, "evidence_text": evidence, "service_label": grid["service"]}
        if not evidence or evidence not in (page.get("text") or ""):
            return item, None
        corrected["page"] = item.get("page")
        corrected["evidence_text"] = evidence
        return item, corrected

    workers = 1 if len(unsure) < 2 else min(settings.max_concurrent_llm, len(unsure))
    if workers == 1:
        results = [correct(item) for item in unsure]
    else:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            results = list(pool.map(correct, unsure))
    updated = 0
    for item, corrected in results:
        if not corrected:
            continue
        page = page_by_num.get(item.get("page")) or {}
        payload = {**(item.get("data") or {}), **corrected}
        try:
            grounding = ground(payload, page.get("text") or "")
            verdict = judge_item(payload, page.get("text") or "", run_id=run_id)
        except Exception:
            continue
        if not grounding.get("passed"):
            continue
        fields = {
            "data": payload,
            "service_label": corrected.get("service_label") or item.get("service_label"),
            "grounding": grounding,
            "judge_verdict": verdict.get("verdict") or "UNAVAILABLE",
            "judge_reason": verdict.get("reason"),
            "review_state": initial_state(grounding, verdict.get("verdict") or "UNAVAILABLE", item.get("question_verdict")),
        }
        if item.get("review_state") in {"accepted", "edited", "rejected"}:
            fields.pop("review_state")
        repo.write_item(item["id"], fields, actor="engine")
        updated += 1
    recorder.record("recheck", f"Rechecked {len(unsure)} flagged rows", detail={"updated": updated, "flagged": len(unsure)})
    return updated


def _all_grid(pages: list[dict]) -> list[dict]:
    return [row for page in pages for row in (page.get("grid_rows") or [])]


def _grid_saved(service: str, items: list[dict]) -> bool:
    return any(_labels_match(service, item.get("service_label") or "") for item in items)


def _matching_grid(label: str, rows: list[dict]) -> dict | None:
    for row in rows:
        if _labels_match(label, row.get("service") or ""):
            return row
    return None


def _labels_match(left: str, right: str) -> bool:
    a = re.sub(r"[^a-z0-9]+", " ", left.lower()).strip()
    b = re.sub(r"[^a-z0-9]+", " ", right.lower()).strip()
    if len(a) < 4 or len(b) < 4:
        return False
    if a in b or b in a:
        return True
    a_words = [word for word in a.split() if len(word) >= 5][:2]
    return bool(a_words) and all(word in b for word in a_words)


def _reject_fragment_coverage(policy_id: str, recorder=None) -> int:
    """Auto-reject coverage rows that are wrapped sentence fragments, not services.

    Restores only previously auto-rejected rows whose label is now plausible AND
    the judge already marked them ACCURATE with grounding passed (avoids reviving junk).
    """
    repo = get_repo()
    rejected = 0
    restored = 0
    reason = (
        "Auto-rejected: label is a wrapped eligibility or mid-sentence fragment, "
        "not a benefit-chart service."
    )
    for item in repo.items_for(policy_id):
        if not item or item.get("item_type") != "coverage":
            continue
        if item.get("edited_by_human"):
            continue
        label = (item.get("service_label") or (item.get("data") or {}).get("service_label") or "").strip()
        state = item.get("review_state")
        # Restore false positives from an earlier fragment pass — only clean ACCURATE rows.
        if state == "rejected" and (item.get("judge_reason") or "").startswith("Auto-rejected: label is a wrapped"):
            cleaned = normalize_service_label(label)
            evidence = ((item.get("data") or {}).get("evidence_text") or "").strip()
            if evidence:
                parsed = service_label_from_line(evidence)
                if is_plausible_service_label(parsed) and not is_plausible_service_label(cleaned):
                    cleaned = parsed
            grounded = bool((item.get("grounding") or {}).get("passed"))
            accurate = (item.get("judge_verdict") or "") == "ACCURATE"
            if is_plausible_service_label(cleaned) and grounded and accurate:
                data = {**(item.get("data") or {}), "service_label": cleaned}
                new_state = initial_state(item.get("grounding") or {}, "ACCURATE", item.get("question_verdict"))
                repo.write_item(
                    item["id"],
                    {
                        "review_state": new_state,
                        "service_label": cleaned,
                        "data": data,
                        "judge_reason": "Restored: label is a plausible chart service.",
                    },
                    actor="engine",
                )
                restored += 1
            continue
        if state in {"accepted", "edited", "rejected"}:
            continue
        cleaned = normalize_service_label(label)
        if cleaned != label and is_plausible_service_label(cleaned):
            data = {**(item.get("data") or {}), "service_label": cleaned}
            # Clear PA borrowed onto junk titles that we just cleaned into a real name —
            # annotate will re-apply on finalize/reconcile when appropriate.
            repo.write_item(
                item["id"],
                {"service_label": cleaned, "data": data},
                actor="engine",
            )
            label = cleaned
        if is_plausible_service_label(label):
            continue
        # Also clear PA on rows we are about to reject so FHIR/catalog stay clean if revived.
        data = {**(item.get("data") or {})}
        data["pa_required"] = False
        data["pa_status"] = "not_required"
        data["marker_used"] = None
        repo.write_item(
            item["id"],
            {
                "review_state": "rejected",
                "judge_reason": reason,
                "data": data,
            },
            actor="engine",
        )
        rejected += 1
    if recorder is not None:
        recorder.record(
            "cleanup",
            f"Rejected {rejected} fragment coverage rows (restored {restored})",
            detail={"rejected": rejected, "restored": restored},
        )
    return rejected


def _finish_draft(policy, items, memory, recorder) -> None:
    repo = get_repo()
    rejected = _reject_fragment_coverage(policy["id"], recorder)
    fresh = repo.items_for(policy["id"])
    active = [item for item in fresh if item and item["review_state"] != "rejected"]
    resource = build_for_policy(policy, active, status="draft")
    errors = validate_resource(resource)
    report = _validation_report(active, errors)
    report = {
        **(report or {}),
        "fragment_rows_rejected": rejected,
        "rejected_item_count": sum(1 for item in fresh if item and item["review_state"] == "rejected"),
        "active_item_count": len(active),
    }
    changes = {
        "status": "draft",
        "working_memory": memory,
        "validation_report": report,
    }
    if resource.get("resourceType") == "Questionnaire":
        changes["fhir_questionnaire"] = resource if not errors else None
    else:
        changes["fhir_insurance_plan"] = resource if not errors else None
    repo.update_policy(policy["id"], changes, actor="engine")
    recorder.record("fhir", "Built draft FHIR" + ("" if not errors else " with validation errors"), detail={"errors": errors})


def _normalize(raw: dict, role: str) -> dict:
    if "service_label" in raw or role == "benefit_summary" and "pa_required" in raw:
        label = normalize_service_label(str(raw.get("service_label") or ""))
        if role == "benefit_summary" and not is_plausible_service_label(label):
            evidence = " ".join(str(raw.get("evidence_text") or "").split())
            parsed = service_label_from_line(evidence) if evidence else ""
            if parsed and is_plausible_service_label(parsed):
                label = parsed
            else:
                raise ValueError("coverage label is a non-service fragment")
        return {
            "item_type": "coverage",
            "item_key": re.sub(r"[^a-z0-9]+", "_", str(label).lower()).strip("_")[:48] or "coverage",
            "page": int(raw["page"]),
            "service_label": label,
            "service_codes": raw.get("service_codes") or [],
            "payload": {**raw, "service_label": label},
        }
    key = raw["criterion_key"]
    applies = raw.get("applies_to") or []
    return {
        "item_type": "rule",
        "item_key": key,
        "page": int(raw.get("policy_page") or raw.get("page")),
        "service_label": applies[0] if applies else raw.get("requirement_text"),
        "service_codes": [code for code in applies if re.fullmatch(r"\d{5}", str(code))],
        "payload": raw,
    }


def _recover_missing_coverage(policy, pages, page_by_num, run_id, recorder, digest) -> list[dict]:
    """Ask again for chart lines the first pass skipped, then keep any line still missing."""
    repo = get_repo()
    added = []
    for page in pages:
        text = page.get("text") or ""
        missing = uncovered_lines(text, repo.items_for(policy["id"]))
        if not missing:
            continue

        def load(page=page, missing=missing, text=text):
            digest_lines = ",".join(missing)
            hit_key = cache.stage_key(digest, "extract", f"recover:{page['page']}:{digest_lines}")
            hit = cache.get(hit_key)
            if hit is not None:
                return hit
            data = complete(
                "p0",
                {
                    "pages": [{"page": page["page"], "text": text, "role": "target"}],
                    "uncovered_lines": missing,
                    "items_found": [row.get("service_label") for row in repo.items_for(policy["id"])],
                    "memory": {},
                    "service_rows_on_these_pages": len(missing),
                },
                schema=None,
                stage="extract",
                run_id=run_id,
                group_no=page["page"],
            )["data"]
            payload = {"data": data}
            cache.put(hit_key, "stage", digest, payload, None)
            return payload

        result = recorder.step(
            "extract",
            f"Reading {len(missing)} missed lines on page {page['page']}",
            load,
            group_no=page["page"],
            done=lambda v, page=page: f"Recovered missed lines on page {page['page']}",
        )
        model_items = (result.get("data") or {}).get("coverage") or []
        existing = {row["item_key"] for row in repo.items_for(policy["id"])}
        fresh = merge_uncovered(missing, model_items if isinstance(model_items, list) else [], page["page"], existing)
        if fresh:
            added.extend(_persist_items(policy, fresh, page_by_num, run_id, recorder))
    return [row for row in added if row]


def _complete_batches(role: str, group: list[dict], data: dict, prompt: str, run_id: str, index: int, memory: dict, digest: str) -> list[tuple[dict, list[dict]]]:
    """A batch that returns only a few rows is split in half and read again."""
    items = (data.get("coverage") or data.get("criteria")) if isinstance(data, dict) else None
    if not _batch_needs_split(role, group, items if isinstance(items, list) else []):
        return [(data, group)]
    mid = max(1, len(group) // 2)
    halves = [group[:mid], group[mid:]]
    found = []
    for part_index, part in enumerate(halves, start=1):
        if not part:
            continue
        pages_key = ",".join(str(p["page"]) for p in part)
        hit_key = cache.stage_key(digest, "extract", f"{index}:{part_index}:{pages_key}")
        hit = cache.get(hit_key)
        if hit is not None:
            part_data = hit["data"]
        else:
            signals = _service_row_signals(part)
            table_rows = [row["line"] for page in part for row in (page.get("grid_rows") or [])]
            user = {
                "pages": [{"page": p["page"], "text": p["text"], "role": "target" if p.get("citable", True) else "context"} for p in part],
                "memory": render(memory),
                "items_found": memory.get("items_found") or [],
                "service_rows_on_these_pages": len(table_rows) or signals,
                "table_rows": table_rows,
            }
            part_data = complete(prompt, user, schema=None, stage="extract", run_id=run_id, group_no=index)["data"]
            cache.put(hit_key, "stage", pages_key, {"data": part_data}, None)
        found.extend(_complete_batches(role, part, part_data, prompt, run_id, index, memory, digest))
    return found


def _batch_needs_split(role: str, group: list[dict], items: list) -> bool:
    if _benefit_batch_is_thin(role, group, items):
        return True
    if role != "clinical_policy" or len(group) <= 1:
        return False
    signals = 0
    for page in group:
        signals += len(re.findall(r"\b(must|unless|except|at least|required)\b", page.get("text") or "", re.I))
    got = [item for item in items if isinstance(item, dict)]
    return signals >= 4 and len(got) * 3 < signals


def _benefit_batch_is_thin(role: str, group: list[dict], items: list) -> bool:
    if role != "benefit_summary" or len(group) <= 1:
        return False
    signals = _service_row_signals(group)
    got = [item for item in items if isinstance(item, dict)]
    return signals >= 6 and len(got) * 4 < signals


def _service_row_signals(group: list[dict]) -> int:
    count = 0
    for page in group:
        for line in (page.get("text") or "").splitlines():
            low = line.lower()
            if "in-network" in low and "out-of-network" in low:
                continue
            if any(word in low for word in ("copay", "coinsurance", "covered", "authorization", "you pay")):
                count += 1
    return count


def _groups(pages: list[dict], size: int | None = None) -> list[list[dict]]:
    """One model call covers up to CONTEXT_GROUP_PAGES target pages, split only when the token budget is full.

    Benefit charts use a smaller batch so each call lists every service row instead of one example.
    """
    size = max(1, size if size is not None else settings.context_group_pages)
    budget = max(1, settings.context_max_tokens)
    groups: list[list[dict]] = []
    current: list[dict] = []
    tokens = 0
    for page in pages:
        page_tokens = max(1, len((page.get("text") or "").split()))
        if current and (len(current) >= size or tokens + page_tokens > budget):
            groups.append(current)
            current = []
            tokens = 0
        current.append(page)
        tokens += page_tokens
    if current:
        groups.append(current)
    return groups or [[]]


def _key(item: dict) -> str:
    return item.get("criterion_key") or item.get("service_label") or "item"


def _validation_report(items: list[dict], fhir_errors: list[str]) -> dict:
    items = [item for item in items if item]
    grounding_failed = sum(1 for item in items if not (item.get("grounding") or {}).get("passed"))
    verdicts = {"ACCURATE": 0, "WRONG_VALUE": 0, "HALLUCINATED": 0, "VAGUE": 0, "UNAVAILABLE": 0}
    for item in items:
        verdict = item.get("judge_verdict")
        if verdict in verdicts:
            verdicts[verdict] += 1
    rules = [item for item in items if item["item_type"] == "rule"]
    covered = 1.0
    if rules:
        covered = sum(1 for item in rules if item.get("question_verdict") == "complete") / len(rules)
    return {
        "grounding": {"passed": len(items) - grounding_failed, "failed": grounding_failed},
        "judge": verdicts,
        "questions": {"condition_coverage": covered},
        "fhir": {"valid": not fhir_errors, "errors": fhir_errors},
    }


def upload_response(policy: dict, *, cached: bool) -> dict:
    repo = get_repo()
    items = repo.items_for(policy["id"])
    identity = policy.get("identity_evidence") or {
        "insurer": {"value": policy.get("insurer"), "evidence": None, "page": None},
        "plan_name": {"value": policy.get("plan_name"), "evidence": None, "page": None},
        "plan_year": {"value": policy.get("plan_year"), "evidence": None, "page": None},
    }
    return {
        "id": policy["id"],
        "cached": cached,
        "sha256": policy.get("sha256"),
        "status": policy["status"],
        "document_role": policy["document_role"],
        "role_hint": policy.get("role_hint"),
        "source_kind": policy["source_kind"],
        "identity": {
            "insurer": identity.get("insurer"),
            "plan_name": identity.get("plan_name"),
            "plan_year": identity.get("plan_year"),
        },
        "sections": policy.get("sections") or {},
        "item_counts": {
            "total": sum(1 for item in items if item["review_state"] != "rejected"),
            "auto_approved": sum(1 for item in items if item["review_state"] == "auto_approved"),
            "pending_review": sum(1 for item in items if item["review_state"] == "pending_review"),
            "rejected": sum(1 for item in items if item["review_state"] == "rejected"),
        },
        "validation_report": policy.get("validation_report") or {},
        "fhir_insurance_plan": policy.get("fhir_insurance_plan"),
        "fhir_questionnaire": policy.get("fhir_questionnaire"),
    }


def finalize_draft(policy_id: str) -> dict:
    """Build draft FHIR from items already extracted (after a mid-run crash past the last group)."""
    repo = get_repo()
    policy = repo.get_policy(policy_id)
    if policy is None:
        raise ApiError("POLICY_NOT_FOUND", "No policy with that id.", 404)
    if policy.get("document_role") == "benefit_summary":
        reconcile_pa_flags(policy_id)
        policy = repo.get_policy(policy_id)
    items = [row for row in repo.items_for(policy_id) if row]
    if not items:
        raise ApiError("INVALID_PDF", "No extracted items to finalize.", 409)
    run = repo.create_run(policy_id, "finalize-" + policy["sha256"][:12])
    recorder = EpisodeRecorder("policy", policy_id, run["id"])
    try:
        memory = policy.get("working_memory") or empty()
        _finish_draft(policy, items, memory, recorder)
        for row in repo._conn.execute(
            "select id from ingestion_runs where policy_id = ? and status = 'running'",
            (policy_id,),
        ):
            repo.finish_run(row[0], "complete")
        repo.finish_run(run["id"], "complete")
    except Exception as exc:
        repo.finish_run(run["id"], "failed", str(exc)[:500])
        raise
    return upload_response(repo.get_policy(policy_id), cached=False)


def reconcile_pa_flags(policy_id: str, *, listing_text: str | None = None) -> dict:
    """Set pa_required / pa_status from EOC page markers (and optional PA listing PDF text).

    When a listing is provided it is the PA authority: matched/created rows take listing
    status, and residual EOC-only rows lose weak dagger flags so totals reflect the listing.
    Matching is by service label against the uploaded EOC pages — never by payer name.
    """
    from app.ingest.pa_markers import annotate_item_from_pages, parse_pa_listing_text
    from app.fhir.builders import build_for_policy
    from app.fhir.validate import validate_resource

    repo = get_repo()
    policy = repo.get_policy(policy_id)
    if policy is None:
        raise ApiError("POLICY_NOT_FOUND", "No policy with that id.", 404)
    if policy["document_role"] != "benefit_summary":
        raise ApiError("INVALID_PDF", "PA flag reconcile applies to Evidence of Coverage / plan documents.", 400)

    digest = policy["sha256"]
    cached = cache.get(cache.stage_key(digest, "clean"))
    if isinstance(cached, dict) and cached.get("pages"):
        pages = cached["pages"]
    else:
        pages, _report = clean(pdf_reader.read_pdf(policy["storage_path"]))

    listing_rows: list[dict] = []
    if listing_text:
        listing_rows = parse_pa_listing_text(listing_text)

    updated = 0
    # Dagger annotate is useful alone; when a listing is present, listing wins afterward.
    if not listing_rows:
        for item in repo.items_for(policy_id):
            if item.get("edited_by_human"):
                continue
            if item.get("review_state") == "rejected":
                continue
            patch = annotate_item_from_pages(item, pages)
            if not patch:
                continue
            data = {**(item.get("data") or {}), **patch}
            if (
                data.get("pa_required") == (item.get("data") or {}).get("pa_required")
                and data.get("pa_status") == (item.get("data") or {}).get("pa_status")
                and data.get("marker_used") == (item.get("data") or {}).get("marker_used")
            ):
                continue
            repo.write_item(item["id"], {"data": data}, actor="engine")
            updated += 1

    listing_applied = 0
    residuals_cleared = 0
    if listing_rows:
        listing_applied = _apply_pa_listing(policy, pages, listing_rows)
        _dedupe_listing_index_rows(policy_id)
        residuals_cleared = _clear_non_listing_pa_flags(policy_id)

    items = [row for row in repo.items_for(policy_id) if row and row["review_state"] != "rejected"]
    resource = build_for_policy(policy, items, status=policy.get("status") if policy.get("status") == "live" else "draft")
    errors = validate_resource(resource)
    listing_indexed = [i for i in items if (i.get("data") or {}).get("listing_index") is not None]
    scorecard_items = listing_indexed or items
    changes = {
        "validation_report": {
            **(policy.get("validation_report") or {}),
            "fhir": {"valid": not errors, "errors": errors},
            "pa_reconcile": {
                "updated": updated,
                "listing_applied": listing_applied,
                "residuals_cleared": residuals_cleared,
                "total": len(items),
                "listing_indexed": len(listing_indexed),
            },
        }
    }
    if resource.get("resourceType") == "InsurancePlan":
        changes["fhir_insurance_plan"] = resource if not errors else policy.get("fhir_insurance_plan")
    repo.update_policy(policy_id, changes, actor="engine")

    totals = {
        "total": len(scorecard_items),
        "required": sum(1 for i in scorecard_items if (i.get("data") or {}).get("pa_status") == "required"),
        "conditional": sum(1 for i in scorecard_items if (i.get("data") or {}).get("pa_status") == "conditional"),
        "not_required": sum(
            1
            for i in scorecard_items
            if (i.get("data") or {}).get("pa_status") == "not_required"
            or (
                (i.get("data") or {}).get("pa_status") is None
                and not (i.get("data") or {}).get("pa_required")
            )
        ),
        "updated": updated,
        "listing_applied": listing_applied,
        "residuals_cleared": residuals_cleared,
        "active_coverage": len(items),
    }
    return {"id": policy_id, "totals": totals, "fhir_valid": not errors}


def _dedupe_listing_index_rows(policy_id: str) -> int:
    """Keep one active coverage row per listing_index; reject extras from prior runs."""
    repo = get_repo()
    by_idx: dict[int, list[dict]] = {}
    for item in repo.items_for(policy_id):
        if item.get("review_state") == "rejected" or item.get("item_type") != "coverage":
            continue
        idx = (item.get("data") or {}).get("listing_index")
        if idx is None:
            continue
        by_idx.setdefault(int(idx), []).append(item)
    rejected = 0
    for idx, group in by_idx.items():
        if len(group) < 2:
            continue
        # Prefer the longest label / pa_list key / newest seq.
        group.sort(
            key=lambda row: (
                len(row.get("service_label") or ""),
                1 if str(row.get("item_key") or "").startswith("pa_list_") else 0,
                row.get("seq") or 0,
            ),
            reverse=True,
        )
        for extra in group[1:]:
            if extra.get("edited_by_human"):
                continue
            repo.write_item(
                extra["id"],
                {
                    "review_state": "rejected",
                    "judge_reason": "Auto-rejected: duplicate listing_index from an earlier reconcile pass.",
                },
                actor="engine",
            )
            rejected += 1
    return rejected


def _clear_non_listing_pa_flags(policy_id: str) -> int:
    """When a listing is the PA authority, drop EOC-only coverage residuals from the review set.

    Listing categories are the benefit catalog for Gate 1 / PA. Chart fragments that never
    received a listing_index stay in the DB as rejected so the queue shows ~N listing rows,
    not every recovered EOC line.
    """
    repo = get_repo()
    cleared = 0
    for item in repo.items_for(policy_id):
        if item.get("item_type") != "coverage":
            continue
        if item.get("edited_by_human") or item.get("review_state") == "rejected":
            continue
        data = item.get("data") or {}
        if data.get("listing_index") is not None:
            continue
        patch = {
            **data,
            "pa_required": False,
            "pa_status": "not_required",
            "marker_used": None,
        }
        repo.write_item(
            item["id"],
            {
                "data": patch,
                "review_state": "rejected",
                "judge_reason": (
                    "Auto-rejected: superseded by the PA listing catalog. "
                    "This EOC line was not one of the listing benefit categories."
                ),
            },
            actor="engine",
        )
        cleared += 1
    return cleared


def _apply_pa_listing(policy: dict, pages: list[dict], listing: list[dict]) -> int:
    """Add or update coverage rows from a PA listing when the service name is on an EOC page."""
    from app.ingest.coverage_lines import is_plausible_service_label
    from app.ingest.pa_markers import best_coverage_for_listing, significant_words

    repo = get_repo()
    page_texts = [(int(p["page"]), p.get("text") or "", p.get("grid_rows") or []) for p in pages]
    items = list(repo.items_for(policy["id"]))
    applied = 0
    matched_ids: set[str] = set()
    claimed_indexes: set[int] = set()

    def ground_page(label: str) -> tuple[int | None, str | None, bool]:
        """Return (page, evidence, chart_shaped). Listing may insert when chart_shaped."""
        label_low = label.lower()
        words = significant_words(label)
        best: tuple[int, str, int, bool] | None = None
        for num, text, grid in page_texts:
            for row in grid:
                service = (row.get("service") or "").strip()
                if service and (
                    label_low in service.lower()
                    or service.lower() in label_low
                    or len(words & significant_words(service)) >= max(2, min(3, len(words) // 2 or 1))
                ):
                    return num, row.get("line") or service, True
            low = text.lower()
            if label_low[:28] in low or label_low in low:
                evidence = label
                chart = False
                for line in text.splitlines():
                    stripped = line.strip()
                    if label_low[:20] not in stripped.lower() and label_low not in stripped.lower():
                        continue
                    evidence = stripped or label
                    chart = is_plausible_service_label(stripped) or is_plausible_service_label(label)
                    if chart:
                        return num, evidence, True
                # Name appears on the page (TOC or chart). Allow listing insert using the listing label.
                return num, evidence, True
            if not words:
                continue
            page_words = set(re.findall(r"[a-z]{3,}", low))
            overlap = len(words & page_words)
            need = max(2, min(3, len(words) // 2 or 1))
            if overlap < need:
                continue
            evidence = label
            chart = False
            for line in text.splitlines():
                line_words = set(re.findall(r"[a-z]{3,}", line.lower()))
                if len(words & line_words) < min(2, len(words)):
                    continue
                evidence = line.strip() or label
                chart = is_plausible_service_label(evidence) or is_plausible_service_label(label)
                if chart:
                    break
            score = overlap + (5 if chart else 0)
            if best is None or score > best[2]:
                best = (num, evidence, score, chart or overlap >= 3)
        if best:
            return best[0], best[1], best[3]
        return None, None, False

    used_keys = {
        (i.get("item_type"), i.get("item_key"))
        for i in items
        if i.get("review_state") != "rejected" or i["id"] in matched_ids
    }

    for row in listing:
        label = " ".join(str(row.get("service_label") or "").split()).strip()
        if len(label) < 8:
            continue
        # Reject truncated junk fragments from a bad earlier parse (bare stems only).
        if label.startswith(("(", "$")):
            continue
        if re.fullmatch(r"(?:visit|tests|exam|screening|infection)s?", label, re.I):
            continue
        # Close obviously truncated open-paren names before matching/grounding.
        if label.count("(") > label.count(")") and not label.endswith(")"):
            # Keep matching on the stem before the open paren when wrap failed.
            stem = label.split("(", 1)[0].strip()
            if len(stem) >= 8:
                label = stem
        idx = row.get("listing_index")
        if idx is not None and idx in claimed_indexes:
            continue

        active = [i for i in items if i.get("review_state") != "rejected"]
        rejected = [i for i in items if i.get("review_state") == "rejected"]
        # Prefer an existing row already tagged with this listing index (idempotent re-runs).
        existing_item = None
        if idx is not None:
            for pool in (active, rejected):
                for item in pool:
                    if item["id"] in matched_ids:
                        continue
                    if (item.get("data") or {}).get("listing_index") == idx:
                        existing_item = item
                        break
                if existing_item:
                    break
        if existing_item is None:
            existing_item = best_coverage_for_listing(label, active, claimed=matched_ids)
        if existing_item is None:
            existing_item = best_coverage_for_listing(label, rejected, claimed=matched_ids)

        page_no, evidence, chart_shaped = ground_page(label)
        if page_no is None and existing_item is None:
            continue
        if page_no is None and existing_item is not None:
            page_no = existing_item.get("page") or (existing_item.get("data") or {}).get("page")
            evidence = (existing_item.get("data") or {}).get("evidence_text") or label
            chart_shaped = True

        marker = None
        if existing_item:
            marker = (existing_item.get("data") or {}).get("marker_used")
        patch = {
            "service_label": label,
            "pa_required": row["pa_required"],
            "pa_status": row["pa_status"],
            "page": page_no,
            "evidence_text": evidence or label,
            "marker_used": marker,
            "listing_index": idx,
        }
        if existing_item is not None:
            if existing_item.get("edited_by_human"):
                continue
            data = {**(existing_item.get("data") or {}), **patch}
            changes = {
                "data": data,
                "service_label": label,
                "page": page_no,
            }
            if existing_item.get("review_state") == "rejected":
                changes["review_state"] = "pending_review"
                changes["judge_reason"] = (
                    "Service name and PA status grounded on the EOC page (listing reconcile)."
                )
            repo.write_item(existing_item["id"], changes, actor="engine")
            matched_ids.add(existing_item["id"])
            if idx is not None:
                claimed_indexes.add(idx)
            applied += 1
            continue
        # New listing row only when the EOC mentions the service (chart_shaped ground).
        if not chart_shaped:
            continue
        # New grounded coverage row from the listing.
        base_key = re.sub(r"[^a-z0-9]+", "_", label.lower()).strip("_")[:40] or "coverage"
        item_key = f"pa_list_{idx}_{base_key}" if idx is not None else f"pa_list_{base_key}"
        item_key = item_key[:64]
        if ("coverage", item_key) in used_keys:
            item_key = f"{item_key}_{applied}"[:64]
        new = {
            "id": new_id(),
            "policy_id": policy["id"],
            "item_type": "coverage",
            "item_key": item_key,
            "seq": len(items) + applied + 1,
            "service_label": label,
            "service_codes": [],
            "data": patch,
            "original_data": patch,
            "page": page_no,
            "grounding": {"passed": True, "failures": []},
            "judge_verdict": "ACCURATE",
            "judge_reason": "Service name and PA status grounded on the EOC page (listing reconcile).",
            "review_state": "pending_review",
            "edited_by_human": False,
        }
        repo.insert_item(new)
        items.append(new)
        used_keys.add(("coverage", item_key))
        matched_ids.add(new["id"])
        if idx is not None:
            claimed_indexes.add(idx)
        applied += 1
    return applied


def fill_gaps(policy_id: str) -> dict:
    """Recover benefit lines the first extract skipped, and rewrite rule questions."""
    reset_steps()
    repo = get_repo()
    policy = repo.get_policy(policy_id)
    if policy is None:
        raise ApiError("POLICY_NOT_FOUND", "No policy with that id.", 404)
    digest = policy["sha256"]
    cached = cache.get(cache.stage_key(digest, "clean"))
    if isinstance(cached, dict) and cached.get("pages"):
        pages = cached["pages"]
    else:
        pages, _report = clean(pdf_reader.read_pdf(policy["storage_path"]))
    page_by_num = {p["page"]: p for p in pages}
    chosen = (policy.get("sections") or {}).get("pages") or []
    selected = [page_by_num[n] for n in chosen if n in page_by_num] or pages
    run = repo.create_run(policy_id, "fill-" + digest[:12])
    recorder = EpisodeRecorder("policy", policy_id, run["id"])
    try:
        before = None
        if policy["document_role"] == "benefit_summary":
            _apply_grid(policy, selected)
            before = benefit_score(_all_grid(selected), repo.items_for(policy_id))
            _fill_grid_gaps(policy, selected, run["id"], recorder)
            rechecked = _recheck_unsure(policy, selected, run["id"], recorder)
        else:
            rechecked = 0
        refreshed = _refresh_questions(policy_id, run["id"])
        after = benefit_score(_all_grid(selected), repo.items_for(policy_id)) if before is not None else None
        repo.finish_run(run["id"], "complete")
    except Exception:
        repo.finish_run(run["id"], "failed")
        raise
    return {"before": before, "after": after, "rechecked": rechecked, "questions_rewritten": refreshed}


def _refresh_questions(policy_id: str, run_id: str) -> int:
    repo = get_repo()
    rewritten = 0
    for item in repo.items_for(policy_id):
        if item.get("item_type") != "rule" or item.get("edited_by_human"):
            continue
        data = item.get("data") or {}
        if not data.get("criterion_key"):
            continue
        built = build_questions(data, run_id=run_id)
        repo.write_item(
            item["id"],
            {
                "data": {**data, "questions": built["questions"], "pass_condition": built["pass_condition"]},
                "question_verdict": built["question_verdict"],
            },
            actor="engine",
        )
        rewritten += 1
    return rewritten


def reprocess(policy_id: str, *, full: bool = False) -> dict:
    """Re-run the overview pipeline in the background.

    Failed runs resume from stage cache + working memory by default (no wipe).
    Pass full=True to clear unlocked items and re-extract every group.
    """
    repo = get_repo()
    policy = repo.get_policy(policy_id)
    if policy is None:
        raise ApiError("POLICY_NOT_FOUND", "No policy with that id.", 404)
    if not full and policy["status"] in {"failed", "ingesting"} and (
        policy["status"] == "failed" or not ingestion_running(policy_id)
    ):
        return resume_ingestion(policy_id)
    repo.delete_unlocked_items(policy_id, None)
    for block in repo.blocks_for(policy_id):
        repo.delete_unlocked_items(policy_id, block["id"])
    _invalidate_extract_stages(policy["sha256"])
    repo.update_policy(
        policy_id,
        {
            "status": "ingesting",
            "working_memory": empty(),
            "fhir_insurance_plan": None,
            "fhir_questionnaire": None,
            "validation_report": {},
            "ingestion_state": {},
        },
        actor="engine",
    )
    start_ingestion(policy_id)
    body = upload_response(repo.get_policy(policy_id), cached=False)
    body["accepted"] = True
    body["message"] = "Reprocess started. The engine is reading pages in document order with working memory."
    return body


def _invalidate_extract_stages(digest: str) -> None:
    """Drop stage artifacts for this PDF so a full reprocess re-reads groups (keeps model-call cache)."""
    repo = get_repo()
    try:
        with repo._lock:
            repo._conn.execute(
                "delete from artifact_cache where fingerprint = ? and layer = 'stage'",
                (digest,),
            )
            repo._conn.commit()
    except Exception:
        pass


def resume_ingestion(policy_id: str) -> dict:
    """Continue a failed/paused read using stage + working memory cache (no wipe)."""
    repo = get_repo()
    policy = repo.get_policy(policy_id)
    if policy is None:
        raise ApiError("POLICY_NOT_FOUND", "No policy with that id.", 404)
    repo.update_policy(policy_id, {"status": "ingesting"}, actor="engine")
    start_ingestion(policy_id)
    body = upload_response(repo.get_policy(policy_id), cached=False)
    body["accepted"] = True
    body["message"] = "Resumed. Completed page groups load from cache; remaining groups continue with working memory."
    return body
