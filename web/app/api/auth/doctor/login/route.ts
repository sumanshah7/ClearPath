import { NextResponse } from "next/server";
import { appendSession, findDoctor } from "@/lib/json-db";
import { profileToSession, setDoctorSessionCookie } from "@/lib/doctor-session";
import { API } from "@/lib/api";

export const runtime = "nodejs";

async function syncProviderToEngine(profile: {
  provider_id: string;
  full_name: string;
  specialty?: string;
}): Promise<void> {
  const url = `${API.startsWith("http") ? API : "http://127.0.0.1:8000"}/providers`;
  try {
    await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        id: profile.provider_id,
        full_name: profile.full_name,
        specialty: profile.specialty || null,
      }),
      cache: "no-store",
    });
  } catch {
    // Engine may be down; login still succeeds for the portal UI.
  }
}

export async function POST(request: Request) {
  const body = await request.json().catch(() => ({}));
  const idOrName = String(body.id_or_name || body.idOrName || "").trim();
  if (!idOrName) {
    return NextResponse.json(
      { error: { message: "Enter your name, login id, or NPI." } },
      { status: 400 },
    );
  }

  const profile = await findDoctor(idOrName);
  if (!profile) {
    return NextResponse.json(
      { error: { message: "No clinician matches that id or name." } },
      { status: 401 },
    );
  }

  const sessionId = crypto.randomUUID();
  const session = profileToSession(profile, sessionId);
  await appendSession({
    id: sessionId,
    user_id: profile.user_id,
    display_name: profile.display_name,
    role: "clinician",
    portal: "doctor",
    provider_id: profile.provider_id,
    npi: profile.npi,
    created_at: session.created_at,
    synthetic: true,
  });
  await setDoctorSessionCookie(session);
  await syncProviderToEngine(profile);

  return NextResponse.json({ ok: true, doctor: session });
}
