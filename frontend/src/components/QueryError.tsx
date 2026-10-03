import { useTranslation } from "react-i18next";
import { errorKind } from "../api/client";

export function QueryError({ error, onRetry }: { error: unknown; onRetry?: () => void }) {
  const { t } = useTranslation();
  return (
    <div className="card flex flex-col items-start gap-3" role="alert">
      <p className="text-sm text-bad-700">{t(`errors.${errorKind(error)}`)}</p>
      {onRetry && (
        <button type="button" className="btn-secondary" onClick={onRetry}>
          {t("app.retry")}
        </button>
      )}
    </div>
  );
}
