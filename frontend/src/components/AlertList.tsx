import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";
import { api, LONG_TIMEOUT_MS } from "../api/client";
import type { ApprovalRequest } from "../api/types";

const URGENCY = ["", "border-slate-200", "border-warn-500", "border-bad-500"];

/** Alerts, most urgent first; each is acknowledged with one tap. */
export function AlertList({ alerts }: { alerts: ApprovalRequest[] }) {
  const { t, i18n } = useTranslation();
  const ar = i18n.language === "ar";
  const qc = useQueryClient();
  const ack = useMutation({
    mutationFn: (a: ApprovalRequest) =>
      api.post(
        `/approvals/${a.id}/resolve`,
        { option_key: a.options[0]?.key },
        { timeoutMs: LONG_TIMEOUT_MS },
      ),
    onSettled: () => qc.invalidateQueries({ queryKey: ["approvals"] }),
  });
  if (alerts.length === 0) return <p className="card text-sm text-ink-500">{t("home.no_alerts")}</p>;
  return (
    <ul className="flex flex-col gap-2" data-testid="alert-list">
      {alerts.map((a) => (
        <li
          key={a.id}
          className={`card flex items-start justify-between gap-3 border-s-4 ${URGENCY[a.urgency] ?? URGENCY[1]}`}
        >
          <div className="min-w-0">
            <p className="text-xs text-ink-500">
              {t(`agents.${a.agent}`, { defaultValue: a.agent })} ·{" "}
              {t("approval.urgency", { level: a.urgency })}
            </p>
            <p className="text-sm text-ink-900" dir="auto">
              {ar ? a.text_ar : a.text_en}
            </p>
          </div>
          {a.options.length > 0 && (
            <button
              type="button"
              className="btn-secondary shrink-0"
              disabled={ack.isPending}
              onClick={() => ack.mutate(a)}
            >
              {ar ? a.options[0].label_ar : a.options[0].label_en}
            </button>
          )}
        </li>
      ))}
    </ul>
  );
}
