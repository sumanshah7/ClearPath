export function pretty(value?: string | null) {
  if (!value) return "";
  const iso = value.length === 10 ? `${value}T00:00:00` : value;
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleDateString("en-US", { month: "short", day: "numeric" });
}

export function percent(readiness: number) {
  return `${Math.round(readiness * 100)}%`;
}

const TONE: Record<string, "amber" | "blue" | "green" | "gray"> = {
  missing: "amber",
  unclear: "amber",
  needs_info: "amber",
  info_requested: "amber",
  rejected: "amber",
  pending_review: "amber",
  ready_for_review: "blue",
  auto_approved: "blue",
  met: "green",
  verified: "green",
  accepted: "green",
  edited: "green",
  approved: "green",
  not_required: "green",
  matching: "gray",
  checking: "gray",
  submitted: "gray",
  in_review: "gray",
  ingesting: "blue",
  started: "blue",
  completed: "green",
  cache_hit: "green",
  failed: "amber",
  timeout: "amber",
  resumed: "blue",
  skipped: "gray",
  paused: "amber",
  draft: "amber",
  live: "green",
  archived: "gray",
  indexed: "gray",
};

export function tone(status: string) {
  return TONE[status] || "gray";
}

export function label(status: string) {
  return status.replaceAll("_", " ");
}

export function documentRoleLabel(role: string) {
  if (role === "benefit_summary") return "Evidence of Coverage / plan";
  if (role === "clinical_policy") return "Clinical policy";
  if (role === "drug_criteria") return "Drug criteria";
  return label(role);
}
