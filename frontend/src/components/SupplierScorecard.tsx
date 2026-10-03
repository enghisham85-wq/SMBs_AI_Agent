import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { useTranslation } from "react-i18next";
import { api } from "../api/client";
import type { DataAsOf, Money } from "../api/types";
import { formatMoney } from "../lib/money";
import { FreshnessLabel } from "./FreshnessLabel";

interface SupplierRow {
  id: string;
  name_en: string;
  name_ar: string;
  reliability_score: number;
}

interface Scorecard {
  supplier: { id: string; name_en: string; name_ar: string; payment_terms_days: number };
  lead_time: {
    stated_days: number;
    observed_days: number | null;
    average_actual_days: number | null;
    planning_days: number;
  };
  reliability_score: number;
  deliveries: {
    count: number;
    on_time: number;
    complete: number;
    recent: {
      po: string;
      received_on: string;
      lead_days: number | null;
      on_time: boolean;
      complete: boolean;
      price_ok: boolean;
    }[];
  };
  orders: { count: number; open: number };
  prices: {
    item_id: string;
    name_en: string;
    name_ar: string;
    latest_change_pct: number | null;
    history: { valid_from: string; price: Money; unit: string; change_pct: number | null }[];
  }[];
  data_as_of: DataAsOf;
}

/** Supplier scorecard: stated vs observed lead time, delivery record, price changes, reliability. */
export function SupplierScorecard() {
  const { t, i18n } = useTranslation();
  const lang = i18n.language;
  const ar = lang === "ar";
  const list = useQuery({
    queryKey: ["suppliers"],
    queryFn: ({ signal }) => api.get<{ suppliers: SupplierRow[] }>("/suppliers", signal),
  });
  const [picked, setPicked] = useState<string | null>(null);
  const id = picked ?? list.data?.suppliers[0]?.id ?? null;
  const card = useQuery({
    queryKey: ["suppliers", id, "scorecard"],
    enabled: !!id,
    queryFn: ({ signal }) => api.get<Scorecard>(`/suppliers/${id}/scorecard`, signal),
  });
  const d = card.data;
  const num = (n: number | null, digits = 1) =>
    n === null ? "—" : n.toLocaleString(ar ? "ar-EG" : "en-US", { maximumFractionDigits: digits });
  const pct = (n: number | null) => (n === null ? "—" : `${n > 0 ? "+" : ""}${num(n)}%`);
  const day = (iso: string) =>
    new Date(iso + "T00:00:00").toLocaleDateString(ar ? "ar-EG" : "en-GB", {
      day: "numeric",
      month: "short",
    });
  const slower =
    d && d.lead_time.observed_days !== null && d.lead_time.observed_days > d.lead_time.stated_days;

  return (
    <div className="flex flex-col gap-3" data-testid="supplier-scorecard">
      <label className="flex flex-wrap items-center gap-2 text-sm">
        <span>{t("scorecard.supplier")}</span>
        <select className="input w-auto" value={id ?? ""} onChange={(e) => setPicked(e.target.value)}>
          {(list.data?.suppliers ?? []).map((s) => (
            <option key={s.id} value={s.id}>
              {ar ? s.name_ar || s.name_en : s.name_en}
            </option>
          ))}
        </select>
      </label>
      {d && (
        <>
          <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
            <Stat
              label={t("scorecard.stated_lead")}
              value={t("scorecard.days", { n: num(d.lead_time.stated_days) })}
            />
            <Stat
              label={t("scorecard.observed_lead")}
              value={
                d.lead_time.observed_days === null
                  ? "—"
                  : t("scorecard.days", { n: num(d.lead_time.observed_days) })
              }
              warn={!!slower}
              note={slower ? t("scorecard.slower") : undefined}
            />
            <Stat
              label={t("scorecard.reliability")}
              value={`${num(d.reliability_score * 100, 0)}%`}
              warn={d.reliability_score < 0.8}
            />
            <Stat
              label={t("scorecard.deliveries")}
              value={`${num(d.deliveries.on_time, 0)}/${num(d.deliveries.count, 0)}`}
              note={t("scorecard.on_time_complete", { complete: num(d.deliveries.complete, 0) })}
            />
          </div>
          <FreshnessLabel asOf={d.data_as_of} />
          <section className="card overflow-x-auto p-0">
            <h3 className="px-3 pt-2 text-sm font-semibold">{t("scorecard.recent_deliveries")}</h3>
            {d.deliveries.recent.length === 0 ? (
              <p className="px-3 pb-3 text-sm text-ink-500">{t("common.none")}</p>
            ) : (
              <table className="table">
                <thead>
                  <tr>
                    <th>{t("scorecard.order")}</th>
                    <th>{t("scorecard.received")}</th>
                    <th>{t("scorecard.lead")}</th>
                    <th>{t("scorecard.result")}</th>
                  </tr>
                </thead>
                <tbody>
                  {d.deliveries.recent.map((r) => (
                    <tr key={`${r.po}-${r.received_on}`}>
                      <td>{r.po}</td>
                      <td>{day(r.received_on)}</td>
                      <td>{r.lead_days === null ? "—" : t("scorecard.days", { n: num(r.lead_days, 0) })}</td>
                      <td className="text-xs">
                        <span className={r.on_time ? "text-good-700" : "text-bad-700"}>
                          {r.on_time ? t("scorecard.on_time") : t("scorecard.late")}
                        </span>
                        {" · "}
                        <span className={r.complete ? "text-good-700" : "text-bad-700"}>
                          {r.complete ? t("scorecard.complete") : t("scorecard.short")}
                        </span>
                        {!r.price_ok && (
                          <span className="text-bad-700"> · {t("scorecard.price_differs")}</span>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </section>
          <section className="card overflow-x-auto p-0">
            <h3 className="px-3 pt-2 text-sm font-semibold">{t("scorecard.prices")}</h3>
            <table className="table">
              <thead>
                <tr>
                  <th>{t("scorecard.item")}</th>
                  <th>{t("scorecard.current_price")}</th>
                  <th>{t("scorecard.last_change")}</th>
                  <th>{t("scorecard.history")}</th>
                </tr>
              </thead>
              <tbody>
                {d.prices.map((p) => {
                  const last = p.history[p.history.length - 1];
                  return (
                    <tr key={p.item_id}>
                      <td>{ar ? p.name_ar || p.name_en : p.name_en}</td>
                      <td className="whitespace-nowrap">
                        {formatMoney(last?.price, lang)} / {last?.unit}
                      </td>
                      <td
                        className={
                          p.latest_change_pct !== null && p.latest_change_pct > 0 ? "text-bad-700" : ""
                        }
                      >
                        {pct(p.latest_change_pct)}
                      </td>
                      <td className="text-xs text-ink-700">
                        {p.history
                          .map((h) => `${day(h.valid_from)}: ${formatMoney(h.price, lang)}`)
                          .join(" → ")}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </section>
        </>
      )}
    </div>
  );
}

function Stat({
  label,
  value,
  note,
  warn = false,
}: {
  label: string;
  value: string;
  note?: string;
  warn?: boolean;
}) {
  return (
    <div className={`card flex flex-col gap-1 border-s-4 ${warn ? "border-warn-500" : "border-slate-200"}`}>
      <span className="text-xs font-semibold uppercase text-ink-500">{label}</span>
      <strong className="text-lg text-ink-900">{value}</strong>
      {note && <span className="text-xs text-ink-700">{note}</span>}
    </div>
  );
}
