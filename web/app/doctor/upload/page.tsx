"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";
import Link from "next/link";
import { api } from "@/lib/api";
import { useDoctorAuth } from "@/components/pa/DoctorAuthProvider";

type UploadResult = {
  upload_id: string;
  extraction_status: "success" | "partial" | "failed";
  order_desk: {
    patient_id: string;
    order_text: string;
    service_code: string;
    clinical_notes: string;
    source_upload_id: string;
  };
};

const DEMO_REPORTS: {
  href: string;
  name: string;
  service: string;
  pa: "required" | "conditional" | "not_required";
}[] = [
  {
    href: "/demo/uhc-reports/01-robert-nguyen-outpatient-diagnostic-tests.pdf",
    name: "Robert Nguyen",
    service: "Outpatient diagnostic tests",
    pa: "required",
  },
  {
    href: "/demo/uhc-reports/02-priya-sharma-outpatient-diagnostic-tests-xrays.pdf",
    name: "Priya Sharma",
    service: "Outpatient diagnostic tests - X-rays",
    pa: "required",
  },
  {
    href: "/demo/uhc-reports/03-marcus-bennett-chiropractic-services.pdf",
    name: "Marcus Bennett",
    service: "Chiropractic services",
    pa: "required",
  },
  {
    href: "/demo/uhc-reports/04-helen-park-annual-wellness-visit.pdf",
    name: "Helen Park",
    service: "Annual wellness visit",
    pa: "not_required",
  },
  {
    href: "/demo/uhc-reports/05-linda-okonkwo-ambulance-services.pdf",
    name: "Linda Okonkwo",
    service: "Ambulance services",
    pa: "conditional",
  },
  {
    href: "/demo/uhc-reports/06-nina-castillo-outpatient-rehabilitation.pdf",
    name: "Nina Castillo",
    service: "Outpatient rehabilitation services",
    pa: "required",
  },
];

function paBadge(pa: "required" | "conditional" | "not_required") {
  if (pa === "required") return <span className="badge amber">PA required</span>;
  if (pa === "conditional") return <span className="badge blue">Conditional</span>;
  return <span className="badge gray">No PA</span>;
}

export default function DoctorUploadPage() {
  const router = useRouter();
  const { doctor, logout } = useDoctorAuth();
  const [file, setFile] = useState<File | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [result, setResult] = useState<UploadResult | null>(null);

  if (!doctor) return null;

  async function onSubmit(event: React.FormEvent) {
    event.preventDefault();
    if (!file || !doctor) return;
    setBusy(true);
    setError("");
    setResult(null);
    try {
      const body = new FormData();
      body.set("file", file);
      body.set("provider_id", doctor.provider_id);
      const data = await api<UploadResult>("/uploads/patient-report", { method: "POST", body });
      setResult(data);
      const params = new URLSearchParams();
      params.set("upload_id", data.upload_id);
      router.push(`/doctor?${params.toString()}`);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Upload failed");
      setBusy(false);
    }
  }

  return (
    <main className="upload-page">
      <header className="page-head">
        <p className="kicker">Clinician</p>
        <h1 className="title">Upload patient report</h1>
        <p className="lead">
          Drop a clinical PDF. ClearPath extracts the patient and order, then opens the Order Desk so you can run
          coverage.
        </p>
      </header>

      <section className="card upload-clinician-bar">
        <div>
          <p className="kicker-sm">Signed in</p>
          <p className="upload-clinician-name">{doctor.full_name}</p>
          <p className="muted" style={{ margin: 0 }}>
            {[doctor.specialty, doctor.credentials, doctor.npi ? `NPI ${doctor.npi}` : null]
              .filter(Boolean)
              .join(" · ")}
          </p>
        </div>
        <div className="row" style={{ gap: "0.5rem" }}>
          <Link className="btn secondary" href="/doctor">
            Order desk
          </Link>
          {logout && (
            <button type="button" className="btn secondary" onClick={logout}>
              Sign out
            </button>
          )}
        </div>
      </section>

      {error && (
        <p className="badge amber" style={{ marginBottom: "1rem" }}>
          {error}
        </p>
      )}

      <div className="upload-layout">
        <form className="card form-card upload-primary" onSubmit={onSubmit}>
          <p className="kicker-sm">Step 1</p>
          <h2 className="title" style={{ marginBottom: "0.35rem" }}>
            Choose a report
          </h2>
          <p className="muted" style={{ marginTop: 0, marginBottom: "1rem" }}>
            PDF preferred. Images and plain text also work.
          </p>

          <label className="field">
            Ordering clinician
            <input
              value={`${doctor.full_name}${doctor.specialty ? ` · ${doctor.specialty}` : ""}`}
              disabled
              readOnly
            />
          </label>

          <label className="field">
            Patient report
            <span className={`file-drop${file ? " has-file" : ""}`}>
              <span className="file-drop-icon" aria-hidden>
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
                  <path d="M12 16V7" />
                  <path d="M8.5 10.5 12 7l3.5 3.5" />
                  <path d="M5 17.5v1A1.5 1.5 0 0 0 6.5 20h11a1.5 1.5 0 0 0 1.5-1.5v-1" />
                </svg>
              </span>
              <span className="file-drop-copy">
                <strong>{file ? "Change file" : "Choose a file"}</strong>
                <span>{file ? file.name : "PDF, image, or scanned report"}</span>
              </span>
              <input
                type="file"
                accept="application/pdf,image/*,.txt,.png,.jpg,.jpeg"
                required
                onChange={(e) => setFile(e.target.files?.[0] || null)}
              />
            </span>
          </label>

          <div className="row" style={{ marginTop: "0.25rem" }}>
            <button className="btn" disabled={busy || !file}>
              {busy ? "Extracting…" : "Extract and open Order Desk"}
            </button>
            <Link className="btn secondary" href="/doctor">
              Skip upload
            </Link>
          </div>

          {result && (
            <p className="badge green" style={{ marginTop: "1rem" }}>
              Extraction {result.extraction_status}. Opening Order Desk…
            </p>
          )}
        </form>

        <aside className="card upload-demo-card">
          <p className="kicker-sm">Demo samples</p>
          <h2 className="title" style={{ marginBottom: "0.35rem" }}>
            UHC Dual Complete OH-S3
          </h2>
          <p className="muted" style={{ marginTop: 0, marginBottom: "0.85rem" }}>
            Synthetic charts that match the live catalog. Download, then upload on the left. Not real PHI.
          </p>

          <ul className="demo-report-list">
            {DEMO_REPORTS.map((row) => (
              <li key={row.href}>
                <a href={row.href} download>
                  <span className="demo-report-name">{row.name}</span>
                  <span className="demo-report-service">{row.service}</span>
                </a>
                {paBadge(row.pa)}
              </li>
            ))}
          </ul>

          <p className="muted" style={{ marginTop: "0.85rem", marginBottom: 0, fontSize: "0.8rem" }}>
            On disk: <code>demo-patient-reports/</code>
          </p>
        </aside>
      </div>
    </main>
  );
}
