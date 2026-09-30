import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { useTranslation } from "react-i18next";
import { api, ApiError } from "../api/client";
import type { DataAsOf, Money } from "../api/types";
import { CashChart, type CashPoint, type Horizon, type WeekPoint } from "../components/CashChart";
import { FreshnessLabel } from "../components/FreshnessLabel";
import { ShortfallPlan, type PlanOut } from "../components/ShortfallPlan";
import { formatMoney } from "../lib/money";

interface Forecast {
  scenario: string;
  primary_scenario: string;
  generated_on: string;
  series: (CashPoint & { below_buffer: boolean })[];
  buffer: Money;
  lowest: { date: string; balance: Money } | null;
  first_below_buffer: string | null;
  confidence: { low: boolean; reason: string | null };
  data_as_of: DataAsOf;
}

interface Position {
  accounts: { id: string; name: string; is_cash_on_hand: boolean; balance: Money }[];
  total: Money;
  cash_on_hand: Money;
  committed_7d: { total: Money; items: { date: string; kind: string; label: string; amount: Money }[] };
  data_as_of: DataAsOf;
}

const SCENARIOS = ["expected", "pessimistic", "optimistic"] as const;

export function Cash() {
  const { t, i18n } = useTranslation();
  const lang = i18n.language;
  const [scenario, setScenario] = useState<string | null>(null);
  const [simulating, setSimulating] = useState<string | null>(null);
  const [horizon, setHorizon] = useState<Horizon>("30d");
  const weekly = useQuery({
    queryKey: ["cash", "forecast", "13w"],
    enabled: horizon === "13w",
    queryFn: () => api.get<{ scenarios: Record<string, WeekPoint[]>; data_as_of: DataAsOf }>("/cash/forecast?horizon=13w"),
  });
  const position = useQuery({ queryKey: ["cash", "position"], queryFn: () => api.get<Position>("/cash/position") });
  const forecast = useQuery({
    queryKey: ["cash", "forecast", scenario],
    queryFn: () => api.get<Forecast>(`/cash/forecast?horizon=30d${scenario ? `&scenario=${scenario}` : ""}`),
  });
  const plan = useQuery({ queryKey: ["cash", "plan"], queryFn: () => api.get<{ plan: PlanOut | null }>("/cash/shortfall-plan") });
  const sim = useQuery({
    queryKey: ["cash", "simulate", simulating],
    enabled: !!simulating,
    queryFn: () => api.post<{ simulated: CashPoint[] }>(`/cash/shortfall-plan/actions/${simulating}/simulate`),
  });
  const f = forecast.data;
  const p = position.data;
  const day = (iso: string) =>
    new Date(iso + "T00:00:00").toLocaleDateString(lang === "ar" ? "ar-EG" : "en-GB", { day: "numeric", month: "short" });

  return (
    <div className="flex flex-col gap-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h1 className="text-xl font-semibold">{t("nav.cash")}</h1>
        <div role="tablist" className="flex flex-wrap gap-1" aria-label={t("cash.scenario")}>
          {SCENARIOS.map((s) => {
            const active = (scenario ?? f?.primary_scenario ?? "expected") === s;
            return (
              <button key={s} role="tab" aria-selected={active} className={active ? "btn-primary" : "btn-secondary"} onClick={() => setScenario(s)}>
                {t(`cash.scenarios.${s}`)}
              </button>
            );
          })}
        </div>
      </div>

      {f?.confidence.low && (
        <div className="card border-warn-500 bg-warn-50 text-sm text-warn-700" role="alert" data-testid="low-confidence">
          {t("cash.low_confidence", { reason: f.confidence.reason ?? "" })}
        </div>
      )}

      <div className="grid gap-3 sm:grid-cols-3">
        <div className="card">
          <p className="text-xs text-ink-500">{t("cash.today_cash")}</p>
          <p className="text-lg font-semibold">{formatMoney(p?.total, lang)}</p>
          <p className="text-xs text-ink-500">{t("cash.cash_on_hand")}: {formatMoney(p?.cash_on_hand, lang)}</p>
        </div>
        <div className="card">
          <p className="text-xs text-ink-500">{t("cash.lowest_30d")}</p>
          <p className={`text-lg font-semibold ${f?.lowest && f.lowest.balance.amount_minor < f.buffer.amount_minor ? "text-bad-700" : ""}`}>
            {formatMoney(f?.lowest?.balance, lang)}
          </p>
          <p className="text-xs text-ink-500">{f?.lowest ? day(f.lowest.date) : "—"} · {t("cash.buffer")}: {formatMoney(f?.buffer, lang)}</p>
        </div>
        <div className="card">
          <p className="text-xs text-ink-500">{t("cash.committed_7d")}</p>
          <p className="text-lg font-semibold">{formatMoney(p?.committed_7d.total, lang)}</p>
          <p className="text-xs text-ink-500">{t("cash.items", { count: p?.committed_7d.items.length ?? 0 })}</p>
        </div>
      </div>
      <FreshnessLabel asOf={f?.data_as_of} />

      {f && (
        <CashChart
          series={f.series}
          buffer={f.buffer}
          lowest={f.lowest}
          simulated={simulating ? sim.data?.simulated ?? null : null}
          horizon={horizon}
          onHorizonChange={setHorizon}
          weekly={weekly.data?.scenarios ?? null}
        />
      )}
      {horizon === "13w" && weekly.data && <FreshnessLabel asOf={weekly.data.data_as_of} />}
      {plan.data?.plan && <ShortfallPlan plan={plan.data.plan} simulating={simulating} onSimulate={setSimulating} />}
      {plan.data && !plan.data.plan && f && !f.first_below_buffer && <p className="text-sm text-good-700">{t("cash.no_shortfall")}</p>}

      <div className="grid gap-4 lg:grid-cols-2">
        <Receivables />
        <Payables />
      </div>
      <StatementUpload />
    </div>
  );
}

function Receivables() {
  const { t, i18n } = useTranslation();
  const lang = i18n.language;
  const q = useQuery({ queryKey: ["cash", "receivables"], queryFn: () => api.get<any>("/cash/receivables") });
  const d = q.data;
  return (
    <section className="card overflow-x-auto" data-testid="receivables-ageing">
      <h2 className="mb-2 text-sm font-semibold">{t("cash.receivables")}</h2>
      {d && (
        <p className="mb-2 flex flex-wrap gap-3 text-xs text-ink-500">
          {Object.entries(d.ageing as Record<string, Money>).map(([k, v]) => (
            <span key={k}>{t(`cash.buckets.${k}`, { defaultValue: k })}: {formatMoney(v, lang)}</span>
          ))}
        </p>
      )}
      <table className="table">
        <thead>
          <tr><th>{t("cash.customer")}</th><th>{t("cash.due")}</th><th>{t("cash.owed")}</th><th>{t("cash.reminders")}</th></tr>
        </thead>
        <tbody>
          {(d?.receivables ?? []).map((r: any) => (
            <tr key={r.id}>
              <td>{r.customer}<span className="block text-xs text-ink-500">{r.number}</span></td>
              <td>{r.due_date}{r.days_overdue > 0 && <span className="block text-xs text-bad-700">{t("cash.days_overdue", { count: r.days_overdue })}</span>}</td>
              <td className="whitespace-nowrap">{formatMoney(r.outstanding, lang)}</td>
              <td className="text-xs">
                {r.reminders.length === 0 ? "—" : r.reminders.map((m: any) => (
                  <span key={m.id} className="block">{t("cash.level", { level: m.level })}: {t(`cash.reminder_status.${m.status}`, { defaultValue: m.status })}</span>
                ))}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </section>
  );
}

function Payables() {
  const { t, i18n } = useTranslation();
  const lang = i18n.language;
  const q = useQuery({ queryKey: ["cash", "payables"], queryFn: () => api.get<any>("/cash/payables") });
  const d = q.data;
  return (
    <section className="card overflow-x-auto" data-testid="payables-schedule">
      <h2 className="mb-2 text-sm font-semibold">{t("cash.payables")}</h2>
      {d?.tight && <p className="mb-2 text-xs text-warn-700">{t("cash.tight")}</p>}
      <table className="table">
        <thead>
          <tr><th>{t("cash.supplier")}</th><th>{t("cash.due")}</th><th>{t("cash.pay_on")}</th><th>{t("cash.owed")}</th></tr>
        </thead>
        <tbody>
          {(d?.payables ?? []).map((p: any) => (
            <tr key={p.id}>
              <td>{p.label}</td>
              <td>{p.due_date}</td>
              <td>{p.pay_on}<span className="block text-xs text-ink-500">{lang === "ar" ? p.reason_ar : p.reason_en}</span></td>
              <td className="whitespace-nowrap">{formatMoney(p.outstanding, lang)}</td>
            </tr>
          ))}
          {(d?.payables ?? []).length === 0 && (
            <tr><td colSpan={4} className="text-ink-500">{t("common.none")}</td></tr>
          )}
        </tbody>
      </table>
    </section>
  );
}

function StatementUpload() {
  const { t, i18n } = useTranslation();
  const qc = useQueryClient();
  const [msg, setMsg] = useState<string | null>(null);
  const upload = useMutation({
    mutationFn: (file: File) => {
      const form = new FormData();
      form.append("file", file);
      return api.upload<{ imported: number; skipped_duplicates: number; errors: unknown[] }>("/bank/statements", form);
    },
    onSuccess: (r) => {
      setMsg(t("cash.imported", { imported: r.imported, skipped: r.skipped_duplicates, errors: r.errors.length }));
      void qc.invalidateQueries({ queryKey: ["cash"] });
    },
    onError: (e) => setMsg(e instanceof ApiError ? e.message_for(i18n.language) : String(e)),
  });
  return (
    <div className="card flex flex-wrap items-center gap-3">
      <label className="btn-primary cursor-pointer">
        {upload.isPending ? t("cash.uploading") : t("cash.upload_statement")}
        <input type="file" accept=".csv,text/csv" className="hidden" disabled={upload.isPending}
          onChange={(e) => e.target.files?.[0] && upload.mutate(e.target.files[0])} />
      </label>
      <span className="text-xs text-ink-500">{t("cash.statement_hint")}</span>
      {msg && <span className="text-sm text-ink-700" role="status">{msg}</span>}
    </div>
  );
}
