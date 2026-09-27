"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";
import Link from "next/link";
import { api } from "@/lib/api";
import { useDoctorAuth } from "@/components/pa/DoctorAuthProvider";
import { DoctorProfileCard } from "@/components/pa/DoctorProfileCard";

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
    <main>
      <header className="page-head">
        <p className="kicker">Order desk</p>
        <h1 className="title">Upload patient report</h1>
        <p className="lead">
          You are signed in. Review your profile, then drop a clinical PDF or report. ClearPath extracts what it can
          and opens the Order Desk.
        </p>
      </header>

      <DoctorProfileCard doctor={doctor} onLogout={logout} />

      {error && <p className="badge amber">{error}</p>}

      <section className="card" style={{ maxWidth: "36rem", marginBottom: "1rem" }}>
        <h2 className="section-title" style={{ marginTop: 0 }}>
          Demo UHC reports (synthetic)
        </h2>
        <p className="muted" style={{ marginBottom: "0.75rem" }}>
          Download a sample Dual Complete OH-S3 chart, then upload it below. Not real PHI.
        </p>
        <ul className="plain-list" style={{ margin: 0, paddingLeft: "1.1rem", lineHeight: 1.55 }}>
          <li>
            <a href="/demo/uhc-reports/patient-01-robert-nguyen-72148-report.pdf">Robert Nguyen</a> — MRI lumbar (72148) · PA required
          </li>
          <li>
            <a href="/demo/uhc-reports/patient-02-priya-sharma-72100-report.pdf">Priya Sharma</a> — X-ray lumbar (72100) · no PA
          </li>
          <li>
            <a href="/demo/uhc-reports/patient-03-marcus-bennett-72141-report.pdf">Marcus Bennett</a> — MRI cervical (72141) · PA required
          </li>
          <li>
            <a href="/demo/uhc-reports/patient-04-linda-okonkwo-97161-report.pdf">Linda Okonkwo</a> — PT evaluation (97161) · conditional
          </li>
          <li>
            <a href="/demo/uhc-reports/patient-06-helen-park-99213-report.pdf">Helen Park</a> — Office visit (99213) · no PA
          </li>
          <li>
            <a href="/demo/uhc-reports/patient-08-nina-castillo-70551-report.pdf">Nina Castillo</a> — MRI brain (70551) · PA required
          </li>
        </ul>
      </section>

      <form className="card form-card" onSubmit={onSubmit} style={{ maxWidth: "36rem" }}>
        <label className="field">
          Ordering clinician
          <input value={`${doctor.full_name}${doctor.specialty ? ` | ${doctor.specialty}` : ""}`} disabled readOnly />
        </label>
        <label className="field">
          Patient report
          <span className="file-drop">
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
        <div className="row">
          <button className="btn" disabled={busy || !file}>
            {busy ? "Extracting..." : "Extract and open Order Desk"}
          </button>
          <Link className="btn secondary" href="/doctor">
            Skip to Order Desk
          </Link>
        </div>
      </form>

      {result && (
        <p className="badge green" style={{ marginTop: "1rem" }}>
          Extraction {result.extraction_status}. Opening Order Desk...
        </p>
      )}
    </main>
  );
}
