import type { QueryClient } from "@tanstack/react-query";
import { lazy, type ComponentType } from "react";
import { booksDocumentsQuery, cashForecastQuery, homeQuery, stockItemsQuery } from "../api/queries";
import type { Role } from "../api/types";

interface PageDef {
  path: string;
  key: string;
  min: Role;
  demoOnly?: boolean;
  /** Loads the page's chunk; also used to warm it before navigation. */
  load: () => Promise<ComponentType>;
  /** Prefetches the query the page shows first, alongside the chunk. */
  prefetch?: (qc: QueryClient) => Promise<void>;
}

/** Routes. Each page is its own chunk, so the charts library only loads with the pages that draw charts. */
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

/** Warm a route before the user gets there: its chunk and its first query. */
export function prefetchRoute(qc: QueryClient, path: string): void {
  const page = pages.find((p) => p.path === path);
  if (!page) return;
  // A failed chunk load surfaces again, with the error boundary, when the route is actually opened.
  page.load().catch(() => undefined);
  if (page.prefetch) void page.prefetch(qc);
}
