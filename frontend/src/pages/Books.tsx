import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import { api, ApiError } from "../api/client";
import type { ApprovalRequest, DataAsOf, Money } from "../api/types";
import { ApprovalCard } from "../components/ApprovalCard";
import { DocumentDetail } from "../components/DocumentDetail";
import { FreshnessLabel } from "../components/FreshnessLabel";
import { formatMoney } from "../lib/money";

type Tab = "inbox" | "review" | "reconciliation" | "pnl" | "balance" | "receivables";

export function Books() {
  const { t } = useTranslation();
  const [tab, setTab] = useState<Tab>("inbox");
  const tabs: Tab[] = ["inbox", "review", "reconciliation", "pnl", "balance", "receivables"];
  return (
    <div className="flex flex-col gap-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h1 className="text-xl font-semibold">{t("nav.books")}</h1>
        <div role="tablist" className="flex flex-wrap gap-1">
          {tabs.map((k) => (
            <button key={k} role="tab" aria-selected={tab === k} className={tab === k ? "btn-primary" : "btn-secondary"} onClick={() => setTab(k)}>
              {t(`books.tabs.${k}`)}
            </button>
          ))}
        </div>
      </div>
      {tab === "inbox" && <Inbox />}
      {tab === "review" && <Review />}
      {tab === "reconciliation" && <Reconciliation />}
      {tab === "pnl" && <Pnl />}
      {tab === "balance" && <Balance />}
      {tab === "receivables" && <Receivables />}
    </div>
  );
}

const DOC_STYLE: Record<string, string> = {
  posted: "bg-good-50 text-good-700",
  needs_review: "bg-warn-50 text-warn-700",
  duplicate: "bg-slate-100 text-ink-500",
  rejected: "bg-bad-50 text-bad-700",
};

function Inbox() {
  const { t, i18n } = useTranslation();
  const qc = useQueryClient();
  const [selected, setSelected] = useState<string | null>(null);
  const [msg, setMsg] = useState<string | null>(null);
  const docs = useQuery({ queryKey: ["books", "documents"], queryFn: () => api.get<{ documents: any[] }>("/documents") });
  const samples = useQuery({ queryKey: ["books", "samples"], queryFn: () => api.get<{ samples: any[] }>("/documents/samples") });
  const done = (r: any) => {
    setMsg(t(`books.outcomes.${r.outcome ?? (r.waiting_for_owner ? "waiting" : "done")}`, { defaultValue: r.outcome }));
    setSelected(r.document_id);
    void qc.invalidateQueries({ queryKey: ["books"] });
    void qc.invalidateQueries({ queryKey: ["approvals"] });
  };
  const fail = (e: unknown) => setMsg(e instanceof ApiError ? e.message_for(i18n.language) : String(e));
  const upload = useMutation({
    mutationFn: (file: File) => {
      const form = new FormData();
      form.append("file", file);
      return api.upload<any>("/documents", form);
    },
    onSuccess: done,
    onError: fail,
  });
  const sample = useMutation({ mutationFn: (name: string) => api.post<any>(`/documents/samples/${name}`), onSuccess: done, onError: fail });
  const busy = upload.isPending || sample.isPending;

  return (
    <div className="flex flex-col gap-4">
      <div className="card flex flex-wrap items-center gap-3">
        <label className="btn-primary cursor-pointer">
          {busy ? t("books.reading") : t("books.upload")}
          <input type="file" accept="application/pdf,image/*" className="hidden" disabled={busy}
            onChange={(e) => e.target.files?.[0] && upload.mutate(e.target.files[0])} />
        </label>
        {(samples.data?.samples.length ?? 0) > 0 && (
          <select className="input w-auto" defaultValue="" disabled={busy} onChange={(e) => e.target.value && sample.mutate(e.target.value)} aria-label={t("books.try_sample")}>
            <option value="">{t("books.try_sample")}</option>
            {samples.data!.samples.map((s) => (
              <option key={s.name} value={s.name}>{s.name}</option>
            ))}
          </select>
        )}
        {msg && <span className="text-sm text-ink-700" role="status">{msg}</span>}
      </div>
      <div className="card overflow-x-auto p-0">
        <table className="table">
          <thead>
            <tr><th>{t("books.document")}</th><th>{t("books.supplier")}</th><th>{t("books.total")}</th><th>{t("books.state")}</th><th>{t("books.confidence")}</th></tr>
          </thead>
          <tbody>
            {(docs.data?.documents ?? []).map((d) => (
              <tr key={d.id} className={`cursor-pointer hover:bg-slate-50 ${selected === d.id ? "bg-brand-50" : ""}`} onClick={() => setSelected(d.id)}>
                <td>{d.invoice?.number ?? d.name}<span className="block text-xs text-ink-500">{d.channel}</span></td>
                <td>{i18n.language === "ar" ? d.invoice?.supplier_ar : d.invoice?.supplier_en}</td>
                <td className="whitespace-nowrap">{d.invoice ? formatMoney(d.invoice.total, i18n.language) : "—"}</td>
                <td><span className={`badge ${DOC_STYLE[d.status] ?? "bg-slate-100 text-ink-700"}`}>{t(`books.status.${d.status}`, { defaultValue: d.status })}</span></td>
                <td>{d.confidence !== null ? `${Math.round(d.confidence * 100)}%` : "—"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {selected && <DocumentDetail id={selected} />}
    </div>
  );
}

function Review() {
  const { t, i18n } = useTranslation();
  const q = useQuery({
    queryKey: ["books", "review"],
    queryFn: () => api.get<{ questions: ApprovalRequest[]; held_invoices: any[]; suggested_matches: any[] }>("/review-queue"),
  });
  const d = q.data;
  return (
    <div className="flex flex-col gap-3">
      {d && d.questions.length + d.held_invoices.length + d.suggested_matches.length === 0 && <p className="text-sm text-ink-500">{t("chat.empty")}</p>}
      {d?.questions.map((r) => <ApprovalCard key={r.id} request={r} />)}
      {d?.held_invoices.map((i) => (
        <div key={i.id} className="card text-sm">
          {t("books.held", { number: i.number, total: formatMoney(i.total, i18n.language), reasons: (i.hold_reason ?? []).join(", ") })}
        </div>
      ))}
      {(d?.suggested_matches.length ?? 0) > 0 && <p className="text-sm text-ink-500">{t("books.see_reconciliation", { count: d!.suggested_matches.length })}</p>}
    </div>
  );
}

function Reconciliation() {
  const { t, i18n } = useTranslation();
  const qc = useQueryClient();
  const q = useQuery({ queryKey: ["books", "reconciliation"], queryFn: () => api.get<any>("/reconciliation") });
  const [account, setAccount] = useState<Record<string, string>>({});
  const confirm = useMutation({
    mutationFn: (v: { id: string; body: any }) => api.post(`/reconciliation/${v.id}/match`, v.body),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["books"] }),
  });
  const d = q.data;
  if (!d) return <p className="text-ink-500">{t("app.loading")}</p>;
  return (
    <div className="flex flex-col gap-3">
      <FreshnessLabel asOf={d.data_as_of as DataAsOf} />
      <div className="card flex flex-wrap items-center gap-6">
        <div><span className="label">{t("books.matched")}</span><strong className="text-2xl">{d.percent_matched}%</strong><span className="ms-2 text-xs text-ink-500">{d.matched} / {d.total}</span></div>
        {Object.entries(d.bank_vs_ledger as Record<string, { ledger: Money; bank: Money }>).map(([name, v]) => (
          <div key={name} className="text-xs">
            <span className="label">{name}</span>
            {t("books.bank_vs_ledger", { bank: formatMoney(v.bank, i18n.language), ledger: formatMoney(v.ledger, i18n.language) })}
          </div>
        ))}
      </div>
      <div className="card overflow-x-auto p-0">
        <table className="table">
          <thead><tr><th>{t("books.date")}</th><th>{t("books.description")}</th><th>{t("books.amount")}</th><th>{t("books.suggestion")}</th><th /></tr></thead>
          <tbody>
            {d.open.map((x: any) => (
              <tr key={x.id}>
                <td className="whitespace-nowrap">{x.date}</td>
                <td>{x.description}<span className="block text-xs text-ink-500">{x.account}</span></td>
                <td className="whitespace-nowrap">{formatMoney(x.amount as Money, i18n.language)}</td>
                <td className="text-xs">{x.suggestion ? `${x.suggestion.name} (${Math.round((x.confidence ?? 0) * 100)}%)` : "—"}</td>
                <td className="whitespace-nowrap">
                  {x.suggestion ? (
                    <button className="btn-primary" disabled={confirm.isPending} onClick={() => confirm.mutate({ id: x.id, body: { use_suggestion: true } })}>{t("books.confirm")}</button>
                  ) : (
                    <span className="flex gap-1">
                      <input className="input w-20" placeholder="5900" value={account[x.id] ?? ""} onChange={(e) => setAccount({ ...account, [x.id]: e.target.value })} aria-label={t("books.account")} />
                      <button className="btn-secondary" disabled={!account[x.id]} onClick={() => confirm.mutate({ id: x.id, body: { type: "expense", account_code: account[x.id] } })}>{t("books.record")}</button>
                    </span>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <details className="card">
        <summary className="cursor-pointer text-sm font-medium">{t("books.recent_matches")}</summary>
        <ul className="mt-2 text-xs">
          {d.recent_matches.map((m: any) => (
            <li key={m.id}>{m.date} · {m.description} · {m.matched_type} · {t(`books.match_sources.${m.source}`, { defaultValue: m.source })} {m.confidence ? `(${Math.round(m.confidence * 100)}%)` : ""}</li>
          ))}
        </ul>
      </details>
    </div>
  );
}

function Section({ title, rows, currency, decimals, lang }: { title: string; rows: any[]; currency: string; decimals: number; lang: string }) {
  return (
    <>
      <tr><th colSpan={2} className="pt-4">{title}</th></tr>
      {rows.map((r) => (
        <tr key={r.code}>
          <td>{r.code} · {lang === "ar" ? r.name_ar : r.name_en}</td>
          <td className="text-end">{formatMoney({ amount_minor: r.amount_minor, currency, decimals, display: "" }, lang)}</td>
        </tr>
      ))}
    </>
  );
}

function Pnl() {
  const { t, i18n } = useTranslation();
  const q = useQuery({ queryKey: ["books", "pnl"], queryFn: () => api.get<any>("/reports/pnl") });
  const d = q.data;
  if (!d) return <p className="text-ink-500">{t("app.loading")}</p>;
  const m = (minor: number) => formatMoney({ amount_minor: minor, currency: d.currency, decimals: d.decimals, display: "" }, i18n.language);
  return (
    <div className="card overflow-x-auto">
      <p className="mb-2 text-sm text-ink-500">{t("books.period", { from: d.from, to: d.to })}</p>
      <table className="table">
        <tbody>
          <Section title={t("books.income")} rows={d.income} currency={d.currency} decimals={d.decimals} lang={i18n.language} />
          <Section title={t("books.expenses")} rows={d.expenses} currency={d.currency} decimals={d.decimals} lang={i18n.language} />
          <tr><th>{t("books.gross_profit")}</th><td className="text-end font-semibold">{m(d.gross_profit_minor)}</td></tr>
          <tr><th>{t("books.net_profit")}</th><td className="text-end font-semibold">{m(d.net_profit_minor)}</td></tr>
        </tbody>
      </table>
    </div>
  );
}

function Balance() {
  const { t, i18n } = useTranslation();
  const q = useQuery({ queryKey: ["books", "balance"], queryFn: () => api.get<any>("/reports/balance-sheet") });
  const d = q.data;
  if (!d) return <p className="text-ink-500">{t("app.loading")}</p>;
  const m = (minor: number) => formatMoney({ amount_minor: minor, currency: d.currency, decimals: d.decimals, display: "" }, i18n.language);
  return (
    <div className="card overflow-x-auto">
      <p className="mb-2 text-sm text-ink-500">{t("books.as_of", { date: d.as_of })} · {d.balanced ? t("books.balances") : t("books.does_not_balance")}</p>
      <table className="table">
        <tbody>
          <Section title={t("books.assets")} rows={d.assets} currency={d.currency} decimals={d.decimals} lang={i18n.language} />
          <tr><th>{t("books.total_assets")}</th><td className="text-end font-semibold">{m(d.total_assets_minor)}</td></tr>
          <Section title={t("books.liabilities")} rows={d.liabilities} currency={d.currency} decimals={d.decimals} lang={i18n.language} />
          <Section title={t("books.equity")} rows={d.equity} currency={d.currency} decimals={d.decimals} lang={i18n.language} />
          <tr><td>{t("books.current_earnings")}</td><td className="text-end">{m(d.current_earnings_minor)}</td></tr>
          <tr><th>{t("books.total_liabilities_equity")}</th><td className="text-end font-semibold">{m(d.total_liabilities_minor + d.total_equity_minor)}</td></tr>
        </tbody>
      </table>
    </div>
  );
}

interface Line { description: string; qty: string; unit_price: string; vat_rate_percent: string }

function Receivables() {
  const { t, i18n } = useTranslation();
  const qc = useQueryClient();
  const list = useQuery({ queryKey: ["books", "receivables"], queryFn: () => api.get<{ receivables: any[] }>("/receivables") });
  const biz = useQuery({ queryKey: ["business"], queryFn: () => api.get<any>("/business") });
  const rate = String(biz.data?.vat_rate_percent ?? "14");
  const today = new Date().toISOString().slice(0, 10);
  const [form, setForm] = useState({ customer_name: "", invoice_date: today, due_date: today });
  const [lines, setLines] = useState<Line[]>([{ description: "", qty: "1", unit_price: "", vat_rate_percent: "" }]);
  const [msg, setMsg] = useState<string | null>(null);
  const totals = useMemo(() => {
    let sub = 0;
    let vat = 0;
    for (const l of lines) {
      const net = (Number(l.qty) || 0) * (Number(l.unit_price) || 0);
      sub += net;
      vat += (net * Number(l.vat_rate_percent || rate)) / 100;
    }
    return { sub, vat, total: sub + vat };
  }, [lines, rate]);
  const cur = biz.data?.currency ?? "EGP";
  const dec = biz.data?.decimals ?? 2;
  const fmt = (v: number) => formatMoney({ amount_minor: Math.round(v * 10 ** dec), currency: cur, decimals: dec, display: "" }, i18n.language);
  const create = useMutation({
    mutationFn: () => api.post("/receivables", {
      ...form,
      lines: lines.map((l) => ({ description: l.description, qty: Number(l.qty), unit_price: Number(l.unit_price), vat_rate_percent: l.vat_rate_percent === "" ? null : Number(l.vat_rate_percent) })),
    }),
    onSuccess: () => {
      setMsg(t("books.invoice_created"));
      setLines([{ description: "", qty: "1", unit_price: "", vat_rate_percent: "" }]);
      void qc.invalidateQueries({ queryKey: ["books", "receivables"] });
    },
    onError: (e) => setMsg(e instanceof ApiError ? e.message_for(i18n.language) : String(e)),
  });
  const voidInv = useMutation({
    mutationFn: (id: string) => api.post(`/receivables/${id}/void`, { reason: "voided by owner" }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["books", "receivables"] }),
    onError: (e) => setMsg(e instanceof ApiError ? e.message_for(i18n.language) : String(e)),
  });
  const setLine = (i: number, patch: Partial<Line>) => setLines(lines.map((l, j) => (j === i ? { ...l, ...patch } : l)));

  return (
    <div className="grid gap-4 lg:grid-cols-[1fr_380px]">
      <div className="card overflow-x-auto p-0">
        <table className="table">
          <thead><tr><th>{t("books.number")}</th><th>{t("books.customer")}</th><th>{t("books.due")}</th><th>{t("books.outstanding")}</th><th>{t("books.state")}</th><th /></tr></thead>
          <tbody>
            {(list.data?.receivables ?? []).map((r) => (
              <tr key={r.id}>
                <td>{r.number}</td>
                <td>{r.customer_name}</td>
                <td className="whitespace-nowrap">{r.due_date}{r.days_overdue > 0 && <span className="block text-xs text-bad-700">{t("books.overdue", { days: r.days_overdue })}</span>}</td>
                <td className="whitespace-nowrap">{formatMoney(r.outstanding, i18n.language)}</td>
                <td><span className="badge bg-slate-100 text-ink-700">{t(`books.rec_status.${r.status}`, { defaultValue: r.status })}</span><span className="block text-xs text-ink-500">{r.ageing}</span></td>
                <td>{r.status === "open" && <button className="btn-danger" disabled={voidInv.isPending} onClick={() => voidInv.mutate(r.id)}>{t("books.void")}</button>}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <form className="card flex flex-col gap-2" onSubmit={(e) => { e.preventDefault(); create.mutate(); }}>
        <h2 className="font-semibold">{t("books.new_invoice")}</h2>
        <input className="input" placeholder={t("books.customer")} value={form.customer_name} onChange={(e) => setForm({ ...form, customer_name: e.target.value })} />
        <div className="flex gap-2">
          <label className="flex-1 text-xs">{t("books.date")}<input className="input" type="date" value={form.invoice_date} onChange={(e) => setForm({ ...form, invoice_date: e.target.value })} /></label>
          <label className="flex-1 text-xs">{t("books.due")}<input className="input" type="date" value={form.due_date} onChange={(e) => setForm({ ...form, due_date: e.target.value })} /></label>
        </div>
        {lines.map((l, i) => (
          <div key={i} className="grid grid-cols-[1fr_60px_80px_56px] gap-1">
            <input className="input" placeholder={t("books.description")} value={l.description} onChange={(e) => setLine(i, { description: e.target.value })} />
            <input className="input" type="number" min="0" step="any" placeholder={t("books.qty")} value={l.qty} onChange={(e) => setLine(i, { qty: e.target.value })} />
            <input className="input" type="number" min="0" step="any" placeholder={t("books.price")} value={l.unit_price} onChange={(e) => setLine(i, { unit_price: e.target.value })} />
            <input className="input" type="number" min="0" max="100" step="any" placeholder={`${rate}%`} value={l.vat_rate_percent} onChange={(e) => setLine(i, { vat_rate_percent: e.target.value })} aria-label={t("books.vat_rate")} />
          </div>
        ))}
        <button type="button" className="btn-secondary" onClick={() => setLines([...lines, { description: "", qty: "1", unit_price: "", vat_rate_percent: "" }])}>{t("books.add_line")}</button>
        <dl className="grid grid-cols-2 text-sm">
          <dt>{t("books.subtotal")}</dt><dd className="text-end">{fmt(totals.sub)}</dd>
          <dt>{t("books.vat")}</dt><dd className="text-end">{fmt(totals.vat)}</dd>
          <dt className="font-semibold">{t("books.total")}</dt><dd className="text-end font-semibold">{fmt(totals.total)}</dd>
        </dl>
        <button type="submit" className="btn-primary" disabled={create.isPending || !form.customer_name || totals.sub <= 0}>{t("books.create")}</button>
        {msg && <p className="text-sm text-ink-700" role="status">{msg}</p>}
      </form>
    </div>
  );
}
