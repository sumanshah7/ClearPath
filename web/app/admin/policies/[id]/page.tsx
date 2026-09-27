"use client";

import { FormEvent, useEffect, useRef, useState } from "react";
import Link from "next/link";
import { useParams, useRouter } from "next/navigation";
import { api, fileUrl } from "@/lib/api";
import { documentRoleLabel, label } from "@/lib/format";
import { IngestionStep, IngestionSteps, PENDING_STEPS } from "@/components/pa/IngestionSteps";
import { ConfidenceBadge, JudgeBadge, LockBadge, SourceBadge, StatusBadge, ruleConfidence } from "@/components/pa/StatusBadge";

type Item = {
  id: string;
  summary: string;
  title?: string;
  page: number;
  page_text: string;
  review_state: string;
  judge_verdict?: string;
  judge_reason?: string;
  question_verdict?: string | null;
  grounding?: { passed?: boolean } | null;
  edited_by_human: boolean;
  suggestion?: { note?: string } | null;
  review_note?: string | null;
  cost_share?: string;
  pa_required?: boolean | null;
  pa_status?: "required" | "conditional" | "not_required" | string | null;
  marker_used?: string | null;
  service_codes?: string[];
  listing_index?: number | null;
  has_exception?: boolean;
  exception?: string | null;
  limits?: string | null;
  evidence?: string;
  why?: string;
  criteria?: string;
  item_type?: string;
  data: {
    requirement_text?: string;
    service_label?: string;
    evidence_text?: string;
    pa_required?: boolean;
    pa_status?: string;
    marker_used?: string | null;
    service_codes?: string[];
    questions?: { link_id: string; text: string; answer_type: string }[];
    pass_condition?: object;
  };
};

type FilterTab = "needs" | "all" | "pa" | "exceptions" | "edited" | "low";
type PaSubFilter = "all" | "required" | "conditional" | "not_required";

function itemPaStatus(item: Item): "required" | "conditional" | "not_required" | null {
  const raw = item.pa_status || item.data?.pa_status || null;
  if (raw === "required" || raw === "conditional" || raw === "not_required") return raw;
  if (item.pa_required === true) return "required";
  if (item.pa_required === false) return "not_required";
  return null;
}

function needsDecision(item: Item): boolean {
  return !["accepted", "edited", "rejected"].includes(item.review_state);
}

function decisionLabel(state: string): string {
  if (state === "accepted") return "Accepted";
  if (state === "edited") return "Edited";
  if (state === "rejected") return "Rejected";
  if (state === "auto_approved") return "Needs review (auto-flagged sure)";
  if (state === "pending_review") return "Needs review";
  return label(state);
}

export default function PolicyPage() {
  const params = useParams<{ id: string }>();
  const router = useRouter();
  const [policy, setPolicy] = useState<Record<string, unknown> | null>(null);
  const [items, setItems] = useState<Item[]>([]);
  const [filter, setFilter] = useState<FilterTab>("needs");
  const [paSubFilter, setPaSubFilter] = useState<PaSubFilter>("required");
  const [error, setError] = useState("");
  const [cacheNote, setCacheNote] = useState("");
  const [bootLive, setBootLive] = useState(false);
  const [justWentLive, setJustWentLive] = useState(false);
  const [busyId, setBusyId] = useState<string | null>(null);
  const [submitBusy, setSubmitBusy] = useState(false);
  const [reconcileBusy, setReconcileBusy] = useState(false);
  const [reconcileNote, setReconcileNote] = useState("");
  const [listingFile, setListingFile] = useState<File | null>(null);
  const [reviewer, setReviewer] = useState("Suman");
  const [editing, setEditing] = useState<Item | null>(null);
  const [draft, setDraft] = useState("");
  const [costDraft, setCostDraft] = useState("");
  const [paStatusDraft, setPaStatusDraft] = useState<"required" | "conditional" | "not_required">("not_required");
  const [evidenceDraft, setEvidenceDraft] = useState("");
  const [note, setNote] = useState("");
  const [fhir, setFhir] = useState("");
  const [extractJson, setExtractJson] = useState("");
  const [fhirBusy, setFhirBusy] = useState(false);
  const [questionnaires, setQuestionnaires] = useState<
    {
      id: string;
      title?: string | null;
      service_category?: string | null;
      created_at?: string;
      item_count: number;
      response_count: number;
      can_delete: boolean;
      delete_blocked_reason?: string | null;
    }[]
  >([]);
  const [qBusyId, setQBusyId] = useState<string | null>(null);
  const editRef = useRef<HTMLFormElement | null>(null);
  const fhirRef = useRef<HTMLPreElement | null>(null);
  const queueRef = useRef<HTMLElement | null>(null);

  async function load() {
    setError("");
    try {
      const detail = await api<Record<string, unknown>>(`/policies/${params.id}`);
      setPolicy(detail);
      const status = String(detail.status || "");
      if (status !== "ingesting") {
        const queue = await api<{ items: Item[] }>(`/policies/${params.id}/review-queue`);
        setItems(queue.items);
      } else {
        setItems([]);
      }
      try {
        const q = await api<{ questionnaires: typeof questionnaires }>(`/policies/${params.id}/questionnaires`);
        setQuestionnaires(q.questionnaires || []);
      } catch {
        setQuestionnaires([]);
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not load this policy");
    }
  }

  useEffect(() => {
    const query = new URLSearchParams(window.location.search);
    setBootLive(query.get("live") === "1");
    const tab = query.get("tab");
    if (tab === "pa" || tab === "prior-auth") setFilter("pa");
    if (query.get("cached") === "1") {
      const sha = query.get("sha") || "";
      setCacheNote(
        sha
          ? `Loaded from cache. Same document fingerprint ${sha} — no re-extract.`
          : "Loaded from cache. This PDF was already read — no re-extract."
      );
    }
  }, []);

  useEffect(() => {
    let stop = false;
    let timer: ReturnType<typeof setInterval> | null = null;

    async function pull(): Promise<boolean> {
      try {
        const detail = await api<Record<string, unknown>>(`/policies/${params.id}`);
        if (stop) return false;
        setPolicy(detail);
        setError("");
        const status = String(detail.status || "");
        const running = Boolean(detail.ingestion_running) || status === "ingesting";
        if (!running) {
          const queue = await api<{ items: Item[] }>(`/policies/${params.id}/review-queue`);
          if (!stop) setItems(queue.items);
          return false;
        }
        setItems([]);
        return true;
      } catch (err) {
        if (!stop) setError(err instanceof Error ? err.message : "Could not load this policy");
        return false;
      }
    }

    void (async () => {
      const stillRunning = await pull();
      if (stop || !stillRunning) return;
      timer = setInterval(() => {
        void (async () => {
          const running = await pull();
          if (!running && timer) {
            clearInterval(timer);
            timer = null;
          }
        })();
      }, 1200);
    })();

    return () => {
      stop = true;
      if (timer) clearInterval(timer);
    };
  }, [params.id]);

  async function act(path: string, body: object, itemId?: string) {
    setError("");
    if (itemId) setBusyId(itemId);
    try {
      await api(path, { method: "POST", body: JSON.stringify(body) });
      setEditing(null);
      if (path.endsWith("/go-live")) setJustWentLive(true);
      if (path.endsWith("/take-offline")) setJustWentLive(false);
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : "That action failed");
    } finally {
      setBusyId(null);
    }
  }

  async function goLiveWithConfirm() {
    const ok = window.confirm(
      "Activate this policy? It will be used for all new PA rule lookups.",
    );
    if (!ok) return;
    await act(`/policies/${params.id}/go-live`, { reviewer });
  }

  async function takeOfflineWithConfirm() {
    const ok = window.confirm(
      "Take this policy offline? It will leave the live catalog for new lookups. Extracted rules and past PA decisions stay on file.",
    );
    if (!ok) return;
    await act(`/policies/${params.id}/take-offline`, { reviewer });
  }

  async function deleteQuestionnaire(qid: string) {
    const ok = window.confirm("Delete this questionnaire? You can regenerate a fresh one afterward.");
    if (!ok) return;
    setError("");
    setQBusyId(qid);
    try {
      await api(`/policies/${params.id}/questionnaires/${qid}`, { method: "DELETE" });
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not delete questionnaire");
    } finally {
      setQBusyId(null);
    }
  }

  async function regenerateQuestionnaire(qid: string) {
    const ok = window.confirm(
      "Regenerate this questionnaire from the currently live policy rules? The old questionnaire is deleted only if no responses were submitted.",
    );
    if (!ok) return;
    setError("");
    setQBusyId(qid);
    try {
      await api(`/policies/${params.id}/questionnaires/${qid}/regenerate`, {
        method: "POST",
        body: JSON.stringify({ reviewer }),
      });
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not regenerate questionnaire");
    } finally {
      setQBusyId(null);
    }
  }

  async function confirmQuestionnairesAndOpenDoctor() {
    setError("");
    setSubmitBusy(true);
    try {
      const statusNow = String(policy?.status || "");
      const stillUndecided = items.some(needsDecision);
      if (statusNow !== "live") {
        if (stillUndecided) {
          setError("Accept, edit, or reject every row in All before confirming questionnaires.");
          setFilter("all");
          return;
        }
        const ok = window.confirm(
          "Activate this policy? It will be used for all new PA rule lookups.",
        );
        if (!ok) return;
        await api(`/policies/${params.id}/go-live`, {
          method: "POST",
          body: JSON.stringify({ reviewer }),
        });
        setJustWentLive(true);
        await load();
      }
      const idBlock = (policy?.identity as Record<string, { value?: string }> | undefined) || {};
      const insurer = String(idBlock.insurer?.value || policy?.insurer || "");
      const plan_name = String(idBlock.plan_name?.value || policy?.plan_name || "");
      const plan_year = String(idBlock.plan_year?.value || policy?.plan_year || "");
      const q = new URLSearchParams({
        from_policy: params.id,
        insurer,
        plan_name,
        plan_year,
      });
      router.push(`/doctor?${q.toString()}`);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not confirm questionnaires");
    } finally {
      setSubmitBusy(false);
    }
  }

  function openEdit(item: Item) {
    setEditing(item);
    setDraft(item.title || item.summary || "");
    setCostDraft(item.cost_share || "");
    setPaStatusDraft(itemPaStatus(item) || (item.pa_required ? "required" : "not_required"));
    setEvidenceDraft(item.evidence || item.data.evidence_text || "");
    setNote(item.review_note || "Reviewed and corrected against the cited page.");
    window.setTimeout(() => editRef.current?.scrollIntoView({ behavior: "smooth", block: "nearest" }), 50);
  }

  async function saveEdit(event: FormEvent) {
    event.preventDefault();
    if (!editing) return;
    if (!note.trim()) {
      setError("Add a short note before saving an edit.");
      return;
    }
    const data = {
      ...editing.data,
      service_label: draft,
      requirement_text: editing.item_type === "rule" ? draft : editing.data.requirement_text,
      evidence_text: evidenceDraft || editing.data.evidence_text,
      pa_status: paStatusDraft,
      pa_required: paStatusDraft !== "not_required",
      cost_share: costDraft,
      page: editing.page,
    };
    await act(`/policies/${params.id}/items/${editing.id}/edit`, { reviewer, note, data }, editing.id);
  }

  async function reconcilePa(withListing: boolean) {
    setError("");
    setReconcileBusy(true);
    setReconcileNote("");
    try {
      const body = new FormData();
      if (withListing && listingFile) body.set("listing", listingFile);
      const result = await api<{
        totals?: {
          required?: number;
          conditional?: number;
          not_required?: number;
          updated?: number;
          listing_applied?: number;
          total?: number;
        };
      }>(`/policies/${params.id}/reconcile-pa`, { method: "POST", body: withListing && listingFile ? body : undefined });
      const totals = result.totals || {};
      setReconcileNote(
        `PA flags refreshed. Required ${totals.required ?? 0} · Conditional ${totals.conditional ?? 0} · Not required ${totals.not_required ?? 0}` +
          (totals.updated != null ? ` · Updated ${totals.updated}` : "") +
          (totals.listing_applied ? ` · Listing rows applied ${totals.listing_applied}` : ""),
      );
      setListingFile(null);
      setFilter("pa");
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not refresh prior-auth flags");
    } finally {
      setReconcileBusy(false);
    }
  }

  async function showFhir() {
    setError("");
    setFhirBusy(true);
    try {
      const resource = await api(`/policies/${params.id}/fhir`);
      setFhir(JSON.stringify(resource, null, 2));
      setExtractJson("");
      window.setTimeout(() => fhirRef.current?.scrollIntoView({ behavior: "smooth", block: "start" }), 50);
    } catch (err) {
      setError(err instanceof Error ? err.message : "FHIR could not be built for this policy");
      setFhir("");
    } finally {
      setFhirBusy(false);
    }
  }

  function showExtractJson() {
    if (!policy) return;
    const role = String(policy.document_role || "");
    const identity = (policy.identity || {}) as Record<string, { value?: string }>;
    const payload = {
      document_type: role === "benefit_summary" ? "EOC_PLAN" : role === "drug_criteria" ? "DRUG_CRITERIA" : "CLINICAL_POLICY",
      policy_id: policy.id,
      file_name: policy.file_name,
      sha256: policy.sha256,
      status: policy.status,
      extracted_at: new Date().toISOString(),
      identity: {
        insurer: identity.insurer?.value || null,
        plan_name: identity.plan_name?.value || null,
        plan_year: identity.plan_year?.value || null,
      },
      tables: {
        document_type: role,
        rows: items.map((item) => ({
          id: item.id,
          title: item.title || item.summary,
          page: item.page,
          cost_share: item.cost_share || null,
          pa_required: item.pa_required ?? null,
          exception: item.exception || null,
          evidence: item.evidence || item.data.evidence_text || null,
          review_state: item.review_state,
          judge_verdict: item.judge_verdict || null,
          questions: (item.data.questions || []).map((q) => ({
            link_id: q.link_id,
            text: q.text,
            answer_type: q.answer_type,
          })),
        })),
      },
    };
    setExtractJson(JSON.stringify(payload, null, 2));
    setFhir("");
    window.setTimeout(() => fhirRef.current?.scrollIntoView({ behavior: "smooth", block: "start" }), 50);
  }

  if (!policy && !error) {
    return (
      <main>
        {bootLive ? <IngestionSteps steps={PENDING_STEPS} live /> : <p className="muted">Loading policy...</p>}
      </main>
    );
  }
  if (!policy) return <p className="badge amber">{error} <button className="btn secondary" onClick={load}>Retry</button></p>;

  const identity = (policy.identity || {}) as Record<string, { value?: string; evidence?: string; page?: number }>;
  const sections = (policy.sections || {}) as { tier_used?: string; failed_tiers?: { tier: string; reason: string }[]; pages?: number[]; possibly_truncated?: boolean };
  const undecided = items.some(needsDecision);
  const role = String(policy.document_role || "");
  const status = String(policy.status || "");
  const extracting = status === "ingesting" || Boolean(policy.ingestion_running);
  const isLive = status === "live";
  const isArchived = status === "archived";
  const canGoLive = !extracting && !undecided && (status === "draft" || status === "archived");
  const history = (Array.isArray(policy.status_history) ? policy.status_history : []) as {
    status?: string;
    changed_by?: string;
    timestamp?: string;
  }[];

  const steps = ((policy.ingestion_steps as IngestionStep[] | undefined) || []).map((step) => ({
    ...step,
    status: step.status as IngestionStep["status"],
  }));
  const sureCount = items.filter((item) => ruleConfidence(item).sure).length;
  const isEoc = role === "benefit_summary";
  const paCounts = {
    required: items.filter((item) => itemPaStatus(item) === "required").length,
    conditional: items.filter((item) => itemPaStatus(item) === "conditional").length,
    not_required: items.filter((item) => itemPaStatus(item) === "not_required").length,
  };
  const counts = {
    needs: items.filter(needsDecision).length,
    all: items.length,
    pa: isEoc ? paCounts.required + paCounts.conditional : items.filter((item) => item.pa_required === true).length,
    exceptions: items.filter((item) => item.has_exception).length,
    edited: items.filter((item) => item.edited_by_human || item.review_state === "edited").length,
    low: items.filter((item) => !ruleConfidence(item).sure).length,
  };
  const visible = items.filter((item) => {
    if (filter === "needs") return needsDecision(item);
    if (filter === "pa") {
      if (!isEoc) return item.pa_required === true;
      const status = itemPaStatus(item);
      if (paSubFilter === "all") return status != null;
      return status === paSubFilter;
    }
    if (filter === "exceptions") return Boolean(item.has_exception);
    if (filter === "edited") return item.edited_by_human || item.review_state === "edited";
    if (filter === "low") return !ruleConfidence(item).sure;
    return true;
  });
  const tabs: { id: FilterTab; label: string; count: number }[] = [
    { id: "needs", label: "Needs review", count: counts.needs },
    { id: "all", label: "All", count: counts.all },
    { id: "pa", label: isEoc ? "Prior auth" : "PA required", count: counts.pa },
    { id: "exceptions", label: "Exceptions", count: counts.exceptions },
    { id: "edited", label: "Human edited", count: counts.edited },
    { id: "low", label: "Low confidence", count: counts.low },
  ];
  const paReconcile = ((policy.validation_report as { pa_reconcile?: Record<string, number> } | undefined) || {}).pa_reconcile;

  return (
    <main>
      <div className="row" style={{ justifyContent: "space-between" }}>
        <div>
          <h1 className="title">{String(policy.file_name || "Policy")}</h1>
          <p className="muted">{documentRoleLabel(role)} · {identity.insurer?.value || "..."} · {identity.plan_name?.value || "..."} · {identity.plan_year?.value || "..."}</p>
        </div>
        <div className="row">
          <SourceBadge kind={String(policy.source_kind)} url={String(policy.source_url || "")} />
          <StatusBadge status={status} />
        </div>
      </div>
      {cacheNote && <p className="badge green" style={{ marginTop: 8 }}>{cacheNote}</p>}
      {error && <p className="badge amber">{error}</p>}

      {status === "paused" && (
        <section className="card" style={{ marginTop: 12, borderColor: "#f59e0b" }}>
          <p className="title">Role confirmation needed</p>
          <p className="muted">
            This file was uploaded as {documentRoleLabel(role)}. Precheck thinks it is{" "}
            <strong>{documentRoleLabel(String(policy.role_hint || "benefit_summary"))}</strong>
            {Number(policy.page_count || 0) ? ` · ${policy.page_count} pages` : ""}. Confirm the role to extract
            coverage / rules — that is why the review queue is empty.
          </p>
          <div className="row" style={{ marginTop: 10, flexWrap: "wrap", gap: 8 }}>
            <button
              type="button"
              className="btn"
              disabled={busyId === "confirm-role"}
              onClick={() =>
                act(
                  `/policies/${params.id}/confirm-role`,
                  {
                    reviewer,
                    document_role: String(policy.role_hint || "benefit_summary"),
                  },
                  "confirm-role",
                )
              }
            >
              {busyId === "confirm-role"
                ? "Starting extraction..."
                : `Confirm as ${documentRoleLabel(String(policy.role_hint || "benefit_summary"))}`}
            </button>
            <button
              type="button"
              className="btn secondary"
              disabled={busyId === "confirm-role"}
              onClick={() =>
                act(
                  `/policies/${params.id}/confirm-role`,
                  {
                    reviewer,
                    document_role: "benefit_summary",
                  },
                  "confirm-role",
                )
              }
            >
              Confirm as Evidence of Coverage
            </button>
            <button
              type="button"
              className="btn secondary"
              disabled={busyId === "confirm-role"}
              onClick={() =>
                act(
                  `/policies/${params.id}/confirm-role`,
                  {
                    reviewer,
                    document_role: "clinical_policy",
                  },
                  "confirm-role",
                )
              }
            >
              Keep as clinical policy
            </button>
          </div>
        </section>
      )}

      {extracting && <IngestionSteps steps={steps.length ? steps : PENDING_STEPS} live />}

      {(isLive || justWentLive) && (
        <section className="live-banner">
          <p className="title">This document is live</p>
          <p className="muted">
            Go live publishes accepted and edited rows into the order catalog. Rejected rows stay out.
            Clinicians open an order and submit a packet; the insurer queue reviews questionnaires, may ask for more evidence, then approve.
            You can take it offline later without losing extracted rules.
          </p>
          <div className="row" style={{ marginTop: 10 }}>
            <Link className="btn" href="/insurer">Open insurer review</Link>
            <Link className="btn secondary" href="/doctor">Open clinician order</Link>
            <button type="button" className="btn secondary" onClick={takeOfflineWithConfirm}>
              Take offline
            </button>
            <button
              type="button"
              className="btn secondary"
              onClick={() => {
                setFilter("all");
                queueRef.current?.scrollIntoView({ behavior: "smooth", block: "start" });
              }}
            >
              Browse all decisions
            </button>
          </div>
        </section>
      )}

      {isArchived && (
        <section className="card" style={{ marginTop: 12, borderColor: "#888" }}>
          <p className="title">This document is offline (archived)</p>
          <p className="muted">
            Extracted rules stay on file. It is not used for new PA lookups until you Go live again.
          </p>
        </section>
      )}

      <section className="card" style={{ marginTop: 12 }}>
        <p className="title">Identity</p>
        {(["insurer", "plan_name", "plan_year"] as const).map((key) => (
          <p key={key} className="muted">{key}: {identity[key]?.value || "—"} {identity[key]?.evidence ? `· “${identity[key]?.evidence}” p.${identity[key]?.page}` : ""}</p>
        ))}
        <p className="muted">Tier {sections.tier_used || "—"} · pages {(sections.pages || []).join(", ") || "—"} {sections.possibly_truncated ? "· possibly truncated" : ""}</p>
        {(sections.failed_tiers || []).map((tier) => <p key={tier.tier} className="muted">Tier {tier.tier} failed: {tier.reason}</p>)}
        <p className="muted" style={{ marginTop: 8 }}>
          {extracting
            ? "Extraction is still running. Rows appear in the review queue when the draft is saved."
            : role === "benefit_summary"
              ? `Plan coverage from this Evidence of Coverage: ${items.length} service rows. ${counts.needs} still need a human decision. ${sureCount} look sure. FHIR output is InsurancePlan.`
              : `${items.length} rules. ${counts.needs} still need a human decision. ${sureCount} look sure. FHIR output is Questionnaire.`}
        </p>
        <div className="row">
          <a className="btn secondary" href={fileUrl(`/policies/${params.id}/audit.md`)}>Export audit</a>
          <a className="btn secondary" href={fileUrl(`/policies/${params.id}/file`)} target="_blank">View PDF</a>
          <button className="btn secondary" disabled={extracting || fhirBusy} onClick={showFhir}>{fhirBusy ? "Building FHIR…" : "FHIR"}</button>
          <button className="btn secondary" disabled={extracting || items.length === 0} onClick={showExtractJson}>Extract JSON</button>
          <label className="field">Reviewer<input value={reviewer} onChange={(e) => setReviewer(e.target.value)} /></label>
          {isLive ? (
            <button className="btn secondary" disabled={extracting} onClick={takeOfflineWithConfirm}>
              Take offline
            </button>
          ) : (
            <button
              className="btn"
              disabled={!canGoLive}
              onClick={goLiveWithConfirm}
              title={undecided ? "Accept, edit, or reject every row first" : "Publish accepted rows for prior auth"}
            >
              {isArchived ? "Go live again" : "Go live"}
            </button>
          )}
        </div>
        {undecided && !extracting && !isLive && (
          <p className="muted">Go live stays off until every item is accepted, edited, or rejected. Use Needs review first, then check All.</p>
        )}
        {isLive && <p className="muted">Live. You can still change a row in All (Accept / Reject / Edit); that updates the catalog for new orders. Take offline anytime without deleting rules.</p>}
        {isArchived && !undecided && (
          <p className="muted">Archived. Go live again to put this plan back in the order catalog — no re-extract needed.</p>
        )}
        {history.length > 0 && (
          <div style={{ marginTop: 10 }}>
            <p className="muted">Status history</p>
            <ul className="muted" style={{ margin: "4px 0 0", paddingLeft: 18 }}>
              {history.map((entry, idx) => (
                <li key={`${entry.timestamp || idx}-${entry.status}`}>
                  {entry.status} · {entry.changed_by || "—"} · {entry.timestamp || "—"}
                </li>
              ))}
            </ul>
          </div>
        )}
      </section>

      {!extracting && (isLive || isArchived || questionnaires.length > 0) && (
        <section className="card" style={{ marginTop: 12 }}>
          <p className="title">Generated questionnaires</p>
          <p className="muted">
            Delete is only available when no QuestionnaireResponse has been submitted. Regenerate builds a fresh questionnaire from the live policy rules (demo-safe).
          </p>
          {questionnaires.length === 0 && (
            <p className="muted" style={{ marginTop: 8 }}>
              No stored questionnaires yet. They appear after a clinician order snapshots criteria, or after regenerate on a clinical policy.
            </p>
          )}
          {questionnaires.map((q) => (
            <div key={q.id} className="row" style={{ marginTop: 10, justifyContent: "space-between", flexWrap: "wrap", gap: 8 }}>
              <div>
                <p className="title" style={{ fontSize: 14 }}>{q.title || "Questionnaire"}</p>
                <p className="muted">
                  {q.item_count} groups · {q.response_count} response{q.response_count === 1 ? "" : "s"}
                  {q.service_category ? ` · ${q.service_category}` : ""}
                  {q.created_at ? ` · ${q.created_at}` : ""}
                </p>
                {!q.can_delete && (
                  <p className="muted">{q.delete_blocked_reason || "responses already submitted against this questionnaire"}</p>
                )}
              </div>
              <div className="row">
                <button
                  type="button"
                  className="btn secondary"
                  disabled={!q.can_delete || qBusyId === q.id || !isLive}
                  title={
                    !q.can_delete
                      ? q.delete_blocked_reason || "responses already submitted against this questionnaire"
                      : !isLive
                        ? "Policy must be live to regenerate"
                        : "Delete and rebuild from live rules"
                  }
                  onClick={() => regenerateQuestionnaire(q.id)}
                >
                  {qBusyId === q.id ? "Working…" : "Regenerate"}
                </button>
                <button
                  type="button"
                  className="btn secondary"
                  disabled={!q.can_delete || qBusyId === q.id}
                  title={
                    q.can_delete
                      ? "Delete questionnaire"
                      : q.delete_blocked_reason || "responses already submitted against this questionnaire"
                  }
                  onClick={() => deleteQuestionnaire(q.id)}
                >
                  Delete
                </button>
              </div>
            </div>
          ))}
        </section>
      )}
      {(fhir || extractJson) && (
        <pre ref={fhirRef} className="card" style={{ marginTop: 12, overflow: "auto", fontSize: 12, maxHeight: 360 }}>
          {fhir || extractJson}
        </pre>
      )}
      {!extracting && (
        <section ref={queueRef} style={{ marginTop: 16 }}>
          <div className="row" style={{ justifyContent: "space-between", alignItems: "center" }}>
            <p className="title">Review queue</p>
            <p className="muted">Showing {visible.length} of {items.length}</p>
          </div>
          <div className="filter-tabs" role="tablist" aria-label="Review filters">
            {tabs.map((tab) => (
              <button
                key={tab.id}
                type="button"
                role="tab"
                aria-selected={filter === tab.id}
                className={filter === tab.id ? "filter-tab active" : "filter-tab"}
                onClick={() => setFilter(tab.id)}
              >
                {tab.label} <span>{tab.count}</span>
              </button>
            ))}
          </div>
          {filter === "pa" && isEoc && (
            <section className="card" style={{ marginTop: 8 }}>
              <p className="title">Prior auth extracted from this Evidence of Coverage</p>
              <p className="muted">
                These flags come from chart markers (and an optional PA listing PDF). They drive whether an order needs a questionnaire.
              </p>
              <div className="row" style={{ marginTop: 10, flexWrap: "wrap", gap: 8 }}>
                <span className="badge amber">Required {paCounts.required}</span>
                <span className="badge blue">Conditional {paCounts.conditional}</span>
                <span className="badge gray">Not required {paCounts.not_required}</span>
                {paReconcile && (
                  <span className="badge green">
                    Last reconcile updated {paReconcile.updated ?? 0}
                    {paReconcile.listing_applied ? ` · listing ${paReconcile.listing_applied}` : ""}
                  </span>
                )}
              </div>
              <div className="filter-tabs" style={{ marginTop: 12 }} role="tablist" aria-label="Prior auth status">
                {(
                  [
                    ["required", "Required", paCounts.required],
                    ["conditional", "Conditional", paCounts.conditional],
                    ["not_required", "Not required", paCounts.not_required],
                    ["all", "All statuses", paCounts.required + paCounts.conditional + paCounts.not_required],
                  ] as [PaSubFilter, string, number][]
                ).map(([id, label, count]) => (
                  <button
                    key={id}
                    type="button"
                    role="tab"
                    aria-selected={paSubFilter === id}
                    className={paSubFilter === id ? "filter-tab active" : "filter-tab"}
                    onClick={() => setPaSubFilter(id)}
                  >
                    {label} <span>{count}</span>
                  </button>
                ))}
              </div>
              <div className="row" style={{ marginTop: 12, alignItems: "flex-end", flexWrap: "wrap", gap: 10 }}>
                <button type="button" className="btn secondary" disabled={reconcileBusy} onClick={() => reconcilePa(false)}>
                  {reconcileBusy ? "Refreshing..." : "Refresh PA flags from EOC"}
                </button>
                <label className="field" style={{ minWidth: "14rem", margin: 0 }}>
                  Optional PA listing PDF
                  <input
                    type="file"
                    accept="application/pdf"
                    onChange={(e) => setListingFile(e.target.files?.[0] || null)}
                  />
                </label>
                <button
                  type="button"
                  className="btn"
                  disabled={reconcileBusy || !listingFile}
                  onClick={() => reconcilePa(true)}
                >
                  Apply listing + refresh
                </button>
              </div>
              {reconcileNote && <p className="badge green" style={{ marginTop: 10 }}>{reconcileNote}</p>}
            </section>
          )}
          {items.length === 0 && (
            <p className="muted">
              {status === "paused"
                ? "No items yet — confirm the document role above to start extraction."
                : extracting
                  ? "No items yet — extraction is still running."
                  : role === "benefit_summary"
                    ? "No coverage rows yet. Re-run extraction or check the source PDF."
                    : role === "drug_criteria"
                      ? "No items yet. Drug blocks are extracted one at a time."
                      : "No rules yet. Re-run extraction or check the source PDF."}
            </p>
          )}
          {items.length > 0 && visible.length === 0 && filter === "needs" && (
            <div className="card" style={{ marginTop: 8 }}>
              <p className="muted">Every row already has a human decision. Stay here, or open All to see Accept / Reject / Edit status on each row.</p>
              <button type="button" className="btn secondary" style={{ marginTop: 8 }} onClick={() => setFilter("all")}>Open All</button>
            </div>
          )}
          {items.length > 0 && visible.length === 0 && filter !== "needs" && <p className="muted">No rows in this tab.</p>}
          {filter === "all" && items.length > 0 && (
            <section className="card" style={{ marginTop: 8, borderColor: undecided ? undefined : "#c9a227" }}>
              <p className="title">Confirm questionnaires</p>
              <p className="muted">
                Review every row above. When they look right, submit to publish them (if not already live) and open the doctor order screen to fill the clinical questionnaire for a patient.
              </p>
              <p className="muted" style={{ marginTop: 6 }}>
                {undecided
                  ? `${counts.needs} row(s) still need Accept / Edit / Reject.`
                  : `${items.filter((i) => i.review_state !== "rejected").length} questionnaire/coverage rows ready for clinicians.`}
              </p>
              <button
                type="button"
                className="btn"
                style={{ marginTop: 10 }}
                disabled={submitBusy || extracting || undecided}
                onClick={confirmQuestionnairesAndOpenDoctor}
              >
                {submitBusy ? "Submitting..." : isLive ? "Submit and open doctor questionnaires" : "Submit questionnaires and open doctor"}
              </button>
              {undecided && (
                <p className="muted" style={{ marginTop: 8 }}>Finish Needs review (or decide each row in All) before submit unlocks.</p>
              )}
            </section>
          )}
          {visible.map((item) => {
            const confidence = ruleConfidence(item);
            const title = item.title || item.summary;
            const decided = !needsDecision(item);
            const isEditing = editing?.id === item.id;
            const busy = busyId === item.id;
            return (
              <article key={item.id} className={confidence.sure && !decided ? "card" : decided ? "card" : "card needs-look"} style={{ marginTop: 8 }}>
                <div className="row" style={{ justifyContent: "space-between" }}>
                  <div className="row">
                    <ConfidenceBadge sure={confidence.sure} importOnly={confidence.importOnly} />
                    <StatusBadge status={item.review_state} />
                    <span className={`badge ${item.review_state === "rejected" ? "amber" : decided ? "green" : "blue"}`}>
                      {decisionLabel(item.review_state)}
                    </span>
                    <JudgeBadge verdict={item.judge_verdict} />
                    {item.edited_by_human && <LockBadge />}
                    {(() => {
                      const status = itemPaStatus(item);
                      if (status === "required") return <span className="badge amber">PA required</span>;
                      if (status === "conditional") return <span className="badge blue">PA conditional</span>;
                      if (status === "not_required") return <span className="badge gray">No PA</span>;
                      if (item.pa_required === true) return <span className="badge amber">PA required</span>;
                      if (item.pa_required === false) return <span className="badge gray">No PA</span>;
                      return null;
                    })()}
                    {item.has_exception && <span className="badge amber">Exception</span>}
                  </div>
                  <span className="muted">p.{item.page}</span>
                </div>
                <p className="title" style={{ marginTop: 8 }}>{title}</p>
                <div className="facts">
                  <p><span className="fact-key">Cost share</span> {item.cost_share || "—"}</p>
                  {item.limits && <p><span className="fact-key">Limits</span> {item.limits}</p>}
                  <p><span className="fact-key">Page</span> {item.page}</p>
                  <p><span className="fact-key">Decision</span> {decisionLabel(item.review_state)}</p>
                  {itemPaStatus(item) && (
                    <p><span className="fact-key">Prior auth</span> {itemPaStatus(item)?.replaceAll("_", " ")}</p>
                  )}
                  {(item.service_codes || item.data.service_codes || []).length > 0 && (
                    <p><span className="fact-key">Codes</span> {(item.service_codes || item.data.service_codes || []).join(", ")}</p>
                  )}
                  {(item.marker_used || item.data.marker_used) && (
                    <p><span className="fact-key">Marker</span> {item.marker_used || item.data.marker_used}</p>
                  )}
                  {item.listing_index != null && (
                    <p><span className="fact-key">Listing #</span> {item.listing_index}</p>
                  )}
                </div>
                {item.criteria && (
                  <div className="criteria-box">
                    <span className="fact-key">Criteria</span>
                    <p>{item.criteria}</p>
                  </div>
                )}
                {item.exception && (
                  <div className="exception-box">
                    <span className="fact-key">Exception</span>
                    <p>{item.exception}</p>
                  </div>
                )}
                {(item.why || item.judge_reason) && <p className="muted" style={{ marginTop: 8 }}>{item.why || item.judge_reason}</p>}
                {!confidence.sure && confidence.detail && <p className="muted">{confidence.detail}</p>}
                {item.review_note && <p className="decision-note">Reviewer note: {item.review_note}</p>}
                {item.suggestion && <p className="badge amber">A newer extraction is waiting. It did not overwrite this edit.</p>}

                <div className="decision-actions" role="group" aria-label="Review decision">
                  <button
                    type="button"
                    className={`decision-btn${item.review_state === "accepted" ? " active accept" : ""}`}
                    disabled={busy}
                    onClick={() => act(`/policies/${params.id}/items/${item.id}/accept`, { reviewer, note: "Reviewed against the cited page." }, item.id)}
                  >
                    Accept
                  </button>
                  <button
                    type="button"
                    className={`decision-btn${isEditing || item.review_state === "edited" ? " active edit" : ""}`}
                    disabled={busy}
                    onClick={() => (isEditing ? setEditing(null) : openEdit(item))}
                  >
                    {isEditing ? "Close edit" : "Edit"}
                  </button>
                  <button
                    type="button"
                    className={`decision-btn${item.review_state === "rejected" ? " active reject" : ""}`}
                    disabled={busy}
                    onClick={() => act(`/policies/${params.id}/items/${item.id}/reject`, { reviewer, note: "Not used for this policy." }, item.id)}
                  >
                    Reject
                  </button>
                  {decided && (
                    <span className="muted">
                      Clicked: <strong>{decisionLabel(item.review_state)}</strong>
                      {item.review_state === "edited" ? " — open Edit to change values" : " — use Edit only if values need a change"}
                    </span>
                  )}
                </div>

                {(item.data.questions || []).length > 0 && (
                  <ul>
                    {item.data.questions!.map((question) => <li key={question.link_id} className="muted">{question.text} · {question.answer_type}</li>)}
                  </ul>
                )}
                {isEditing && (
                  <form ref={editRef} className="edit-panel" onSubmit={saveEdit}>
                    <p className="title">Edit this row</p>
                    <p className="muted">Fields stay locked until you click Edit. Save writes an Edited decision.</p>
                    <p className="quote">{(editing.page_text || "").slice(0, 700) || "Page text is not cached. Open View PDF for the source."}</p>
                    <label className="field">Service / rule name<textarea value={draft} onChange={(e) => setDraft(e.target.value)} rows={2} /></label>
                    <label className="field">Cost share<input value={costDraft} onChange={(e) => setCostDraft(e.target.value)} /></label>
                    <label className="field">Evidence quote<textarea value={evidenceDraft} onChange={(e) => setEvidenceDraft(e.target.value)} rows={3} /></label>
                    <label className="field">
                      Prior authorization
                      <select
                        value={paStatusDraft}
                        onChange={(e) => setPaStatusDraft(e.target.value as "required" | "conditional" | "not_required")}
                      >
                        <option value="required">Required</option>
                        <option value="conditional">Conditional</option>
                        <option value="not_required">Not required</option>
                      </select>
                    </label>
                    <label className="field">Note (required)<input value={note} onChange={(e) => setNote(e.target.value)} /></label>
                    <div className="row">
                      <button className="btn" disabled={busy}>Save edit</button>
                      <button type="button" className="btn secondary" onClick={() => setEditing(null)}>Cancel</button>
                    </div>
                  </form>
                )}
              </article>
            );
          })}
        </section>
      )}
    </main>
  );
}
