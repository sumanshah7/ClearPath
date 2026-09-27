"use client";

import { useEffect, useMemo, useState } from "react";
import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { api, API } from "@/lib/api";
import { useDoctorAuth } from "@/components/pa/DoctorAuthProvider";
import { DoctorProfileCard } from "@/components/pa/DoctorProfileCard";

type Patient = {
  id: string;
  full_name: string;
  synthetic: boolean;
  dob?: string | null;
  sex?: string | null;
  member_id?: string | null;
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
type Provider = { id: string; full_name: string; specialty?: string };
type Plan = { insurer: string; plan_name: string; plan_year: string; benefit_summary_id?: string; demo?: boolean };
type Service = {
  label: string;
  codes: string[];
  kind: string;
  insurer?: string;
  plan_name?: string;
  plan_year?: string;
  demo?: boolean;
};

type ServiceOption = { key: string; label: string; code: string };

function looksLikeServiceCode(value: string): boolean {
  const c = value.trim();
  return (
    /^\d{4,5}[A-Z]?$/i.test(c) ||
    /^[A-Z]\d{4}$/i.test(c) ||
    /^D\d{4}$/i.test(c)
  );
}

function codeFromLabel(label: string): string {
  const paren = label.match(/\((\d{4,5}[A-Z]?)\)/i);
  if (paren) return paren[1];
  const bare = label.match(/\b(\d{5})\b/);
  return bare ? bare[1] : "";
}

function yearsMatch(serviceYear?: string | null, planYear?: string | null): boolean {
  const a = serviceYear ?? "";
  const b = planYear ?? "";
  if (!a || !b) return true;
  return a === b;
}

function pickCodeForRow(row: Service): string {
  const codes = (row.codes || []).map(String).map((c) => c.trim()).filter(Boolean);
  const cpt = codes.find(looksLikeServiceCode);
  if (cpt) return cpt;
  if (codes[0]) return codes[0];
  return codeFromLabel(row.label || "");
}

function resolveServiceCode(label: string, rows: Service[]): string {
  if (!label) return "";
  const matches = rows.filter((s) => s.label === label);
  const ordered = [
    ...matches.filter((s) => s.kind === "coverage"),
    ...matches.filter((s) => s.kind !== "coverage"),
  ];
  for (const row of ordered) {
    const code = pickCodeForRow(row);
    if (code) return code;
  }
  return codeFromLabel(label);
}

function planIdentity(plan: Plan): string {
  return `${plan.insurer}|${plan.plan_name}|${plan.plan_year || ""}`;
}

function mergePlans(engine: Plan[], demo: Plan[]): Plan[] {
  const map = new Map<string, Plan>();
  for (const plan of demo) map.set(planIdentity(plan), { ...plan, demo: true });
  for (const plan of engine) map.set(planIdentity(plan), { ...plan, demo: false });
  return [...map.values()].sort((a, b) =>
    `${a.insurer} ${a.plan_name}`.localeCompare(`${b.insurer} ${b.plan_name}`),
  );
}

function mergeServices(engine: Service[], demo: Service[]): Service[] {
  if (engine.length === 0) return demo;
  const keys = new Set(
    engine.map((s) => `${s.insurer}|${s.plan_name}|${s.plan_year || ""}|${s.label}|${(s.codes || []).join(",")}`),
  );
  const extras = demo.filter(
    (s) => !keys.has(`${s.insurer}|${s.plan_name}|${s.plan_year || ""}|${s.label}|${(s.codes || []).join(",")}`),
  );
  return [...engine, ...extras];
}

function mergePatients(engine: Patient[], demo: Patient[]): Patient[] {
  const map = new Map<string, Patient>();
  for (const p of engine) {
    map.set(p.id, { ...p, synthetic: !!p.synthetic });
  }
  for (const p of demo) {
    const existing = map.get(p.id);
    map.set(p.id, {
      ...(existing || {}),
      ...p,
      synthetic: true,
      demo_tag: p.demo_tag || existing?.demo_tag,
      insurance: p.insurance || existing?.insurance || null,
      suggested_order: p.suggested_order || existing?.suggested_order || null,
    });
  }
  // Also merge by name when engine has a different id for the same demo person.
  for (const p of demo) {
    const byName = [...map.values()].find(
      (row) => row.id !== p.id && row.full_name.toLowerCase() === p.full_name.toLowerCase(),
    );
    if (byName) {
      map.set(byName.id, {
        ...byName,
        insurance: p.insurance || byName.insurance || null,
        suggested_order: p.suggested_order || byName.suggested_order || null,
        demo_set: p.demo_set || byName.demo_set,
        member_id: byName.member_id || p.member_id,
      });
    }
  }
  return [...map.values()].sort((a, b) => a.full_name.localeCompare(b.full_name));
}

async function syncPeopleToEngine(patients: Patient[], doctorProviderId?: string) {
  const engineBase = API.startsWith("http") ? API : "";
  for (const patient of patients) {
    try {
      await api("/patients", {
        method: "POST",
        body: JSON.stringify({
          id: patient.id,
          full_name: patient.full_name,
          dob: patient.dob || null,
          sex: patient.sex || null,
          member_id: patient.member_id || patient.insurance?.member_id || null,
          synthetic: true,
        }),
      });
    } catch {
      // Engine may reject if route not reloaded yet; demo catalog still works in UI.
    }
  }
  if (doctorProviderId) {
    // no-op; provider already synced at login
  }
  void engineBase;
}

export default function OrderPage() {
  const router = useRouter();
  const search = useSearchParams();
  const { doctor, logout } = useDoctorAuth();
  const [patients, setPatients] = useState<Patient[]>([]);
  const [plans, setPlans] = useState<Plan[]>([]);
  const [services, setServices] = useState<Service[]>([]);
  const [drugs, setDrugs] = useState<{ label: string }[]>([]);
  const [patientId, setPatientId] = useState("");
  const [providerId, setProviderId] = useState("");
  const [insurer, setInsurer] = useState("");
  const [planKey, setPlanKey] = useState("");
  const [order, setOrder] = useState("");
  const [code, setCode] = useState("");
  const [drug, setDrug] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [loaded, setLoaded] = useState(false);
  const [uploadNote, setUploadNote] = useState("");
  const [sourceUploadId, setSourceUploadId] = useState("");
  const [clinicalNotes, setClinicalNotes] = useState("");
  const [autofillNote, setAutofillNote] = useState("");
  const [patientLocked, setPatientLocked] = useState(false);
  const fromPolicy = search.get("from_policy");
  const uploadId = search.get("upload_id");

  useEffect(() => {
    if (doctor?.provider_id) setProviderId(doctor.provider_id);
  }, [doctor]);

  const insurers = useMemo(
    () => [...new Set(plans.map((p) => p.insurer).filter(Boolean))].sort(),
    [plans],
  );
  const plansForInsurer = useMemo(
    () => plans.filter((p) => !insurer || p.insurer === insurer),
    [plans, insurer],
  );
  const selectedPlan = useMemo(() => {
    if (!planKey) return null;
    const [i, n, y] = planKey.split("|");
    if (insurer && i !== insurer) return null;
    return plans.find((p) => p.insurer === i && p.plan_name === n && p.plan_year === y) || null;
  }, [plans, planKey, insurer]);
  /** Demo-only plans are not backed by a live PolicyLibrary row. */
  const noActivePolicyForPlan = Boolean(selectedPlan?.demo);  const servicesForPlan = useMemo(() => {
    if (!selectedPlan) return [];
    return services.filter(
      (s) =>
        s.insurer === selectedPlan.insurer &&
        s.plan_name === selectedPlan.plan_name &&
        yearsMatch(s.plan_year, selectedPlan.plan_year),
    );
  }, [services, selectedPlan]);
  const serviceOptions = useMemo(() => {
    const ordered = [
      ...servicesForPlan.filter((s) => s.kind === "coverage"),
      ...servicesForPlan.filter((s) => s.kind !== "coverage"),
    ];
    const best = new Map<string, ServiceOption>();
    for (const row of ordered) {
      if (!row.label) continue;
      const nextCode = pickCodeForRow(row);
      const prev = best.get(row.label);
      if (!prev) {
        best.set(row.label, { key: row.label, label: row.label, code: nextCode });
        continue;
      }
      if (!prev.code && nextCode) best.set(row.label, { key: row.label, label: row.label, code: nextCode });
      else if (nextCode && looksLikeServiceCode(nextCode) && !looksLikeServiceCode(prev.code)) {
        best.set(row.label, { key: row.label, label: row.label, code: nextCode });
      }
    }
    return [...best.values()].sort((a, b) => a.label.localeCompare(b.label));
  }, [servicesForPlan]);
  const selectedServiceKey =
    serviceOptions.find((o) => o.label === order && o.code === code)?.key ||
    serviceOptions.find((o) => o.label === order)?.key ||
    "";
  const selectedServiceOption = serviceOptions.find((o) => o.key === selectedServiceKey) || null;
  const selectedPatient = patients.find((p) => p.id === patientId) || null;

  useEffect(() => {
    if (!selectedServiceOption?.code) return;
    if (code === selectedServiceOption.code) return;
    if (order === selectedServiceOption.label) setCode(selectedServiceOption.code);
  }, [selectedServiceOption, order, code]);

  function pickPlanKey(plan: Plan | undefined): string {
    if (!plan) return "";
    return `${plan.insurer}|${plan.plan_name}|${plan.plan_year}`;
  }

  function applyServiceForPatient(
    patient: Patient | undefined,
    catalogPlans: Plan[],
    catalogServices: Service[],
    nextInsurer: string,
    nextPlanKey: string,
  ) {
    if (!patient?.suggested_order?.order_text && !patient?.suggested_order?.service_code) return false;
    const [planInsurer, planName, planYear] = nextPlanKey.split("|");
    const planServices = catalogServices.filter(
      (s) =>
        (!nextInsurer || s.insurer === nextInsurer) &&
        (!planName || s.plan_name === planName) &&
        yearsMatch(s.plan_year, planYear),
    );
    const wantedLabel = patient.suggested_order?.order_text || "";
    const wantedCode = patient.suggested_order?.service_code || "";
    const byLabel = planServices.find((s) => s.label === wantedLabel);
    const byCode = wantedCode
      ? planServices.find((s) => (s.codes || []).map(String).includes(wantedCode))
      : undefined;
    const hit = byLabel || byCode;
    if (hit) {
      setOrder(hit.label);
      setCode(pickCodeForRow(hit) || wantedCode);
      return true;
    }
    // Keep the patient's suggested wording even if the plan catalog is thin.
    if (wantedLabel) {
      setOrder(wantedLabel);
      if (wantedCode) setCode(wantedCode);
      return true;
    }
    void catalogPlans;
    return false;
  }

  function applyPatientDetails(
    patient: Patient | undefined,
    catalogPlans: Plan[],
    catalogServices: Service[],
    opts?: { forceOrder?: boolean; lockPatient?: boolean },
  ) {
    if (!patient) return;
    if (opts?.lockPatient !== false) setPatientLocked(true);
    const insurance = patient.insurance;
    let nextInsurer = insurer;
    let nextPlanKey = planKey;
    if (insurance?.insurer) {
      const matchedPlan =
        catalogPlans.find(
          (p) =>
            p.insurer === insurance.insurer &&
            (!insurance.plan_name || p.plan_name === insurance.plan_name) &&
            (!insurance.plan_year || p.plan_year === insurance.plan_year),
        ) || catalogPlans.find((p) => p.insurer === insurance.insurer);
      if (matchedPlan) {
        nextInsurer = matchedPlan.insurer;
        nextPlanKey = pickPlanKey(matchedPlan);
        setInsurer(nextInsurer);
        setPlanKey(nextPlanKey);
      } else {
        nextInsurer = insurance.insurer;
        nextPlanKey = `${insurance.insurer}|${insurance.plan_name}|${insurance.plan_year || ""}`;
        setInsurer(nextInsurer);
        setPlanKey(nextPlanKey);
      }
    }

    const suggested = patient.suggested_order;
    if (suggested?.order_text && (opts?.forceOrder || !order)) {
      applyServiceForPatient(patient, catalogPlans, catalogServices, nextInsurer, nextPlanKey);
    }

    if (insurance?.insurer || suggested?.order_text) {
      const bits = [
        insurance ? `${insurance.insurer} / ${insurance.plan_name}` : null,
        suggested?.order_text || null,
        patient.member_id || insurance?.member_id || null,
      ].filter(Boolean);
      setAutofillNote(`Locked to extracted patient chart: ${bits.join(" · ")}`);
    } else {
      setAutofillNote(`Order for ${patient.full_name}`);
    }
  }

  function load() {
    setError("");
    Promise.all([
      api<{ patients: Patient[] }>("/patients").catch(() => ({ patients: [] as Patient[] })),
      api<{ providers: Provider[] }>("/providers").catch(() => ({ providers: [] as Provider[] })),
      api<{ insurers: Plan[]; services: Service[]; drugs: { label: string }[] }>("/policies").catch(() => ({
        insurers: [] as Plan[],
        services: [] as Service[],
        drugs: [] as { label: string }[],
      })),
      fetch("/api/demo/order-catalog", { cache: "no-store" }).then((r) => r.json()),
    ]).then(async ([people, clinicians, catalog, demo]) => {
      const mergedPlans = mergePlans(catalog.insurers || [], demo.insurers || []);
      const mergedServices = mergeServices(catalog.services || [], demo.services || []);
      const mergedPatients = mergePatients(people.patients || [], demo.patients || []);

      setPlans(mergedPlans);
      setServices(mergedServices);
      setPatients(mergedPatients);
      setDrugs(catalog.drugs || []);
      setProviderId(doctor?.provider_id || clinicians.providers[0]?.id || "");

      await syncPeopleToEngine(mergedPatients, doctor?.provider_id);

      const qInsurer = search.get("insurer");
      const qPlan = search.get("plan_name");
      const qYear = search.get("plan_year");
      const fromQuery = mergedPlans.find(
        (p) =>
          (!qInsurer || p.insurer === qInsurer) &&
          (!qPlan || p.plan_name === qPlan) &&
          (!qYear || p.plan_year === qYear),
      );
      const uhc = mergedPlans.find((p) => /unitedhealthcare/i.test(p.insurer || ""));
      const northwind = mergedPlans.find((p) => /northwind/i.test(p.insurer || ""));
      let first = fromQuery || uhc || northwind || mergedPlans[0];

      const uhcPatient = mergedPatients.find((p) => p.demo_set === "uhc-patients");
      const defaultPatient = uhcPatient || mergedPatients[0];

      if (!uploadId) {
        setPatientId(defaultPatient?.id || "");
        if (defaultPatient) {
          applyPatientDetails(defaultPatient, mergedPlans, mergedServices, { forceOrder: true, lockPatient: true });
          if (defaultPatient.insurance?.insurer) {
            const insuredPlan =
              mergedPlans.find(
                (p) =>
                  p.insurer === defaultPatient.insurance!.insurer &&
                  p.plan_name === defaultPatient.insurance!.plan_name,
              ) || mergedPlans.find((p) => p.insurer === defaultPatient.insurance!.insurer);
            if (insuredPlan) first = insuredPlan;
          }
        } else if (first) {
          setInsurer(first.insurer);
          setPlanKey(pickPlanKey(first));
        }
      } else if (first) {
        setPatientLocked(true);
        setInsurer(first.insurer);
        setPlanKey(pickPlanKey(first));
      }

      if (!uploadId && first && !defaultPatient?.insurance) {
        setInsurer(first.insurer);
        setPlanKey(pickPlanKey(first));
      }

      setLoaded(true);

      if (uploadId) {
        api<{
          extraction_status: string;
          order_desk: {
            patient_id: string;
            order_text: string;
            service_code: string;
            insurer: string;
            plan_name: string;
            plan_year: string;
            catalog_status: string;
            clinical_notes: string;
            source_upload_id: string;
          };
        }>(`/uploads/${uploadId}`)
          .then((upload) => {
            const desk = upload.order_desk;
            api<{ patients: Patient[] }>("/patients").then((fresh) => {
              const again = mergePatients(fresh.patients || [], demo.patients || []);
              if (desk.patient_id) {
                const chosen = again.find((p) => p.id === desk.patient_id);
                // Order Desk after upload: only this patient exists on the form.
                setPatients(chosen ? [chosen] : again.filter((p) => p.id === desk.patient_id));
                setPatientId(desk.patient_id);
                setPatientLocked(true);
                if (chosen && !desk.insurer) applyPatientDetails(chosen, mergedPlans, mergedServices, { lockPatient: true });
                else if (chosen) {
                  setAutofillNote(`Order for ${chosen.full_name}`);
                }
              } else {
                setPatients(again);
              }
            });            const resolvedPlan = desk.insurer
              ? mergedPlans.find(
                  (p) =>
                    p.insurer === desk.insurer &&
                    (!desk.plan_name || p.plan_name === desk.plan_name) &&
                    (!desk.plan_year || p.plan_year === desk.plan_year),
                ) || mergedPlans.find((p) => p.insurer === desk.insurer)
              : undefined;
            if (resolvedPlan) {
              setInsurer(resolvedPlan.insurer);
              setPlanKey(pickPlanKey(resolvedPlan));
            }
            if (desk.order_text) setOrder(desk.order_text);
            if (desk.service_code) setCode(desk.service_code);
            setClinicalNotes(desk.clinical_notes || "");
            setSourceUploadId(desk.source_upload_id || uploadId);
            const matched = desk.catalog_status === "matched" && !!resolvedPlan;
            setUploadNote(
              matched
                ? "Report extracted and matched to a live plan service. Review the prefilled fields, then run coverage."
                : upload.extraction_status === "failed"
                  ? "Extraction was incomplete. Enter the order manually, then run coverage."
                  : "Report extracted, but the service is not in a live plan catalog yet. Choose the plan and service, then run coverage.",
            );
          })
          .catch((err) => {
            setError(err instanceof Error ? err.message : "Could not load the uploaded report");
          });
      }
    }).catch((err) => setError(err instanceof Error ? err.message : "The catalog could not be loaded"));
  }

  useEffect(() => {
    if (doctor) load();
  }, [search, doctor?.provider_id]);

  useEffect(() => {
    if (!insurer || !plansForInsurer.length) return;
    const stillValid = plansForInsurer.some((p) => pickPlanKey(p) === planKey);
    if (!stillValid) setPlanKey(pickPlanKey(plansForInsurer[0]));
  }, [insurer, plansForInsurer, planKey]);

  if (!doctor) return null;

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    const orderingProviderId = doctor?.provider_id || providerId;
    if (!orderingProviderId) {
      setError("Sign in again so your clinician id is available.");
      return;
    }
    if (!patientId) {
      setError("Upload a patient report first — this order needs that patient’s name.");
      return;
    }
    // Ensure patient exists in engine before check.
    try {
      const chosen = patients.find((p) => p.id === patientId);
      if (chosen) {
        await api("/patients", {
          method: "POST",
          body: JSON.stringify({
            id: chosen.id,
            full_name: chosen.full_name,
            dob: chosen.dob || null,
            sex: chosen.sex || null,
            member_id: chosen.member_id || chosen.insurance?.member_id || null,
            synthetic: true,
          }),
        });
      }
    } catch {
      // continue; check will 404 clearly if missing
    }
    const [planInsurer, plan_name, plan_year] = planKey.split("|");
    setBusy(true);
    setError("");
    try {
      const created = await api<{ id: string }>("/pa/check", {
        method: "POST",
        body: JSON.stringify({
          patient_id: patientId,
          ordering_provider_id: orderingProviderId,
          insurer: planInsurer,
          plan_name,
          plan_year,
          order_text: order,
          service_code: code || null,
          drug_name: drug || null,
          service_category: order || null,
          source_upload_id: sourceUploadId || null,
          source_document_reference: sourceUploadId ? `upload:${sourceUploadId}` : null,
        }),
      });
      router.push(`/doctor/pa/${created.id}`);
    } catch (err) {
      setError(err instanceof Error ? err.message : "The order could not be checked");
      setBusy(false);
    }
  }

  return (
    <main>
      <header className="page-head">
        <p className="kicker">Order desk</p>
        <h1 className="title">New order</h1>
        <p className="lead">Choose the patient, plan, and service. ClearPath checks coverage from live documents.</p>
        <p style={{ marginTop: "0.75rem" }}>
          <Link className="btn secondary" href="/doctor/upload">Upload a patient report first</Link>
        </p>
      </header>
      <DoctorProfileCard doctor={doctor} onLogout={logout} />
      {uploadNote && (
        <p className="badge green" style={{ marginBottom: "1rem" }}>
          {uploadNote}
        </p>
      )}
      {autofillNote && (
        <p className="badge green" style={{ marginBottom: "1rem" }}>
          {autofillNote}
        </p>
      )}
      {fromPolicy && (
        <p className="badge green" style={{ marginBottom: "1rem" }}>
          Questionnaires from your live policies are ready to use.
        </p>
      )}
      {noActivePolicyForPlan && (
        <p className="badge amber" style={{ marginBottom: "1rem" }}>
          No active policy for this plan. Go live on a matching Evidence of Coverage in the{" "}
          <Link href="/admin/policies">Policy library</Link> before running PA lookups.
        </p>
      )}
      {error && (
        <p className="badge amber" style={{ marginBottom: "1rem" }}>
          {error}{" "}
          <button className="btn secondary" type="button" onClick={load}>
            Retry
          </button>
        </p>
      )}
      {loaded && plans.length === 0 && !error && (
        <p className="muted">No plans available yet. Demo catalog failed to load.</p>
      )}
      <form className="card form-card" onSubmit={submit}>
        <label className="field">
          Patient
          <input
            value={selectedPatient?.full_name || ""}
            disabled
            readOnly
            required
            placeholder={loaded ? "No patient from upload" : "Loading…"}
          />
          <input type="hidden" name="patient_id" value={patientId} />
        </label>
        {!selectedPatient && loaded && (
          <p className="muted" style={{ marginTop: "-0.35rem", marginBottom: "0.75rem" }}>
            Upload a patient report first so this order has a fixed patient name.
          </p>
        )}
        <label className="field">
          Ordering clinician
          <input
            value={`${doctor.full_name}${doctor.specialty ? ` | ${doctor.specialty}` : ""}`}
            disabled
            readOnly
          />
        </label>
        <label className="field">
          Insurance company
          <select
            value={insurer}
            onChange={(e) => {
              const next = e.target.value;
              setInsurer(next);
              const nextPlans = plans.filter((p) => p.insurer === next);
              const nextKey = pickPlanKey(nextPlans[0]);
              setPlanKey(nextKey);
              const matched = applyServiceForPatient(selectedPatient || undefined, plans, services, next, nextKey);
              setAutofillNote(
                matched
                  ? `Insurance selected · service matched from patient chart (${selectedPatient?.suggested_order?.order_text || "order"})`
                  : "Insurance selected · choose a service for this plan",
              );
              if (!matched) {
                setOrder("");
                setCode("");
              }
            }}
            required
          >
            <option value="">Select insurer</option>
            {insurers.map((name) => (
              <option key={name} value={name}>
                {name}
              </option>
            ))}
          </select>
        </label>
        <label className="field">
          Insurance plan
          <select
            value={plansForInsurer.some((p) => pickPlanKey(p) === planKey) ? planKey : ""}
            onChange={(e) => {
              const nextKey = e.target.value;
              setPlanKey(nextKey);
              const matched = applyServiceForPatient(
                selectedPatient || undefined,
                plans,
                services,
                insurer,
                nextKey,
              );
              setAutofillNote(
                matched
                  ? `Plan selected · service matched from patient chart (${selectedPatient?.suggested_order?.order_text || "order"})`
                  : "Plan selected · choose a service for this plan",
              );
              if (!matched) {
                setOrder("");
                setCode("");
              }
            }}
            required
            disabled={!insurer}
          >
            <option value="">Select plan</option>
            {plansForInsurer.map((plan) => {
              const key = pickPlanKey(plan);
              return (
                <option key={key} value={key}>
                  {plan.plan_name}
                  {plan.plan_year ? ` | ${plan.plan_year}` : ""}
                  {plan.demo ? " · demo" : ""}
                </option>
              );
            })}
          </select>
        </label>
        <label className="field">
          Service
          <select
            value={selectedServiceKey}
            onChange={(e) => {
              const opt = serviceOptions.find((o) => o.key === e.target.value);
              if (!opt) {
                setOrder("");
                setCode("");
                return;
              }
              setOrder(opt.label);
              setCode(opt.code || "");
            }}
            disabled={!selectedPlan}
          >
            <option value="">Choose or type below</option>
            {serviceOptions.map((opt) => (
              <option key={opt.key} value={opt.key}>
                {opt.code ? `${opt.label} | ${opt.code}` : opt.label}
              </option>
            ))}
          </select>
        </label>
        <label className="field">
          Order text
          <input
            value={order}
            onChange={(e) => setOrder(e.target.value)}
            required
            placeholder="Service description"
          />
        </label>
        <label className="field">
          Service code
          <input
            value={code}
            onChange={(e) => setCode(e.target.value)}
            placeholder="CPT / HCPCS / CDT"
          />
        </label>
        {drugs.length > 0 && (
          <label className="field">
            Drug
            <select value={drug} onChange={(e) => setDrug(e.target.value)}>
              <option value="">Not required</option>
              {drugs.map((item) => (
                <option key={item.label} value={item.label}>
                  {item.label}
                </option>
              ))}
            </select>
          </label>
        )}
        {clinicalNotes && (
          <label className="field">
            Notes from uploaded report
            <textarea value={clinicalNotes} readOnly rows={3} />
          </label>
        )}
        <button className="btn" disabled={busy || !selectedPlan || noActivePolicyForPlan || !insurer || !order.trim() || !patientId}>
          {busy ? "Running coverage..." : "Run coverage check"}
        </button>
        {noActivePolicyForPlan && (
          <p className="muted" style={{ marginTop: 8 }}>
            Coverage stays locked until a live policy exists for this insurer/plan.
          </p>
        )}
      </form>
    </main>
  );
}
