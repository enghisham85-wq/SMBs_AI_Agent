import { Suspense, useState, type ReactNode } from "react";
import { useTranslation } from "react-i18next";
import { NavLink, useLocation } from "react-router-dom";
import { errorText } from "../api/errorText";
import type { Role } from "../api/types";
import { useAuth } from "../hooks/useAuth";
import { useMediaQuery } from "../hooks/useMediaQuery";
import { ChatPanel } from "./ChatPanel";
import { ClockControl } from "./ClockControl";
import { ErrorBoundary } from "./ErrorBoundary";

export const NAV: { to: string; key: string; min: Role; demoOnly?: boolean }[] = [
  { to: "/", key: "home", min: "manager" },
  { to: "/stock", key: "stock", min: "staff" },
  { to: "/cash", key: "cash", min: "manager" },
  { to: "/books", key: "books", min: "manager" },
  { to: "/harness", key: "harness", min: "manager" },
  { to: "/chaos", key: "chaos", min: "owner", demoOnly: true },
  { to: "/settings", key: "settings", min: "owner" },
];

export function Layout({ children }: { children: ReactNode }) {
  const { t, i18n } = useTranslation();
  const { me, can, logout, setLanguage } = useAuth();
  const { pathname } = useLocation();
  const [chatOpen, setChatOpen] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);
  // Tailwind's lg breakpoint. Exactly one ChatPanel exists (one /chat/stream connection): docked when
  // wide, inside the drawer only while it is open on narrow screens.
  const wide = useMediaQuery("(min-width: 1024px)");
  const links = NAV.filter((n) => can(n.min) && (!n.demoOnly || me?.business.demo_mode));
  const run = (action: Promise<void>) => {
    setActionError(null);
    action.catch((e: unknown) => setActionError(errorText(e)));
  };

  return (
    <div className="min-h-screen">
      <header className="sticky top-0 z-20 border-b border-slate-200 bg-white/95 backdrop-blur">
        <div className="mx-auto flex max-w-7xl flex-wrap items-center gap-3 px-4 py-2">
          <strong className="text-brand-700">{me?.business.name ?? t("app.title")}</strong>
          <nav className="flex flex-1 flex-wrap gap-1" aria-label={t("nav.main")}>
            {links.map((n) => (
              <NavLink
                key={n.to}
                to={n.to}
                end={n.to === "/"}
                className={({ isActive }) =>
                  `rounded-md px-2 py-1 text-sm ${isActive ? "bg-brand-50 font-semibold text-brand-700" : "text-ink-700 hover:bg-slate-100"}`
                }
              >
                {t(`nav.${n.key}`)}
              </NavLink>
            ))}
          </nav>
          <button type="button" className="btn-secondary lg:hidden" onClick={() => setChatOpen(true)}>
            {t("chat.open")}
          </button>
          <button
            type="button"
            className="btn-secondary"
            onClick={() => run(setLanguage(i18n.language === "ar" ? "en" : "ar"))}
          >
            {t("lang.switch")}
          </button>
          <span className="text-xs text-ink-500">
            {me?.user.username} · {me?.user.role}
          </span>
          <button type="button" className="btn-secondary" onClick={() => run(logout())}>
            {t("nav.logout")}
          </button>
        </div>
        {actionError && (
          <p className="mx-auto max-w-7xl px-4 pb-2 text-sm text-bad-700" role="alert">
            {actionError}
          </p>
        )}
        <div className="mx-auto max-w-7xl px-4 pb-2">
          <ClockControl />
        </div>
      </header>
      <div className="mx-auto grid max-w-7xl gap-4 px-4 py-4 lg:grid-cols-[1fr_360px]">
        <main className="min-w-0">
          <ErrorBoundary resetKey={pathname}>
            <Suspense fallback={<p className="text-ink-500">{t("app.loading")}</p>}>{children}</Suspense>
          </ErrorBoundary>
        </main>
        {wide && (
          <aside>
            <div className="sticky top-28 max-h-[calc(100vh-8rem)] overflow-y-auto">
              <ChatPanel />
            </div>
          </aside>
        )}
      </div>
      {chatOpen && !wide && (
        <div className="fixed inset-0 z-30 flex" role="dialog" aria-modal="true">
          <button
            type="button"
            className="flex-1 bg-black/30"
            aria-label={t("chat.close")}
            onClick={() => setChatOpen(false)}
          />
          <div className="h-full w-[90%] max-w-sm overflow-y-auto bg-slate-50 p-4 shadow-xl">
            <button type="button" className="btn-secondary mb-3" onClick={() => setChatOpen(false)}>
              {t("chat.close")}
            </button>
            <ChatPanel />
          </div>
        </div>
      )}
    </div>
  );
}
