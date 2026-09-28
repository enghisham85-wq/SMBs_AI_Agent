import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useCallback, useState } from "react";
import { useTranslation } from "react-i18next";
import { api } from "../api/client";
import type { ApprovalRequest } from "../api/types";
import { useEventStream } from "../hooks/useEventStream";
import { ApprovalCard } from "./ApprovalCard";

/** The in-dashboard chat: pending requests plus a live feed of what the assistants did. */
export function ChatPanel() {
  const { t, i18n } = useTranslation();
  const qc = useQueryClient();
  const [activity, setActivity] = useState<ApprovalRequest[]>([]);
  const pending = useQuery({
    queryKey: ["approvals", "pending"],
    queryFn: () => api.get<{ approvals: ApprovalRequest[] }>("/approvals?status=pending"),
    refetchInterval: 30_000,
  });

  const onEvent = useCallback(
    (_: string, data: any) => {
      const req = data?.request as ApprovalRequest | undefined;
      if (req) setActivity((prev) => [req, ...prev.filter((r) => r.id !== req.id)].slice(0, 20));
      void qc.invalidateQueries({ queryKey: ["approvals"] });
      void qc.invalidateQueries({ queryKey: ["home"] });
    },
    [qc],
  );
  useEventStream("/chat/stream", onEvent, ["created", "resolved"]);

  const items = pending.data?.approvals ?? [];
  const recent = activity.filter((a) => a.status !== "pending");

  return (
    <section aria-label={t("chat.title")} className="flex h-full flex-col gap-3">
      <h2 className="text-sm font-semibold uppercase tracking-wide text-ink-500">{t("chat.title")}</h2>
      {pending.isLoading && <p className="text-sm text-ink-500">{t("app.loading")}</p>}
      {!pending.isLoading && items.length === 0 && <p className="text-sm text-ink-500">{t("chat.empty")}</p>}
      <div className="flex flex-col gap-3">
        {items.map((r) => (
          <ApprovalCard key={r.id} request={r} compact />
        ))}
      </div>
      {recent.length > 0 && (
        <>
          <h3 className="mt-2 text-xs font-semibold uppercase tracking-wide text-ink-400">{t("chat.activity")}</h3>
          <ul className="flex flex-col gap-2">
            {recent.map((r) => (
              <li key={r.id} className="rounded-lg bg-slate-100 p-2 text-xs text-ink-700">
                {i18n.language === "ar" ? r.text_ar : r.text_en}
                <span className="block text-ink-500">
                  {r.resolved_option} · {r.resolved_via}
                </span>
              </li>
            ))}
          </ul>
        </>
      )}
    </section>
  );
}
