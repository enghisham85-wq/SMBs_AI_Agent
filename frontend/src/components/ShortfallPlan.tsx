import { useTranslation } from "react-i18next";
import type { Money } from "../api/types";
import { formatMoney } from "../lib/money";

export interface PlanActionOut {
  id: string;
  rank: number;
  type: string;
  description_en: string;
  description_ar: string;
  impact: Money;
  risk: "low" | "medium" | "high";
  simulated_lowest: { date: string; balance: Money };
  status: string;
}

export interface PlanOut {
  id: string;
  status: string;
  gap: Money;
  gap_date: string;
  days_to_act: number;
  lowest: { date: string; balance: Money };
  combined_lowest: Money;
  actions: PlanActionOut[];
}

const RISK_STYLE: Record<string, string> = {
  low: "bg-good-50 text-good-700",
  medium: "bg-warn-50 text-warn-700",
  high: "bg-bad-50 text-bad-700",
};

/** Ranked gap-closing actions; "Simulate" overlays the balance after that action on the chart. */
export function ShortfallPlan({
  plan,
  simulating,
  onSimulate,
}: {
  plan: PlanOut;
  simulating: string | null;
  onSimulate: (id: string | null) => void;
}) {
  const { t, i18n } = useTranslation();
  const lang = i18n.language;
  const day = (iso: string) =>
    new Date(iso + "T00:00:00").toLocaleDateString(lang === "ar" ? "ar-EG" : "en-GB", { day: "numeric", month: "short" });
  return (
    <section className="card flex flex-col gap-3" data-testid="shortfall-plan">
      <div>
        <h2 className="text-base font-semibold text-bad-700">
          {t("cash.gap_title", { gap: formatMoney(plan.gap, lang), date: day(plan.gap_date) })}
        </h2>
        <p className="text-sm text-ink-700">
          {t("cash.gap_detail", {
            days: plan.days_to_act,
            lowest: formatMoney(plan.lowest.balance, lang),
            date: day(plan.lowest.date),
            combined: formatMoney(plan.combined_lowest, lang),
          })}
        </p>
      </div>
      <ol className="flex flex-col gap-2">
        {plan.actions.map((a) => (
          <li key={a.id} className={`rounded-lg border p-3 ${simulating === a.id ? "border-brand-500 bg-brand-50" : "border-slate-200"}`}>
            <div className="flex flex-wrap items-start justify-between gap-2">
              <p className="text-sm text-ink-900">
                <span className="me-1 font-semibold">{a.rank}.</span>
                {lang === "ar" ? a.description_ar : a.description_en}
                {a.status === "accepted" && <span className="badge ms-2 bg-good-50 text-good-700">{t("cash.accepted")}</span>}
              </p>
              <button type="button" className={simulating === a.id ? "btn-primary" : "btn-secondary"} aria-pressed={simulating === a.id}
                onClick={() => onSimulate(simulating === a.id ? null : a.id)}>
                {t("cash.simulate")}
              </button>
            </div>
            <p className="mt-1 flex flex-wrap gap-3 text-xs text-ink-500">
              <span>{t("cash.impact")}: +{formatMoney(a.impact, lang)}</span>
              <span className={`badge ${RISK_STYLE[a.risk]}`}>{t(`cash.risk.${a.risk}`)}</span>
              <span>{t("cash.new_lowest", { amount: formatMoney(a.simulated_lowest.balance, lang), date: day(a.simulated_lowest.date) })}</span>
            </p>
          </li>
        ))}
      </ol>
    </section>
  );
}
