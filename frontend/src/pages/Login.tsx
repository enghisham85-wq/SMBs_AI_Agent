import { useState } from "react";
import { useTranslation } from "react-i18next";
import { ApiError, isTransient, RequestError } from "../api/client";
import { errorText } from "../api/errorText";
import { useAuth } from "../hooks/useAuth";
import { applyLanguage } from "../i18n";

export function Login() {
  const { t, i18n } = useTranslation();
  const { login } = useAuth();
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  return (
    <div className="flex min-h-screen items-center justify-center p-4">
      <form
        className="card w-full max-w-sm"
        onSubmit={async (e) => {
          e.preventDefault();
          setBusy(true);
          setError(null);
          try {
            await login(username, password);
          } catch (err) {
            // An unreachable, failing or slow server is not a wrong password.
            if (err instanceof RequestError || isTransient(err)) setError(errorText(err));
            else setError(err instanceof ApiError ? err.message_for(i18n.language) : t("login.failed"));
          } finally {
            setBusy(false);
          }
        }}
      >
        <div className="mb-4 flex items-center justify-between">
          <h1 className="text-xl font-semibold text-brand-700">{t("login.title")}</h1>
          <button
            type="button"
            className="btn-secondary"
            onClick={() => applyLanguage(i18n.language === "ar" ? "en" : "ar")}
          >
            {t("lang.switch")}
          </button>
        </div>
        <label className="label" htmlFor="username">
          {t("login.username")}
        </label>
        <input
          id="username"
          className="input mb-3"
          autoComplete="username"
          value={username}
          onChange={(e) => setUsername(e.target.value)}
        />
        <label className="label" htmlFor="password">
          {t("login.password")}
        </label>
        <input
          id="password"
          type="password"
          className="input mb-4"
          autoComplete="current-password"
          value={password}
          onChange={(e) => setPassword(e.target.value)}
        />
        {error && (
          <p className="mb-3 text-sm text-bad-700" role="alert">
            {error}
          </p>
        )}
        <button type="submit" className="btn-primary w-full" disabled={busy || !username || !password}>
          {t("login.submit")}
        </button>
      </form>
    </div>
  );
}
