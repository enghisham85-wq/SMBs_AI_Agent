import { useTranslation } from "react-i18next";
import type { Money } from "../api/types";
import { formatMoney } from "../lib/money";

export interface StockItem {
  id: string;
  name_en: string;
  name_ar: string;
  unit: string;
  quantity: string;
  days_of_cover: number | null;
  projected_stockout: string | null;
  reorder_status: "ok" | "reorder" | "on_order";
  is_critical: boolean;
  open_orders: { po: string; status: string; qty: string; expected_date: string }[];
  expiry_risk: { days_to_use: number | null; shelf_life_days: number; suggestion: string } | null;
  supplier: string | null;
  unit_cost?: Money;
}

const STATUS_STYLE: Record<StockItem["reorder_status"], string> = {
  ok: "bg-good-50 text-good-700",
  reorder: "bg-bad-50 text-bad-700",
  on_order: "bg-warn-50 text-warn-700",
};

export function StockTable({ items, selected, onSelect }: { items: StockItem[]; selected: string | null; onSelect: (id: string) => void }) {
  const { t, i18n } = useTranslation();
  const ar = i18n.language === "ar";
  const qty = (v: string) => Number(v).toLocaleString(ar ? "ar-EG" : "en-US", { maximumFractionDigits: 2 });
  return (
    <div className="card overflow-x-auto p-0">
      <table className="table">
        <thead>
          <tr>
            <th>{t("stock.item")}</th>
            <th>{t("stock.in_stock")}</th>
            <th>{t("stock.cover")}</th>
            <th>{t("stock.status")}</th>
            <th className="hidden md:table-cell">{t("stock.expiry")}</th>
            {items.some((i) => i.unit_cost) && <th className="hidden md:table-cell">{t("stock.unit_cost")}</th>}
          </tr>
        </thead>
        <tbody>
          {items.map((it) => (
            <tr
              key={it.id}
              className={`cursor-pointer hover:bg-slate-50 ${selected === it.id ? "bg-brand-50" : ""}`}
              onClick={() => onSelect(it.id)}
            >
              <td>
                <span className="font-medium">{ar ? it.name_ar : it.name_en}</span>
                {it.is_critical && <span className="badge ms-1 bg-slate-100 text-ink-700">{t("stock.critical")}</span>}
                <span className="block text-xs text-ink-500">{it.supplier}</span>
              </td>
              <td className="whitespace-nowrap">
                {qty(it.quantity)} {it.unit}
              </td>
              <td className="whitespace-nowrap">
                {it.days_of_cover === null ? "—" : t("stock.days", { count: it.days_of_cover })}
                {it.projected_stockout && (
                  <span className="block text-xs text-ink-500">{t("stock.runs_out", { date: it.projected_stockout })}</span>
                )}
              </td>
              <td>
                <span className={`badge ${STATUS_STYLE[it.reorder_status]}`}>{t(`stock.statuses.${it.reorder_status}`)}</span>
                {it.open_orders.map((o) => (
                  <span key={o.po} className="block text-xs text-ink-500">
                    {o.po} · {t(`stock.po_status.${o.status}`, { defaultValue: o.status })}
                  </span>
                ))}
              </td>
              <td className="hidden md:table-cell">
                {it.expiry_risk ? (
                  <span className="text-xs text-warn-700">{t("stock.expiry_risk", { days: it.expiry_risk.shelf_life_days })}</span>
                ) : (
                  <span className="text-xs text-ink-400">—</span>
                )}
              </td>
              {it.unit_cost && <td className="hidden whitespace-nowrap md:table-cell">{formatMoney(it.unit_cost, i18n.language)}</td>}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
