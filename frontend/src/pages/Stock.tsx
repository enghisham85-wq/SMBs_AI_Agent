import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { useTranslation } from "react-i18next";
import { api, ApiError } from "../api/client";
import type { DataAsOf, Money } from "../api/types";
import { ForecastChart, type ForecastPoint } from "../components/ForecastChart";
import { FreshnessLabel } from "../components/FreshnessLabel";
import { StockTable, type StockItem } from "../components/StockTable";
import { useAuth } from "../hooks/useAuth";
import { formatMoney } from "../lib/money";

interface PO {
  id: string;
  number: string;
  status: string;
  supplier: { name_en: string; name_ar: string } | null;
  total?: Money;
  expected_date: string;
  late: boolean;
  lines: { item_id: string; name_en: string; name_ar: string; qty: string; unit: string; line_total?: Money }[];
  deliveries: { id: string; received_on: string; discrepancies: any[] }[];
}

type Tab = "items" | "orders" | "delivery" | "waste" | "sales";

export function Stock() {
  const { t, i18n } = useTranslation();
  const { can } = useAuth();
  const [tab, setTab] = useState<Tab>("items");
  const tabs: Tab[] = can("manager") ? ["items", "orders", "delivery", "waste", "sales"] : ["items", "delivery", "waste"];

  return (
    <div className="flex flex-col gap-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h1 className="text-xl font-semibold">{t("nav.stock")}</h1>
        <div role="tablist" className="flex flex-wrap gap-1">
          {tabs.map((k) => (
            <button key={k} role="tab" aria-selected={tab === k} className={tab === k ? "btn-primary" : "btn-secondary"} onClick={() => setTab(k)}>
              {t(`stock.tabs.${k}`)}
            </button>
          ))}
        </div>
      </div>
      {tab === "items" && <ItemsTab />}
      {tab === "orders" && <OrdersTab />}
      {tab === "delivery" && <DeliveryTab />}
      {tab === "waste" && <WasteTab />}
      {tab === "sales" && <SalesTab lang={i18n.language} />}
    </div>
  );
}

function ItemsTab() {
  const { t } = useTranslation();
  const { can } = useAuth();
  const [selected, setSelected] = useState<string | null>(null);
  const items = useQuery({
    queryKey: ["stock", "items"],
    queryFn: () => api.get<{ items: StockItem[]; data_as_of: DataAsOf }>("/stock/items"),
  });
  const chosen = items.data?.items.find((i) => i.id === selected) ?? null;
  const forecast = useQuery({
    queryKey: ["stock", "forecast", selected],
    enabled: !!selected && can("manager"),
    queryFn: () => api.get<{ method: string; series: ForecastPoint[]; data_as_of: DataAsOf }>(`/stock/items/${selected}/forecast`),
  });
  const today = new Date().toISOString().slice(0, 10);
  const todayPoint = forecast.data?.series.find((p) => p.actual === null)?.date ?? today;

  if (items.isLoading) return <p className="text-ink-500">{t("app.loading")}</p>;
  if (items.isError) return <p className="text-bad-700">{t("app.error")}</p>;
  return (
    <>
      <FreshnessLabel asOf={items.data?.data_as_of} />
      <StockTable items={items.data?.items ?? []} selected={selected} onSelect={setSelected} />
      {chosen && can("manager") && forecast.data && (
        <ForecastChart series={forecast.data.series} unit={chosen.unit} today={todayPoint} method={forecast.data.method} />
      )}
    </>
  );
}

function OrdersTab() {
  const { t, i18n } = useTranslation();
  const orders = useQuery({ queryKey: ["stock", "pos"], queryFn: () => api.get<{ purchase_orders: PO[]; data_as_of: DataAsOf }>("/purchase-orders") });
  const ar = i18n.language === "ar";
  return (
    <div className="flex flex-col gap-3">
      <FreshnessLabel asOf={orders.data?.data_as_of} />
      {(orders.data?.purchase_orders ?? []).map((po) => (
        <article key={po.id} className="card">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <strong>
              {po.number} · {ar ? po.supplier?.name_ar : po.supplier?.name_en}
            </strong>
            <span className="flex items-center gap-2 text-sm">
              {po.late && <span className="badge bg-bad-50 text-bad-700">{t("stock.late")}</span>}
              <span className="badge bg-slate-100 text-ink-700">{t(`stock.po_status.${po.status}`, { defaultValue: po.status })}</span>
              {po.total && <span className="font-medium">{formatMoney(po.total, i18n.language)}</span>}
            </span>
          </div>
          <p className="text-xs text-ink-500">{t("stock.expected", { date: po.expected_date })}</p>
          <ul className="mt-2 text-sm">
            {po.lines.map((l) => (
              <li key={l.item_id}>
                {Number(l.qty)} {l.unit} {ar ? l.name_ar : l.name_en}
              </li>
            ))}
          </ul>
          {po.deliveries.map((d) => (
            <p key={d.id} className="mt-1 text-xs text-ink-500">
              {t("stock.delivered_on", { date: d.received_on })}
              {d.discrepancies?.length ? ` · ${t("stock.discrepancies", { count: d.discrepancies.length })}` : ""}
            </p>
          ))}
        </article>
      ))}
      {orders.data && orders.data.purchase_orders.length === 0 && <p className="text-sm text-ink-500">{t("common.none")}</p>}
    </div>
  );
}

function DeliveryTab() {
  const { t, i18n } = useTranslation();
  const qc = useQueryClient();
  const ar = i18n.language === "ar";
  const orders = useQuery({ queryKey: ["stock", "pos", "open"], queryFn: () => api.get<{ purchase_orders: PO[] }>("/purchase-orders?status=sent") });
  const [poId, setPoId] = useState("");
  const [qty, setQty] = useState<Record<string, string>>({});
  const [photo, setPhoto] = useState<File | null>(null);
  const [msg, setMsg] = useState<string | null>(null);
  const po = orders.data?.purchase_orders.find((p) => p.id === poId);
  const save = useMutation({
    mutationFn: () => {
      const form = new FormData();
      form.append("lines", JSON.stringify(po!.lines.map((l) => ({ item_id: l.item_id, qty_received: qty[l.item_id] ?? l.qty }))));
      if (photo) form.append("photo", photo);
      return api.upload(`/purchase-orders/${poId}/deliveries`, form);
    },
    onSuccess: () => {
      setMsg(t("stock.delivery_saved"));
      setPoId("");
      setQty({});
      setPhoto(null);
      void qc.invalidateQueries({ queryKey: ["stock"] });
    },
    onError: (e) => setMsg(e instanceof ApiError ? e.message_for(i18n.language) : String(e)),
  });
  return (
    <form className="card flex flex-col gap-3" onSubmit={(e) => { e.preventDefault(); save.mutate(); }}>
      <label className="label" htmlFor="po">{t("stock.choose_order")}</label>
      <select id="po" className="input" value={poId} onChange={(e) => setPoId(e.target.value)}>
        <option value="">—</option>
        {(orders.data?.purchase_orders ?? []).map((p) => (
          <option key={p.id} value={p.id}>
            {p.number} · {ar ? p.supplier?.name_ar : p.supplier?.name_en}
          </option>
        ))}
      </select>
      {po?.lines.map((l) => (
        <div key={l.item_id} className="flex items-center gap-2">
          <span className="flex-1 text-sm">
            {ar ? l.name_ar : l.name_en} ({t("stock.ordered")}: {Number(l.qty)} {l.unit})
          </span>
          <input
            className="input w-28"
            type="number"
            min="0"
            step="any"
            inputMode="decimal"
            aria-label={t("stock.received")}
            value={qty[l.item_id] ?? String(Number(l.qty))}
            onChange={(e) => setQty({ ...qty, [l.item_id]: e.target.value })}
          />
        </div>
      ))}
      {po && (
        <>
          <label className="label" htmlFor="photo">{t("stock.photo")}</label>
          <input id="photo" type="file" accept="image/*" capture="environment" onChange={(e) => setPhoto(e.target.files?.[0] ?? null)} />
          <button type="submit" className="btn-primary" disabled={save.isPending}>{t("stock.confirm_delivery")}</button>
        </>
      )}
      {msg && <p className="text-sm text-ink-700" role="status">{msg}</p>}
    </form>
  );
}

function WasteTab() {
  const { t, i18n } = useTranslation();
  const qc = useQueryClient();
  const ar = i18n.language === "ar";
  const items = useQuery({ queryKey: ["stock", "items"], queryFn: () => api.get<{ items: StockItem[] }>("/stock/items") });
  const [form, setForm] = useState({ item_id: "", qty: "", type: "waste", reason: "" });
  const [msg, setMsg] = useState<string | null>(null);
  const save = useMutation({
    mutationFn: () => api.post("/stock/waste", { ...form, qty: Number(form.qty) }),
    onSuccess: () => {
      setMsg(t("stock.waste_saved"));
      setForm({ item_id: "", qty: "", type: "waste", reason: "" });
      void qc.invalidateQueries({ queryKey: ["stock"] });
    },
    onError: (e) => setMsg(e instanceof ApiError ? e.message_for(i18n.language) : String(e)),
  });
  return (
    <form className="card flex flex-col gap-3" onSubmit={(e) => { e.preventDefault(); save.mutate(); }}>
      <label className="label" htmlFor="w-item">{t("stock.item")}</label>
      <select id="w-item" className="input" value={form.item_id} onChange={(e) => setForm({ ...form, item_id: e.target.value })}>
        <option value="">—</option>
        {(items.data?.items ?? []).map((i) => (
          <option key={i.id} value={i.id}>{ar ? i.name_ar : i.name_en} ({i.unit})</option>
        ))}
      </select>
      <div className="flex gap-2">
        <div className="flex-1">
          <label className="label" htmlFor="w-qty">{t("stock.quantity")}</label>
          <input id="w-qty" className="input" type="number" min="0" step="any" inputMode="decimal" value={form.qty} onChange={(e) => setForm({ ...form, qty: e.target.value })} />
        </div>
        <div className="flex-1">
          <label className="label" htmlFor="w-type">{t("stock.type")}</label>
          <select id="w-type" className="input" value={form.type} onChange={(e) => setForm({ ...form, type: e.target.value })}>
            <option value="waste">{t("stock.types.waste")}</option>
            <option value="spoilage">{t("stock.types.spoilage")}</option>
          </select>
        </div>
      </div>
      <label className="label" htmlFor="w-reason">{t("stock.reason")}</label>
      <input id="w-reason" className="input" value={form.reason} onChange={(e) => setForm({ ...form, reason: e.target.value })} />
      <button type="submit" className="btn-primary" disabled={save.isPending || !form.item_id || !form.qty || form.reason.length < 2}>
        {t("common.save")}
      </button>
      {msg && <p className="text-sm text-ink-700" role="status">{msg}</p>}
    </form>
  );
}

function SalesTab({ lang }: { lang: string }) {
  const { t } = useTranslation();
  const qc = useQueryClient();
  const [file, setFile] = useState<File | null>(null);
  const [result, setResult] = useState<any>(null);
  const [error, setError] = useState<string | null>(null);
  const products = useQuery({
    queryKey: ["stock", "products"],
    queryFn: () => api.get<{ products: { id: string; name_en: string; name_ar: string }[] }>("/stock/products"),
  });
  const [manual, setManual] = useState({ date: "", item_id: "", qty: "", amount: "", payment_method: "cash" });
  const upload = useMutation({
    mutationFn: () => {
      const form = new FormData();
      form.append("file", file!);
      return api.upload("/sales/import", form);
    },
    onSuccess: (r) => {
      setResult(r);
      setError(null);
      void qc.invalidateQueries({ queryKey: ["stock"] });
    },
    onError: (e) => setError(e instanceof ApiError ? e.message_for(lang) : String(e)),
  });
  const enter = useMutation({
    mutationFn: () =>
      api.post("/sales/manual", {
        date: manual.date,
        payment_method: manual.payment_method,
        lines: [{ item_id: manual.item_id, qty: Number(manual.qty), amount: Number(manual.amount) }],
      }),
    onSuccess: (r) => {
      setResult(r);
      setError(null);
    },
    onError: (e) => setError(e instanceof ApiError ? e.message_for(lang) : String(e)),
  });

  return (
    <div className="grid gap-4 md:grid-cols-2">
      <form className="card flex flex-col gap-3" onSubmit={(e) => { e.preventDefault(); if (file) upload.mutate(); }}>
        <h2 className="font-semibold">{t("stock.import_csv")}</h2>
        <p className="text-xs text-ink-500">{t("stock.csv_hint")}</p>
        <input type="file" accept=".csv,text/csv" onChange={(e) => setFile(e.target.files?.[0] ?? null)} />
        <button type="submit" className="btn-primary" disabled={!file || upload.isPending}>{t("stock.upload")}</button>
      </form>
      <form className="card flex flex-col gap-3" onSubmit={(e) => { e.preventDefault(); enter.mutate(); }}>
        <h2 className="font-semibold">{t("stock.manual_entry")}</h2>
        <input className="input" type="date" value={manual.date} onChange={(e) => setManual({ ...manual, date: e.target.value })} aria-label={t("stock.date")} />
        <select className="input" value={manual.item_id} onChange={(e) => setManual({ ...manual, item_id: e.target.value })} aria-label={t("stock.item")}>
          <option value="">—</option>
          {(products.data?.products ?? []).map((p) => (
            <option key={p.id} value={p.id}>{lang === "ar" ? p.name_ar : p.name_en}</option>
          ))}
        </select>
        <div className="flex gap-2">
          <input className="input" type="number" min="0" step="any" placeholder={t("stock.quantity")} value={manual.qty} onChange={(e) => setManual({ ...manual, qty: e.target.value })} />
          <input className="input" type="number" min="0" step="any" placeholder={t("stock.amount")} value={manual.amount} onChange={(e) => setManual({ ...manual, amount: e.target.value })} />
        </div>
        <select className="input" value={manual.payment_method} onChange={(e) => setManual({ ...manual, payment_method: e.target.value })} aria-label={t("stock.payment_method")}>
          {["cash", "card", "transfer", "credit"].map((m) => (
            <option key={m} value={m}>{t(`stock.methods_pay.${m}`)}</option>
          ))}
        </select>
        <button type="submit" className="btn-primary" disabled={enter.isPending || !manual.date || !manual.item_id || !manual.qty}>{t("common.save")}</button>
      </form>
      {(result || error) && (
        <div className="card md:col-span-2" role="status">
          {error && <p className="text-bad-700">{error}</p>}
          {result && (
            <>
              <p className="text-sm">{t("stock.import_result", { imported: result.imported, skipped: result.skipped_duplicates })}</p>
              {result.errors?.length > 0 && (
                <ul className="mt-2 text-xs text-bad-700">
                  {result.errors.map((e: any) => (
                    <li key={e.row}>{t("stock.row_error", { row: e.row, reason: e.reason })}</li>
                  ))}
                </ul>
              )}
            </>
          )}
        </div>
      )}
    </div>
  );
}
