import type { ReactNode } from "react";
import type { Role } from "../api/types";

/** Routes; each story replaces its placeholder with the real page. */
export const pages: { path: string; key: string; min: Role; element?: ReactNode }[] = [
  { path: "/", key: "home", min: "manager" },
  { path: "/stock", key: "stock", min: "staff" },
  { path: "/cash", key: "cash", min: "manager" },
  { path: "/books", key: "books", min: "manager" },
  { path: "/harness", key: "harness", min: "manager" },
  { path: "/chaos", key: "chaos", min: "owner" },
  { path: "/settings", key: "settings", min: "owner" },
];
