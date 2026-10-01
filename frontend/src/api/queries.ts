// Query definitions shared by a page and whatever prefetches it, so both hit the same cache entry.
// PREFETCHED keeps a just-prefetched answer from being fetched again when the page mounts a moment later;
// writes still refresh these at once because they invalidate their keys.
import { queryOptions } from "@tanstack/react-query";
import type { CashPoint } from "../components/CashChart";
import type { Health } from "../components/HealthStrip";
import type { StockItem } from "../components/StockTable";
import { api } from "./client";
import type { ApprovalRequest, DataAsOf, Money } from "./types";

const PREFETCHED = 30_000;

export interface HomeData {
  health: Health;
  decisions: ApprovalRequest[];
  alerts: ApprovalRequest[];
  business_date: string;
  data_as_of: DataAsOf;
}

export interface Forecast {
  scenario: string;
  primary_scenario: string;
  generated_on: string;
  series: (CashPoint & { below_buffer: boolean })[];
  buffer: Money;
  lowest: { date: string; balance: Money } | null;
  first_below_buffer: string | null;
  confidence: { low: boolean; reason: string | null };
  data_as_of: DataAsOf;
}

// Under the "approvals" key so answering any request, here or in the chat panel, refreshes Home.
export const homeQuery = queryOptions({
  queryKey: ["approvals", "home"],
  queryFn: ({ signal }) => api.get<HomeData>("/home", signal),
  staleTime: PREFETCHED,
});

export const stockItemsQuery = queryOptions({
  queryKey: ["stock", "items"],
  queryFn: ({ signal }) => api.get<{ items: StockItem[]; data_as_of: DataAsOf }>("/stock/items", signal),
  staleTime: PREFETCHED,
});

export const cashForecastQuery = (scenario: string | null) =>
  queryOptions({
    queryKey: ["cash", "forecast", scenario],
    queryFn: ({ signal }) =>
      api.get<Forecast>(`/cash/forecast?horizon=30d${scenario ? `&scenario=${scenario}` : ""}`, signal),
    staleTime: PREFETCHED,
  });

export const booksDocumentsQuery = queryOptions({
  queryKey: ["books", "documents"],
  queryFn: ({ signal }) => api.get<{ documents: any[]; data_as_of: DataAsOf }>("/documents", signal),
  staleTime: PREFETCHED,
});
