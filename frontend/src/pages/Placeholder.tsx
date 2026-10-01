import { useTranslation } from "react-i18next";

export function Placeholder({ titleKey }: { titleKey: string }) {
  const { t } = useTranslation();
  return (
    <div className="card">
      <h1 className="mb-2 text-lg font-semibold">{t(titleKey)}</h1>
      <p className="text-sm text-ink-500">{t("placeholder.coming")}</p>
    </div>
  );
}
