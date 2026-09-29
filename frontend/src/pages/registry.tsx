import type { ReactNode } from "react";
import type { Role } from "../api/types";
import { Books } from "./Books";
import { Cash } from "./Cash";
import { Harness } from "./Harness";
import { Settings } from "./Settings";
import { Stock } from "./Stock";

/** Routes; each story replaces its placeholder with the real page. */
export const pages: { path: string; key: string; min: Role; element?: ReactNode }[] = [
  { path: "/", key: "home", min: "manager" },
  { path: "/stock", key: "stock", min: "staff", element: <Stock /> },
  { path: "/cash", key: "cash", min: "manager", element: <Cash /> },
  { path: "/books", key: "books", min: "manager", element: <Books /> },
  { path: "/harness", key: "harness", min: "manager", element: <Harness /> },
  { path: "/chaos", key: "chaos", min: "owner" },
  { path: "/settings", key: "settings", min: "owner", element: <Settings /> },
];
