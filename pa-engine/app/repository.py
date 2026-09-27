"""The only module that touches the database. Enforces the human edit lock (H1)."""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from typing import Any

from app.config import settings

SCHEMA = """
create table if not exists patients (
  id text primary key,
  full_name text not null,
  dob text,
  sex text,
  member_id text,
  synthetic integer not null default 1
);
create table if not exists providers (
  id text primary key,
  full_name text not null,
  specialty text
);
create table if not exists documents (
  id text primary key,
  patient_id text references patients(id),
  file_name text not null,
  storage_path text not null,
  doc_type text,
  in_chart integer not null default 0,
  synthetic integer not null default 1,
  created_at text default current_timestamp
);
create table if not exists policies (
  id text primary key,
  document_role text not null check (document_role in ('benefit_summary','clinical_policy','drug_criteria')),
  role_hint text,
  role_confirmed_by text,
  source_kind text not null default 'published' check (source_kind in ('published','fictional_fallback')),
  source_url text,
  downloaded_at text,
  file_name text not null,
  storage_path text not null,
  sha256 text not null unique,
  page_count integer,
  insurer text, plan_name text, plan_year text,
  identity_evidence text,
  identity_edited_by_human integer not null default 0,
  format_hints text,
  sections text,
  possibly_truncated integer not null default 0,
  context_report text,
  working_memory text,
  validation_report text,
  fhir_questionnaire text,
  fhir_insurance_plan text,
  ingestion_state text not null default '{}',
  status text not null default 'ingesting' check (status in ('ingesting','paused','draft','live','archived','failed')),
  went_live_by text, went_live_at text,
  status_history text not null default '[]',
  created_at text default current_timestamp
);
create table if not exists policy_blocks (
  id text primary key,
  policy_id text not null references policies(id) on delete cascade,
  block_key text not null,
  label text not null,
  start_page integer not null,
  end_page integer not null,
  tier_used text not null,
  status text not null default 'indexed' check (status in ('indexed','extracting','draft','live','archived')),
  fhir_questionnaire text,
  went_live_by text, went_live_at text,
  unique (policy_id, block_key)
);
create table if not exists policy_items (
  id text primary key,
  policy_id text not null references policies(id) on delete cascade,
  block_id text references policy_blocks(id),
  item_type text not null check (item_type in ('coverage','rule')),
  item_key text not null,
  seq integer not null,
  service_label text,
  service_codes text,
  data text not null,
  original_data text not null,
  page integer not null,
  grounding text,
  judge_verdict text check (judge_verdict in ('ACCURATE','WRONG_VALUE','HALLUCINATED','VAGUE','UNAVAILABLE')),
  judge_reason text,
  question_verdict text check (question_verdict in ('complete','missing_condition','added_condition','leading','unavailable')),
  review_state text not null check (review_state in ('auto_approved','pending_review','accepted','edited','rejected')),
  edited_by_human integer not null default 0,
  suggestion text,
  reviewed_by text, reviewed_at text, review_note text,
  unique (policy_id, item_type, item_key)
);
create table if not exists plan_memory (
  id text primary key,
  insurer text not null, plan_name text not null, plan_year text,
  memory text not null,
  updated_at text default current_timestamp,
  unique (insurer, plan_name, plan_year)
);
create table if not exists ingestion_runs (
  id text primary key,
  policy_id text references policies(id) on delete cascade,
  plan_key text not null,
  status text not null check (status in ('queued','running','complete','partial','failed')),
  stop_reason text,
  started_at text, finished_at text
);
create table if not exists episodes (
  seq integer primary key autoincrement,
  id text not null unique,
  subject_type text not null check (subject_type in ('policy','block','pa_request','synthetic_scenario','eval_run')),
  subject_id text not null,
  run_id text,
  stage text not null,
  step_no integer, group_no integer, item_key text,
  actor text not null,
  status text not null check (status in ('started','completed','failed','skipped','resumed','cache_hit')),
  input_fingerprint text,
  output_fingerprint text,
  artifact_ref text,
  summary text not null,
  detail text,
  pipeline_version text not null,
  created_at text default current_timestamp
);
create table if not exists llm_calls (
  id text primary key,
  run_id text not null,
  episode_id text,
  cache_key text,
  stage text not null, step_no integer not null, group_no integer, item_key text,
  provider text not null, model text not null, prompt_name text not null,
  status text not null check (status in ('ok','repaired','invalid','timeout','error')),
  latency_ms integer, tokens_in integer, tokens_out integer,
  created_at text default current_timestamp
);
create table if not exists artifact_cache (
  key text primary key,
  layer text not null check (layer in ('stage','model_call','fhir','qr','answer','synthetic')),
  fingerprint text not null,
  pipeline_version text not null,
  storage_path text,
  value text,
  finalized integer not null default 0,
  created_by_episode text,
  created_at text default current_timestamp
);
create table if not exists clinical_records (
  id text primary key,
  patient_id text not null references patients(id),
  resource_type text not null,
  record_kind text not null,
  code text, code_system text, display text,
  value text, unit text,
  start_date text, end_date text,
  body text,
  author_provider_id text references providers(id),
  source_document_id text references documents(id),
  fhir_resource text,
  synthetic integer not null default 1,
  created_at text default current_timestamp
);
create table if not exists pa_requests (
  id text primary key,
  patient_id text not null references patients(id),
  ordering_provider_id text not null references providers(id),
  insurer text not null, plan_name text not null, plan_year text,
  order_text text not null, service_code text, drug_name text,
  benefit_summary_id text references policies(id),
  coverage_item_id text references policy_items(id),
  criteria_policy_id text references policies(id),
  criteria_block_id text references policy_blocks(id),
  match_candidates text,
  status text not null check (status in (
    'draft','matching','checking','not_required','needs_info','ready_for_review',
    'submitted','in_review','info_requested','approved')),
  coverage_note text,
  readiness real not null default 0 check (readiness between 0 and 1),
  submission_packet text, packet_version integer not null default 0, submitted_at text,
  insurance_plan_id text,
  service_category text,
  source_document_reference text,
  ingest_payload text,
  source_upload_id text,
  questionnaire_id text,
  questionnaire_response_id text,
  created_at text default current_timestamp, updated_at text default current_timestamp
);
create table if not exists fhir_questionnaires (
  id text primary key,
  insurance_plan_id text,
  service_category text,
  resource text not null,
  created_at text default current_timestamp
);
create table if not exists fhir_questionnaire_responses (
  id text primary key,
  pa_request_id text not null references pa_requests(id) on delete cascade,
  questionnaire_id text,
  resource text not null,
  created_at text default current_timestamp
);
create table if not exists pa_criteria (
  id text primary key,
  pa_request_id text not null references pa_requests(id) on delete cascade,
  policy_item_id text references policy_items(id),
  seq integer not null,
  criterion_key text not null,
  requirement_text text not null,
  criterion_type text not null,
  policy_page integer not null,
  origin text not null default 'policy' check (origin in ('policy','insurer_request')),
  pass_condition text not null,
  status text not null check (status in ('met','missing','unclear')),
  status_reason text,
  evidence_text text,
  likely_owner_provider_id text references providers(id),
  likely_owner_reason text,
  verified_by text references providers(id), verified_at text,
  unique (pa_request_id, criterion_key)
);
create table if not exists pa_answers (
  id text primary key,
  pa_criterion_id text not null references pa_criteria(id) on delete cascade,
  link_id text not null, seq integer not null,
  question_text text not null,
  answer_type text not null,
  enabled integer not null default 1,
  value text, unit text,
  fill_method text,
  evidence_text text,
  evidence_record_id text references clinical_records(id),
  source_document_id text references documents(id),
  review_state text not null default 'unanswered',
  edited_by_human integer not null default 0,
  rejected_ai_value text, reject_reason text, attestation text,
  answered_by text references providers(id), answered_at text,
  unique (pa_criterion_id, link_id)
);
create table if not exists pa_events (
  id text primary key,
  pa_request_id text not null references pa_requests(id) on delete cascade,
  episode_id text,
  event_type text not null,
  message text,
  actor text not null check (actor in ('engine','clinician','insurer')),
  created_at text default current_timestamp
);
create table if not exists review_log (
  id text primary key,
  episode_id text,
  actor text not null,
  actor_role text not null check (actor_role in ('policy_reviewer','clinician','content_reviewer')),
  target_table text not null, target_id text not null,
  action text not null,
  before text, after text, note text,
  created_at text default current_timestamp
);
"""

JSON_COLS = {
    "policies": [
        "identity_evidence", "format_hints", "sections", "context_report",
        "working_memory", "validation_report", "fhir_questionnaire",
        "fhir_insurance_plan", "ingestion_state", "status_history",
    ],
    "policy_items": ["service_codes", "data", "original_data", "grounding", "suggestion"],
    "policy_blocks": ["fhir_questionnaire"],
    "plan_memory": ["memory"],
    "artifact_cache": ["value"],
    "clinical_records": ["fhir_resource"],
    "pa_requests": ["match_candidates", "submission_packet", "ingest_payload"],
    "pa_criteria": ["pass_condition"],
    "pa_answers": ["value", "rejected_ai_value"],
    "episodes": ["detail"],
    "review_log": ["before", "after"],
    "fhir_questionnaires": ["resource"],
    "fhir_questionnaire_responses": ["resource"],
    "uploaded_reports": ["extracted_json"],
}


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def payer_label(policy: dict) -> str | None:
    """Display label for insurance company from stored identity (no brand hardcoding)."""
    if policy.get("insurer"):
        return str(policy["insurer"]).strip() or None
    plan = (policy.get("plan_name") or "").strip()
    if plan:
        return plan
    name = (policy.get("file_name") or "").strip()
    if name:
        return name.rsplit(".", 1)[0]
    return None


def new_id() -> str:
    return str(uuid.uuid4())


def _loads(table: str, row: sqlite3.Row | None) -> dict | None:
    if row is None:
        return None
    data = dict(row)
    for col in JSON_COLS.get(table, []):
        if data.get(col) and isinstance(data[col], str):
            try:
                data[col] = json.loads(data[col])
            except json.JSONDecodeError:
                pass
    return data


class Repository:
    def __init__(self, path: str):
        self.path = path
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(path, check_same_thread=False, timeout=10.0)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("pragma foreign_keys = on")
        self._conn.execute("pragma busy_timeout = 10000")
        try:
            self._conn.execute("pragma journal_mode = WAL")
        except sqlite3.Error:
            pass
        self._conn.commit()

    def init(self) -> None:
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._migrate()
            self._conn.commit()

    def _migrate(self) -> None:
        """Additive columns + widen pa_requests status check to include draft."""
        cols = {row[1] for row in self._conn.execute("pragma table_info(pa_requests)").fetchall()}
        for name, decl in (
            ("insurance_plan_id", "text"),
            ("service_category", "text"),
            ("source_document_reference", "text"),
            ("ingest_payload", "text"),
            ("questionnaire_id", "text"),
            ("questionnaire_response_id", "text"),
        ):
            if name not in cols:
                self._conn.execute(f"alter table pa_requests add column {name} {decl}")

        # Detect whether CHECK allows 'draft' by reading sqlite_master SQL.
        row = self._conn.execute(
            "select sql from sqlite_master where type='table' and name='pa_requests'"
        ).fetchone()
        ddl = (row[0] if row else "") or ""
        if "'draft'" not in ddl and '"draft"' not in ddl:
            self._rebuild_pa_requests_for_draft()

        self._conn.execute(
            """create table if not exists fhir_questionnaires (
              id text primary key,
              insurance_plan_id text,
              service_category text,
              resource text not null,
              created_at text default current_timestamp
            )"""
        )
        self._conn.execute(
            """create table if not exists fhir_questionnaire_responses (
              id text primary key,
              pa_request_id text not null,
              questionnaire_id text,
              resource text not null,
              created_at text default current_timestamp
            )"""
        )

        answer_cols = {row[1] for row in self._conn.execute("pragma table_info(pa_answers)").fetchall()}
        if "source_document_id" not in answer_cols:
            self._conn.execute("alter table pa_answers add column source_document_id text")

        pa_cols = {row[1] for row in self._conn.execute("pragma table_info(pa_requests)").fetchall()}
        if "source_upload_id" not in pa_cols:
            self._conn.execute("alter table pa_requests add column source_upload_id text")

        self._conn.execute(
            """create table if not exists uploaded_reports (
              id text primary key,
              patient_id text references patients(id),
              uploaded_by text references providers(id),
              file_name text not null,
              file_reference text not null,
              storage_path text not null,
              extracted_json text,
              extraction_status text not null check (extraction_status in ('success','failed','partial')),
              uploaded_at text default current_timestamp
            )"""
        )

        policy_cols = {row[1] for row in self._conn.execute("pragma table_info(policies)").fetchall()}
        if "status_history" not in policy_cols:
            self._conn.execute("alter table policies add column status_history text not null default '[]'")

        policy_ddl = (
            self._conn.execute("select sql from sqlite_master where type='table' and name='policies'").fetchone() or [""]
        )[0] or ""
        if "archived" not in policy_ddl or "retired" in policy_ddl:
            self._rebuild_policies_status_archived()

        block_ddl = (
            self._conn.execute("select sql from sqlite_master where type='table' and name='policy_blocks'").fetchone()
            or [""]
        )[0] or ""
        if "archived" not in block_ddl or "retired" in block_ddl:
            self._rebuild_policy_blocks_status_archived()

    def _rebuild_pa_requests_for_draft(self) -> None:
        cols = {row[1] for row in self._conn.execute("pragma table_info(pa_requests)").fetchall()}
        optional = [
            "insurance_plan_id",
            "service_category",
            "source_document_reference",
            "ingest_payload",
            "questionnaire_id",
            "questionnaire_response_id",
        ]
        select_extra = ", ".join(c if c in cols else f"null as {c}" for c in optional)
        self._conn.executescript(
            f"""
            create table pa_requests_v2 (
              id text primary key,
              patient_id text not null,
              ordering_provider_id text not null,
              insurer text not null, plan_name text not null, plan_year text,
              order_text text not null, service_code text, drug_name text,
              benefit_summary_id text,
              coverage_item_id text,
              criteria_policy_id text,
              criteria_block_id text,
              match_candidates text,
              status text not null check (status in (
                'draft','matching','checking','not_required','needs_info','ready_for_review',
                'submitted','in_review','info_requested','approved')),
              coverage_note text,
              readiness real not null default 0,
              submission_packet text, packet_version integer not null default 0, submitted_at text,
              insurance_plan_id text,
              service_category text,
              source_document_reference text,
              ingest_payload text,
              questionnaire_id text,
              questionnaire_response_id text,
              created_at text, updated_at text
            );
            insert into pa_requests_v2 (
              id, patient_id, ordering_provider_id, insurer, plan_name, plan_year,
              order_text, service_code, drug_name, benefit_summary_id, coverage_item_id,
              criteria_policy_id, criteria_block_id, match_candidates, status, coverage_note,
              readiness, submission_packet, packet_version, submitted_at,
              insurance_plan_id, service_category, source_document_reference, ingest_payload,
              questionnaire_id, questionnaire_response_id, created_at, updated_at
            )
            select
              id, patient_id, ordering_provider_id, insurer, plan_name, plan_year,
              order_text, service_code, drug_name, benefit_summary_id, coverage_item_id,
              criteria_policy_id, criteria_block_id, match_candidates, status, coverage_note,
              readiness, submission_packet, packet_version, submitted_at,
              {select_extra},
              created_at, updated_at
            from pa_requests;
            drop table pa_requests;
            alter table pa_requests_v2 rename to pa_requests;
            """
        )

    def _rebuild_policies_status_archived(self) -> None:
        """Widen policies.status CHECK: retired -> archived; keep pipeline statuses."""
        self._conn.execute("update policies set status = 'archived' where status = 'retired'")
        row = self._conn.execute("select sql from sqlite_master where type='table' and name='policies'").fetchone()
        ddl = (row[0] if row else "") or ""
        if "archived" in ddl and "retired" not in ddl:
            return
        new_ddl = (
            ddl.replace(
                "('ingesting','paused','draft','live','retired','failed')",
                "('ingesting','paused','draft','live','archived','failed')",
            )
            .replace("create table policies", "create table policies_v2", 1)
            .replace("CREATE TABLE policies", "CREATE TABLE policies_v2", 1)
        )
        if new_ddl == ddl.replace("create table policies", "create table policies_v2", 1).replace(
            "CREATE TABLE policies", "CREATE TABLE policies_v2", 1
        ):
            # Unknown CHECK shape — still force archived into a known DDL.
            new_ddl = None
        if new_ddl and "policies_v2" in new_ddl:
            cols = [r[1] for r in self._conn.execute("pragma table_info(policies)").fetchall()]
            col_list = ", ".join(cols)
            self._conn.execute(new_ddl)
            self._conn.execute(f"insert into policies_v2 ({col_list}) select {col_list} from policies")
            self._conn.execute("drop table policies")
            self._conn.execute("alter table policies_v2 rename to policies")
            return
        # Fallback full recreate matching current SCHEMA.
        cols = [r[1] for r in self._conn.execute("pragma table_info(policies)").fetchall()]
        if "status_history" not in cols:
            self._conn.execute("alter table policies add column status_history text not null default '[]'")
            cols.append("status_history")
        col_list = ", ".join(cols)
        self._conn.executescript(
            f"""
            create table policies_v2 (
              id text primary key,
              document_role text not null check (document_role in ('clinical_policy','benefit_summary','drug_criteria')),
              role_hint text, role_confirmed_by text,
              source_kind text not null check (source_kind in ('published','fictional_fallback')),
              source_url text, downloaded_at text,
              file_name text not null, storage_path text not null, sha256 text not null unique,
              insurer text, plan_name text, plan_year text,
              identity_evidence text,
              identity_edited_by_human integer not null default 0,
              format_hints text,
              sections text,
              possibly_truncated integer not null default 0,
              context_report text,
              working_memory text,
              validation_report text,
              fhir_questionnaire text,
              fhir_insurance_plan text,
              ingestion_state text not null default '{{}}',
              status text not null default 'ingesting' check (status in ('ingesting','paused','draft','live','archived','failed')),
              went_live_by text, went_live_at text,
              status_history text not null default '[]',
              created_at text default current_timestamp
            );
            insert into policies_v2 ({col_list}) select {col_list} from policies;
            drop table policies;
            alter table policies_v2 rename to policies;
            """
        )

    def _rebuild_policy_blocks_status_archived(self) -> None:
        self._conn.execute("update policy_blocks set status = 'archived' where status = 'retired'")
        row = self._conn.execute("select sql from sqlite_master where type='table' and name='policy_blocks'").fetchone()
        ddl = (row[0] if row else "") or ""
        if "archived" in ddl and "retired" not in ddl:
            return
        new_ddl = (
            ddl.replace(
                "('indexed','extracting','draft','live','retired')",
                "('indexed','extracting','draft','live','archived')",
            )
            .replace("create table policy_blocks", "create table policy_blocks_v2", 1)
            .replace("CREATE TABLE policy_blocks", "CREATE TABLE policy_blocks_v2", 1)
        )
        if "policy_blocks_v2" not in new_ddl:
            return
        self._conn.execute(new_ddl)
        self._conn.execute(
            """insert into policy_blocks_v2
               (id, policy_id, block_key, label, start_page, end_page, tier_used, status, fhir_questionnaire, went_live_by, went_live_at)
               select id, policy_id, block_key, label, start_page, end_page, tier_used, status, fhir_questionnaire, went_live_by, went_live_at
               from policy_blocks"""
        )
        self._conn.execute("drop table policy_blocks")
        self._conn.execute("alter table policy_blocks_v2 rename to policy_blocks")

    def save_fhir_questionnaire(self, row: dict) -> dict:
        row = {**row, "id": row.get("id") or new_id(), "created_at": now()}
        self._insert(
            "insert into fhir_questionnaires (id, insurance_plan_id, service_category, resource, created_at) values (?, ?, ?, ?, ?)",
            (row["id"], row.get("insurance_plan_id"), row.get("service_category"), _dump(row["resource"]), row["created_at"]),
        )
        return self._one("fhir_questionnaires", "select * from fhir_questionnaires where id = ?", (row["id"],))  # type: ignore[return-value]

    def get_fhir_questionnaire(self, qid: str) -> dict | None:
        return self._one("fhir_questionnaires", "select * from fhir_questionnaires where id = ?", (qid,))

    def save_fhir_qr(self, row: dict) -> dict:
        row = {**row, "id": row.get("id") or new_id(), "created_at": now()}
        self._insert(
            "insert into fhir_questionnaire_responses (id, pa_request_id, questionnaire_id, resource, created_at) values (?, ?, ?, ?, ?)",
            (row["id"], row["pa_request_id"], row.get("questionnaire_id"), _dump(row["resource"]), row["created_at"]),
        )
        return self._one("fhir_questionnaire_responses", "select * from fhir_questionnaire_responses where id = ?", (row["id"],))  # type: ignore[return-value]

    def get_fhir_qr(self, qrid: str) -> dict | None:
        return self._one("fhir_questionnaire_responses", "select * from fhir_questionnaire_responses where id = ?", (qrid,))

    def list_fhir_questionnaires_for_plan(self, insurance_plan_id: str) -> list[dict]:
        return self._all(
            "fhir_questionnaires",
            "select * from fhir_questionnaires where insurance_plan_id = ? order by created_at desc",
            (insurance_plan_id,),
        )

    def count_questionnaire_responses(self, questionnaire_id: str) -> int:
        with self._lock:
            row = self._conn.execute(
                "select count(*) as n from fhir_questionnaire_responses where questionnaire_id = ?",
                (questionnaire_id,),
            ).fetchone()
        return int(row[0] if row else 0)

    def delete_fhir_questionnaire(self, questionnaire_id: str) -> None:
        # Clear PA pointers that still reference this questionnaire (no responses requested by caller).
        self._write(
            "update pa_requests set questionnaire_id = null where questionnaire_id = ?",
            (questionnaire_id,),
        )
        self._write("delete from fhir_questionnaires where id = ?", (questionnaire_id,))

    def list_pas_for_patient(self, patient_id: str) -> list[dict]:
        return self._all(
            "pa_requests",
            "select * from pa_requests where patient_id = ? order by updated_at desc, created_at desc",
            (patient_id,),
        )

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def _write(self, sql: str, params: tuple = ()) -> None:
        with self._lock:
            self._conn.execute(sql, params)
            self._conn.commit()

    def _insert(self, sql: str, params: tuple = ()) -> None:
        self._write(sql, params)

    def _one(self, table: str, sql: str, params: tuple = ()) -> dict | None:
        with self._lock:
            cur = self._conn.execute(sql, params)
            row = cur.fetchone()
        return _loads(table, row)

    def _all(self, table: str, sql: str, params: tuple = ()) -> list[dict]:
        with self._lock:
            cur = self._conn.execute(sql, params)
            rows = cur.fetchall()
        return [d for row in rows if (d := _loads(table, row))]

    def ping(self) -> bool:
        with self._lock:
            self._conn.execute("select 1")
        return True

    # --- policies ---
    def policy_by_sha(self, sha: str) -> dict | None:
        return self._one("policies", "select * from policies where sha256 = ?", (sha,))

    def get_policy(self, policy_id: str) -> dict | None:
        return self._one("policies", "select * from policies where id = ?", (policy_id,))

    def list_policies(self, include_drafts: bool) -> list[dict]:
        if include_drafts:
            return self._all("policies", "select * from policies order by created_at desc")
        return self._all(
            "policies",
            "select * from policies where status = 'live' order by created_at desc",
        )

    def create_policy(self, row: dict) -> dict:
        row = {**row, "id": row.get("id") or new_id(), "created_at": now()}
        cols = list(row)
        self._insert(
            f"insert into policies ({','.join(cols)}) values ({','.join('?' for _ in cols)})",
            tuple(_dump(row[c]) for c in cols),
        )
        return self.get_policy(row["id"])  # type: ignore[return-value]

    def update_policy(self, policy_id: str, changes: dict, *, actor: str) -> dict:
        current = self.get_policy(policy_id)
        if current is None:
            raise KeyError(policy_id)
        if current["identity_edited_by_human"] and actor == "engine":
            kept = dict(changes)
            for key in ("insurer", "plan_name", "plan_year", "identity_evidence"):
                kept.pop(key, None)
            changes = kept
        if not changes:
            return current
        sets = ", ".join(f"{k} = ?" for k in changes)
        self._write(
            f"update policies set {sets} where id = ?",
            tuple(_dump(v) for v in changes.values()) + (policy_id,),
        )
        return self.get_policy(policy_id)  # type: ignore[return-value]

    def delete_policy(self, policy_id: str) -> dict:
        """Remove a policy from the DB, its episodes, fingerprint artifacts, and stored PDF."""
        import time

        policy = self.get_policy(policy_id)
        if policy is None:
            raise KeyError(policy_id)
        sha = policy.get("sha256")
        storage_path = policy.get("storage_path")
        item_ids = [row["id"] for row in self.items_for(policy_id)]
        block_ids = [row["id"] for row in self.blocks_for(policy_id)]
        last_error: Exception | None = None
        for attempt in range(4):
            try:
                with self._lock:
                    if item_ids:
                        placeholders = ",".join("?" for _ in item_ids)
                        self._conn.execute(
                            f"update pa_requests set coverage_item_id = null where coverage_item_id in ({placeholders})",
                            item_ids,
                        )
                        self._conn.execute(
                            f"update pa_criteria set policy_item_id = null where policy_item_id in ({placeholders})",
                            item_ids,
                        )
                    self._conn.execute(
                        "update pa_requests set benefit_summary_id = null where benefit_summary_id = ?",
                        (policy_id,),
                    )
                    self._conn.execute(
                        "update pa_requests set criteria_policy_id = null, criteria_block_id = null where criteria_policy_id = ?",
                        (policy_id,),
                    )
                    self._conn.execute(
                        "delete from episodes where subject_type = 'policy' and subject_id = ?",
                        (policy_id,),
                    )
                    for block_id in block_ids:
                        self._conn.execute(
                            "delete from episodes where subject_type = 'block' and subject_id = ?",
                            (block_id,),
                        )
                    if sha:
                        self._conn.execute("delete from artifact_cache where fingerprint = ?", (sha,))
                    self._conn.execute(
                        "delete from review_log where target_table = 'policies' and target_id = ?",
                        (policy_id,),
                    )
                    if item_ids:
                        placeholders = ",".join("?" for _ in item_ids)
                        self._conn.execute(
                            f"delete from review_log where target_table = 'policy_items' and target_id in ({placeholders})",
                            item_ids,
                        )
                    # Children cascade, but clear explicitly so a partial FK graph cannot block delete.
                    self._conn.execute("delete from policy_items where policy_id = ?", (policy_id,))
                    self._conn.execute("delete from policy_blocks where policy_id = ?", (policy_id,))
                    self._conn.execute("delete from ingestion_runs where policy_id = ?", (policy_id,))
                    self._conn.execute("delete from policies where id = ?", (policy_id,))
                    self._conn.commit()
                last_error = None
                break
            except sqlite3.OperationalError as exc:
                last_error = exc
                if "locked" not in str(exc).lower() or attempt == 3:
                    raise
                time.sleep(0.2 * (attempt + 1))
        if last_error is not None:
            raise last_error
        removed_file = False
        if storage_path:
            from pathlib import Path

            path = Path(storage_path)
            if path.is_file():
                path.unlink()
                removed_file = True
        return {
            "id": policy_id,
            "deleted": True,
            "sha256": sha,
            "storage_removed": removed_file,
            "file_name": policy.get("file_name"),
        }

    def create_block(self, row: dict) -> dict:
        row = {**row, "id": row.get("id") or new_id()}
        cols = list(row)
        self._insert(
            f"insert into policy_blocks ({','.join(cols)}) values ({','.join('?' for _ in cols)})",
            tuple(_dump(row[c]) for c in cols),
        )
        return self.get_block(row["id"])  # type: ignore[return-value]

    def get_block(self, block_id: str) -> dict | None:
        return self._one("policy_blocks", "select * from policy_blocks where id = ?", (block_id,))

    def blocks_for(self, policy_id: str) -> list[dict]:
        return self._all(
            "policy_blocks",
            "select * from policy_blocks where policy_id = ? order by start_page",
            (policy_id,),
        )

    def update_block(self, block_id: str, changes: dict) -> dict:
        sets = ", ".join(f"{k} = ?" for k in changes)
        self._write(
            f"update policy_blocks set {sets} where id = ?",
            tuple(_dump(v) for v in changes.values()) + (block_id,),
        )
        return self.get_block(block_id)  # type: ignore[return-value]

    def live_blocks(self) -> list[dict]:
        return self._all(
            "policy_blocks",
            "select * from policy_blocks where status = 'live' order by label",
        )

    def insert_item(self, row: dict) -> dict:
        row = {**row, "id": row.get("id") or new_id()}
        cols = list(row)
        self._insert(
            f"insert into policy_items ({','.join(cols)}) values ({','.join('?' for _ in cols)})",
            tuple(_dump(row[c]) for c in cols),
        )
        return self.get_item(row["id"])  # type: ignore[return-value]

    def get_item(self, item_id: str) -> dict | None:
        return self._one("policy_items", "select * from policy_items where id = ?", (item_id,))

    def items_for(self, policy_id: str, block_id: str | None = None) -> list[dict]:
        if block_id:
            return self._all(
                "policy_items",
                "select * from policy_items where policy_id = ? and block_id = ? order by seq",
                (policy_id, block_id),
            )
        return self._all(
            "policy_items",
            "select * from policy_items where policy_id = ? order by seq",
            (policy_id,),
        )

    def live_items(self, item_type: str | None = None) -> list[dict]:
        # While a policy is live, keep non-rejected rows in the catalog even if HITL
        # demo reset moved them back to pending_review / auto_approved.
        # HALLUCINATED rows may stay open in the Review queue for human look, but
        # they must not drive clinician questionnaires or order matching.
        sql = """
        select i.* from policy_items i
        join policies p on p.id = i.policy_id
        left join policy_blocks b on b.id = i.block_id
        where p.status = 'live'
          and i.review_state != 'rejected'
          and (i.judge_verdict is null or i.judge_verdict != 'HALLUCINATED')
          and (i.block_id is null or b.status = 'live')
        """
        if item_type:
            sql += " and i.item_type = ?"
            return self._all("policy_items", sql + " order by i.seq", (item_type,))
        return self._all("policy_items", sql + " order by i.seq")

    def live_items_for_plan(
        self,
        *,
        insurer: str | None = None,
        plan_name: str | None = None,
        plan_year: str | None = None,
        item_type: str | None = None,
    ) -> list[dict]:
        """Live accepted items limited to one insurance plan (questionnaires for that payer only)."""
        rows = self.live_items(item_type)
        if not insurer and not plan_name:
            return rows
        out = []
        for item in rows:
            policy = self.get_policy(item["policy_id"])
            if policy is None:
                continue
            if not self._policy_matches_plan(policy, insurer=insurer, plan_name=plan_name, plan_year=plan_year):
                continue
            out.append(item)
        return out

    def _policy_matches_plan(
        self,
        policy: dict,
        *,
        insurer: str | None,
        plan_name: str | None,
        plan_year: str | None,
    ) -> bool:
        label = payer_label(policy) or ""
        stored_insurer = (policy.get("insurer") or "").strip()
        stored_plan = (policy.get("plan_name") or policy.get("file_name") or "").strip()
        if insurer:
            if insurer not in {stored_insurer, label, stored_plan}:
                return False
        if plan_name:
            if plan_name not in {stored_plan, label, (policy.get("file_name") or "")}:
                if plan_name != stored_plan and plan_name != label:
                    return False
        if plan_year is not None and plan_year != "":
            if (policy.get("plan_year") or "") != plan_year:
                return False
        return True

    def write_item(self, item_id: str, changes: dict, *, actor: str) -> dict:
        """H1: an engine write never overwrites a human edit. The change is stored as a suggestion."""
        current = self.get_item(item_id)
        if current is None:
            raise KeyError(item_id)
        if current["edited_by_human"] and actor == "engine":
            suggestion = {
                "changes": changes,
                "at": now(),
                "note": "Newer extractor output kept as a suggestion because a human edit is locked.",
            }
            self._write(
                "update policy_items set suggestion = ? where id = ?",
                (json.dumps(suggestion), item_id),
            )
            return {**self.get_item(item_id), "lock_preserved": True}  # type: ignore[dict-item]
        if "original_data" in changes and actor == "engine":
            changes = {k: v for k, v in changes.items() if k != "original_data"}
        if not changes:
            return current
        sets = ", ".join(f"{k} = ?" for k in changes)
        self._write(
            f"update policy_items set {sets} where id = ?",
            tuple(_dump(v) for v in changes.values()) + (item_id,),
        )
        updated = self.get_item(item_id)
        assert updated is not None
        updated["lock_preserved"] = False
        return updated

    def delete_unlocked_items(self, policy_id: str, block_id: str | None = None) -> None:
        if block_id:
            self._write(
                "delete from policy_items where policy_id = ? and block_id = ? and edited_by_human = 0",
                (policy_id, block_id),
            )
        else:
            self._write(
                "delete from policy_items where policy_id = ? and edited_by_human = 0 and block_id is null",
                (policy_id,),
            )

    # --- runs, cache, calls ---
    def create_run(self, policy_id: str | None, plan_key: str) -> dict:
        running = self._one(
            "ingestion_runs",
            "select * from ingestion_runs where plan_key = ? and status = 'running'",
            (plan_key,),
        )
        if running:
            # Stale lock after process restart: no in-process thread can own this row yet
            # when create_run is invoked from a fresh start_ingestion. Reclaim and continue.
            thread_alive = False
            if policy_id:
                try:
                    from app.pipeline import pipeline_runner

                    thread_alive = pipeline_runner.ingestion_running(policy_id)
                except Exception:
                    thread_alive = False
            if thread_alive:
                from app.errors import ApiError

                raise ApiError("RUN_LIMIT", "A run is already in progress for this plan.", 429)
            self.finish_run(running["id"], "failed", "stale_run_reclaimed")
            if policy_id:
                pol = self.get_policy(policy_id)
                if pol and pol.get("status") == "ingesting" and not thread_alive:
                    # Leave ingesting so the new run can proceed; caller owns the work.
                    pass
        row = {
            "id": new_id(),
            "policy_id": policy_id,
            "plan_key": plan_key,
            "status": "running",
            "started_at": now(),
        }
        cols = list(row)
        self._insert(
            f"insert into ingestion_runs ({','.join(cols)}) values ({','.join('?' for _ in cols)})",
            tuple(row[c] for c in cols),
        )
        return self._one("ingestion_runs", "select * from ingestion_runs where id = ?", (row["id"],))  # type: ignore[return-value]

    def reclaim_stale_ingestion(self) -> int:
        """On API startup: no ingest threads exist yet, so any 'running' row is orphaned."""
        rows = self._all(
            "ingestion_runs",
            "select * from ingestion_runs where status = 'running'",
        )
        for row in rows:
            self.finish_run(row["id"], "failed", "process_restart")
            pid = row.get("policy_id")
            if pid:
                pol = self.get_policy(pid)
                if pol and pol.get("status") == "ingesting":
                    self.update_policy(pid, {"status": "failed"}, actor="engine")
        return len(rows)

    def finish_run(self, run_id: str, status: str, stop_reason: str | None = None) -> None:
        self._write(
            "update ingestion_runs set status = ?, stop_reason = ?, finished_at = ? where id = ?",
            (status, stop_reason, now(), run_id),
        )

    def insert_episode(self, row: dict) -> dict:
        row = {
            "id": row.get("id") or new_id(),
            "pipeline_version": row.get("pipeline_version"),
            "created_at": now(),
            **row,
        }
        cols = [c for c in row if c != "seq"]
        self._insert(
            f"insert into episodes ({','.join(cols)}) values ({','.join('?' for _ in cols)})",
            tuple(_dump(row[c]) for c in cols),
        )
        return self._one("episodes", "select * from episodes where id = ?", (row["id"],))  # type: ignore[return-value]

    def episodes_for(self, subject_type: str, subject_id: str) -> list[dict]:
        return self._all(
            "episodes",
            "select * from episodes where subject_type = ? and subject_id = ? order by seq",
            (subject_type, subject_id),
        )

    def completed_groups(self, run_id: str, stage: str) -> set[int]:
        with self._lock:
            rows = self._conn.execute(
                """
                select group_no from episodes
                where run_id = ? and stage = ? and status = 'completed' and group_no is not null
                """,
                (run_id, stage),
            ).fetchall()
        return {int(r[0]) for r in rows}

    def insert_llm_call(self, row: dict) -> None:
        row = {**row, "id": row.get("id") or new_id(), "created_at": now()}
        cols = list(row)
        self._insert(
            f"insert into llm_calls ({','.join(cols)}) values ({','.join('?' for _ in cols)})",
            tuple(row[c] for c in cols),
        )

    def count_llm_calls(self, run_id: str) -> int:
        with self._lock:
            count = self._conn.execute(
                "select count(*) from llm_calls where run_id = ?", (run_id,)
            ).fetchone()[0]
        return int(count)

    def cache_get(self, key: str) -> dict | None:
        return self._one(
            "artifact_cache",
            "select * from artifact_cache where key = ? and finalized = 1",
            (key,),
        )

    def cache_put(self, row: dict) -> None:
        with self._lock:
            existing = self._conn.execute(
                "select key from artifact_cache where key = ?", (row["key"],)
            ).fetchone()
        if existing:
            self._write(
                """update artifact_cache set layer=?, fingerprint=?, pipeline_version=?,
                   storage_path=?, value=?, finalized=?, created_by_episode=? where key=?""",
                (
                    row["layer"],
                    row["fingerprint"],
                    row["pipeline_version"],
                    row.get("storage_path"),
                    _dump(row.get("value")),
                    1 if row.get("finalized") else 0,
                    row.get("created_by_episode"),
                    row["key"],
                ),
            )
            return
        cols = [
            "key", "layer", "fingerprint", "pipeline_version", "storage_path",
            "value", "finalized", "created_by_episode", "created_at",
        ]
        values = (
            row["key"], row["layer"], row["fingerprint"], row["pipeline_version"],
            row.get("storage_path"), _dump(row.get("value")),
            1 if row.get("finalized") else 0, row.get("created_by_episode"), now(),
        )
        self._insert(
            f"insert into artifact_cache ({','.join(cols)}) values ({','.join('?' for _ in cols)})",
            values,
        )

    def cache_delete(self, key: str) -> None:
        self._write("delete from artifact_cache where key = ?", (key,))

    # --- people and chart ---
    def upsert_patient(self, row: dict) -> dict:
        self._write(
            """insert into patients (id, full_name, dob, sex, member_id, synthetic)
               values (?, ?, ?, ?, ?, ?)
               on conflict(id) do update set full_name=excluded.full_name, dob=excluded.dob,
                 sex=excluded.sex, member_id=excluded.member_id, synthetic=excluded.synthetic""",
            (row["id"], row["full_name"], row.get("dob"), row.get("sex"), row.get("member_id"), 1 if row.get("synthetic", True) else 0),
        )
        return self._one("patients", "select * from patients where id = ?", (row["id"],))  # type: ignore[return-value]

    def list_patients(self) -> list[dict]:
        return self._all("patients", "select * from patients order by full_name")

    def get_patient(self, patient_id: str) -> dict | None:
        return self._one("patients", "select * from patients where id = ?", (patient_id,))

    def upsert_provider(self, row: dict) -> dict:
        self._write(
            """insert into providers (id, full_name, specialty) values (?, ?, ?)
               on conflict(id) do update set full_name=excluded.full_name, specialty=excluded.specialty""",
            (row["id"], row["full_name"], row.get("specialty")),
        )
        return self._one("providers", "select * from providers where id = ?", (row["id"],))  # type: ignore[return-value]

    def list_providers(self) -> list[dict]:
        return self._all("providers", "select * from providers order by full_name")

    def get_provider(self, provider_id: str) -> dict | None:
        return self._one("providers", "select * from providers where id = ?", (provider_id,))

    def create_document(self, row: dict) -> dict:
        row = {**row, "id": row.get("id") or new_id(), "created_at": now()}
        cols = list(row)
        self._insert(
            f"insert into documents ({','.join(cols)}) values ({','.join('?' for _ in cols)})",
            tuple(_dump(row[c]) for c in cols),
        )
        return self.get_document(row["id"])  # type: ignore[return-value]

    def get_document(self, document_id: str) -> dict | None:
        return self._one("documents", "select * from documents where id = ?", (document_id,))

    def documents_for(self, patient_id: str, *, pending_only: bool = False) -> list[dict]:
        if pending_only:
            return self._all(
                "documents",
                "select * from documents where patient_id = ? and in_chart = 0 order by created_at",
                (patient_id,),
            )
        return self._all(
            "documents",
            "select * from documents where patient_id = ? order by created_at",
            (patient_id,),
        )

    def update_document(self, document_id: str, changes: dict) -> None:
        sets = ", ".join(f"{k} = ?" for k in changes)
        self._write(
            f"update documents set {sets} where id = ?",
            tuple(_dump(v) for v in changes.values()) + (document_id,),
        )

    def create_uploaded_report(self, row: dict) -> dict:
        row = {**row, "id": row.get("id") or new_id(), "uploaded_at": row.get("uploaded_at") or now()}
        cols = list(row)
        self._insert(
            f"insert into uploaded_reports ({','.join(cols)}) values ({','.join('?' for _ in cols)})",
            tuple(_dump(row[c]) for c in cols),
        )
        return self.get_uploaded_report(row["id"])  # type: ignore[return-value]

    def get_uploaded_report(self, upload_id: str) -> dict | None:
        return self._one("uploaded_reports", "select * from uploaded_reports where id = ?", (upload_id,))

    def update_uploaded_report(self, upload_id: str, changes: dict) -> dict:
        sets = ", ".join(f"{k} = ?" for k in changes)
        self._write(
            f"update uploaded_reports set {sets} where id = ?",
            tuple(_dump(v) for v in changes.values()) + (upload_id,),
        )
        return self.get_uploaded_report(upload_id)  # type: ignore[return-value]

    def find_patient_by_name_dob(self, full_name: str, dob: str | None = None) -> dict | None:
        name = (full_name or "").strip().lower()
        if not name:
            return None
        rows = self.list_patients()
        for row in rows:
            if (row.get("full_name") or "").strip().lower() != name:
                continue
            if dob and row.get("dob") and row.get("dob") != dob:
                continue
            return row
        return None

    def insert_record(self, row: dict) -> dict:
        row = {**row, "id": row.get("id") or new_id(), "created_at": now()}
        cols = list(row)
        self._insert(
            f"insert into clinical_records ({','.join(cols)}) values ({','.join('?' for _ in cols)})",
            tuple(_dump(row[c]) for c in cols),
        )
        return self._one("clinical_records", "select * from clinical_records where id = ?", (row["id"],))  # type: ignore[return-value]

    def records_for(self, patient_id: str) -> list[dict]:
        return self._all(
            "clinical_records",
            "select * from clinical_records where patient_id = ? order by start_date",
            (patient_id,),
        )

    def record_set_hash(self, patient_id: str) -> str:
        import hashlib

        with self._lock:
            rows = self._conn.execute(
                "select id, code, value, body, start_date from clinical_records where patient_id = ? order by id",
                (patient_id,),
            ).fetchall()
        raw = json.dumps([tuple(r) for r in rows], default=str)
        return hashlib.sha256(raw.encode()).hexdigest()

    # --- PA ---
    def create_pa(self, row: dict) -> dict:
        row = {**row, "id": row.get("id") or new_id(), "created_at": now(), "updated_at": now()}
        cols = list(row)
        self._insert(
            f"insert into pa_requests ({','.join(cols)}) values ({','.join('?' for _ in cols)})",
            tuple(_dump(row[c]) for c in cols),
        )
        return self.get_pa(row["id"])  # type: ignore[return-value]

    def get_pa(self, pa_id: str) -> dict | None:
        return self._one("pa_requests", "select * from pa_requests where id = ?", (pa_id,))

    def list_pas(self, *, limit: int = 20) -> list[dict]:
        return self._all(
            "pa_requests",
            "select * from pa_requests order by updated_at desc, created_at desc limit ?",
            (limit,),
        )

    def update_pa(self, pa_id: str, changes: dict) -> dict:
        changes = {**changes, "updated_at": now()}
        sets = ", ".join(f"{k} = ?" for k in changes)
        self._write(
            f"update pa_requests set {sets} where id = ?",
            tuple(_dump(v) for v in changes.values()) + (pa_id,),
        )
        return self.get_pa(pa_id)  # type: ignore[return-value]

    def replace_check_rows(self, pa_id: str, criteria: list[dict], answers: list[dict]) -> None:
        """Replace unlocked, unverified criteria. Locked answers and verified rules stay."""
        existing = self.criteria_for(pa_id)
        verified = {c["criterion_key"] for c in existing if c.get("verified_by")}
        locked_answers = {}
        for c in existing:
            for a in self.answers_for(c["id"]):
                if a["edited_by_human"]:
                    locked_answers[(c["criterion_key"], a["link_id"])] = a
        with self._lock:
            self._conn.execute("delete from pa_answers where pa_criterion_id in (select id from pa_criteria where pa_request_id = ?)", (pa_id,))
            self._conn.execute("delete from pa_criteria where pa_request_id = ?", (pa_id,))
            self._conn.commit()
        for c in criteria:
            if c["criterion_key"] in verified:
                old = next(x for x in existing if x["criterion_key"] == c["criterion_key"])
                self._insert_raw("pa_criteria", {k: old[k] for k in (
                    "id", "pa_request_id", "policy_item_id", "seq", "criterion_key",
                    "requirement_text", "criterion_type", "policy_page", "origin",
                    "pass_condition", "status", "status_reason", "evidence_text",
                    "likely_owner_provider_id", "likely_owner_reason", "verified_by", "verified_at",
                ) if k in old or True})
                for a in self._answers_snapshot(locked_answers, c["criterion_key"], old["id"]):
                    pass
                # reinsert previous answers for verified criterion
                # handled below via snapshot stored before delete — we already deleted.
                # Verified path is rebuilt from `existing` captured above.
            else:
                self._insert_raw("pa_criteria", c)
        # Reinsert answers. For verified criteria, restore prior answers from the snapshot
        # taken before delete. The caller passes new answers for unverified criteria.
        prior_answers = getattr(self, "_answer_snapshot", {})
        for a in answers:
            crit = self._one(
                "pa_criteria",
                "select * from pa_criteria where pa_request_id = ? and criterion_key = ?",
                (pa_id, a.pop("_criterion_key")),
            )
            if crit is None:
                continue
            if crit["criterion_key"] in verified:
                continue
            key = (crit["criterion_key"], a["link_id"])
            if key in locked_answers:
                old = locked_answers[key]
                old = dict(old)
                old["pa_criterion_id"] = crit["id"]
                old.pop("id", None)
                self._insert_raw("pa_answers", {**old, "id": new_id()})
            else:
                a["pa_criterion_id"] = crit["id"]
                self._insert_raw("pa_answers", a)
        # restore verified criterion answers
        for c in existing:
            if c["criterion_key"] not in verified:
                continue
            new_c = self._one(
                "pa_criteria",
                "select * from pa_criteria where pa_request_id = ? and criterion_key = ?",
                (pa_id, c["criterion_key"]),
            )
            if not new_c:
                continue
            for a in prior_answers.get(c["id"], []):
                a = dict(a)
                a["id"] = new_id()
                a["pa_criterion_id"] = new_c["id"]
                self._insert_raw("pa_answers", a)

    def _answers_snapshot(self, locked, key, old_id):
        return []

    def snapshot_answers(self, pa_id: str) -> dict[str, list[dict]]:
        snap: dict[str, list[dict]] = {}
        for c in self.criteria_for(pa_id):
            snap[c["id"]] = self.answers_for(c["id"])
        return snap

    def _insert_raw(self, table: str, row: dict) -> None:
        row = {k: v for k, v in row.items() if v is not None or k in {"status_reason", "value"}}
        # keep explicit nulls for nullable text by inserting all keys that exist as columns
        cols = list(row)
        self._insert(
            f"insert into {table} ({','.join(cols)}) values ({','.join('?' for _ in cols)})",
            tuple(_dump(row[c]) for c in cols),
        )

    def save_criteria_tree(self, pa_id: str, criteria: list[dict]) -> None:
        """Atomic replace of unverified criteria. Verified rules and locked answers survive."""
        existing = self.criteria_for(pa_id)
        verified_keys = {c["criterion_key"] for c in existing if c.get("verified_by")}
        locked: dict[tuple[str, str], dict] = {}
        prior_verified_answers: dict[str, list[dict]] = {}
        for c in existing:
            answers = self.answers_for(c["id"])
            if c["criterion_key"] in verified_keys:
                prior_verified_answers[c["criterion_key"]] = answers
            for a in answers:
                if a["edited_by_human"]:
                    locked[(c["criterion_key"], a["link_id"])] = a
        with self._lock:
            self._conn.execute(
                "delete from pa_answers where pa_criterion_id in (select id from pa_criteria where pa_request_id = ?)",
                (pa_id,),
            )
            self._conn.execute("delete from pa_criteria where pa_request_id = ?", (pa_id,))
            self._conn.commit()
        for c in criteria:
            answers = c.pop("answers")
            key = c["criterion_key"]
            if key in verified_keys:
                old = next(x for x in existing if x["criterion_key"] == key)
                keep = {
                    "id": new_id(),
                    "pa_request_id": pa_id,
                    "policy_item_id": old.get("policy_item_id"),
                    "seq": old["seq"],
                    "criterion_key": old["criterion_key"],
                    "requirement_text": old["requirement_text"],
                    "criterion_type": old["criterion_type"],
                    "policy_page": old["policy_page"],
                    "origin": old["origin"],
                    "pass_condition": old["pass_condition"],
                    "status": old["status"],
                    "status_reason": old.get("status_reason"),
                    "evidence_text": old.get("evidence_text"),
                    "likely_owner_provider_id": old.get("likely_owner_provider_id"),
                    "likely_owner_reason": old.get("likely_owner_reason"),
                    "verified_by": old.get("verified_by"),
                    "verified_at": old.get("verified_at"),
                }
                self._insert_raw("pa_criteria", keep)
                saved = self._one(
                    "pa_criteria",
                    "select * from pa_criteria where pa_request_id = ? and criterion_key = ?",
                    (pa_id, key),
                )
                for a in prior_verified_answers.get(key, []):
                    payload = {k: a[k] for k in a if k not in {"id"}}
                    payload["id"] = new_id()
                    payload["pa_criterion_id"] = saved["id"]
                    self._insert_raw("pa_answers", payload)
                continue
            c["id"] = c.get("id") or new_id()
            c["pa_request_id"] = pa_id
            self._insert_raw("pa_criteria", c)
            saved = self.get_criterion(c["id"])
            for a in answers:
                prev = locked.get((key, a["link_id"]))
                if prev:
                    payload = {k: prev[k] for k in prev if k != "id"}
                    payload["id"] = new_id()
                    payload["pa_criterion_id"] = saved["id"]
                    self._insert_raw("pa_answers", payload)
                else:
                    a["id"] = a.get("id") or new_id()
                    a["pa_criterion_id"] = saved["id"]
                    self._insert_raw("pa_answers", a)

    def criteria_for(self, pa_id: str) -> list[dict]:
        return self._all(
            "pa_criteria",
            "select * from pa_criteria where pa_request_id = ? order by seq",
            (pa_id,),
        )

    def get_criterion(self, cid: str) -> dict | None:
        return self._one("pa_criteria", "select * from pa_criteria where id = ?", (cid,))

    def update_criterion(self, cid: str, changes: dict) -> dict:
        sets = ", ".join(f"{k} = ?" for k in changes)
        self._write(
            f"update pa_criteria set {sets} where id = ?",
            tuple(_dump(v) for v in changes.values()) + (cid,),
        )
        return self.get_criterion(cid)  # type: ignore[return-value]

    def answers_for(self, criterion_id: str) -> list[dict]:
        return self._all(
            "pa_answers",
            "select * from pa_answers where pa_criterion_id = ? order by seq",
            (criterion_id,),
        )

    def get_answer(self, answer_id: str) -> dict | None:
        return self._one("pa_answers", "select * from pa_answers where id = ?", (answer_id,))

    def write_answer(self, answer_id: str, changes: dict, *, actor: str) -> dict:
        current = self.get_answer(answer_id)
        if current is None:
            raise KeyError(answer_id)
        if current["edited_by_human"] and actor == "engine":
            return {**current, "lock_preserved": True}
        sets = ", ".join(f"{k} = ?" for k in changes)
        self._write(
            f"update pa_answers set {sets} where id = ?",
            tuple(_dump(v) for v in changes.values()) + (answer_id,),
        )
        updated = self.get_answer(answer_id)
        assert updated is not None
        updated["lock_preserved"] = False
        return updated

    def add_event(self, pa_id: str, event_type: str, message: str, actor: str, episode_id: str | None = None) -> dict:
        row = {
            "id": new_id(),
            "pa_request_id": pa_id,
            "episode_id": episode_id,
            "event_type": event_type,
            "message": message,
            "actor": actor,
            "created_at": now(),
        }
        cols = list(row)
        self._insert(
            f"insert into pa_events ({','.join(cols)}) values ({','.join('?' for _ in cols)})",
            tuple(row[c] for c in cols),
        )
        return row

    def events_for(self, pa_id: str) -> list[dict]:
        return self._all(
            "pa_events",
            "select * from pa_events where pa_request_id = ? order by created_at",
            (pa_id,),
        )

    def add_review_log(self, row: dict) -> dict:
        row = {**row, "id": row.get("id") or new_id(), "created_at": now()}
        cols = list(row)
        self._insert(
            f"insert into review_log ({','.join(cols)}) values ({','.join('?' for _ in cols)})",
            tuple(_dump(row[c]) for c in cols),
        )
        return self._one("review_log", "select * from review_log where id = ?", (row["id"],))  # type: ignore[return-value]

    def review_log_for(self, target_table: str, target_id: str) -> list[dict]:
        return self._all(
            "review_log",
            "select * from review_log where target_table = ? and target_id = ? order by created_at",
            (target_table, target_id),
        )

    def review_log_for_policy(self, policy_id: str) -> list[dict]:
        item_ids = [i["id"] for i in self.items_for(policy_id)]
        logs = self.review_log_for("policies", policy_id)
        for iid in item_ids:
            logs.extend(self.review_log_for("policy_items", iid))
        logs.sort(key=lambda r: r["created_at"])
        return logs


def _dump(value: Any) -> Any:
    if isinstance(value, (dict, list)):
        return json.dumps(value)
    if isinstance(value, bool):
        return 1 if value else 0
    return value


_repo: Repository | None = None
_repo_path: str | None = None


def get_repo() -> Repository:
    global _repo, _repo_path
    path = settings.database_path
    if _repo is None or _repo_path != path:
        if _repo is not None:
            _repo.close()
        parent = __import__("pathlib").Path(path).parent
        parent.mkdir(parents=True, exist_ok=True)
        _repo = Repository(path)
        _repo.init()
        _repo_path = path
    return _repo


def reset_repo(path: str | None = None) -> Repository:
    global _repo, _repo_path
    if _repo is not None:
        _repo.close()
        _repo = None
    if path:
        import os

        os.environ["DATABASE_PATH"] = path
    return get_repo()
