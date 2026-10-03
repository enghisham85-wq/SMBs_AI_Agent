import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { useTranslation } from "react-i18next";
import { api } from "../api/client";
import { errorText } from "../api/errorText";
import { useAuth } from "../hooks/useAuth";

export interface RuleRow {
  id: string;
  agent: string;
  kind: string;
  rule_text_en: string;
  rule_text_ar: string;
  trigger: Record<string, unknown>;
  status: string;
  version: number;
  times_applied: number;
  times_overridden: number;
}

export function RulesPanel({ rules }: { rules: RuleRow[] }) {
  const { t, i18n } = useTranslation();
  const { can } = useAuth();
  const qc = useQueryClient();
  const [editing, setEditing] = useState<string | null>(null);
  const [text, setText] = useState("");
  const [error, setError] = useState<string | null>(null);
  const act = useMutation({
    mutationFn: ({ id, verb, body }: { id: string; verb: string; body?: unknown }) =>
      verb === "edit" ? api.patch(`/harness/rules/${id}`, body) : api.post(`/harness/rules/${id}/${verb}`),
    onSuccess: () => {
      setEditing(null);
      setError(null);
      void qc.invalidateQueries({ queryKey: ["harness"] });
    },
    onError: (e) => setError(errorText(e)),
  });
  const owner = can("owner");
  const groups = [
    { key: "proposed", rows: rules.filter((r) => r.status === "proposed") },
    { key: "active", rows: rules.filter((r) => r.status === "active") },
  ];
  return (
    <section className="flex flex-col gap-3" data-testid="rules-panel">
      {error && (
        <p className="text-sm text-bad-700" role="alert">
          {error}
        </p>
      )}
      {groups.map((g) => (
        <div key={g.key} className="card">
          <h3 className="mb-2 text-sm font-semibold">
            {t(`harness.rules_${g.key}`)} ({g.rows.length})
          </h3>
          {g.rows.length === 0 && <p className="text-sm text-ink-500">{t("common.none")}</p>}
          <ul className="flex flex-col gap-2">
            {g.rows.map((r) => (
              <li key={r.id} className="rounded-lg border border-slate-200 p-3">
                {editing === r.id ? (
                  <textarea
                    className="input"
                    rows={2}
                    value={text}
                    onChange={(e) => setText(e.target.value)}
                    aria-label={t("harness.rule_text")}
                  />
                ) : (
                  <p className="text-sm text-ink-900">
                    {i18n.language === "ar" && r.rule_text_ar ? r.rule_text_ar : r.rule_text_en}
                  </p>
                )}
                <p className="mt-1 text-xs text-ink-500">
                  {t(`agents.${r.agent}`, { defaultValue: r.agent })} ·{" "}
                  {t(`harness.kinds.${r.kind}`, { defaultValue: r.kind })} · v{r.version}
                  {g.key === "active" &&
                    ` · ${t("harness.applied", { n: r.times_applied })} · ${t("harness.overridden", { n: r.times_overridden })}`}
                </p>
                <code className="mt-1 block truncate text-xs text-ink-500">{JSON.stringify(r.trigger)}</code>
                {owner && (
                  <div className="mt-2 flex flex-wrap gap-2">
                    {editing === r.id ? (
                      <>
                        <button
                          className="btn-primary"
                          disabled={act.isPending}
                          onClick={() => act.mutate({ id: r.id, verb: "edit", body: { rule_text_en: text } })}
                        >
                          {t("common.save")}
                        </button>
                        <button className="btn-secondary" onClick={() => setEditing(null)}>
                          {t("common.cancel")}
                        </button>
                      </>
                    ) : (
                      <>
                        {r.status === "proposed" && (
                          <button
                            className="btn-primary"
                            disabled={act.isPending}
                            onClick={() => act.mutate({ id: r.id, verb: "approve" })}
                          >
                            {t("harness.approve")}
                          </button>
                        )}
                        <button
                          className="btn-secondary"
                          onClick={() => {
                            setEditing(r.id);
                            setText(r.rule_text_en);
                          }}
                        >
                          {t("harness.edit")}
                        </button>
                        {r.status === "proposed" ? (
                          <button
                            className="btn-danger"
                            disabled={act.isPending}
                            onClick={() => act.mutate({ id: r.id, verb: "reject" })}
                          >
                            {t("harness.reject")}
                          </button>
                        ) : (
                          <button
                            className="btn-danger"
                            disabled={act.isPending}
                            onClick={() => act.mutate({ id: r.id, verb: "deactivate" })}
                          >
                            {t("harness.deactivate")}
                          </button>
                        )}
                      </>
                    )}
                  </div>
                )}
              </li>
            ))}
          </ul>
        </div>
      ))}
    </section>
  );
}
