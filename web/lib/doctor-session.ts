import { cookies } from "next/headers";
import type { DoctorProfile } from "@/lib/json-db";

export const DOCTOR_SESSION_COOKIE = "clearpath_doctor_session";
const MAX_AGE_SECONDS = 60 * 60 * 12; // 12 hours

export type DoctorSession = {
  session_id: string;
  user_id: string;
  provider_id: string;
  display_name: string;
  npi: string;
  full_name: string;
  specialty?: string;
  credentials?: string;
  phone?: string;
  email?: string;
  tax_id?: string;
  clinic_id?: string | null;
  clinic_name?: string | null;
  clinic_phone?: string | null;
  clinic_npi?: string | null;
  clinic_address?: string | null;
  created_at: string;
};

function formatAddress(address?: {
  line1?: string;
  city?: string;
  state?: string;
  postal_code?: string;
} | null): string | null {
  if (!address) return null;
  const cityLine = [address.city, address.state].filter(Boolean).join(", ");
  const parts = [address.line1, cityLine, address.postal_code].filter(Boolean);
  return parts.length ? parts.join(" · ") : null;
}

export function profileToSession(profile: DoctorProfile, sessionId: string): DoctorSession {
  return {
    session_id: sessionId,
    user_id: profile.user_id,
    provider_id: profile.provider_id,
    display_name: profile.display_name,
    npi: profile.npi,
    full_name: profile.full_name,
    specialty: profile.specialty,
    credentials: profile.credentials,
    phone: profile.phone,
    email: profile.email,
    tax_id: profile.tax_id,
    clinic_id: profile.clinic?.id || null,
    clinic_name: profile.clinic?.name || null,
    clinic_phone: profile.clinic?.phone || null,
    clinic_npi: profile.clinic?.npi || null,
    clinic_address: formatAddress(profile.clinic?.address),
    created_at: new Date().toISOString().replace(/\.\d{3}Z$/, "Z"),
  };
}

function cookieSecure(): boolean {
  return process.env.NODE_ENV === "production" || process.env.VERCEL === "1";
}

export async function setDoctorSessionCookie(session: DoctorSession): Promise<void> {
  const jar = await cookies();
  jar.set(DOCTOR_SESSION_COOKIE, JSON.stringify(session), {
    httpOnly: true,
    sameSite: "lax",
    secure: cookieSecure(),
    path: "/",
    maxAge: MAX_AGE_SECONDS,
  });
}

export async function clearDoctorSessionCookie(): Promise<void> {
  const jar = await cookies();
  jar.delete(DOCTOR_SESSION_COOKIE);
}

export async function readDoctorSession(): Promise<DoctorSession | null> {
  const jar = await cookies();
  const raw = jar.get(DOCTOR_SESSION_COOKIE)?.value;
  if (!raw) return null;
  try {
    const parsed = JSON.parse(raw) as DoctorSession;
    if (!parsed?.provider_id || !parsed?.session_id) return null;
    return parsed;
  } catch {
    return null;
  }
}
