import { useTranslation } from "react-i18next";
import type { DataAsOf } from "../api/types";

/** States how fresh every figure is, e.g. "Bank data as of 3 Oct, 23:59". */
export function FreshnessLabel({ asOf }: { asOf: DataAsOf | undefined }) {
  const { t, i18n } = useTranslation();
  if (!asOf) return null;
  const fmt = (iso: string) =>
    new Date(iso.length === 10 ? iso + "T00:00:00" : iso).toLocaleString(i18n.language === "ar" ? "ar-EG" : "en-GB", {
      day: "numeric",
      month: "short",
      hour: iso.length > 10 ? "2-digit" : undefined,
      minute: iso.length > 10 ? "2-digit" : undefined,
    });
  return (
    <p className="text-xs text-ink-500" data-testid="freshness">
      {Object.entries(asOf).map(([source, when], i) => {
        const name = t(`freshness.sources.${source}`, { defaultValue: source });
        return (
          <span key={source}>
            {i > 0 && " · "}
            {when ? t("freshness.as_of", { source: name, time: fmt(when) }) : t("freshness.unknown", { source: name })}
          </span>
        );
      })}
    </p>
  );
}
