import { useTranslation } from "react-i18next";
import { Link } from "react-router-dom";
import type { DataAsOf, Money } from "../api/types";
import { formatMoney, isNegative } from "../lib/money";
import { FreshnessLabel } from "./FreshnessLabel";

export interface Health {
  stock: {
    items: number;
    ok: number;
    warnings: number;
    critical_warnings: number;
    on_order: number;
    data_as_of: DataAsOf;
  };
  cash: {
    lowest: { date: string; balance: Money } | null;
    buffer: Money;
    first_below_buffer: string | null;
    confidence: { low: boolean; reason: string | null };
    data_as_of: DataAsOf;
  };
  books: { percent_reconciled: number; unmatched: number; review_count: number; data_as_of: DataAsOf };
}

type Tone = "good" | "warn" | "bad";
const TONE: Record<Tone, string> = {
  good: "border-good-500",
  warn: "border-warn-500",
  bad: "border-bad-500",
};

/** The four numbers the owner checks first, each with its data freshness (FR-045, FR-046). */
export function HealthStrip({ health }: { health: Health }) {
  const { t, i18n } = useTranslation();
  const lang = i18n.language;
  const num = (n: number) => n.toLocaleString(lang === "ar" ? "ar-EG" : "en-US");
  const day = (iso: string) =>
    new Date(iso + "T00:00:00").toLocaleDateString(lang === "ar" ? "ar-EG" : "en-GB", {
      day: "numeric",
      month: "short",
    });
  const { stock, cash, books } = health;
  const low = cash.lowest?.balance;
  const cashTone: Tone = !low || isNegative(low) ? "bad" : cash.first_below_buffer ? "warn" : "good";
  const stockTone: Tone = stock.critical_warnings > 0 ? "bad" : stock.warnings > 0 ? "warn" : "good";
  const reconTone: Tone =
    books.percent_reconciled >= 90 ? "good" : books.percent_reconciled >= 70 ? "warn" : "bad";
  const cashNote = [
    cash.lowest ? t("home.on_date", { date: day(cash.lowest.date) }) : "",
    cash.first_below_buffer ? t("home.below_buffer", { date: day(cash.first_below_buffer) }) : "",
    cash.confidence.low ? t("home.low_confidence") : "",
  ]
    .filter(Boolean)
    .join(" · ");

  return (
    <section
      className="grid grid-cols-2 gap-3 lg:grid-cols-4"
      aria-label={t("home.health")}
      data-testid="health-strip"
    >
      <Tile
        to="/stock"
        tone={stockTone}
        label={t("home.stock")}
        asOf={stock.data_as_of}
        value={
          stock.warnings === 0 ? t("home.stock_ok") : t("home.stock_warnings", { count: stock.warnings })
        }
        note={t("home.stock_note", { ok: num(stock.ok), on_order: num(stock.on_order) })}
      />
      <Tile
        to="/cash"
        tone={cashTone}
        label={t("home.lowest_cash")}
        asOf={cash.data_as_of}
        value={formatMoney(low, lang)}
        note={cashNote}
      />
      <Tile
        to="/books"
        tone={reconTone}
        label={t("home.reconciled")}
        asOf={books.data_as_of}
        value={`${num(books.percent_reconciled)}%`}
        note={t("home.unmatched", { count: books.unmatched })}
      />
      <Tile
        to="/books"
        tone={books.review_count === 0 ? "good" : "warn"}
        label={t("home.to_review")}
        asOf={books.data_as_of}
        value={num(books.review_count)}
        note={t("home.review_note")}
      />
    </section>
  );
}

function Tile(props: { to: string; tone: Tone; label: string; value: string; note: string; asOf: DataAsOf }) {
  return (
    <Link
      to={props.to}
      className={`card flex flex-col gap-1 border-s-4 ${TONE[props.tone]} hover:bg-slate-50`}
    >
      <span className="text-xs font-semibold uppercase text-ink-500">{props.label}</span>
      <strong className="text-lg text-ink-900" dir="auto">
        {props.value}
      </strong>
      {props.note && <span className="text-xs text-ink-700">{props.note}</span>}
      <FreshnessLabel asOf={props.asOf} />
    </Link>
  );
}
