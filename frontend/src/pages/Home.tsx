import { useQuery } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";
import { homeQuery } from "../api/queries";
import { AlertList } from "../components/AlertList";
import { ApprovalCard } from "../components/ApprovalCard";
import { FreshnessLabel } from "../components/FreshnessLabel";
import { HealthStrip } from "../components/HealthStrip";
import { QueryError } from "../components/QueryError";

/** Chat panel and clock control live in Layout, not here. */
export function Home() {
  const { t } = useTranslation();
  // Same query main.tsx prefetches, so this reuses that request.
  const home = useQuery({ ...homeQuery, refetchInterval: 60_000 });
  const d = home.data;
  if (home.isLoading) return <p className="text-ink-500">{t("app.loading")}</p>;
  // On a 401 the cache re-checks /me and redirects. This covers the gap.
  if (!d) return <QueryError error={home.error} onRetry={() => void home.refetch()} />;
  const decisions = d.decisions ?? [];
  return (
    <div className="flex flex-col gap-4">
      <h1 className="text-xl font-semibold">{t("nav.home")}</h1>
      <HealthStrip health={d.health} />
      <section className="flex flex-col gap-2" aria-label={t("home.decisions")} data-testid="decisions">
        <h2 className="text-base font-semibold">
          {t("home.decisions")}
          {decisions.length > 0 && (
            <span className="badge ms-2 bg-brand-50 text-brand-700">{decisions.length}</span>
          )}
        </h2>
        {decisions.length === 0 ? (
          <p className="card text-sm text-ink-500">{t("home.no_decisions")}</p>
        ) : (
          decisions.map((r) => <ApprovalCard key={r.id} request={r} />)
        )}
      </section>
      <section className="flex flex-col gap-2" aria-label={t("home.alerts")}>
        <h2 className="text-base font-semibold">{t("home.alerts")}</h2>
        <AlertList alerts={d.alerts ?? []} />
      </section>
      <FreshnessLabel asOf={d.data_as_of} />
    </div>
  );
}
