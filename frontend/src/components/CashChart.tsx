import { useState, type ReactNode } from "react";
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

export type Horizon = "30d" | "13w";

export interface WeekPoint {
  week_start: string;
  closing: Money;
  below_buffer: boolean;
}

// Lines differ by dash as well as colour, and the legend is always on.
const BALANCE = "#0d9488";
const AFTER = "#334155";
const BUFFER = "#dc2626";
const SCENARIO_STYLE: Record<string, { color: string; dash?: string }> = {
  expected: { color: BALANCE },
  pessimistic: { color: "#b45309", dash: "6 3" },
  optimistic: { color: "#2563eb", dash: "2 3" },
};

export function CashChart({
  series,
  buffer,
  lowest,
  simulated,
  horizon = "30d",
  onHorizonChange,
  weekly,
}: {
  series: CashPoint[];
  buffer: Money;
  lowest: { date: string; balance: Money } | null;
  simulated?: CashPoint[] | null;
  horizon?: Horizon;
  onHorizonChange?: (h: Horizon) => void;
  weekly?: Record<string, WeekPoint[]> | null;
}) {
  const { t, i18n } = useTranslation();
  const [asTable, setAsTable] = useState(false);
  const lang = i18n.language;
  const toggle = onHorizonChange && (
    <div role="tablist" aria-label={t("cash.horizon")} className="flex gap-1">
      {(["30d", "13w"] as const).map((h) => (
        <button
          key={h}
          type="button"
          role="tab"
          aria-selected={horizon === h}
          className={horizon === h ? "btn-primary" : "btn-secondary"}
          onClick={() => onHorizonChange(h)}
        >
          {t(`cash.horizons.${h}`)}
        </button>
      ))}
    </div>
  );
  if (horizon === "13w") return <WeeklyChart weekly={weekly ?? null} buffer={buffer} toggle={toggle} />;
  const after = new Map((simulated ?? []).map((p) => [p.date, p.closing]));
  const data = series.map((p) => ({
    date: p.date,
    balance: toMajor(p.closing),
    after: after.has(p.date) ? toMajor(after.get(p.date)!) : null,
  }));
  const fmtDay = (iso: string) =>
    new Date(iso + "T00:00:00").toLocaleDateString(lang === "ar" ? "ar-EG" : "en-GB", {
      day: "numeric",
      month: "short",
    });
  const money = (v: number) =>
    formatMoney({ ...buffer, amount_minor: Math.round(v * 10 ** buffer.decimals) }, lang);
  const compact = (v: number) =>
    new Intl.NumberFormat(lang === "ar" ? "ar-EG" : "en-US", { notation: "compact" }).format(v);

  return (
    <figure className="card" data-testid="cash-chart">
      <figcaption className="mb-2 flex flex-wrap items-center justify-between gap-2">
        <span className="text-sm font-semibold text-ink-900">{t("cash.chart_title")}</span>
        <span className="flex flex-wrap gap-2">
          {toggle}
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
              <XAxis
                dataKey="date"
                tickFormatter={fmtDay}
                tick={{ fontSize: 11, fill: "#64748b" }}
                minTickGap={24}
                reversed={lang === "ar"}
              />
              <YAxis
                tickFormatter={compact}
                tick={{ fontSize: 11, fill: "#64748b" }}
                width={48}
                orientation={lang === "ar" ? "right" : "left"}
              />
              <Tooltip
                labelFormatter={(l) => fmtDay(String(l))}
                formatter={(value: any, name: any) => [value === null ? "—" : money(Number(value)), name]}
                contentStyle={{ fontSize: 12, borderRadius: 8 }}
              />
              <Legend wrapperStyle={{ fontSize: 12, color: "#334155" }} />
              <ReferenceLine
                y={toMajor(buffer)}
                stroke={BUFFER}
                strokeDasharray="4 4"
                label={{
                  value: t("cash.buffer_line"),
                  fontSize: 11,
                  fill: BUFFER,
                  position: "insideTopRight",
                }}
              />
              <Line
                dataKey="balance"
                name={t("cash.balance")}
                stroke={BALANCE}
                strokeWidth={2}
                dot={false}
                isAnimationActive={false}
              />
              {simulated && (
                <Line
                  dataKey="after"
                  name={t("cash.after_action")}
                  stroke={AFTER}
                  strokeWidth={2}
                  strokeDasharray="6 4"
                  dot={false}
                  isAnimationActive={false}
                />
              )}
              {lowest && (
                <ReferenceDot
                  x={lowest.date}
                  y={toMajor(lowest.balance)}
                  r={5}
                  fill={BUFFER}
                  stroke="#fff"
                  label={{
                    value: t("cash.lowest_marker"),
                    fontSize: 11,
                    fill: "#334155",
                    position: "bottom",
                  }}
                />
              )}
            </ComposedChart>
          </ResponsiveContainer>
        </div>
      )}
    </figure>
  );
}

function WeeklyChart({
  weekly,
  buffer,
  toggle,
}: {
  weekly: Record<string, WeekPoint[]> | null;
  buffer: Money;
  toggle: ReactNode;
}) {
  const { t, i18n } = useTranslation();
  const lang = i18n.language;
  const [asTable, setAsTable] = useState(false);
  const scenarios = ["expected", "pessimistic", "optimistic"].filter((s) => weekly?.[s]);
  const weeks = weekly?.expected ?? [];
  const data = weeks.map((w, i) => ({
    date: w.week_start,
    ...Object.fromEntries(scenarios.map((s) => [s, weekly?.[s][i] ? toMajor(weekly[s][i].closing) : null])),
  }));
  const fmtDay = (iso: string) =>
    new Date(iso + "T00:00:00").toLocaleDateString(lang === "ar" ? "ar-EG" : "en-GB", {
      day: "numeric",
      month: "short",
    });
  const money = (v: number) =>
    formatMoney({ ...buffer, amount_minor: Math.round(v * 10 ** buffer.decimals) }, lang);
  const compact = (v: number) =>
    new Intl.NumberFormat(lang === "ar" ? "ar-EG" : "en-US", { notation: "compact" }).format(v);
  return (
    <figure className="card" data-testid="cash-chart-13w">
      <figcaption className="mb-2 flex flex-wrap items-center justify-between gap-2">
        <span className="text-sm font-semibold text-ink-900">{t("cash.chart_title_13w")}</span>
        <span className="flex flex-wrap gap-2">
          {toggle}
          <button type="button" className="btn-secondary" onClick={() => setAsTable((v) => !v)}>
            {asTable ? t("stock.show_chart") : t("stock.show_table")}
          </button>
        </span>
      </figcaption>
      {!weekly ? (
        <p className="text-sm text-ink-500">{t("app.loading")}</p>
      ) : asTable ? (
        <div className="max-h-72 overflow-auto">
          <table className="table">
            <thead>
              <tr>
                <th>{t("cash.week_of")}</th>
                {scenarios.map((s) => (
                  <th key={s}>{t(`cash.scenarios.${s}`)}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {weeks.map((w, i) => (
                <tr key={w.week_start}>
                  <td>{fmtDay(w.week_start)}</td>
                  {scenarios.map((s) => {
                    const p = weekly[s][i];
                    return (
                      <td key={s} className={`whitespace-nowrap ${p?.below_buffer ? "text-bad-700" : ""}`}>
                        {formatMoney(p?.closing, lang)}
                      </td>
                    );
                  })}
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
              <XAxis
                dataKey="date"
                tickFormatter={fmtDay}
                tick={{ fontSize: 11, fill: "#64748b" }}
                reversed={lang === "ar"}
              />
              <YAxis
                tickFormatter={compact}
                tick={{ fontSize: 11, fill: "#64748b" }}
                width={48}
                orientation={lang === "ar" ? "right" : "left"}
              />
              <Tooltip
                labelFormatter={(l) => t("cash.week_of_date", { date: fmtDay(String(l)) })}
                formatter={(value: any, name: any) => [value === null ? "—" : money(Number(value)), name]}
                contentStyle={{ fontSize: 12, borderRadius: 8 }}
              />
              <Legend wrapperStyle={{ fontSize: 12, color: "#334155" }} />
              <ReferenceLine
                y={toMajor(buffer)}
                stroke={BUFFER}
                strokeDasharray="4 4"
                label={{
                  value: t("cash.buffer_line"),
                  fontSize: 11,
                  fill: BUFFER,
                  position: "insideTopRight",
                }}
              />
              {scenarios.map((s) => (
                <Line
                  key={s}
                  dataKey={s}
                  name={t(`cash.scenarios.${s}`)}
                  stroke={SCENARIO_STYLE[s].color}
                  strokeDasharray={SCENARIO_STYLE[s].dash}
                  strokeWidth={2}
                  dot={{ r: 2 }}
                  isAnimationActive={false}
                />
              ))}
            </ComposedChart>
          </ResponsiveContainer>
        </div>
      )}
    </figure>
  );
}
