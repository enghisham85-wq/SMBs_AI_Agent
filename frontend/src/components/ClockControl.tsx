import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { useTranslation } from "react-i18next";
import { api, LONG_TIMEOUT_MS } from "../api/client";
import { errorText } from "../api/errorText";
import type { ClockState } from "../api/types";
import { useAuth } from "../hooks/useAuth";

/** Business date and, for the owner in demo mode, controls to move it forward. */
export function ClockControl() {
  const { t, i18n } = useTranslation();
  const { can } = useAuth();
  const qc = useQueryClient();
  const [target, setTarget] = useState("");
  const [error, setError] = useState<string | null>(null);
  const clock = useQuery({
    queryKey: ["clock"],
    queryFn: ({ signal }) => api.get<ClockState>("/clock", signal),
    refetchInterval: 20_000,
  });

  const advance = useMutation({
    mutationFn: (body: { days?: number; to_date?: string }) =>
      api.post("/clock/advance", body, { timeoutMs: LONG_TIMEOUT_MS }),
    onSuccess: () => {
      setError(null);
      void qc.invalidateQueries();
    },
    onError: (e) => setError(errorText(e)),
  });

  const c = clock.data;
  const busy = advance.isPending || !!c?.advancing;
  const simulated = c?.mode === "simulated";
  const dateText = c
    ? new Date(c.current_date + "T00:00:00").toLocaleDateString(i18n.language === "ar" ? "ar-EG" : "en-GB", {
        weekday: "short",
        day: "numeric",
        month: "short",
        year: "numeric",
      })
    : "…";

  // Reserve the row the owner's buttons will need, and room for the date, so the sticky header does not
  // grow and push the page down when /clock answers.
  return (
    <div
      className={`flex flex-wrap items-center gap-2 text-sm ${can("owner") ? "min-h-[40px]" : ""}`}
      data-testid="clock-control"
    >
      <span className="text-ink-500">{t("clock.today")}:</span>
      <strong className="inline-block min-w-[9rem] text-ink-900">{dateText}</strong>
      {!simulated && <span className="badge bg-slate-100 text-ink-500">{t("clock.real")}</span>}
      {simulated && can("owner") && (
        <>
          <button
            type="button"
            className="btn-secondary"
            disabled={busy}
            onClick={() => advance.mutate({ days: 1 })}
          >
            {busy ? t("clock.advancing") : t("clock.advance1")}
          </button>
          <form
            className="flex items-center gap-1"
            onSubmit={(e) => {
              e.preventDefault();
              if (target) advance.mutate({ to_date: target });
            }}
          >
            <label className="sr-only" htmlFor="jump-date">
              {t("clock.jump")}
            </label>
            <input
              id="jump-date"
              type="date"
              className="input w-auto"
              min={c?.current_date}
              value={target}
              onChange={(e) => setTarget(e.target.value)}
            />
            <button type="submit" className="btn-secondary" disabled={busy || !target}>
              {t("clock.go")}
            </button>
          </form>
        </>
      )}
      {error && (
        <span className="text-bad-700" role="alert">
          {error}
        </span>
      )}
    </div>
  );
}
