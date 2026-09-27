import Link from "next/link";
import { BrandMark } from "@/components/pa/BrandMark";
import { PaPathPreview } from "@/components/pa/PaPathPreview";

export default function Home() {
  return (
    <main className="landing-page">
      <header className="topbar">
        <Link href="/" className="brand-lockup">
          <BrandMark size={52} />
          <span className="brand-word">ClearPath</span>
        </Link>
        <nav className="topbar-nav" aria-label="Primary">
          <Link className="topbar-link" href="/admin/policies">
            Policies
          </Link>
          <Link className="topbar-link" href="/insurer">
            Insurer
          </Link>
          <Link className="btn" href="/doctor/login">
            Doctor sign in
          </Link>
        </nav>
      </header>

      <section className="hero">
        <div className="hero-copy">
          <h1 className="section-title">ClearPath</h1>
          <p className="hero-sub">Cut the wait between the order and the therapy.</p>
          <p className="lead">
            When prior auth stalls, days slip by and treatments slip with them. ClearPath helps
            clinicians clear coverage faster, with judgment still in their hands, so more patients
            reach the care that can change an outcome.
          </p>
          <div className="hero-actions">
            <Link className="btn" href="/doctor/login">
              Doctor sign in
            </Link>
            <Link className="btn secondary" href="/doctor/upload">
              Upload a report
            </Link>
          </div>
          <ul className="hero-points">
            <li>Know if PA is needed in seconds</li>
            <li>Only the questions that block care</li>
            <li>Clinician signs off before anything ships</li>
          </ul>
        </div>

        <PaPathPreview />
      </section>
    </main>
  );
}
