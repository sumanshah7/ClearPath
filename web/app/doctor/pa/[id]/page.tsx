"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { useParams } from "next/navigation";
import { api, fileUrl, isNotFound } from "@/lib/api";
import { pretty } from "@/lib/format";
import { ConfidenceBadge, JudgeBadge, ReadinessBar, StatusBadge, ruleConfidence } from "@/components/pa/StatusBadge";
import { answerFieldHints, isBooleanQuestion, sourceKindOptions, type SourceKind } from "@/lib/answerPrompts";

type Question = {
  id: string;
  text: string;
  answer_type?: string;
  enabled: boolean;
  value: unknown;
  fill_method?: string | null;
  review_state: string;
  evidence_text?: string | null;
  evidence_source?: { author?: string; date?: string; file_name?: string; document_id?: string } | null;
  reject_reason?: string | null;
  rejected_ai_value?: unknown;
  attestation?: string | null;
};
type Criterion = {
  id: string;
  requirement_text: string;
  criterion_type: string;
  policy_page: number;
  status: string;
  status_reason?: string | null;
  judge_verdict?: string | null;
  likely_owner?: { full_name: string; reason: string } | null;
  verified_by?: { full_name: string } | null;
  questions: Question[];
};
type RequestView = {
  id: string;
  status: string;
  readiness: number;
  met_count: number;
  total_count: number;
  can_submit: boolean;
  order_text: string;
  service_code?: string;
  service_category?: string;
  insurer?: string;
  plan_name?: string;
  plan_year?: string;
  patient: { id: string; full_name: string; synthetic: boolean };
  ordering_provider: { id: string; full_name: string };
  coverage?: { pa_required: boolean; pa_status?: string; page?: number | null; evidence_text?: string | null; document_url?: string | null; service_label?: string | null } | null;
  pa_determination?: {
    requirement: string;
    label: string;
    pa_required: boolean | null;
    page?: number | null;
    evidence_text?: string | null;
    service_label?: string | null;
    document_url?: string | null;
  } | null;
  questionnaire?: { resourceType?: string; title?: string; item?: { linkId: string; text?: string; item?: { linkId: string; text?: string; type?: string }[] }[] } | null;
  questionnaire_response?: { resourceType?: string; status?: string; item?: unknown[] } | null;
  criteria_source?: { title?: string; source_kind?: string; policy_id?: string } | null;
  criteria: Criterion[];
  events: { event_type: string; message?: string; actor: string; created_at: string }[];
  match_candidates?: { item_id: string; label: string; page?: number; phrase?: string }[] | null;
};

export default function ChecklistPage() {
  const params = useParams<{ id: string }>();
  const [view, setView] = useState<RequestView | null>(null);
  const [error, setError] = useState("");
  const [missing, setMissing] = useState(false);
  const [latestId, setLatestId] = useState<string | null>(null);
  const [docs, setDocs] = useState<{ id: string; file_name: string; in_chart?: number }[]>([]);
  const [packet, setPacket] = useState("");
  const [active, setActive] = useState<Question | null>(null);

  async function load() {
    const data = await api<RequestView>(`/pa/${params.id}`);
    setView(data);
    setMissing(false);
    const docs = await api<{ documents: { id: string; file_name: string; in_chart?: number }[] }>(`/patients/${data.patient.id}/documents`);
    setDocs(docs.documents);
  }

  useEffect(() => {
    let stop = false;
    let timer: ReturnType<typeof setInterval> | null = null;

    async function markMissing() {
      setMissing(true);
      setView(null);
      try {
        const listed = await api<{ requests: { id: string }[] }>("/pa");
        if (!stop) setLatestId(listed.requests[0]?.id || null);
      } catch {
        if (!stop) setLatestId(null);
      }
    }

    async function pull() {
      try {
        const data = await api<RequestView>(`/pa/${params.id}`);
        if (stop) return;
        setView(data);
        setMissing(false);
        const docs = await api<{ documents: { id: string; file_name: string; in_chart?: number }[] }>(`/patients/${data.patient.id}/documents`);
        if (!stop) setDocs(docs.documents);
        setError("");
        // Terminal outcomes do not need live polling.
        const terminal = data.status === "not_required" || data.status === "approved";
        if (terminal) {
          if (timer) {
            clearInterval(timer);
            timer = null;
          }
          return;
        }
        if (!timer && !stop) {
          timer = setInterval(pull, 2500);
        }
      } catch (err) {
        if (stop) return;
        if (isNotFound(err)) {
          if (timer) {
            clearInterval(timer);
            timer = null;
          }
          setError(err instanceof Error ? err.message : "No request with that id.");
          await markMissing();
          return;
        }
        setError(err instanceof Error ? err.message : "Could not load this request");
      }
    }

    pull();
    return () => {
      stop = true;
      if (timer) clearInterval(timer);
    };
  }, [params.id]);

  async function post(path: string, body: object) {
    setError("");
    try {
      const data = await api<RequestView>(path, { method: "POST", body: JSON.stringify(body) });
      setView(data);
      setActive(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : "That action failed");
    }
  }

  async function addDocument(documentId: string) {
    if (!view) return;
    const body = new FormData();
    body.set("patient_id", view.patient.id);
    body.set("document_id", documentId);
    await api("/reports/extract", { method: "POST", body });
    const data = await api<RequestView>(`/pa/${view.id}/recheck`, { method: "POST" });
    setView(data);
    setDocs((rows) => rows.map((row) => row.id === documentId ? { ...row, in_chart: 1 } : row));
  }

  if (!view && !error) return <p className="muted">Loading checklist...</p>;
  if (!view) {
    return (
      <main>
        <p className="badge amber">{error || "No request with that id."}</p>
        {missing ? (
          <div className="card" style={{ marginTop: 12, display: "grid", gap: 8, maxWidth: 520 }}>
            <p>This prior-auth id is not in the database (stale tab or DB reset).</p>
            <div className="row">
              <Link className="btn" href="/doctor">Start a new order</Link>
              {latestId && <Link className="btn secondary" href={`/doctor/pa/${latestId}`}>Open latest request</Link>}
            </div>
          </div>
        ) : (
          <button className="btn secondary" onClick={load}>Retry</button>
        )}
      </main>
    );
  }

  return (
    <main>
      <div className="row" style={{ justifyContent: "space-between" }}>
        <div>
          <h1 className="title">
            {view.patient.full_name}
            <span className="muted" style={{ fontWeight: 500 }}>
              {" "}
              | {view.order_text}
            </span>
          </h1>
          <p className="muted">{view.ordering_provider.full_name}</p>
          <p style={{ marginTop: 6 }}>
            <Link className="btn secondary" href={`/patient/${view.patient.id}`}>
              Patient status page
            </Link>
          </p>
        </div>
        <StatusBadge status={view.status} />
      </div>
      {error && <p className="badge amber">{error}</p>}
      {view.status === "draft" && (
        <section className="card" style={{ marginTop: 12 }}>
          <p className="title">Confirm ingested order</p>
          <p className="muted">
            Review the pre-filled patient, plan, and treatment from the extraction JSON, then confirm to run PA determination.
          </p>
          <p style={{ marginTop: 8 }}>
            {[view.insurer, view.plan_name, view.plan_year].filter(Boolean).join(" | ") || "Plan not set"}
          </p>
          <p className="muted">Treatment: {view.service_category || view.order_text}{view.service_code ? ` (${view.service_code})` : ""}</p>
          <button
            className="btn"
            style={{ marginTop: 10 }}
            onClick={() => post(`/pa-requests/${view.id}/confirm`, {
              order_text: view.order_text,
              service_category: view.service_category || view.order_text,
              service_code: view.service_code,
            })}
          >
            Confirm and determine PA
          </button>
        </section>
      )}
      {view.pa_determination && view.status !== "draft" && view.status !== "not_required" && (
        <p className="card" style={{ marginTop: 12, borderColor: view.pa_determination.pa_required ? "#c9a227" : undefined }}>
          <strong>{view.pa_determination.label}</strong>
          {view.pa_determination.requirement ? `  |  ${view.pa_determination.requirement}` : ""}
          {view.pa_determination.page ? `  |  plan coverage p.${view.pa_determination.page}` : ""}
          {view.pa_determination.evidence_text && (
            <span className="quote" style={{ display: "block" }}>{view.pa_determination.evidence_text}</span>
          )}
        </p>
      )}
      {view.coverage && !view.pa_determination && view.status !== "not_required" && (
        <p className="card" style={{ marginTop: 12 }}>
          {view.coverage.pa_required ? "Prior authorization required" : "Prior authorization is not required"}
          {view.coverage.page ? `  |  plan coverage p.${view.coverage.page}` : ""}
          {view.coverage.evidence_text && <span className="quote" style={{ display: "block" }}>{view.coverage.evidence_text}</span>}
          {view.coverage.document_url && <a href={fileUrl(view.coverage.document_url)}>View source</a>}
        </p>
      )}
      {view.status === "not_required" && (
        <section className="card feature" style={{ marginTop: 12 }}>
          <div className="row" style={{ justifyContent: "space-between", alignItems: "flex-start" }}>
            <div>
              <p className="kicker">Coverage result</p>
              <h2 className="title">No prior authorization needed</h2>
            </div>
            <StatusBadge status="not_required" />
          </div>
          <p style={{ marginTop: 10 }}>
            {view.pa_determination?.label || "Prior authorization is not required for this treatment under this plan."}
          </p>
          {(view.pa_determination?.service_label || view.coverage?.service_label || view.order_text) && (
            <p className="muted" style={{ marginTop: 6 }}>
              Service: {view.pa_determination?.service_label || view.coverage?.service_label || view.order_text}
            </p>
          )}
          {(view.pa_determination?.page || view.coverage?.page) && (
            <p className="muted">
              Cited from the plan document
              {view.pa_determination?.page || view.coverage?.page
                ? ` | p.${view.pa_determination?.page || view.coverage?.page}`
                : ""}
            </p>
          )}
          {(view.pa_determination?.evidence_text || view.coverage?.evidence_text) && (
            <p className="quote" style={{ marginTop: 10 }}>
              {view.pa_determination?.evidence_text || view.coverage?.evidence_text}
            </p>
          )}
          {(view.pa_determination?.document_url || view.coverage?.document_url) && (
            <p style={{ marginTop: 8 }}>
              <a href={fileUrl(view.pa_determination?.document_url || view.coverage?.document_url || "")}>
                Open plan PDF
              </a>
            </p>
          )}
          <p className="muted" style={{ marginTop: 12 }}>
            You can proceed clinically. ClearPath will not build a questionnaire or insurer packet for this order.
          </p>
          <div className="row" style={{ marginTop: 14 }}>
            <Link className="btn" href="/doctor">
              Place another order
            </Link>
          </div>
        </section>
      )}
      {view.questionnaire && view.status !== "draft" && view.status !== "not_required" && (
        <section className="card" style={{ marginTop: 12 }}>
          <p className="title">Clinical checklist</p>
          <p className="muted">{view.questionnaire.title || "Questions drawn from the live criteria for this order"}</p>
          {(view.questionnaire.item || []).map((group) => (
            <div key={group.linkId} style={{ marginTop: 10 }}>
              <p className="title" style={{ fontSize: 14 }}>{group.text || group.linkId}</p>
              {(group.item || []).map((q) => (
                <p key={q.linkId} className="muted" style={{ marginTop: 4 }}>
                  {q.text || q.linkId}{q.type ? ` (${q.type})` : ""}
                </p>
              ))}
            </div>
          ))}
          <p className="muted" style={{ marginTop: 8 }}>
            Answer each rule below, verify when met, then preview the packet. Answers ship as a FHIR QuestionnaireResponse.
          </p>
        </section>
      )}
      {view.status === "info_requested" && (
        <section className="card" style={{ marginTop: 12, borderColor: "#c9a227" }}>
          <p className="kicker">Insurer decision</p>
          <p className="title">Additional information requested</p>
          <p className="muted" style={{ marginTop: 6 }}>
            The payer desk asked for more evidence. Add documentation below, verify, and resubmit. Status updates live from the same request record.
          </p>
        </section>
      )}
      {view.status === "approved" && (
        <section className="card feature" style={{ marginTop: 12 }}>
          <p className="kicker">Insurer decision</p>
          <div className="row" style={{ justifyContent: "space-between", alignItems: "flex-start" }}>
            <div>
              <p className="title">Approved</p>
              <p className="muted" style={{ marginTop: 6 }}>
                Recorded on the insurer queue. This checklist and{" "}
                <Link href="/doctor/requests">Request status</Link> stay in sync with that decision.
              </p>
            </div>
            <StatusBadge status="approved" />
          </div>
        </section>
      )}
      {view.status === "in_review" && (
        <p className="badge blue" style={{ marginTop: 12 }}>
          With the insurer queue. Waiting for approve or an information request.{" "}
          <Link href={`/insurer/pa/${view.id}`}>Open insurer view</Link>
          {" · "}
          <Link href="/doctor/requests">Request status</Link>
        </p>
      )}
      {view.status === "submitted" && (
        <p className="badge blue" style={{ marginTop: 12 }}>
          Submitted. Waiting for an insurer decision — this page polls automatically.{" "}
          <Link href="/doctor/requests">Request status</Link>
          {" · "}
          <Link href="/insurer">Open insurer queue</Link>
        </p>
      )}
      {view.criteria_source && <p className="muted">Criteria: {view.criteria_source.title} ({view.criteria_source.source_kind})</p>}
      {view.total_count > 0 && <ReadinessBar met={view.met_count} total={view.total_count} readiness={view.readiness} />}
      {view.status === "matching" && (
        <section className="card">
          <p className="title">Match this order to the plan</p>
          <p className="muted">
            Pick the benefit-chart row that matches what you ordered. ClearPath then opens the clinical checklist for that service.
          </p>
          {(view.match_candidates || []).length === 0 && (
            <div style={{ marginTop: 8 }}>
              <p className="badge amber">
                No automatic match yet. Confirm or add a service code on a new order, then try again.
              </p>
              <Link className="btn" style={{ marginTop: 8 }} href="/doctor">
                Back to order desk
              </Link>
            </div>
          )}
          {(view.match_candidates || []).map((candidate) => (
            <button key={candidate.item_id} className="btn secondary" style={{ marginTop: 6 }} onClick={() => post(`/pa/${view.id}/match`, { item_id: candidate.item_id })}>
              {candidate.label} {candidate.page ? `p.${candidate.page}` : ""} {candidate.phrase ? ` "${candidate.phrase}"` : ""}
            </button>
          ))}
        </section>
      )}
      {view.criteria.length === 0 && view.status !== "draft" && view.status !== "matching" && view.status !== "not_required" && (
        <p className="muted" style={{ marginTop: 12 }}>No questionnaire items yet for this order.</p>
      )}
      {view.criteria.length > 0 && view.status !== "draft" && view.status !== "not_required" && (
        <p className="badge green" style={{ marginTop: 12 }}>
          Questionnaire ready — answer each rule below, verify when met, then submit the packet to the insurer.
        </p>
      )}
      {view.criteria.length === 0 && (view.status === "needs_info" || view.status === "ready_for_review") && (
        <p className="card" style={{ marginTop: 12 }}>
          No clinical questions were built for this service yet. Upload chart evidence and Recheck, or confirm the plan service label matches a live coverage row.
        </p>
      )}
      {view.status === "not_required" ? (
        <div style={{ marginTop: 16, maxWidth: "36rem" }}>
          <aside className="card episode-panel" style={{ marginTop: 0 }}>
            <p className="title">Timeline</p>
            <p className="muted">Coverage finished. No PA packet is needed.</p>
            <ol className="episode-rail">
              {view.events.map((event, index) => (
                <li
                  key={`${event.created_at}-${index}`}
                  className={`episode-item ${eventTone(event.event_type)}`}
                >
                  <div className="episode-dot" aria-hidden />
                  <div className="episode-body">
                    <div className="row" style={{ justifyContent: "space-between" }}>
                      <span className="episode-stage">{labelEvent(event.event_type)}</span>
                      <span className="muted">{event.actor}</span>
                    </div>
                    <p className="episode-summary">{event.message || labelEvent(event.event_type)}</p>
                  </div>
                </li>
              ))}
            </ol>
            {view.events.length === 0 && <p className="muted">No events yet.</p>}
          </aside>
        </div>
      ) : (
      <div className="split" style={{ marginTop: 16 }}>
        <div>
          {view.criteria.map((criterion) => {
            const answered = criterion.questions.filter((question) => question.enabled).every((question) => question.value != null);
            const confidence = ruleConfidence({
              judge_verdict: criterion.judge_verdict,
              grounding: { passed: criterion.status === "met" && answered },
              question_verdict: "complete",
            });
            return (
            <article key={criterion.id} className={confidence.sure ? "card" : "card needs-look"} style={{ marginBottom: 10 }}>
              <div className="row">
                <ConfidenceBadge sure={confidence.sure} />
                <StatusBadge status={criterion.verified_by ? "verified" : criterion.status} />
                <span className="muted">p.{criterion.policy_page}</span>
                <JudgeBadge verdict={criterion.judge_verdict} />
              </div>
              {!confidence.sure && (
                <p className="muted">
                  {criterion.status === "met"
                    ? confidence.detail
                    : answerFieldHints({
                        questionText: criterion.questions.find((q) => q.enabled)?.text || criterion.requirement_text,
                        criterionType: criterion.criterion_type,
                      }).hint || "Enter an answer below, then Verify when the rule shows met."}
                </p>
              )}
              <p className="title" style={{ marginTop: 8 }}>{criterion.requirement_text}</p>
              {criterion.status_reason && <p>{criterion.status_reason}</p>}
              {criterion.likely_owner && <p className="muted">Likely with {criterion.likely_owner.full_name}. {criterion.likely_owner.reason}.</p>}
              {criterion.questions.filter((q) => q.enabled).map((question) => (
                <div key={question.id} style={{ marginTop: 8 }}>
                  <p className="muted">{question.text}</p>
                  <p>{showValue(question.value)} {question.fill_method === "clinician_entered" ? " | Clinician attested" : question.fill_method ? " | Found in chart" : ""}</p>
                  {question.evidence_text && (
                    <p className="quote">
                      "{question.evidence_text}"
                      {(question.evidence_source?.file_name || question.evidence_source?.author) && (
                        <span className="muted">
                          {" "}
                          - {question.evidence_source.file_name || question.evidence_source.author}
                          {question.evidence_source?.date ? ` | ${pretty(question.evidence_source.date)}` : ""}
                        </span>
                      )}
                    </p>
                  )}
                  {question.reject_reason && <p className="muted">Set aside: {question.reject_reason}. Earlier value: {showValue(question.rejected_ai_value)}</p>}
                  {question.attestation && <p className="muted">Attestation: {question.attestation}</p>}
                  {!criterion.verified_by && question.review_state !== "clinician_entered" && question.value != null && active?.id !== question.id && (
                    <button type="button" className="btn secondary" onClick={() => setActive(question)}>Set aside</button>
                  )}
                  {!criterion.verified_by && active?.id === question.id && (
                    <form
                      className="edit-panel"
                      style={{ marginTop: 8 }}
                      onSubmit={(e) => {
                        e.preventDefault();
                        const reason = String(new FormData(e.currentTarget).get("reason") || "").trim();
                        if (reason.length < 5) {
                          setError("Set-aside reason needs at least 5 characters.");
                          return;
                        }
                        post(`/pa/${view.id}/answers/${question.id}/reject`, {
                          provider_id: view.ordering_provider.id,
                          reason,
                        });
                      }}
                    >
                      <p className="title">Set this answer aside</p>
                      <p className="muted">Clears the chart/AI answer so you can enter a replacement below.</p>
                      <label className="field">
                        Reason (at least 5 characters)
                        <input name="reason" required minLength={5} autoFocus placeholder="Why this answer is wrong" />
                      </label>
                      <div className="row" style={{ marginTop: 8 }}>
                        <button type="submit" className="btn">Confirm set aside</button>
                        <button type="button" className="btn secondary" onClick={() => setActive(null)}>Cancel</button>
                      </div>
                    </form>
                  )}
                  {!criterion.verified_by && (question.value == null || question.review_state === "clinician_rejected") && (
                    <EnterForm
                      question={question}
                      criterionType={criterion.criterion_type}
                      providerId={view.ordering_provider.id}
                      patientId={view.patient.id}
                      documents={docs}
                      onDocumentsChange={setDocs}
                      onSubmit={(body) => post(`/pa/${view.id}/answers/${question.id}/enter`, body)}
                    />
                  )}
                </div>
              ))}
              {criterion.status === "met" && !criterion.verified_by && (
                <button className="btn" style={{ marginTop: 8 }} onClick={() => post(`/pa/${view.id}/criteria/${criterion.id}/verify`, { provider_id: view.ordering_provider.id })}>Verify</button>
              )}
              {criterion.verified_by && <p className="muted">Verified by {criterion.verified_by.full_name}</p>}
            </article>
            );
          })}
          {docs.some((doc) => !doc.in_chart) && (
            <section className="card">
              <p className="title">Chart documents to add</p>
              {docs.filter((doc) => !doc.in_chart).map((doc) => (
                <button key={doc.id} className="btn secondary" onClick={() => addDocument(doc.id)}>Add {doc.file_name}</button>
              ))}
            </section>
          )}
          {view.status !== "draft" && view.status !== "approved" && view.status !== "not_required" && (
            <section className="card" style={{ marginTop: 10 }}>
              <p className="title">Upload clinical evidence</p>
              <p className="muted">
                Add a note or report PDF to this patient chart. ClearPath rechecks unanswered questions against it.
              </p>
              <label className="field">
                PDF
                <input
                  type="file"
                  accept="application/pdf"
                  onChange={async (e) => {
                    const file = e.target.files?.[0];
                    if (!file || !view) return;
                    setError("");
                    try {
                      const body = new FormData();
                      body.set("patient_id", view.patient.id);
                      body.set("file", file);
                      await api("/reports/extract", { method: "POST", body });
                      if (view.status !== "matching" && view.status !== "submitted" && view.status !== "in_review") {
                        const data = await api<RequestView>(`/pa/${view.id}/recheck`, { method: "POST" });
                        setView(data);
                      }
                      const listed = await api<{ documents: { id: string; file_name: string; in_chart?: number }[] }>(`/patients/${view.patient.id}/documents`);
                      setDocs(listed.documents);
                    } catch (err) {
                      setError(err instanceof Error ? err.message : "Upload failed");
                    }
                  }}
                />
              </label>
            </section>
          )}
        </div>
        <aside className="card episode-panel">
          <p className="title">Timeline</p>
          <p className="muted">
            {view.status === "not_required"
              ? "This order is complete at the coverage gate."
              : view.status === "approved"
                ? "Insurer decision is on the record."
                : "Updates while this request stays open."}
          </p>
          <ol className="episode-rail">
            {view.events.map((event, index) => (
              <li
                key={`${event.created_at}-${index}`}
                className={`episode-item ${eventTone(event.event_type)}`}
              >
                <div className="episode-dot" aria-hidden />
                <div className="episode-body">
                  <div className="row" style={{ justifyContent: "space-between" }}>
                    <span className="episode-stage">{labelEvent(event.event_type)}</span>
                    <span className="muted">{event.actor}</span>
                  </div>
                  <p className="episode-summary">{event.message || labelEvent(event.event_type)}</p>
                </div>
              </li>
            ))}
          </ol>
          {view.events.length === 0 && <p className="muted">No events yet.</p>}
          {showsSubmitControls(view.status) ? (
            <>
              <button
                className="btn secondary"
                disabled={!view.can_submit}
                onClick={async () => setPacket(JSON.stringify(await api(`/pa/${view.id}/packet`), null, 2))}
              >
                Preview packet
              </button>
              {!view.can_submit && view.total_count > 0 && (
                <p className="muted">Verify every rule before preview unlocks.</p>
              )}
              <button
                className="btn"
                disabled={!view.can_submit}
                onClick={() => post(`/pa/${view.id}/submit`, { provider_id: view.ordering_provider.id })}
              >
                Submit to insurer
              </button>
              {packet && <pre className="episode-json">{packet}</pre>}
            </>
          ) : null}
        </aside>
      </div>
      )}
    </main>
  );
}

function EnterForm({
  question,
  criterionType,
  providerId,
  patientId,
  documents,
  onDocumentsChange,
  onSubmit,
}: {
  question: Question;
  criterionType?: string;
  providerId: string;
  patientId: string;
  documents: { id: string; file_name: string }[];
  onDocumentsChange: (docs: { id: string; file_name: string; in_chart?: number }[]) => void;
  onSubmit: (body: object) => void;
}) {
  const [open, setOpen] = useState(false);
  const [sourceKind, setSourceKind] = useState<SourceKind>(
    documents.length ? "chart_document" : "clinician_note"
  );
  const [sourceId, setSourceId] = useState("");
  const [uploading, setUploading] = useState(false);
  const [formError, setFormError] = useState("");
  const isBoolean = isBooleanQuestion(question.text, question.answer_type);
  const hints = answerFieldHints({
    questionText: question.text,
    criterionType,
    sourceKind,
  });
  const sourceOptions = sourceKindOptions({
    questionText: question.text,
    criterionType,
    hasDocuments: documents.length > 0,
  });
  const needsDocument = sourceKind === "chart_document";
  const canSave = !uploading && (!needsDocument || Boolean(sourceId));

  if (!open) {
    return (
      <button className="btn secondary" type="button" onClick={() => setOpen(true)}>
        Enter answer
      </button>
    );
  }
  return (
    <form
      className="edit-panel"
      onSubmit={(e) => {
        e.preventDefault();
        setFormError("");
        const form = new FormData(e.currentTarget);
        const raw = String(form.get("value") || "").trim();
        if (needsDocument && !sourceId) {
          setFormError("Pick a chart document, upload a PDF, or choose Clinician note / Not documented.");
          return;
        }
        let value: unknown = raw;
        if (isBoolean) {
          const low = raw.toLowerCase();
          if (["true", "yes", "y", "1"].includes(low)) value = true;
          else if (["false", "no", "n", "0"].includes(low)) value = false;
          else return;
        } else if (raw !== "" && !Number.isNaN(Number(raw)) && !["true", "false"].includes(raw.toLowerCase())) {
          value = Number(raw);
        }
        onSubmit({
          provider_id: providerId,
          value,
          source_kind: sourceKind,
          source_document_id: needsDocument ? sourceId : undefined,
          evidence_text: form.get("evidence"),
          attestation: form.get("attestation"),
        });
      }}
    >
      {hints.hint && <p className="muted">{hints.hint}</p>}
      <label className="field">
        Value
        {isBoolean ? (
          <select name="value" required defaultValue={hints.preferNo ? "false" : ""}>
            <option value="" disabled>
              Select
            </option>
            <option value="false">No</option>
            <option value="true">Yes</option>
          </select>
        ) : (
          <input name="value" required placeholder="Number or short text" />
        )}
      </label>
      <label className="field">
        Source type
        <select
          value={sourceKind}
          onChange={(e) => {
            const next = e.target.value as SourceKind;
            setSourceKind(next);
            setFormError("");
            if (next !== "chart_document") setSourceId("");
          }}
        >
          {sourceOptions.map((opt) => (
            <option key={opt.value} value={opt.value} disabled={opt.value === "chart_document" && documents.length === 0}>
              {opt.label}
            </option>
          ))}
        </select>
      </label>
      <p className="muted">{sourceOptions.find((o) => o.value === sourceKind)?.hint}</p>
      {needsDocument && (
        <>
          <label className="field">
            Chart document
            <select
              name="source"
              required={needsDocument}
              value={sourceId}
              onChange={(e) => setSourceId(e.target.value)}
            >
              <option value="" disabled>
                {documents.length ? "Select a chart document" : "No documents yet - upload below"}
              </option>
              {documents.map((doc) => (
                <option key={doc.id} value={doc.id}>
                  {doc.file_name}
                </option>
              ))}
            </select>
          </label>
          <label className="field">
            Or upload a PDF
            <input
              type="file"
              accept="application/pdf"
              disabled={uploading}
              onChange={async (e) => {
                const file = e.target.files?.[0];
                if (!file) return;
                setUploading(true);
                setFormError("");
                try {
                  const body = new FormData();
                  body.set("patient_id", patientId);
                  body.set("file", file);
                  await api("/reports/extract", { method: "POST", body });
                  const listed = await api<{ documents: { id: string; file_name: string; in_chart?: number }[] }>(
                    `/patients/${patientId}/documents`
                  );
                  onDocumentsChange(listed.documents);
                  const match =
                    listed.documents.find((d) => d.file_name === file.name) ||
                    listed.documents[listed.documents.length - 1];
                  if (match) {
                    setSourceKind("chart_document");
                    setSourceId(match.id);
                  }
                } catch (err) {
                  setFormError(err instanceof Error ? err.message : "Upload failed");
                } finally {
                  setUploading(false);
                  e.target.value = "";
                }
              }}
            />
          </label>
          {uploading && <p className="muted">Uploading source...</p>}
        </>
      )}
      <label className="field">
        Evidence text
        <input name="evidence" required placeholder={hints.evidencePlaceholder} />
      </label>
      <label className="field">
        Attestation
        <input name="attestation" required minLength={10} placeholder={hints.attestationPlaceholder} />
      </label>
      {formError && <p className="badge amber">{formError}</p>}
      <div className="row">
        <button className="btn" type="submit" disabled={!canSave}>
          Save answer
        </button>
        <button className="btn secondary" type="button" onClick={() => setOpen(false)}>
          Cancel
        </button>
      </div>
    </form>
  );
}

function showValue(value: unknown) {
  if (value == null) return "No answer yet";
  if (typeof value === "boolean") return value ? "Yes" : "No";
  if (typeof value === "string") {
    const low = value.trim().toLowerCase();
    if (["true", "yes", "y", "1"].includes(low)) return "Yes";
    if (["false", "no", "n", "0"].includes(low)) return "No";
  }
  if (typeof value === "object") return JSON.stringify(value);
  return String(value);
}

function showsSubmitControls(status: string) {
  return status === "needs_info" || status === "ready_for_review" || status === "info_requested";
}

function eventTone(eventType: string) {
  if (eventType === "not_required" || eventType === "approved" || eventType === "verified") return "green";
  if (eventType === "info_requested" || eventType === "answer_rejected") return "amber";
  if (eventType === "submitted" || eventType === "in_review" || eventType === "checked") return "blue";
  return "gray";
}

function labelEvent(event: string) {
  return event.replaceAll("_", " ");
}
