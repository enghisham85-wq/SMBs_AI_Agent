import type { ReactNode } from "react";
import { useTranslation } from "react-i18next";
import { Navigate, Route, Routes } from "react-router-dom";
import type { Role } from "./api/types";
import { Layout } from "./components/Layout";
import { useAuth } from "./hooks/useAuth";
import { Login } from "./pages/Login";
import { Placeholder } from "./pages/Placeholder";
import { pages } from "./pages/registry";

function Guard({ min, children }: { min: Role; children: ReactNode }) {
  const { can } = useAuth();
  return can(min) ? <>{children}</> : <Navigate to="/stock" replace />;
}

export default function App() {
  const { t } = useTranslation();
  const { me, loading } = useAuth();
  if (loading) return <p className="p-6 text-ink-500">{t("app.loading")}</p>;
  if (!me) return <Login />;
  return (
    <Layout>
      <Routes>
        {pages.map((p) => (
          <Route
            key={p.path}
            path={p.path}
            element={<Guard min={p.min}>{p.element ?? <Placeholder titleKey={`nav.${p.key}`} />}</Guard>}
          />
        ))}
        <Route path="*" element={<Navigate to={me.user.role === "staff" ? "/stock" : "/"} replace />} />
      </Routes>
    </Layout>
  );
}
