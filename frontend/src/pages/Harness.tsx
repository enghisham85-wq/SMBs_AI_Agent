import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useCallback, useState } from "react";
import { useTranslation } from "react-i18next";
import { api } from "../api/client";
import { CalibrationChart, type CalibrationData } from "../components/CalibrationChart";
import { IncidentLog, type IncidentRow } from "../components/IncidentLog";
import { PipelineView, type ActionRow } from "../components/PipelineView";
import { RulesPanel, type RuleRow } from "../components/RulesPanel";
import { useEventStream } from "../hooks/useEventStream";

type Tab = "pipeline" | "incidents" | "rules" | "calibration";

export function Harness() {
  const { t } = useTranslation();
  const qc = useQueryClient();
  const [tab, setTab] = useState<Tab>("pipeline");
  const [selected, setSelected] = useState<string | null>(null);
  const actions = useQuery({
    queryKey: ["harness", "actions"],
    queryFn: ({ signal }) => api.get<{ actions: ActionRow[] }>("/harness/actions?limit=60", signal),
  });
  const incidents = useQuery({
    queryKey: ["harness", "incidents"],
    queryFn: ({ signal }) => api.get<{ incidents: IncidentRow[] }>("/harness/incidents", signal),
  });
  const rules = useQuery({
    queryKey: ["harness", "rules"],
    queryFn: ({ signal }) => api.get<{ rules: RuleRow[] }>("/harness/rules", signal),
  });
  const calibration = useQuery({
    queryKey: ["harness", "calibration"],
    queryFn: ({ signal }) => api.get<CalibrationData>("/harness/calibration", signal),
  });

  // Stage messages update rows in place; a new action refetches the list.
  const onEvent = useCallback(
    (event: string, data: any) => {
      if (event === "stage") {
        qc.setQueryData<{ actions: ActionRow[] }>(["harness", "actions"], (old) => {
          if (!old) return old;
          const found = old.actions.some((a) => a.id === data.action_id);
          if (!found) {
            void qc.invalidateQueries({ queryKey: ["harness", "actions"] });
            return old;
          }
          return {
            actions: old.actions.map((a) =>
              a.id === data.action_id ? { ...a, stage: data.stage, attempt: data.attempt } : a,
            ),
          };
        });
        if (data.action_id === selected)
          void qc.invalidateQueries({ queryKey: ["harness", "action", selected] });
      } else if (event === "incident.opened") {
        void qc.invalidateQueries({ queryKey: ["harness", "incidents"] });
      } else if (event === "rule.proposed") {
        void qc.invalidateQueries({ queryKey: ["harness", "rules"] });
      } else if (event === "calibration") {
        void qc.invalidateQueries({ queryKey: ["harness", "calibration"] });
      }
    },
    [qc, selected],
  );
  useEventStream("/harness/stream", onEvent, ["stage", "incident.opened", "rule.proposed", "calibration"]);

  const tabs: Tab[] = ["pipeline", "incidents", "rules", "calibration"];
  const pendingRules = (rules.data?.rules ?? []).filter((r) => r.status === "proposed").length;
  return (
    <div className="flex flex-col gap-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h1 className="text-xl font-semibold">{t("nav.harness")}</h1>
        <div role="tablist" className="flex flex-wrap gap-1">
          {tabs.map((k) => (
            <button
              key={k}
              role="tab"
              aria-selected={tab === k}
              className={tab === k ? "btn-primary" : "btn-secondary"}
              onClick={() => setTab(k)}
            >
              {t(`harness.tabs.${k}`)}
              {k === "rules" && pendingRules > 0 && (
                <span className="badge ms-1 bg-warn-50 text-warn-700">{pendingRules}</span>
              )}
            </button>
          ))}
        </div>
      </div>
      <div className="grid gap-4 lg:grid-cols-[1fr_minmax(0,420px)]">
        <div className="min-w-0">
          {tab === "pipeline" && (
            <PipelineView actions={actions.data?.actions ?? []} selected={selected} onSelect={setSelected} />
          )}
          {tab === "incidents" && (
            <IncidentLog incidents={incidents.data?.incidents ?? []} onOpenAction={setSelected} />
          )}
          {tab === "rules" && <RulesPanel rules={rules.data?.rules ?? []} />}
          {tab === "calibration" && calibration.data && <CalibrationChart data={calibration.data} />}
        </div>
        {selected && <ActionDrawer id={selected} onClose={() => setSelected(null)} />}
      </div>
    </div>
  );
}

function ActionDrawer({ id, onClose }: { id: string; onClose: () => void }) {
  const { t, i18n } = useTranslation();
  const q = useQuery({
    queryKey: ["harness", "action", id],
    queryFn: ({ signal }) => api.get<any>(`/harness/actions/${id}`, signal),
  });
  const d = q.data;
  const when = (iso: string) =>
    new Date(iso).toLocaleTimeString(i18n.language === "ar" ? "ar-EG" : "en-GB", {
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
    });
  return (
    <aside
      className="card flex max-h-[80vh] flex-col gap-3 overflow-auto"
      data-testid="action-drawer"
      aria-label={t("harness.action_detail")}
    >
      <div className="flex items-center justify-between">
        <h2 className="text-base font-semibold">{d ? d.action.type.replace(/_/g, " ") : "…"}</h2>
        <button type="button" className="btn-secondary" onClick={onClose}>
          {t("common.back")}
        </button>
      </div>
      {d && (
        <>
          <p className="text-xs text-ink-500">
            {t(`agents.${d.action.agent}`, { defaultValue: d.action.agent })} ·{" "}
            {t(`harness.risk.${d.action.risk_class}`, { defaultValue: d.action.risk_class })} ·{" "}
            {t(`harness.stages.${d.action.stage}`, { defaultValue: d.action.stage })}
          </p>
          <Section title={t("harness.plan")}>
            <pre className="whitespace-pre-wrap text-xs">{JSON.stringify(d.action.plan, null, 2)}</pre>
          </Section>
          <Section title={t("harness.checks")}>
            {d.checks.length === 0 ? (
              <p className="text-xs text-ink-500">{t("common.none")}</p>
            ) : (
              <ul className="text-xs">
                {d.checks.map((c: any, i: number) => (
                  <li key={i} className={c.passed ? "text-good-700" : "text-bad-700"}>
                    {c.passed ? "✓" : "✗"} {c.name}
                    {c.learned_rule_id ? ` (${t("harness.learned_rule")})` : ""}
                  </li>
                ))}
              </ul>
            )}
          </Section>
          <Section title={t("harness.verifier")}>
            {d.action.verifier_verdict ? (
              <div className="text-xs">
                <p className={d.action.verifier_verdict.agrees ? "text-good-700" : "text-bad-700"}>
                  {d.action.verifier_verdict.agrees
                    ? t("harness.verifier_agrees")
                    : t("harness.verifier_disagrees")}
                </p>
                <ul>
                  {(d.action.verifier_verdict.issues ?? []).map((iss: any, i: number) => (
                    <li key={i}>
                      {iss.field}: {iss.problem} ({iss.severity})
                    </li>
                  ))}
                </ul>
              </div>
            ) : (
              <p className="text-xs text-ink-500">{t("harness.no_verifier")}</p>
            )}
          </Section>
          <Section title={t("harness.audit_trail")}>
            <ol className="text-xs">
              {d.audit.map((e: any, i: number) => (
                <li key={i} className="flex gap-2">
                  <span className="text-ink-500">{when(e.at)}</span>
                  <span>{t(`harness.events.${e.event.replace(":", "_")}`, { defaultValue: e.event })}</span>
                  {e.verification && (
                    <span
                      className={
                        e.verification === "passed" || e.verification === "agreed"
                          ? "text-good-700"
                          : "text-bad-700"
                      }
                    >
                      {e.verification}
                    </span>
                  )}
                </li>
              ))}
            </ol>
          </Section>
        </>
      )}
    </aside>
  );
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section>
      <h3 className="mb-1 text-xs font-semibold uppercase text-ink-500">{title}</h3>
      {children}
    </section>
  );
}
