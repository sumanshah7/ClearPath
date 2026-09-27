import { label, tone } from "@/lib/format";

export function StatusBadge({ status }: { status: string }) {
  return (
    <span className={`badge ${tone(status)}`}>
      <i />
      {label(status)}
    </span>
  );
}

export function SyntheticBadge() {
  return <span className="badge gray">Synthetic</span>;
}

export function SourceBadge({ kind, url }: { kind?: string; url?: string | null }) {
  if (kind === "fictional_fallback") return <span className="badge amber">Fictional fallback</span>;
  return (
    <a className="badge green" href={url || "#"}>
      Published
    </a>
  );
}

export function ruleConfidence(input: {
  judge_verdict?: string | null;
  judge_reason?: string | null;
  grounding?: { passed?: boolean; failures?: string[] } | null;
  question_verdict?: string | null;
}): { sure: boolean; detail: string; importOnly?: boolean } {
  const grounded = input.grounding?.passed === true;
  const accurate = input.judge_verdict === "ACCURATE";
  const questionsOk = input.question_verdict == null || input.question_verdict === "complete";
  const importOnly =
    input.judge_verdict === "UNAVAILABLE" &&
    (/structured import|awaiting extract\+judge|overview pipeline/i.test(input.judge_reason || "") ||
      (input.grounding?.failures || []).some((f) => /structured import|overview pipeline/i.test(f)));
  if (importOnly) {
    return {
      sure: false,
      importOnly: true,
      detail: "Structured import only — not page-grounded or judged. Run overview Reprocess for engine rules.",
    };
  }
  if (grounded && accurate && questionsOk) {
    return { sure: true, detail: "The quote is on the cited page and the judge called it accurate." };
  }
  if (input.grounding && input.grounding.passed === false) {
    return { sure: false, detail: "The quote is not on the cited page." };
  }
  if (input.judge_verdict === "WRONG_VALUE") return { sure: false, detail: "A number, code, or duration does not match the page." };
  if (input.judge_verdict === "HALLUCINATED") return { sure: false, detail: "This row is not supported by the cited page." };
  if (input.judge_verdict === "VAGUE") return { sure: false, detail: "The wording is too vague to be sure." };
  if (input.judge_verdict === "UNAVAILABLE") return { sure: false, detail: "The judge did not return a verdict." };
  if (!questionsOk) return { sure: false, detail: "A condition does not have a question yet." };
  return { sure: false, detail: "This row has not been fully checked." };
}

export function ConfidenceBadge({ sure, importOnly }: { sure: boolean; importOnly?: boolean }) {
  if (importOnly) return <span className="badge blue">Import — not judged</span>;
  return <span className={`badge ${sure ? "green" : "amber"}`}>{sure ? "100% sure" : "Needs a look"}</span>;
}

export function JudgeBadge({ verdict }: { verdict?: string | null }) {
  if (!verdict) return null;
  const toneName = verdict === "ACCURATE" ? "green" : verdict === "WRONG_VALUE" || verdict === "HALLUCINATED" ? "amber" : "blue";
  return <span className={`badge ${toneName}`}>Judge: {verdict.replaceAll("_", " ")}</span>;
}

export function LockBadge() {
  return <span className="badge gray">Locked</span>;
}

export function ReadinessBar({ met, total, readiness }: { met: number; total: number; readiness: number }) {
  const pct = Math.round(readiness * 100);
  return (
    <div>
      <div className="bar" aria-hidden>
        <span style={{ width: `${pct}%` }} />
      </div>
      <p className="muted" style={{ margin: "6px 0 0" }}>
        {met} of {total} met, {pct}%
      </p>
    </div>
  );
}
