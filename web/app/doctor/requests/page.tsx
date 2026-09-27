"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { api } from "@/lib/api";
import { StatusBadge } from "@/components/pa/StatusBadge";
import { useDoctorAuth } from "@/components/pa/DoctorAuthProvider";

type RequestRow = {
  id: string;
  status: string;
  order_text?: string;
  service_code?: string;
  service_category?: string;
  insurer?: string;
  plan_name?: string;
  plan_year?: string;
  updated_at?: string;
  submitted_at?: string;
  patient?: { id?: string; full_name?: string };
};

const DECISION_STATUSES = new Set(["approved", "info_requested", "denied", "rejected"]);
const PENDING_STATUSES = new Set(["submitted", "in_review"]);

function bucketLabel(status: string): string {
  if (status === "approved") return "Approved";
  if (status === "info_requested") return "Additional info requested";
  if (status === "denied" || status === "rejected") return "Denied";
  if (PENDING_STATUSES.has(status)) return "Pending insurer decision";
  if (status === "not_required") return "PA not required";
  if (status === "ready_for_review") return "Ready to submit";
  if (status === "needs_info") return "Needs clinician answers";
  if (status === "matching") return "Matching service";
  if (status === "draft") return "Draft";
  return status.replace(/_/g, " ");
}

export default function DoctorRequestsPage() {
  const { doctor } = useDoctorAuth();
  const [rows, setRows] = useState<RequestRow[]>([]);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  function load() {
    setBusy(true);
    setError("");
    api<{ requests: RequestRow[] }>("/pa?queue=doctor")
      .then((data) => setRows(data.requests || []))
      .catch((err) => setError(err instanceof Error ? err.message : "Could not load requests"))
      .finally(() => setBusy(false));
  }

  useEffect(() => {
    load();
    const timer = setInterval(load, 3000);
    return () => clearInterval(timer);
  }, []);

  const decided = rows.filter((r) => DECISION_STATUSES.has(r.status));
  const pending = rows.filter((r) => PENDING_STATUSES.has(r.status));
  const open = rows.filter(
    (r) => !DECISION_STATUSES.has(r.status) && !PENDING_STATUSES.has(r.status),
  );

  return (
    <main>
      <header className="page-head">
        <div className="row" style={{ justifyContent: "space-between", alignItems: "flex-start" }}>
          <div>
            <p className="kicker">Clinician desk</p>
            <h1 className="title">Request status</h1>
            <p className="lead">
              Live status for prior-auth requests from this session
              {doctor?.full_name ? ` · ${doctor.full_name}` : ""}. Updates when the insurer queue records a decision — same PARequest data, no second copy.
            </p>
          </div>
          <button type="button" className="btn secondary" onClick={load} disabled={busy}>
            {busy ? "Refreshing..." : "Refresh"}
          </button>
        </div>
      </header>
      {error && <p className="badge amber">{error}</p>}

      <QueueSection title="Insurer decisions" empty="No approvals, denials, or info requests yet." rows={decided} />
      <QueueSection title="Waiting on insurer" empty="Nothing pending a payer decision." rows={pending} />
      <QueueSection title="Open on clinician side" empty="No open drafts or checklists." rows={open} />
    </main>
  );
}

function QueueSection({ title, empty, rows }: { title: string; empty: string; rows: RequestRow[] }) {
  return (
    <section className="section-gap">
      <p className="title">
        {title} <span className="muted">({rows.length})</span>
      </p>
      {rows.length === 0 && <p className="muted">{empty}</p>}
      <div className="stack" style={{ marginTop: "0.75rem" }}>
        {rows.map((row) => (
          <article key={row.id} className="card">
            <div className="row" style={{ justifyContent: "space-between" }}>
              <div>
                <p className="title" style={{ marginBottom: 4 }}>
                  {row.patient?.full_name || "Patient"} · {row.order_text || row.service_category || "Order"}
                </p>
                <p className="muted">
                  {[row.insurer, row.plan_name, row.service_code].filter(Boolean).join(" · ")}
                </p>
                <p className="muted" style={{ marginTop: 4 }}>
                  {bucketLabel(row.status)}
                  {row.updated_at ? ` · updated ${row.updated_at}` : ""}
                </p>
              </div>
              <div className="row">
                <StatusBadge status={row.status} />
                <Link className="btn" href={`/doctor/pa/${row.id}`}>
                  Open
                </Link>
              </div>
            </div>
          </article>
        ))}
      </div>
    </section>
  );
}
