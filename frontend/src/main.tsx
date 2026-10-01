import { QueryCache, QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { BrowserRouter } from "react-router-dom";
import App from "./App";
import { errorKind, isTransient } from "./api/client";
import { AuthProvider } from "./hooks/useAuth";
import { EventStreamProvider } from "./hooks/useEventStream";
import "./i18n";
import "./index.css";
import { prefetchRoute } from "./pages/registry";

// Boosthis performance monitoring: loaded only when a project key is set at build time.
const boosthisKey = import.meta.env.VITE_BOOSTHIS_INVITE_KEY as string | undefined;
if (boosthisKey) {
  void import("@workspace/boosthis-runtime-web").then(({ startWebVitals, enableTelemetry }) => {
    startWebVitals({ bubble: true });
    enableTelemetry({
      // Generated once; never change it, or this browser app registers as a second install.
      installId: "8501f537-b218-4d28-be35-e054fa150ed4",
      inviteKey: boosthisKey,
      endpoint: "https://www.boosthis.com/api",
      appName: "SMBAgents Dashboard",
    });
  });
}

const queryClient: QueryClient = new QueryClient({
  queryCache: new QueryCache({
    onError: (error) => {
      // A 401 on any call means the session ended: re-check /me so the app falls back to the sign-in
      // screen. Skipped while nobody is signed in, so the landing prefetch below cannot trigger it.
      if (errorKind(error) === "auth" && queryClient.getQueryData(["me"]))
        void queryClient.invalidateQueries({ queryKey: ["me"] });
    },
  }),
  defaultOptions: {
    queries: {
      // One retry for a flaky network or server; a 4xx or a timeout would only fail again more slowly.
      retry: (failures, error) => failures < 1 && isTransient(error),
      refetchOnWindowFocus: false,
    },
  },
});

// Home's data and chunk load alongside /me instead of waiting for it. A 401 here is expected when signed
// out: it is not retried, and Home refetches after sign-in because login invalidates every query.
if (window.location.pathname === "/") prefetchRoute(queryClient, "/");

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      <BrowserRouter>
        <AuthProvider>
          <EventStreamProvider>
            <App />
          </EventStreamProvider>
        </AuthProvider>
      </BrowserRouter>
    </QueryClientProvider>
  </StrictMode>,
);
