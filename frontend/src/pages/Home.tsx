import { useQuery } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";
import { api } from "../api/client";
import type { ApprovalRequest, DataAsOf } from "../api/types";
import { AlertList } from "../components/AlertList";
import { ApprovalCard } from "../components/ApprovalCard";
import { FreshnessLabel } from "../components/FreshnessLabel";
import { HealthStrip, type Health } from "../components/HealthStrip";

interface HomeData {
  health: Health;
  decisions: ApprovalRequest[];
  alerts: ApprovalRequest[];
  business_date: string;
  data_as_of: DataAsOf;
}

/** Home (FR-045): health strip, today's decisions (one tap each) and alerts by urgency.
 * The chat panel (docked on desktop, a drawer on mobile) and the owner's clock control come from Layout. */
export function Home() {
  const { t } = useTranslation();
  // Under the "approvals" key so answering any request, here or in the chat panel, refreshes Home.
  const home = useQuery({
    queryKey: ["approvals", "home"],
    queryFn: () => api.get<HomeData>("/home"),
    refetchInterval: 60_000,
  });
  const d = home.data;
  if (home.isLoading) return <p className="text-ink-500">{t("app.loading")}</p>;
  if (!d) return <p className="card text-bad-700">{t("home.error")}</p>;
  return (
    <div className="flex flex-col gap-4">
      <h1 className="text-xl font-semibold">{t("nav.home")}</h1>
      <HealthStrip health={d.health} />
      <section className="flex flex-col gap-2" aria-label={t("home.decisions")} data-testid="decisions">
        <h2 className="text-base font-semibold">
          {t("home.decisions")}
          {d.decisions.length > 0 && (
            <span className="badge ms-2 bg-brand-50 text-brand-700">{d.decisions.length}</span>
          )}
        </h2>
        {d.decisions.length === 0 ? (
          <p className="card text-sm text-ink-500">{t("home.no_decisions")}</p>
        ) : (
          d.decisions.map((r) => <ApprovalCard key={r.id} request={r} />)
        )}
      </section>
      <section className="flex flex-col gap-2" aria-label={t("home.alerts")}>
        <h2 className="text-base font-semibold">{t("home.alerts")}</h2>
        <AlertList alerts={d.alerts} />
      </section>
      <FreshnessLabel asOf={d.data_as_of} />
    </div>
  );
}
