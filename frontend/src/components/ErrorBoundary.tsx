import { Component, type ErrorInfo, type ReactNode } from "react";
import { useTranslation } from "react-i18next";

interface Props {
  /** Usually the route. Changing it clears the error. */
  resetKey: string;
  children: ReactNode;
}

/** So one broken page or chunk doesn't blank the whole app. */
export class ErrorBoundary extends Component<Props, { error: unknown }> {
  state: { error: unknown } = { error: null };

  static getDerivedStateFromError(error: unknown) {
    return { error };
  }

  componentDidCatch(error: unknown, info: ErrorInfo) {
    console.error("page crashed", error, info.componentStack);
  }

  componentDidUpdate(prev: Props) {
    if (this.state.error !== null && prev.resetKey !== this.props.resetKey) this.setState({ error: null });
  }

  render() {
    if (this.state.error !== null) return <PageCrashed onRetry={() => this.setState({ error: null })} />;
    return this.props.children;
  }
}

function PageCrashed({ onRetry }: { onRetry: () => void }) {
  const { t } = useTranslation();
  return (
    <div className="card flex flex-col items-start gap-3" role="alert">
      <p className="text-sm text-bad-700">{t("errors.page")}</p>
      <div className="flex flex-wrap gap-2">
        <button type="button" className="btn-secondary" onClick={onRetry}>
          {t("app.retry")}
        </button>
        <button type="button" className="btn-secondary" onClick={() => window.location.reload()}>
          {t("errors.reload")}
        </button>
      </div>
    </div>
  );
}
