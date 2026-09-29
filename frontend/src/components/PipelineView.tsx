import { useTranslation } from "react-i18next";

export interface ActionRow {
  id: string;
  agent: string;
  type: string;
  stage: string;
  risk_class: string;
  attempt: number;
  created_at: string;
  incident_id: string | null;
}

// The harness lifecycle in order; each action shows how far it got.
const STEPS = ["planned", "prechecked", "awaiting_approval", "executing", "verifying", "completed"] as const;
const END_STYLE: Record<string, string> = {
  completed: "bg-good-50 text-good-700",
  escalated: "bg-bad-50 text-bad-700",
  failed: "bg-bad-50 text-bad-700",
  rejected: "bg-slate-100 text-ink-700",
  rolled_back: "bg-warn-50 text-warn-700",
  retrying: "bg-warn-50 text-warn-700",
  awaiting_approval: "bg-brand-50 text-brand-700",
};

function reached(stage: string): number {
  if (stage === "rolled_back" || stage === "retrying") return STEPS.indexOf("verifying");
  if (["escalated", "failed", "rejected"].includes(stage)) return -1;
  return STEPS.indexOf(stage as (typeof STEPS)[number]);
}

/** Live node stages per action, fed by /harness/stream. */
export function PipelineView({ actions, onSelect, selected }: { actions: ActionRow[]; onSelect: (id: string) => void; selected: string | null }) {
  const { t, i18n } = useTranslation();
  const time = (iso: string) =>
    new Date(iso).toLocaleTimeString(i18n.language === "ar" ? "ar-EG" : "en-GB", { hour: "2-digit", minute: "2-digit" });
  if (actions.length === 0) return <p className="card text-sm text-ink-500">{t("harness.no_actions")}</p>;
  return (
    <div className="card overflow-x-auto p-0" data-testid="pipeline">
      <table className="table">
        <thead>
          <tr>
            <th>{t("harness.action")}</th>
            <th>{t("harness.pipeline")}</th>
            <th>{t("harness.state")}</th>
          </tr>
        </thead>
        <tbody>
          {actions.map((a) => {
            const n = reached(a.stage);
            return (
              <tr key={a.id} className={`cursor-pointer hover:bg-slate-50 ${selected === a.id ? "bg-brand-50" : ""}`} onClick={() => onSelect(a.id)}>
                <td>
                  <span className="font-medium">{t(`harness.types.${a.type}`, { defaultValue: a.type.replace(/_/g, " ") })}</span>
                  <span className="block text-xs text-ink-500">
                    {t(`agents.${a.agent}`, { defaultValue: a.agent })} · {time(a.created_at)}
                    {a.attempt > 1 && ` · ${t("harness.attempt", { n: a.attempt })}`}
                  </span>
                </td>
                <td>
                  <ol className="flex gap-1" aria-label={t("harness.pipeline")}>
                    {STEPS.map((s, i) => (
                      <li key={s} title={t(`harness.stages.${s}`)}
                        className={`h-2 w-6 rounded-full ${i <= n ? "bg-brand-500" : "bg-slate-200"}`} />
                    ))}
                  </ol>
                </td>
                <td>
                  <span className={`badge ${END_STYLE[a.stage] ?? "bg-slate-100 text-ink-700"}`}>
                    {t(`harness.stages.${a.stage}`, { defaultValue: a.stage })}
                  </span>
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}
