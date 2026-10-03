import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useCallback, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { api, LONG_TIMEOUT_MS } from "../api/client";
import { errorText } from "../api/errorText";
import { useEventStream } from "../hooks/useEventStream";

interface Scenario {
  key: string;
  number: number;
  agent: string;
  title_en: string;
  title_ar: string;
  expected_en: string;
  expected_ar: string;
  advances_clock: boolean;
}

interface TimelineStep {
  stage: string;
  at_seconds: number | null;
  text_en: string;
}

interface Outcome {
  detected: boolean;
  detection_method: string | null;
  explanation_en: string | null;
  explanation_ar: string | null;
  correction: string | null;
  root_cause: string | null;
  rule_text_en: string | null;
  rule_text_ar: string | null;
  elapsed_seconds: number;
  timeline: TimelineStep[];
}

interface Injection {
  id: string;
  scenario: string;
  number: number | null;
  status: "running" | "completed" | "failed";
  detected: boolean;
  rule_id: string | null;
  elapsed_seconds: number | null;
  outcome: Outcome | Record<string, never>;
  error: string | null;
}

interface LiveStep {
  stage: string;
  at: number;
  text: string;
}

const STAGES = ["injected", "detected", "explained", "corrected", "rule_proposed"] as const;
const LIMIT_SECONDS = 120; // every scenario should fit in 2 minutes on stage

export function Chaos() {
  const { t, i18n } = useTranslation();
  const ar = i18n.language === "ar";
  const qc = useQueryClient();
  const list = useQuery({
    queryKey: ["chaos"],
    queryFn: ({ signal }) =>
      api.get<{ demo_mode: boolean; scenarios: Scenario[]; recent: Injection[] }>("/chaos/scenarios", signal),
  });
  const [running, setRunning] = useState<string | null>(null);
  const [live, setLive] = useState<LiveStep[]>([]);
  const [result, setResult] = useState<Injection | null>(null);
  const [error, setError] = useState<string | null>(null);
  const started = useRef<number>(0);

  const add = useCallback((stage: string, text: string) => {
    setLive((old) => [...old, { stage, at: (performance.now() - started.current) / 1000, text }]);
  }, []);

  const onEvent = useCallback(
    (event: string, data: any) => {
      if (!running) return;
      if (event === "chaos.injected") add("injected", "");
      else if (event === "incident.opened") add("detected", data.summary ?? "");
      else if (event === "rule.proposed") add("rule_proposed", data.text ?? "");
      else if (event === "stage" && data.stage === "awaiting_approval")
        add("explained", t("chaos.asked_owner"));
    },
    [running, add, t],
  );
  useEventStream(running ? "/harness/stream" : null, onEvent, [
    "chaos.injected",
    "incident.opened",
    "rule.proposed",
    "stage",
  ]);

  const inject = useMutation({
    mutationFn: (key: string) =>
      api.post<Injection>("/chaos/inject", { scenario: key }, { timeoutMs: LONG_TIMEOUT_MS }),
    onMutate: (key) => {
      started.current = performance.now();
      setRunning(key);
      setLive([]);
      setResult(null);
      setError(null);
    },
    onSuccess: (inj) => setResult(inj),
    onError: (e) => setError(errorText(e)),
    onSettled: () => {
      setRunning(null);
      void qc.invalidateQueries({ queryKey: ["chaos"] });
      void qc.invalidateQueries({ queryKey: ["harness"] });
    },
  });

  const scenarios = list.data?.scenarios ?? [];
  const last: Record<string, Injection> = {};
  for (const r of list.data?.recent ?? []) if (!last[r.scenario]) last[r.scenario] = r;
  const current = scenarios.find((s) => s.key === (running ?? result?.scenario));

  return (
    <div className="flex flex-col gap-4">
      <div>
        <h1 className="text-xl font-semibold">{t("nav.chaos")}</h1>
        <p className="text-sm text-ink-500">{t("chaos.intro")}</p>
      </div>
      {list.data && !list.data.demo_mode && (
        <p className="card text-sm text-warn-700">{t("chaos.demo_only")}</p>
      )}
      <div className="grid gap-4 lg:grid-cols-[minmax(0,1fr)_minmax(0,420px)]">
        <ul className="grid gap-3 sm:grid-cols-2" data-testid="chaos-scenarios">
          {scenarios.map((s) => (
            <li
              key={s.key}
              className={`card flex flex-col gap-2 ${running === s.key ? "ring-2 ring-brand-500" : ""}`}
            >
              <div className="flex items-start justify-between gap-2">
                <h2 className="text-sm font-semibold">
                  {s.number}. {ar ? s.title_ar : s.title_en}
                </h2>
                <span className="badge bg-slate-100 text-ink-700">
                  {t(`agents.${s.agent}`, { defaultValue: s.agent })}
                </span>
              </div>
              <p className="text-xs text-ink-700">{ar ? s.expected_ar : s.expected_en}</p>
              {s.advances_clock && <p className="text-xs text-ink-500">{t("chaos.advances_clock")}</p>}
              <div className="mt-auto flex flex-wrap items-center justify-between gap-2">
                <button
                  type="button"
                  className="btn-primary"
                  disabled={!!running || list.data?.demo_mode === false}
                  onClick={() => inject.mutate(s.key)}
                >
                  {running === s.key ? t("chaos.running") : t("chaos.inject")}
                </button>
                {last[s.key] && <OutcomeBadge inj={last[s.key]} />}
              </div>
            </li>
          ))}
        </ul>
        <aside className="card flex flex-col gap-3" aria-live="polite" data-testid="chaos-timeline">
          <h2 className="text-base font-semibold">
            {t("chaos.timeline")}
            {current && (
              <span className="font-normal text-ink-500"> · {ar ? current.title_ar : current.title_en}</span>
            )}
          </h2>
          {!running && !result && !error && <p className="text-sm text-ink-500">{t("chaos.pick")}</p>}
          {error && <p className="text-sm text-bad-700">{error}</p>}
          {running && <LiveTimeline steps={live} />}
          {result && <ResultView inj={result} />}
        </aside>
      </div>
    </div>
  );
}

function OutcomeBadge({ inj }: { inj: Injection }) {
  const { t } = useTranslation();
  if (inj.status === "failed")
    return <span className="badge bg-bad-50 text-bad-700">{t("chaos.failed")}</span>;
  const ok = inj.detected && !!inj.rule_id && (inj.elapsed_seconds ?? LIMIT_SECONDS) < LIMIT_SECONDS;
  return (
    <span className={`badge ${ok ? "bg-good-50 text-good-700" : "bg-warn-50 text-warn-700"}`}>
      {inj.detected ? t("chaos.detected") : t("chaos.not_detected")}
      {inj.elapsed_seconds != null && ` · ${t("chaos.seconds", { n: inj.elapsed_seconds.toFixed(1) })}`}
    </span>
  );
}

function LiveTimeline({ steps }: { steps: LiveStep[] }) {
  const { t } = useTranslation();
  const reached = new Set(steps.map((s) => s.stage));
  return (
    <ol className="flex flex-col gap-2 text-sm">
      {STAGES.map((stage) => {
        const hit = steps.find((s) => s.stage === stage);
        return (
          <li key={stage} className={`flex gap-2 ${reached.has(stage) ? "" : "text-ink-500"}`}>
            <span aria-hidden>{hit ? "●" : "○"}</span>
            <span className="font-medium">{t(`chaos.stages.${stage}`)}</span>
            {hit && (
              <span className="text-xs text-ink-500">{t("chaos.seconds", { n: hit.at.toFixed(1) })}</span>
            )}
            {hit?.text && <span className="block text-xs text-ink-700">{hit.text}</span>}
          </li>
        );
      })}
      <li className="text-xs text-ink-500">{t("chaos.watching")}</li>
    </ol>
  );
}

function ResultView({ inj }: { inj: Injection }) {
  const { t, i18n } = useTranslation();
  const ar = i18n.language === "ar";
  if (inj.status === "failed")
    return (
      <p className="text-sm text-bad-700">
        {t("chaos.failed")}: {inj.error}
      </p>
    );
  const o = inj.outcome as Outcome;
  const byStage = Object.fromEntries((o.timeline ?? []).map((s) => [s.stage, s]));
  const fast = o.elapsed_seconds < LIMIT_SECONDS;
  return (
    <div className="flex flex-col gap-3">
      <div className="flex flex-wrap gap-2">
        <OutcomeBadge inj={inj} />
        <span className={`badge ${fast ? "bg-good-50 text-good-700" : "bg-bad-50 text-bad-700"}`}>
          {fast ? t("chaos.under_limit") : t("chaos.over_limit")}
        </span>
      </div>
      {!o.detected && <p className="text-sm text-ink-700">{t("chaos.nothing_detected")}</p>}
      <ol className="flex flex-col gap-2 text-sm">
        {STAGES.map((stage) => {
          const step = byStage[stage];
          if (!step) return null;
          const text =
            stage === "explained" && ar && o.explanation_ar
              ? o.explanation_ar
              : stage === "rule_proposed" && ar && o.rule_text_ar
                ? o.rule_text_ar
                : step.text_en;
          return (
            <li key={stage}>
              <div className="flex gap-2">
                <span aria-hidden className="text-good-700">
                  ●
                </span>
                <span className="font-medium">{t(`chaos.stages.${stage}`)}</span>
                {step.at_seconds != null && (
                  <span className="text-xs text-ink-500">
                    {t("chaos.seconds", { n: step.at_seconds.toFixed(1) })}
                  </span>
                )}
              </div>
              <p className="ms-5 text-xs text-ink-700">{stage === "injected" ? "" : text}</p>
            </li>
          );
        })}
      </ol>
      {o.root_cause && (
        <p className="text-xs text-ink-700">
          {t("harness.root_cause")}: {o.root_cause}
        </p>
      )}
      {o.detected && o.rule_text_en && <p className="text-xs text-ink-500">{t("chaos.approve_hint")}</p>}
    </div>
  );
}
