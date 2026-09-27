import { NextResponse } from "next/server";
import { listClinicianUsers } from "@/lib/json-db";

export const runtime = "nodejs";

export async function GET() {
  const clinicians = await listClinicianUsers();
  return NextResponse.json({
    clinicians: clinicians.map((c) => ({
      id: c.id,
      login_name: c.login_name,
      display_name: c.display_name,
      npi: c.npi,
    })),
  });
}
