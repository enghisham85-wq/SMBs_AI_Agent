// Shared by each page and its prefetch so both hit one cache entry.
// PREFETCHED only avoids a refetch right after a prefetch; writes still invalidate.
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

// "approvals" prefix so answering a request anywhere refreshes Home too.
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
