import type { Money } from "../api/types";

/** Uses the value's own decimals (EGP 2, OMR 3), never a fixed count. */
export function formatMoney(m: Money | null | undefined, lang: string = "en"): string {
  if (!m) return "—";
  const value = m.amount_minor / 10 ** m.decimals;
  const text = new Intl.NumberFormat(lang === "ar" ? "ar-EG" : "en-US", {
    minimumFractionDigits: m.decimals,
    maximumFractionDigits: m.decimals,
  }).format(value);
  return lang === "ar" ? `${text} ${m.currency}` : `${m.currency} ${text}`;
}

export function toMajor(m: Money): number {
  return m.amount_minor / 10 ** m.decimals;
}

export function isNegative(m: Money | null | undefined): boolean {
  return !!m && m.amount_minor < 0;
}
