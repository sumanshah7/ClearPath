"use client";

import type { DoctorSessionInfo } from "@/components/pa/DoctorAuthProvider";

export function DoctorProfileCard({
  doctor,
  onLogout,
}: {
  doctor: DoctorSessionInfo;
  onLogout?: () => void;
}) {
  const facts: { label: string; value?: string | null }[] = [
    { label: "NPI", value: doctor.npi },
    { label: "Specialty", value: doctor.specialty },
    { label: "Credentials", value: doctor.credentials },
    { label: "Email", value: doctor.email },
    { label: "Phone", value: doctor.phone },
    { label: "Tax ID", value: doctor.tax_id },
    { label: "Clinic", value: doctor.clinic_name },
    { label: "Clinic NPI", value: doctor.clinic_npi },
    { label: "Clinic phone", value: doctor.clinic_phone },
    { label: "Clinic address", value: doctor.clinic_address },
  ];

  return (
    <section className="card" style={{ marginBottom: "1rem" }}>
      <div className="row" style={{ justifyContent: "space-between", alignItems: "flex-start", gap: "1rem" }}>
        <div>
          <p className="kicker">Signed in</p>
          <h2 className="title" style={{ marginBottom: 4 }}>
            {doctor.full_name}
          </h2>
          <p className="muted">
            {doctor.specialty ? `${doctor.specialty}` : "Clinician"}
            {doctor.credentials ? ` · ${doctor.credentials}` : ""}
            {doctor.npi ? ` · NPI ${doctor.npi}` : ""}
          </p>
        </div>
        {onLogout && (
          <button type="button" className="btn secondary" onClick={onLogout}>
            Sign out
          </button>
        )}
      </div>
      <dl
        className="row"
        style={{
          marginTop: "1rem",
          gap: "0.75rem 1.5rem",
          flexWrap: "wrap",
        }}
      >
        {facts
          .filter((f) => f.value)
          .map((f) => (
            <div key={f.label} style={{ minWidth: "10rem" }}>
              <dt className="muted" style={{ fontSize: "0.8rem" }}>
                {f.label}
              </dt>
              <dd style={{ margin: 0 }}>{f.value}</dd>
            </div>
          ))}
      </dl>
    </section>
  );
}
