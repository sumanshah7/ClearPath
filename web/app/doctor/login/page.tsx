"use client";

import { FormEvent, Suspense, useEffect, useState } from "react";
import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";

type ClinicianHint = {
  id: string;
  login_name: string;
  display_name: string;
  npi?: string | null;
};

function DoctorLoginForm() {
  const router = useRouter();
  const search = useSearchParams();
  const nextPath = search.get("next") || "/doctor/upload";

  const [idOrName, setIdOrName] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [clinicians, setClinicians] = useState<ClinicianHint[]>([]);

  useEffect(() => {
    fetch("/api/auth/doctor/me", { cache: "no-store" })
      .then((res) => {
        if (res.ok) router.replace(nextPath);
      })
      .catch(() => undefined);

    fetch("/api/auth/doctor/clinicians", { cache: "no-store" })
      .then((res) => res.json())
      .then((data) => setClinicians(data.clinicians || []))
      .catch(() => undefined);
  }, [router, nextPath]);

  async function onSubmit(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError("");
    try {
      const res = await fetch("/api/auth/doctor/login", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ id_or_name: idOrName }),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) {
        throw new Error(data?.error?.message || "Could not sign in");
      }
      router.replace(nextPath.startsWith("/doctor") ? nextPath : "/doctor/upload");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not sign in");
      setBusy(false);
    }
  }

  return (
    <main>
      <header className="page-head">
        <p className="kicker">Clinician portal</p>
        <h1 className="title">Doctor sign in</h1>
        <p className="lead">
          Sign in with your name, login id, or NPI. After that you can upload reports and open the order desk.
        </p>
      </header>

      {error && <p className="badge amber">{error}</p>}

      <form className="card form-card" onSubmit={onSubmit} style={{ maxWidth: "28rem" }}>
        <label className="field">
          Name, login id, or NPI
          <input
            value={idOrName}
            onChange={(e) => setIdOrName(e.target.value)}
            placeholder="e.g. ana.reyes or 1679651234"
            autoComplete="username"
            required
          />
        </label>
        <button className="btn" disabled={busy || !idOrName.trim()}>
          {busy ? "Signing in..." : "Sign in"}
        </button>
      </form>

      {clinicians.length > 0 && (
        <section className="card" style={{ marginTop: "1rem", maxWidth: "28rem" }}>
          <p className="title" style={{ fontSize: "1rem" }}>
            Demo clinicians
          </p>
          <p className="muted" style={{ marginBottom: "0.75rem" }}>
            Click one to fill the form.
          </p>
          <ul className="stack" style={{ listStyle: "none", padding: 0, margin: 0, gap: "0.5rem" }}>
            {clinicians.map((c) => (
              <li key={c.id}>
                <button
                  type="button"
                  className="btn secondary"
                  style={{ width: "100%", justifyContent: "flex-start" }}
                  onClick={() => setIdOrName(c.login_name)}
                >
                  {c.display_name}
                  {c.npi ? ` · NPI ${c.npi}` : ""}
                  <span className="muted" style={{ marginLeft: "auto" }}>
                    {c.login_name}
                  </span>
                </button>
              </li>
            ))}
          </ul>
        </section>
      )}

      <p style={{ marginTop: "1rem" }}>
        <Link className="topbar-link" href="/">
          Back to home
        </Link>
      </p>
    </main>
  );
}

export default function DoctorLoginPage() {
  return (
    <Suspense
      fallback={
        <main>
          <p className="muted" style={{ padding: "2rem" }}>
            Loading sign in...
          </p>
        </main>
      }
    >
      <DoctorLoginForm />
    </Suspense>
  );
}
