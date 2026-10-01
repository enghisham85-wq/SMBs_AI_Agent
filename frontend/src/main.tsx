import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { BrowserRouter } from "react-router-dom";
import App from "./App";
import { AuthProvider } from "./hooks/useAuth";
import "./i18n";
import "./index.css";

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

const queryClient = new QueryClient({
  defaultOptions: { queries: { retry: 1, refetchOnWindowFocus: false } },
});

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      <BrowserRouter>
        <AuthProvider>
          <App />
        </AuthProvider>
      </BrowserRouter>
    </QueryClientProvider>
  </StrictMode>,
);
