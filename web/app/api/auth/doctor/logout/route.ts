import { NextResponse } from "next/server";
import { clearDoctorSessionCookie } from "@/lib/doctor-session";

export const runtime = "nodejs";

/** Absolute PA engine base for server-side fetches. */
function engineBase(): string {
  const env = (process.env.PA_ENGINE_URL || process.env.NEXT_PUBLIC_PA_ENGINE_URL || "").trim();
  if (env.startsWith("http://") || env.startsWith("https://")) {
    return env.replace(/\/$/, "");
  }
  if (process.env.VERCEL_URL) {
    return `https://${process.env.VERCEL_URL}/engine`;
  }
  return "http://127.0.0.1:8000";
}

/**
 * HITL demo: reset Gate-1 review_state to pending_review so the next session
 * starts with Needs review again. Does not touch policy live status or PA history.
 */
async function resetReviewQueueOnLogout(): Promise<void> {
  const url = `${engineBase()}/policies/reset-review-queue`;
  try {
    await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ actor: "doctor_logout" }),
      cache: "no-store",
    });
  } catch {
    // Engine may be down; still clear the doctor session cookie.
  }
}

export async function POST() {
  await resetReviewQueueOnLogout();
  await clearDoctorSessionCookie();
  return NextResponse.json({ ok: true });
}
