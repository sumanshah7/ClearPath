import { promises as fs } from "fs";
import path from "path";

export type Address = {
  line1?: string;
  city?: string;
  state?: string;
  postal_code?: string;
};

export type ProviderRow = {
  id: string;
  npi: string;
  full_name: string;
  first_name?: string;
  last_name?: string;
  specialty?: string;
  credentials?: string;
  clinic_id?: string;
  phone?: string;
  email?: string;
  tax_id?: string;
  active?: boolean;
  synthetic?: boolean;
};

export type ClinicRow = {
  id: string;
  name: string;
  npi?: string;
  phone?: string;
  fax?: string;
  address?: Address;
};

export type UserRow = {
  id: string;
  login_name: string;
  display_name: string;
  role: string;
  portal: string;
  provider_id?: string | null;
  patient_id?: string | null;
  payer_id?: string | null;
  npi?: string | null;
  active?: boolean;
};

export type DoctorProfile = {
  user_id: string;
  login_name: string;
  display_name: string;
  role: string;
  portal: string;
  provider_id: string;
  npi: string;
  full_name: string;
  first_name?: string;
  last_name?: string;
  specialty?: string;
  credentials?: string;
  phone?: string;
  email?: string;
  tax_id?: string;
  clinic?: {
    id: string;
    name: string;
    npi?: string;
    phone?: string;
    address?: Address;
  } | null;
};

function dbDir(): string {
  return path.resolve(process.cwd(), "..", "data", "db");
}

async function readCollection<T>(name: string): Promise<T[]> {
  const file = path.join(dbDir(), `${name}.json`);
  const raw = await fs.readFile(file, "utf8");
  const data = JSON.parse(raw);
  if (!Array.isArray(data)) throw new Error(`${name}.json must be an array`);
  return data as T[];
}

function norm(value: string | null | undefined): string {
  return (value || "").trim().toLowerCase().replace(/\s+/g, " ");
}

export async function listClinicianUsers(): Promise<UserRow[]> {
  const users = await readCollection<UserRow>("users");
  return users.filter((u) => u.role === "clinician" && u.active !== false);
}

export async function findDoctor(idOrName: string): Promise<DoctorProfile | null> {
  const needle = norm(idOrName);
  if (!needle) return null;

  const [users, providers, clinics] = await Promise.all([
    readCollection<UserRow>("users"),
    readCollection<ProviderRow>("providers"),
    readCollection<ClinicRow>("clinics"),
  ]);

  const clinicById = new Map(clinics.map((c) => [c.id, c]));

  function profileFrom(user: UserRow, provider: ProviderRow): DoctorProfile {
    const clinic = provider.clinic_id ? clinicById.get(provider.clinic_id) : undefined;
    return {
      user_id: user.id,
      login_name: user.login_name,
      display_name: user.display_name || provider.full_name,
      role: "clinician",
      portal: "doctor",
      provider_id: provider.id,
      npi: provider.npi || user.npi || "",
      full_name: provider.full_name,
      first_name: provider.first_name,
      last_name: provider.last_name,
      specialty: provider.specialty,
      credentials: provider.credentials,
      phone: provider.phone,
      email: provider.email,
      tax_id: provider.tax_id,
      clinic: clinic
        ? {
            id: clinic.id,
            name: clinic.name,
            npi: clinic.npi,
            phone: clinic.phone,
            address: clinic.address,
          }
        : null,
    };
  }

  for (const user of users) {
    if (user.role !== "clinician" || user.active === false) continue;
    const candidates = [user.id, user.login_name, user.display_name, user.npi, user.provider_id];
    const exact = candidates.some((c) => c && norm(String(c)) === needle);
    const display = norm(user.display_name);
    const soft = display && (needle === display.split(" ").pop() || display.includes(needle));
    if (!exact && !soft) continue;
    const provider = providers.find((p) => p.id === user.provider_id);
    if (!provider || provider.active === false) continue;
    return profileFrom(user, provider);
  }

  for (const provider of providers) {
    if (provider.active === false) continue;
    const candidates = [provider.id, provider.full_name, provider.npi, provider.last_name, provider.email];
    if (!candidates.some((c) => c && norm(String(c)) === needle)) continue;
    const user =
      users.find((u) => u.provider_id === provider.id && u.role === "clinician") ||
      ({
        id: `user-from-provider-${provider.id}`,
        login_name: norm(provider.full_name),
        display_name: provider.full_name,
        role: "clinician",
        portal: "doctor",
        provider_id: provider.id,
        npi: provider.npi,
        active: true,
      } as UserRow);
    return profileFrom(user, provider);
  }

  return null;
}

export async function appendSession(session: Record<string, unknown>): Promise<void> {
  const file = path.join(dbDir(), "sessions.json");
  let rows: Record<string, unknown>[] = [];
  try {
    rows = JSON.parse(await fs.readFile(file, "utf8"));
    if (!Array.isArray(rows)) rows = [];
  } catch {
    rows = [];
  }
  rows.push(session);
  await fs.writeFile(file, `${JSON.stringify(rows, null, 2)}\n`);

  const auditFile = path.join(dbDir(), "audit_log.json");
  let audit: Record<string, unknown>[] = [];
  try {
    audit = JSON.parse(await fs.readFile(auditFile, "utf8"));
    if (!Array.isArray(audit)) audit = [];
  } catch {
    audit = [];
  }
  audit.push({
    id: crypto.randomUUID(),
    action: "login",
    collection: "sessions",
    target_id: session.id,
    detail: { user_id: session.user_id, portal: "doctor" },
    created_at: new Date().toISOString().replace(/\.\d{3}Z$/, "Z"),
  });
  await fs.writeFile(auditFile, `${JSON.stringify(audit.slice(-500), null, 2)}\n`);
}
