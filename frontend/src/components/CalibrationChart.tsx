import { useTranslation } from "react-i18next";
import { CartesianGrid, Legend, Line, LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";

export interface CalibrationData {
  agents: {
    agent: string;
    high_confidence_threshold: number;
    low_confidence_threshold: number;
    auto_approve_factor: number;
    degraded: boolean;
    metrics: { metric: string; current_value: number | null; threshold: number; restore_progress: number; method_override: string | null }[];
  }[];
  history: { agent: string; metric: string; date: string; value: number; threshold: number; degraded: boolean }[];
}

// One series per agent; identity is carried by colour and dash pattern plus the legend.
const SERIES: Record<string, { color: string; dash?: string }> = {
  stock: { color: "#0d9488" },
  cashflow: { color: "#334155", dash: "6 4" },
  accountant: { color: "#b45309", dash: "2 3" },
};
const pct = (v: number | null | undefined) => (v === null || v === undefined ? "—" : `${Math.round(v * 100)}%`);

/** Error rate over time per agent, and the current thresholds and auto-approve limits. */
export function CalibrationChart({ data }: { data: CalibrationData }) {
  const { t, i18n } = useTranslation();
  const byDate = new Map<string, Record<string, number | string>>();
  for (const h of data.history) {
    const row = byDate.get(h.date) ?? { date: h.date };
    row[h.agent] = Math.max(Number(row[h.agent] ?? 0), h.value);
    byDate.set(h.date, row);
  }
  const rows = [...byDate.values()].sort((a, b) => String(a.date).localeCompare(String(b.date)));
  const fmtDay = (iso: string) =>
    new Date(iso + "T00:00:00").toLocaleDateString(i18n.language === "ar" ? "ar-EG" : "en-GB", { day: "numeric", month: "short" });
  return (
    <section className="flex flex-col gap-3" data-testid="calibration">
      <div className="grid gap-3 sm:grid-cols-3">
        {data.agents.map((a) => (
          <div key={a.agent} className={`card ${a.degraded ? "border-warn-500" : ""}`}>
            <p className="flex items-center justify-between text-sm font-semibold">
              {t(`agents.${a.agent}`, { defaultValue: a.agent })}
              <span className={`badge ${a.degraded ? "bg-warn-50 text-warn-700" : "bg-good-50 text-good-700"}`}>
                {a.degraded ? t("harness.degraded") : t("harness.healthy")}
              </span>
            </p>
            <dl className="mt-2 grid grid-cols-2 gap-1 text-xs text-ink-700">
              <dt>{t("harness.act_above")}</dt><dd>{pct(a.high_confidence_threshold)}</dd>
              <dt>{t("harness.ask_below")}</dt><dd>{pct(a.low_confidence_threshold)}</dd>
              <dt>{t("harness.auto_approve")}</dt><dd>{pct(a.auto_approve_factor)}</dd>
              {a.metrics.map((m) => (
                <div key={m.metric} className="contents">
                  <dt>{t(`harness.metrics.${m.metric}`, { defaultValue: m.metric })}</dt>
                  <dd>{pct(m.current_value)} / {pct(m.threshold)}{m.method_override ? ` · ${m.method_override}` : ""}</dd>
                </div>
              ))}
            </dl>
          </div>
        ))}
      </div>
      <figure className="card">
        <figcaption className="mb-2 text-sm font-semibold">{t("harness.error_over_time")}</figcaption>
        {rows.length === 0 ? (
          <p className="text-sm text-ink-500">{t("harness.no_history")}</p>
        ) : (
          <div className="h-56 w-full">
            <ResponsiveContainer>
              <LineChart data={rows} margin={{ top: 8, right: 8, bottom: 0, left: 0 }}>
                <CartesianGrid stroke="#e2e8f0" vertical={false} />
                <XAxis dataKey="date" tickFormatter={fmtDay} tick={{ fontSize: 11, fill: "#64748b" }} reversed={i18n.language === "ar"} />
                <YAxis tickFormatter={(v) => pct(Number(v))} tick={{ fontSize: 11, fill: "#64748b" }} width={40}
                  orientation={i18n.language === "ar" ? "right" : "left"} />
                <Tooltip labelFormatter={(l) => fmtDay(String(l))} formatter={(v: any, n: any) => [pct(Number(v)), n]} contentStyle={{ fontSize: 12, borderRadius: 8 }} />
                <Legend wrapperStyle={{ fontSize: 12, color: "#334155" }} />
                {Object.entries(SERIES).map(([agent, s]) => (
                  <Line key={agent} dataKey={agent} name={t(`agents.${agent}`, { defaultValue: agent })} stroke={s.color}
                    strokeDasharray={s.dash} strokeWidth={2} dot={false} connectNulls isAnimationActive={false} />
                ))}
              </LineChart>
            </ResponsiveContainer>
          </div>
        )}
      </figure>
    </section>
  );
}
