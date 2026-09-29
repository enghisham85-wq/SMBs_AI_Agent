import { useState } from "react";
import { useTranslation } from "react-i18next";
import {
  CartesianGrid,
  ComposedChart,
  Legend,
  Line,
  ReferenceDot,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import type { Money } from "../api/types";
import { formatMoney, toMajor } from "../lib/money";

export interface CashPoint {
  date: string;
  closing: Money;
}

// Projected balance (solid teal), the owner's minimum buffer (dashed red) and, when simulating an
// action, the balance after it (dashed dark ink). Line style differs, and a legend is always shown.
const BALANCE = "#0d9488";
const AFTER = "#334155";
const BUFFER = "#dc2626";

export function CashChart({
  series,
  buffer,
  lowest,
  simulated,
}: {
  series: CashPoint[];
  buffer: Money;
  lowest: { date: string; balance: Money } | null;
  simulated?: CashPoint[] | null;
}) {
  const { t, i18n } = useTranslation();
  const [asTable, setAsTable] = useState(false);
  const lang = i18n.language;
  const after = new Map((simulated ?? []).map((p) => [p.date, p.closing]));
  const data = series.map((p) => ({
    date: p.date,
    balance: toMajor(p.closing),
    after: after.has(p.date) ? toMajor(after.get(p.date)!) : null,
  }));
  const fmtDay = (iso: string) =>
    new Date(iso + "T00:00:00").toLocaleDateString(lang === "ar" ? "ar-EG" : "en-GB", { day: "numeric", month: "short" });
  const money = (v: number) => formatMoney({ ...buffer, amount_minor: Math.round(v * 10 ** buffer.decimals) }, lang);
  const compact = (v: number) => new Intl.NumberFormat(lang === "ar" ? "ar-EG" : "en-US", { notation: "compact" }).format(v);

  return (
    <figure className="card" data-testid="cash-chart">
      <figcaption className="mb-2 flex flex-wrap items-center justify-between gap-2">
        <span className="text-sm font-semibold text-ink-900">{t("cash.chart_title")}</span>
        <button type="button" className="btn-secondary" onClick={() => setAsTable((v) => !v)}>
          {asTable ? t("stock.show_chart") : t("stock.show_table")}
        </button>
      </figcaption>
      {asTable ? (
        <div className="max-h-72 overflow-auto">
          <table className="table">
            <thead>
              <tr>
                <th>{t("stock.date")}</th>
                <th>{t("cash.balance")}</th>
                {simulated && <th>{t("cash.after_action")}</th>}
              </tr>
            </thead>
            <tbody>
              {series.map((p) => (
                <tr key={p.date} className={p.closing.amount_minor < buffer.amount_minor ? "bg-bad-50" : ""}>
                  <td>{fmtDay(p.date)}</td>
                  <td className="whitespace-nowrap">{formatMoney(p.closing, lang)}</td>
                  {simulated && <td className="whitespace-nowrap">{formatMoney(after.get(p.date), lang)}</td>}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : (
        <div className="h-72 w-full">
          <ResponsiveContainer>
            <ComposedChart data={data} margin={{ top: 16, right: 8, bottom: 0, left: 0 }}>
              <CartesianGrid stroke="#e2e8f0" vertical={false} />
              <XAxis dataKey="date" tickFormatter={fmtDay} tick={{ fontSize: 11, fill: "#64748b" }} minTickGap={24} reversed={lang === "ar"} />
              <YAxis tickFormatter={compact} tick={{ fontSize: 11, fill: "#64748b" }} width={48} orientation={lang === "ar" ? "right" : "left"} />
              <Tooltip
                labelFormatter={(l) => fmtDay(String(l))}
                formatter={(value: any, name: any) => [value === null ? "—" : money(Number(value)), name]}
                contentStyle={{ fontSize: 12, borderRadius: 8 }}
              />
              <Legend wrapperStyle={{ fontSize: 12, color: "#334155" }} />
              <ReferenceLine y={toMajor(buffer)} stroke={BUFFER} strokeDasharray="4 4"
                label={{ value: t("cash.buffer_line"), fontSize: 11, fill: BUFFER, position: "insideTopRight" }} />
              <Line dataKey="balance" name={t("cash.balance")} stroke={BALANCE} strokeWidth={2} dot={false} isAnimationActive={false} />
              {simulated && (
                <Line dataKey="after" name={t("cash.after_action")} stroke={AFTER} strokeWidth={2} strokeDasharray="6 4" dot={false} isAnimationActive={false} />
              )}
              {lowest && (
                <ReferenceDot x={lowest.date} y={toMajor(lowest.balance)} r={5} fill={BUFFER} stroke="#fff"
                  label={{ value: t("cash.lowest_marker"), fontSize: 11, fill: "#334155", position: "bottom" }} />
              )}
            </ComposedChart>
          </ResponsiveContainer>
        </div>
      )}
    </figure>
  );
}
