import { useTranslation } from "react-i18next";

export interface IncidentRow {
  id: string;
  agent: string;
  type: string;
  detected_by: string;
  summary: string;
  action_taken: string;
  root_cause: string | null;
  status: string;
  created_at: string;
  action_id: string | null;
  rules: { id: string; status: string; text_en: string }[];
}

const STATUS_STYLE: Record<string, string> = {
  open: "bg-bad-50 text-bad-700",
  investigating: "bg-warn-50 text-warn-700",
  resolved: "bg-good-50 text-good-700",
};

export function IncidentLog({ incidents, onOpenAction }: { incidents: IncidentRow[]; onOpenAction: (id: string) => void }) {
  const { t, i18n } = useTranslation();
  const when = (iso: string) =>
    new Date(iso).toLocaleString(i18n.language === "ar" ? "ar-EG" : "en-GB", { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });
  if (incidents.length === 0) return <p className="card text-sm text-ink-500">{t("harness.no_incidents")}</p>;
  return (
    <ul className="flex flex-col gap-2" data-testid="incident-log">
      {incidents.map((i) => (
        <li key={i.id} className="card">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <span className="text-xs text-ink-500">
              {t(`agents.${i.agent}`, { defaultValue: i.agent })} · {when(i.created_at)} · {t("harness.detected_by")}: {i.detected_by}
            </span>
            <span className={`badge ${STATUS_STYLE[i.status] ?? "bg-slate-100 text-ink-700"}`}>{t(`harness.incident_status.${i.status}`, { defaultValue: i.status })}</span>
          </div>
          <p className="mt-1 text-sm text-ink-900">{i.summary}</p>
          {i.action_taken && <p className="text-xs text-ink-700">{t("harness.action_taken")}: {i.action_taken}</p>}
          {i.root_cause && <p className="text-xs text-ink-700">{t("harness.root_cause")}: {i.root_cause}</p>}
          {i.rules.map((r) => (
            <p key={r.id} className="text-xs text-brand-700">{t("harness.proposed_rule")}: {r.text_en} ({t(`harness.rule_status.${r.status}`, { defaultValue: r.status })})</p>
          ))}
          {i.action_id && (
            <button type="button" className="mt-1 text-xs text-brand-700 underline" onClick={() => onOpenAction(i.action_id!)}>
              {t("harness.open_action")}
            </button>
          )}
        </li>
      ))}
    </ul>
  );
}
