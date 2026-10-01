// The vendored Boosthis kit (lib/boosthis-runtime-web, aliased in vite.config.ts) does not pass this
// project's strict compiler settings and must stay byte-for-byte as shipped, so tsc sees only the calls we make.
declare module "@workspace/boosthis-runtime-web" {
  export function startWebVitals(options?: { bubble?: boolean }): void;
  export function enableTelemetry(opts: {
    installId: string;
    inviteKey?: string;
    endpoint?: string;
    appName?: string;
  }): unknown;
}
