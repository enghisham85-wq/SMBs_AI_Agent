import type { ReactNode } from "react";
import { useTranslation } from "react-i18next";
import { Navigate, Route, Routes } from "react-router-dom";
import type { Role } from "./api/types";
import { Layout } from "./components/Layout";
import { QueryError } from "./components/QueryError";
import { useAuth } from "./hooks/useAuth";
import { Login } from "./pages/Login";
import { pages } from "./pages/registry";

function Guard({ min, children }: { min: Role; children: ReactNode }) {
  const { can } = useAuth();
  return can(min) ? <>{children}</> : <Navigate to="/stock" replace />;
}

export default function App() {
  const { t } = useTranslation();
  const { me, loading, error, reload } = useAuth();
  if (loading) return <p className="p-6 text-ink-500">{t("app.loading")}</p>;
  // Only a 401 means "logged out" (me is null then); any other failure must not look like a sign-out.
  if (!me && error)
    return (
      <div className="mx-auto max-w-md p-6">
        <QueryError error={error} onRetry={reload} />
      </div>
    );
  if (!me) return <Login />;
  return (
    <Layout>
      <Routes>
        {pages
          .filter((p) => !p.demoOnly || me.business.demo_mode)
          .map(({ path, min, Component }) => (
            <Route
              key={path}
              path={path}
              element={
                <Guard min={min}>
                  <Component />
                </Guard>
              }
            />
          ))}
        <Route path="*" element={<Navigate to={me.user.role === "staff" ? "/stock" : "/"} replace />} />
      </Routes>
    </Layout>
  );
}
