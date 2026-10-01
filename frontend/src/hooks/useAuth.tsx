import { useQuery, useQueryClient } from "@tanstack/react-query";
import { createContext, useCallback, useContext, useEffect, type ReactNode } from "react";
import { api, ApiError, setCsrfToken } from "../api/client";
import type { Me, Role } from "../api/types";
import { applyLanguage } from "../i18n";

const RANK: Record<Role, number> = { staff: 0, manager: 1, owner: 2 };

interface AuthValue {
  me: Me | null;
  loading: boolean;
  /** Why /me failed when it was not a 401 (server down, offline, timeout); the app shows a retry for it. */
  error: unknown;
  reload: () => void;
  can: (min: Role) => boolean;
  login: (username: string, password: string) => Promise<void>;
  logout: () => Promise<void>;
  setLanguage: (lang: "en" | "ar") => Promise<void>;
}

const AuthContext = createContext<AuthValue | null>(null);

export function AuthProvider({ children }: { children: ReactNode }) {
  const qc = useQueryClient();
  const meQuery = useQuery({
    queryKey: ["me"],
    queryFn: async ({ signal }) => {
      try {
        return await api.get<Me>("/me", signal);
      } catch (e) {
        if (e instanceof ApiError && e.status === 401) return null;
        throw e;
      }
    },
    staleTime: 60_000,
  });
  const me = meQuery.data ?? null;
  // A retry after a failed first load shows the loading text again rather than a dead error screen.
  const loading = meQuery.isLoading || (meQuery.isError && meQuery.isFetching && !me);

  useEffect(() => {
    if (me) {
      setCsrfToken(me.csrf_token);
      applyLanguage(me.user.language);
    }
  }, [me]);

  const login = useCallback(
    async (username: string, password: string) => {
      const res = await api.post<{ csrf_token: string }>("/auth/login", { username, password });
      setCsrfToken(res.csrf_token);
      await qc.invalidateQueries();
    },
    [qc],
  );

  const logout = useCallback(async () => {
    try {
      await api.post("/auth/logout");
    } catch (e) {
      // An already-expired session is as good as a logout; anything else leaves the user signed in.
      if (!(e instanceof ApiError && e.status === 401)) throw e;
    }
    setCsrfToken(null);
    qc.clear();
    await qc.invalidateQueries({ queryKey: ["me"] });
  }, [qc]);

  const setLanguage = useCallback(
    async (lang: "en" | "ar") => {
      applyLanguage(lang);
      await api.patch("/me", { language: lang });
      await qc.invalidateQueries({ queryKey: ["me"] });
    },
    [qc],
  );

  const refetchMe = meQuery.refetch;
  const reload = useCallback(() => void refetchMe(), [refetchMe]);

  const can = useCallback((min: Role) => !!me && RANK[me.user.role] >= RANK[min], [me]);

  return (
    <AuthContext.Provider
      value={{ me, loading, error: meQuery.error, reload, can, login, logout, setLanguage }}
    >
      {children}
    </AuthContext.Provider>
  );
}

export function useAuth(): AuthValue {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error("useAuth outside AuthProvider");
  return ctx;
}
