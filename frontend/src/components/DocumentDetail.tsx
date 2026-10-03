import { useQuery } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";
import { api } from "../api/client";
import type { ApprovalRequest, Money } from "../api/types";
import { formatMoney } from "../lib/money";
import { ApprovalCard } from "./ApprovalCard";
import { QueryError } from "./QueryError";

interface Detail {
  document: {
    id: string;
    name: string;
    mime: string;
    status: string;
    language: string | null;
    confidence: number | null;
    file_url: string;
  };
  extractions: { attempt: number; source: string; document_confidence: number; fields: any }[];
  invoice: {
    number: string;
    status: string;
    invoice_date: string;
    due_date: string | null;
    supplier: { name_en: string; name_ar: string } | null;
    lines: any[];
    subtotal: Money;
    vat_amount: Money;
    total: Money;
    hold_reason: string[] | null;
    match_result: { warnings?: string[] } | null;
    supplier_vat_number: string | null;
  } | null;
  questions: ApprovalRequest[];
  checks: { name: string; passed: boolean; details: Record<string, string> }[];
}

function Confidence({ value }: { value: number | undefined }) {
  if (value === undefined || value === null) return null;
  const pct = Math.round(value * 100);
  const cls =
    value >= 0.9
      ? "bg-good-50 text-good-700"
      : value >= 0.6
        ? "bg-warn-50 text-warn-700"
        : "bg-bad-50 text-bad-700";
  return <span className={`badge ${cls}`}>{pct}%</span>;
}

/** Original file next to what was read from it; every value shows its confidence and source. */
export function DocumentDetail({ id }: { id: string }) {
  const { t, i18n } = useTranslation();
  const ar = i18n.language === "ar";
  const q = useQuery({
    queryKey: ["books", "document", id],
    queryFn: ({ signal }) => api.get<Detail>(`/documents/${id}`, signal),
  });
  if (!q.data && q.isError) return <QueryError error={q.error} onRetry={() => void q.refetch()} />;
  if (!q.data) return <p className="text-ink-500">{t("app.loading")}</p>;
  const { document: doc, extractions = [], invoice, questions = [], checks = [] } = q.data;
  const last = extractions[extractions.length - 1];
  const conf = last?.fields?.confidence ?? {};
  const source = last ? t(`books.sources.${last.source}`, { defaultValue: last.source }) : "";
  const lowRow = (key: string) => ((conf[key] ?? 1) < 0.6 ? "bg-warn-50" : "");

  return (
    <div className="grid gap-4 lg:grid-cols-2">
      <div className="card p-2">
        {doc.mime === "application/pdf" ? (
          <iframe title={doc.name} src={doc.file_url} className="h-[480px] w-full rounded" />
        ) : (
          // Fixed box like the PDF frame: the image scales inside it, so nothing below jumps when it loads.
          <img
            src={doc.file_url}
            alt={doc.name}
            decoding="async"
            className="h-[480px] w-full rounded object-contain"
          />
        )}
      </div>
      <div className="flex flex-col gap-3">
        <div className="card">
          <div className="mb-2 flex flex-wrap items-center justify-between gap-2">
            <strong>
              {invoice
                ? `${invoice.number} · ${ar ? invoice.supplier?.name_ar : invoice.supplier?.name_en}`
                : doc.name}
            </strong>
            <span className="flex items-center gap-2 text-xs">
              <span className="badge bg-slate-100 text-ink-700">
                {t(`books.status.${invoice?.status ?? doc.status}`, {
                  defaultValue: invoice?.status ?? doc.status,
                })}
              </span>
              <Confidence value={doc.confidence ?? undefined} />
            </span>
          </div>
          <p className="mb-2 text-xs text-ink-500">
            {t("books.read_by", { source, attempts: extractions.length, language: doc.language ?? "?" })}
          </p>
          {invoice && (
            <table className="table">
              <tbody>
                <tr className={lowRow("invoice_date")}>
                  <th>{t("books.date")}</th>
                  <td>{invoice.invoice_date}</td>
                  <td>
                    <Confidence value={conf.invoice_date} />
                  </td>
                </tr>
                <tr>
                  <th>{t("books.due")}</th>
                  <td>{invoice.due_date ?? "—"}</td>
                  <td />
                </tr>
                <tr className={lowRow("supplier")}>
                  <th>{t("books.vat_number")}</th>
                  <td>{invoice.supplier_vat_number ?? "—"}</td>
                  <td>
                    <Confidence value={conf.supplier} />
                  </td>
                </tr>
                <tr>
                  <th>{t("books.subtotal")}</th>
                  <td>{formatMoney(invoice.subtotal, i18n.language)}</td>
                  <td>
                    <Confidence value={conf.subtotal} />
                  </td>
                </tr>
                <tr>
                  <th>{t("books.vat")}</th>
                  <td>{formatMoney(invoice.vat_amount, i18n.language)}</td>
                  <td>
                    <Confidence value={conf.vat_amount} />
                  </td>
                </tr>
                <tr className={lowRow("total")}>
                  <th>{t("books.total")}</th>
                  <td className="font-semibold">{formatMoney(invoice.total, i18n.language)}</td>
                  <td>
                    <Confidence value={conf.total} />
                  </td>
                </tr>
              </tbody>
            </table>
          )}
        </div>
        {invoice && (invoice.lines?.length ?? 0) > 0 && (
          <div className="card overflow-x-auto p-0">
            <table className="table">
              <thead>
                <tr>
                  <th>{t("books.line")}</th>
                  <th>{t("books.qty")}</th>
                  <th>{t("books.account")}</th>
                  <th />
                </tr>
              </thead>
              <tbody>
                {invoice.lines.map((l: any, i: number) => (
                  <tr key={i} className={(l.confidence ?? 1) < 0.6 ? "bg-warn-50" : ""}>
                    <td>{l.description}</td>
                    <td>
                      {Number(l.qty)} {l.unit}
                    </td>
                    <td className="text-xs">
                      {l.account_code} ·{" "}
                      {t(`books.account_sources.${l.account_source}`, { defaultValue: l.account_source })}
                    </td>
                    <td>
                      <Confidence value={l.account_confidence ?? l.confidence} />
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
        {checks.length > 0 && (
          <ul className="flex flex-wrap gap-1 text-xs" aria-label={t("harness.checks")}>
            {checks.map((c) => (
              <li
                key={c.name}
                className={`badge ${c.passed ? "bg-good-50 text-good-700" : "bg-warn-50 text-warn-700"}`}
              >
                {c.passed ? "✓" : "!"}{" "}
                {t(`books.check_names.${c.name}`, { defaultValue: c.name.replace(/_/g, " ") })}
              </li>
            ))}
          </ul>
        )}
        {invoice?.match_result?.warnings?.map((w) => (
          <p key={w} className="rounded-lg bg-warn-50 p-2 text-xs text-warn-700">
            {w}
          </p>
        ))}
        {questions.map((r) => (
          <ApprovalCard key={r.id} request={r} />
        ))}
      </div>
    </div>
  );
}
