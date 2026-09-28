import { useState } from "react";
import { useTranslation } from "react-i18next";
import {
  Area,
  CartesianGrid,
  ComposedChart,
  Legend,
  Line,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";

export interface ForecastPoint {
  date: string;
  actual: number | null;
  forecast: number | null;
  low: number | null;
  high: number | null;
}

// Two series on one axis: actual (solid, dark ink) and forecast (dashed, brand teal) with a light
// range band. Identity never relies on colour alone: line style differs and a legend is shown.
const ACTUAL = "#334155";
const FORECAST = "#0d9488";
const BAND = "#99f6e4";

export function ForecastChart({ series, unit, today, method }: { series: ForecastPoint[]; unit: string; today: string; method: string }) {
  const { t, i18n } = useTranslation();
  const [asTable, setAsTable] = useState(false);
  const data = series.map((p) => ({ ...p, band: p.low !== null && p.high !== null ? [p.low, p.high] : null }));
  const fmtDay = (iso: string) =>
    new Date(iso + "T00:00:00").toLocaleDateString(i18n.language === "ar" ? "ar-EG" : "en-GB", { day: "numeric", month: "short" });
  const fmt = (v: number | null | undefined) => (v === null || v === undefined ? "—" : v.toFixed(1));

  return (
    <figure className="card" data-testid="forecast-chart">
      <figcaption className="mb-2 flex flex-wrap items-center justify-between gap-2">
        <span className="text-sm font-semibold text-ink-900">{t("stock.forecast_title", { unit })}</span>
        <span className="flex items-center gap-2 text-xs text-ink-500">
          {t("stock.method")}: {t(`stock.methods.${method}`, { defaultValue: method })}
          <button type="button" className="btn-secondary" onClick={() => setAsTable((v) => !v)}>
            {asTable ? t("stock.show_chart") : t("stock.show_table")}
          </button>
        </span>
      </figcaption>
      {asTable ? (
        <div className="max-h-72 overflow-auto">
          <table className="table">
            <thead>
              <tr>
                <th>{t("stock.date")}</th>
                <th>{t("stock.actual")}</th>
                <th>{t("stock.forecast")}</th>
                <th>{t("stock.range")}</th>
              </tr>
            </thead>
            <tbody>
              {series.map((p) => (
                <tr key={p.date}>
                  <td>{fmtDay(p.date)}</td>
                  <td>{fmt(p.actual)}</td>
                  <td>{fmt(p.forecast)}</td>
                  <td>{p.low !== null ? `${fmt(p.low)} – ${fmt(p.high)}` : "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : (
        <div className="h-64 w-full">
          <ResponsiveContainer>
            <ComposedChart data={data} margin={{ top: 8, right: 8, bottom: 0, left: 0 }}>
              <CartesianGrid stroke="#e2e8f0" vertical={false} />
              <XAxis dataKey="date" tickFormatter={fmtDay} tick={{ fontSize: 11, fill: "#64748b" }} minTickGap={24} reversed={i18n.language === "ar"} />
              <YAxis tick={{ fontSize: 11, fill: "#64748b" }} width={40} orientation={i18n.language === "ar" ? "right" : "left"} />
              <Tooltip
                labelFormatter={(l) => fmtDay(String(l))}
                formatter={(value: any, name: any) => [Array.isArray(value) ? `${fmt(value[0])} – ${fmt(value[1])}` : fmt(value), name]}
                contentStyle={{ fontSize: 12, borderRadius: 8 }}
              />
              <Legend wrapperStyle={{ fontSize: 12, color: "#334155" }} />
              <Area dataKey="band" name={t("stock.range")} stroke="none" fill={BAND} fillOpacity={0.5} isAnimationActive={false} />
              <Line dataKey="actual" name={t("stock.actual")} stroke={ACTUAL} strokeWidth={2} dot={false} connectNulls={false} isAnimationActive={false} />
              <Line dataKey="forecast" name={t("stock.forecast")} stroke={FORECAST} strokeWidth={2} strokeDasharray="6 4" dot={false} isAnimationActive={false} />
              <ReferenceLine x={today} stroke="#94a3b8" strokeDasharray="2 2" label={{ value: t("stock.today"), fontSize: 11, fill: "#64748b", position: "top" }} />
            </ComposedChart>
          </ResponsiveContainer>
        </div>
      )}
    </figure>
  );
}
