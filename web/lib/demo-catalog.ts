import { promises as fs } from "fs";
import path from "path";

export type DemoPlan = {
  id: string;
  insurer: string;
  plan_name: string;
  plan_year: string;
  benefit_summary_id?: string;
  demo?: boolean;
};

export type DemoService = {
  label: string;
  codes: string[];
  kind: string;
  insurer?: string;
  plan_name?: string;
  plan_year?: string;
  pa_status?: string;
  demo?: boolean;
};

export type DemoPatient = {
  id: string;
  full_name: string;
  dob?: string | null;
  sex?: string | null;
  member_id?: string | null;
  synthetic: boolean;
  demo_set?: string;
  demo_tag?: string;
  insurance?: {
    insurer: string;
    plan_name: string;
    plan_year: string;
    member_id?: string;
    group_number?: string;
  } | null;
  suggested_order?: {
    order_text?: string;
    service_code?: string;
    service_category?: string;
    pa_required?: boolean | null;
    pa_status?: string;
  } | null;
};

function dataRoot(): string {
  return path.resolve(process.cwd(), "..", "data");
}

async function readJson<T>(filePath: string): Promise<T> {
  return JSON.parse(await fs.readFile(filePath, "utf8")) as T;
}

export async function loadDemoOrderCatalog(): Promise<{
  insurers: DemoPlan[];
  services: DemoService[];
  patients: DemoPatient[];
}> {
  const root = dataRoot();
  const plans = await readJson<
    {
      id: string;
      insurer: string;
      plan_name: string;
      plan_year: string;
      demo_services?: { label: string; codes: string[]; pa_status?: string }[];
    }[]
  >(path.join(root, "db", "coverage_plans.json"));

  const insurers: DemoPlan[] = plans.map((p) => ({
    id: p.id,
    insurer: p.insurer,
    plan_name: p.plan_name,
    plan_year: p.plan_year || "",
    benefit_summary_id: p.id,
    demo: true,
  }));

  const services: DemoService[] = [];
  for (const plan of plans) {
    for (const svc of plan.demo_services || []) {
      services.push({
        label: svc.label,
        codes: svc.codes || [],
        kind: "coverage",
        insurer: plan.insurer,
        plan_name: plan.plan_name,
        plan_year: plan.plan_year || "",
        pa_status: svc.pa_status,
        demo: true,
      });
    }
  }

  const basePatients = await readJson<
    {
      id: string;
      full_name: string;
      dob?: string;
      sex?: string;
      member_id?: string;
      synthetic?: boolean;
    }[]
  >(path.join(root, "db", "patients.json"));

  const insuranceRows = await readJson<
    {
      patient_id: string;
      coverage_plan_id: string;
      member_id?: string;
      group_number?: string;
    }[]
  >(path.join(root, "db", "patient_insurance.json"));

  const planById = new Map(plans.map((p) => [p.id, p]));
  const patients: DemoPatient[] = basePatients.map((p) => {
    const link = insuranceRows.find((r) => r.patient_id === p.id);
    const plan = link ? planById.get(link.coverage_plan_id) : undefined;
    return {
      id: p.id,
      full_name: p.full_name,
      dob: p.dob,
      sex: p.sex,
      member_id: p.member_id,
      synthetic: p.synthetic !== false,
      insurance: plan
        ? {
            insurer: plan.insurer,
            plan_name: plan.plan_name,
            plan_year: plan.plan_year || "",
            member_id: link?.member_id || p.member_id,
            group_number: link?.group_number,
          }
        : null,
      suggested_order: null,
    };
  });

  // UnitedHealthcare dedicated demo set
  const uhcDir = path.join(root, "demo", "uhc-patients");
  try {
    const index = await readJson<{ patients: string[] }>(path.join(uhcDir, "index.json"));
    for (const file of index.patients || []) {
      const row = await readJson<{
        id: string;
        full_name: string;
        dob?: string;
        sex?: string;
        member_id?: string;
        synthetic?: boolean;
        demo_set?: string;
        demo_tag?: string;
        insurance?: DemoPatient["insurance"];
        suggested_order?: DemoPatient["suggested_order"];
      }>(path.join(uhcDir, file));
      // Prefer the dedicated UHC file if the same id already exists in base patients.
      const existing = patients.findIndex((p) => p.id === row.id || p.full_name === row.full_name);
      const shaped: DemoPatient = {
        id: row.id,
        full_name: row.full_name,
        dob: row.dob,
        sex: row.sex,
        member_id: row.member_id,
        synthetic: row.synthetic !== false,
        demo_set: row.demo_set || "uhc-patients",
        demo_tag: row.demo_tag,
        insurance: row.insurance || null,
        suggested_order: row.suggested_order || null,
      };
      if (existing >= 0) patients[existing] = shaped;
      else patients.push(shaped);
    }
  } catch {
    // Folder optional at runtime.
  }

  return { insurers, services, patients };
}
