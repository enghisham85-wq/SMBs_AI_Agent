import type { QueryClient } from "@tanstack/react-query";
import { lazy, type ComponentType } from "react";
import { booksDocumentsQuery, cashForecastQuery, homeQuery, stockItemsQuery } from "../api/queries";
import type { Role } from "../api/types";

interface PageDef {
  path: string;
  key: string;
  min: Role;
  demoOnly?: boolean;
  load: () => Promise<ComponentType>;
  prefetch?: (qc: QueryClient) => Promise<void>;
}

// One chunk per page, so the chart library only loads where it's used.
const defs: PageDef[] = [
  {
    path: "/",
    key: "home",
    min: "manager",
    load: () => import("./Home").then((m) => m.Home),
    prefetch: (qc) => qc.prefetchQuery(homeQuery),
  },
  {
    path: "/stock",
    key: "stock",
    min: "staff",
    load: () => import("./Stock").then((m) => m.Stock),
    prefetch: (qc) => qc.prefetchQuery(stockItemsQuery),
  },
  {
    path: "/cash",
    key: "cash",
    min: "manager",
    load: () => import("./Cash").then((m) => m.Cash),
    prefetch: (qc) => qc.prefetchQuery(cashForecastQuery(null)),
  },
  {
    path: "/books",
    key: "books",
    min: "manager",
    load: () => import("./Books").then((m) => m.Books),
    prefetch: (qc) => qc.prefetchQuery(booksDocumentsQuery),
  },
  {
    path: "/harness",
    key: "harness",
    min: "manager",
    load: () => import("./Harness").then((m) => m.Harness),
  },
  {
    path: "/chaos",
    key: "chaos",
    min: "owner",
    demoOnly: true,
    load: () => import("./Chaos").then((m) => m.Chaos),
  },
  {
    path: "/settings",
    key: "settings",
    min: "owner",
    load: () => import("./Settings").then((m) => m.Settings),
  },
];

export const pages = defs.map((d) => ({
  ...d,
  Component: lazy(() => d.load().then((c) => ({ default: c }))),
}));

export function prefetchRoute(qc: QueryClient, path: string): void {
  const page = pages.find((p) => p.path === path);
  if (!page) return;
  // If this fails, the error boundary shows it when the route opens.
  page.load().catch(() => undefined);
  if (page.prefetch) void page.prefetch(qc);
}
