import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { useTranslation } from "react-i18next";
import { api, ApiError, LONG_TIMEOUT_MS } from "../api/client";
import { errorText } from "../api/errorText";
import type { ApprovalRequest } from "../api/types";

const URGENCY_STYLE = ["border-slate-200", "border-warn-500", "border-bad-500"];

/** One owner request, answerable in one tap (or one short reply). */
export function ApprovalCard({ request, compact = false }: { request: ApprovalRequest; compact?: boolean }) {
  const { t, i18n } = useTranslation();
  const lang = i18n.language;
  const qc = useQueryClient();
  const [notice, setNotice] = useState<string | null>(null);
  const [reply, setReply] = useState("");
  const editLines = ((request.context?.edit as any)?.lines ?? []) as {
    item_id: string;
    name_en: string;
    name_ar: string;
    qty: string;
    unit: string;
  }[];
  const [editing, setEditing] = useState(false);
  const [qtys, setQtys] = useState<Record<string, string>>({});
  const text = lang === "ar" ? request.text_ar : request.text_en;
  const label = (o: { label_en: string; label_ar: string }) => (lang === "ar" ? o.label_ar : o.label_en);

  const resolve = useMutation({
    mutationFn: (body: { option_key?: string; text?: string; edits?: Record<string, unknown> }) =>
      api.post(`/approvals/${request.id}/resolve`, body, { timeoutMs: LONG_TIMEOUT_MS }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["approvals"] }),
    onError: (e) => {
      if (e instanceof ApiError && e.status === 409) {
        const winner = (e.extra.request as ApprovalRequest | undefined) ?? null;
        setNotice(t("approval.already", { channel: winner?.resolved_via ?? "" }));
        void qc.invalidateQueries({ queryKey: ["approvals"] });
      } else {
        setNotice(errorText(e));
      }
    },
  });

  const resolvedOption = request.options.find((o) => o.key === request.resolved_option);
  const done = request.status !== "pending";
  const agent = t(`agents.${request.agent}`, { defaultValue: request.agent });

  return (
    <article
      className={`card border-s-4 ${URGENCY_STYLE[Math.min(request.urgency, 3) - 1] ?? URGENCY_STYLE[0]}`}
      aria-live="polite"
      data-testid="approval-card"
    >
      <div className="mb-1 flex items-center justify-between gap-2 text-xs text-ink-500">
        <span>{t("approval.from", { agent })}</span>
        {request.urgency > 1 && (
          <span className="badge bg-warn-50 text-warn-700">
            {t("approval.urgency", { level: request.urgency })}
          </span>
        )}
      </div>
      <p className={`whitespace-pre-line ${compact ? "text-sm" : "text-base"} text-ink-900`}>{text}</p>
      {done ? (
        <p className="mt-2 text-sm font-medium text-good-700">
          {request.status === "timed_out"
            ? t("approval.timed_out", {
                option: resolvedOption ? label(resolvedOption) : request.resolved_option,
              })
            : t("approval.resolved", {
                option: resolvedOption ? label(resolvedOption) : request.resolved_option,
              })}
          {request.resolved_via ? ` · ${request.resolved_via}` : ""}
        </p>
      ) : (
        <div className="mt-3 flex flex-wrap gap-2">
          {request.options.map((o) => (
            <button
              key={o.key}
              type="button"
              className={
                o.effect === "reject" || o.effect === "cancel"
                  ? "btn-danger"
                  : o.effect === "approve" || o.effect === "override"
                    ? "btn-primary"
                    : "btn-secondary"
              }
              disabled={resolve.isPending}
              onClick={() =>
                o.effect === "edit" && editLines.length
                  ? setEditing((v) => !v)
                  : resolve.mutate({ option_key: o.key })
              }
            >
              {label(o)}
            </button>
          ))}
          {editing && (
            <form
              className="flex w-full flex-col gap-2"
              onSubmit={(e) => {
                e.preventDefault();
                resolve.mutate({
                  option_key: request.options.find((o) => o.effect === "edit")!.key,
                  edits: {
                    lines: editLines.map((l) => ({ item_id: l.item_id, qty: qtys[l.item_id] ?? l.qty })),
                  },
                });
              }}
            >
              {editLines.map((l) => (
                <label key={l.item_id} className="flex items-center gap-2 text-sm">
                  <span className="flex-1">{lang === "ar" ? l.name_ar : l.name_en}</span>
                  <input
                    className="input w-24"
                    type="number"
                    min="0"
                    step="any"
                    inputMode="decimal"
                    value={qtys[l.item_id] ?? String(Number(l.qty))}
                    onChange={(e) => setQtys({ ...qtys, [l.item_id]: e.target.value })}
                  />
                  <span className="w-10 text-ink-500">{l.unit}</span>
                </label>
              ))}
              <button type="submit" className="btn-primary" disabled={resolve.isPending}>
                {t("approval.send_edited")}
              </button>
            </form>
          )}
          {request.allow_text && (
            <form
              className="flex w-full gap-2"
              onSubmit={(e) => {
                e.preventDefault();
                if (reply.trim()) resolve.mutate({ text: reply.trim() });
              }}
            >
              <input
                className="input"
                maxLength={100}
                value={reply}
                onChange={(e) => setReply(e.target.value)}
                placeholder={t("chat.reply")}
              />
              <button className="btn-primary" type="submit">
                {t("chat.send")}
              </button>
            </form>
          )}
        </div>
      )}
      {notice && (
        <p className="mt-2 text-sm text-warn-700" role="status">
          {notice}
        </p>
      )}
    </article>
  );
}
