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

const queryClient: QueryClient = new QueryClient({
  queryCache: new QueryCache({
    onError: (error) => {
      // Any 401 means the session ended, so re-check /me to get back to sign-in.
      // Skipped while signed out, otherwise the landing prefetch would trigger it.
      if (errorKind(error) === "auth" && queryClient.getQueryData(["me"]))
        void queryClient.invalidateQueries({ queryKey: ["me"] });
    },
  }),
  defaultOptions: {
    queries: {
      // One retry for flaky networks. 4xx and timeouts would just fail again, slower.
      retry: (failures, error) => failures < 1 && isTransient(error),
      refetchOnWindowFocus: false,
    },
  },
});

// Don't wait for /me to start loading Home. A 401 here is fine when signed out,
// login invalidates everything anyway.
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
