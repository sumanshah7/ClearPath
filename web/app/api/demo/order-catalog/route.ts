import { NextResponse } from "next/server";
import { loadDemoOrderCatalog } from "@/lib/demo-catalog";

export const runtime = "nodejs";

export async function GET() {
  const catalog = await loadDemoOrderCatalog();
  return NextResponse.json(catalog);
}
