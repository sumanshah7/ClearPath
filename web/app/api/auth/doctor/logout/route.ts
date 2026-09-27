import { NextResponse } from "next/server";
import { clearDoctorSessionCookie } from "@/lib/doctor-session";

export const runtime = "nodejs";

export async function POST() {
  await clearDoctorSessionCookie();
  return NextResponse.json({ ok: true });
}
