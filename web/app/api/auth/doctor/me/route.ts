import { NextResponse } from "next/server";
import { readDoctorSession } from "@/lib/doctor-session";

export const runtime = "nodejs";

export async function GET() {
  const session = await readDoctorSession();
  if (!session) {
    return NextResponse.json({ error: { message: "Not signed in." } }, { status: 401 });
  }
  return NextResponse.json({ doctor: session });
}
