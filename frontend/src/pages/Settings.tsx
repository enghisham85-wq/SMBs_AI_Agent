import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { useTranslation } from "react-i18next";
import { api, ApiError } from "../api/client";
import type { Money, Role } from "../api/types";

interface Business {
  country: string;
  currency: string;
  decimals: number;
  vat_rate_percent: string;
  vat_period: "monthly" | "quarterly";
  weekend_days: number[];
  min_cash_buffer: Money;
  currency_locked: boolean;
}
interface Country { code: string; name_en: string; name_ar: string; currency: string }

// Settings shown as plain numbers; money limits are stored in minor units and edited in major units.
const NUMBER_KEYS = ["price_change_pct", "stock_variance_pct", "approval_timeout_hours", "stale_bank_days", "dead_stock_days",
  "cash_variance_pct"] as const;
const FRACTION_KEYS = ["confidence_high", "confidence_low", "action_failure_threshold", "forecast_mape_threshold"] as const;
const MONEY_KEYS = ["po_auto_approve_limit", "journal_value_limit"] as const;
const WEEKDAYS = [1, 2, 3, 4, 5, 6, 7];

function useErrorText() {
  const { i18n } = useTranslation();
  return (e: unknown) => (e instanceof ApiError ? e.message_for(i18n.language) : String(e));
}

export function Settings() {
  const { t } = useTranslation();
  return (
    <div className="flex flex-col gap-4">
      <h1 className="text-xl font-semibold">{t("nav.settings")}</h1>
      <BusinessSettings />
      <Thresholds />
      <Users />
      <TelegramLink />
    </div>
  );
}

function BusinessSettings() {
  const biz = useQuery({ queryKey: ["business"], queryFn: () => api.get<Business>("/business") });
  const countries = useQuery({ queryKey: ["countries"], queryFn: () => api.get<{ countries: Country[] }>("/countries") });
  if (!biz.data) return null;
  return <BusinessForm key={JSON.stringify(biz.data)} b={biz.data} countries={countries.data?.countries ?? []} />;
}

function BusinessForm({ b, countries }: { b: Business; countries: Country[] }) {
  const { t, i18n } = useTranslation();
  const qc = useQueryClient();
  const err = useErrorText();
  const [form, setForm] = useState<Record<string, any>>(() => ({
    country: b.country, currency: b.currency, vat_rate_percent: String(Number(b.vat_rate_percent)), vat_period: b.vat_period,
    weekend_days: b.weekend_days, min_cash_buffer: (b.min_cash_buffer.amount_minor / 10 ** b.min_cash_buffer.decimals).toFixed(b.decimals),
  }));
  const [msg, setMsg] = useState<string | null>(null);
  const save = useMutation({
    mutationFn: (body: Record<string, unknown>) => api.patch<Business>("/business", body),
    onSuccess: () => { setMsg(t("settings.saved")); void qc.invalidateQueries({ queryKey: ["business"] }); },
    onError: (e) => setMsg(err(e)),
  });
  const currencies = [...new Set([b.currency, ...countries.map((c) => c.currency)])];
  const submit = () => {
    const body: Record<string, unknown> = {};
    if (form.country !== b.country) body.country = form.country;
    if (form.currency !== b.currency) body.currency = form.currency;
    if (Number(form.vat_rate_percent) !== Number(b.vat_rate_percent)) body.vat_rate_percent = Number(form.vat_rate_percent);
    if (form.vat_period !== b.vat_period) body.vat_period = form.vat_period;
    if (JSON.stringify(form.weekend_days) !== JSON.stringify(b.weekend_days)) body.weekend_days = form.weekend_days;
    body.min_cash_buffer = form.min_cash_buffer;
    save.mutate(body);
  };
  const dayName = (d: number) => new Date(2024, 0, d).toLocaleDateString(i18n.language === "ar" ? "ar-EG" : "en-GB", { weekday: "short" });
  return (
    <section className="card grid gap-3 sm:grid-cols-2" data-testid="business-settings">
      <h2 className="text-base font-semibold sm:col-span-2">{t("settings.business")}</h2>
      <label className="label">{t("settings.country")}
        <select className="input" value={form.country ?? ""} onChange={(e) => setForm({ ...form, country: e.target.value })}>
          {countries.map((c) => <option key={c.code} value={c.code}>{i18n.language === "ar" ? c.name_ar : c.name_en}</option>)}
        </select>
      </label>
      <label className="label">{t("settings.currency")}
        <select className="input" value={form.currency ?? ""} disabled={b.currency_locked} onChange={(e) => setForm({ ...form, currency: e.target.value })}>
          {currencies.map((c) => <option key={c} value={c}>{c}</option>)}
        </select>
        {b.currency_locked && <span className="mt-1 block text-xs text-ink-500">{t("settings.currency_locked")}</span>}
      </label>
      <label className="label">{t("settings.vat_rate")}
        <span className="flex items-center gap-2">
          <input className="input" type="number" min={0} max={100} step={0.01} value={form.vat_rate_percent ?? ""}
            onChange={(e) => setForm({ ...form, vat_rate_percent: e.target.value })} />
          <span aria-hidden>%</span>
        </span>
      </label>
      <label className="label">{t("settings.vat_period")}
        <select className="input" value={form.vat_period ?? "monthly"} onChange={(e) => setForm({ ...form, vat_period: e.target.value })}>
          <option value="monthly">{t("settings.monthly")}</option>
          <option value="quarterly">{t("settings.quarterly")}</option>
        </select>
      </label>
      <fieldset className="sm:col-span-2">
        <legend className="label">{t("settings.weekend")}</legend>
        <div className="flex flex-wrap gap-2">
          {WEEKDAYS.map((d) => (
            <label key={d} className="flex items-center gap-1 text-sm">
              <input type="checkbox" checked={(form.weekend_days ?? []).includes(d)}
                onChange={(e) => setForm({ ...form, weekend_days: e.target.checked ? [...(form.weekend_days ?? []), d].sort() : (form.weekend_days ?? []).filter((x: number) => x !== d) })} />
              {dayName(d)}
            </label>
          ))}
        </div>
      </fieldset>
      <label className="label">{t("settings.min_cash_buffer")} ({b.currency})
        <input className="input" type="number" min={0} step={10 ** -b.decimals} value={form.min_cash_buffer ?? ""}
          onChange={(e) => setForm({ ...form, min_cash_buffer: e.target.value })} />
      </label>
      <div className="flex items-end gap-3 sm:col-span-2">
        <button className="btn-primary" disabled={save.isPending} onClick={submit}>{t("common.save")}</button>
        {msg && <span className="text-sm text-ink-700" role="status">{msg}</span>}
      </div>
    </section>
  );
}

function Thresholds() {
  const q = useQuery({ queryKey: ["settings"], queryFn: () => api.get<{ settings: Record<string, any> }>("/settings") });
  const biz = useQuery({ queryKey: ["business"], queryFn: () => api.get<Business>("/business") });
  if (!q.data || !biz.data) return null;
  return <ThresholdForm key={JSON.stringify(q.data.settings)} settings={q.data.settings} decimals={biz.data.decimals} currency={biz.data.currency} />;
}

function ThresholdForm({ settings: s, decimals, currency }: { settings: Record<string, any>; decimals: number; currency: string }) {
  const { t } = useTranslation();
  const qc = useQueryClient();
  const err = useErrorText();
  const [form, setForm] = useState<Record<string, any>>(() => {
    const f: Record<string, any> = { reminder_auto_approve: s.reminder_auto_approve };
    for (const k of NUMBER_KEYS) f[k] = s[k];
    for (const k of FRACTION_KEYS) f[k] = Math.round(Number(s[k]) * 100);
    for (const k of MONEY_KEYS) f[k] = (Number(s[k]) / 10 ** decimals).toFixed(decimals);
    return f;
  });
  const [msg, setMsg] = useState<string | null>(null);
  const save = useMutation({
    mutationFn: (values: Record<string, unknown>) => api.patch("/settings", { values }),
    onSuccess: () => { setMsg(t("settings.saved")); void qc.invalidateQueries({ queryKey: ["settings"] }); },
    onError: (e) => setMsg(err(e)),
  });
  const submit = () => {
    const values: Record<string, unknown> = { reminder_auto_approve: form.reminder_auto_approve };
    for (const k of NUMBER_KEYS) values[k] = Number(form[k]);
    for (const k of FRACTION_KEYS) values[k] = Number(form[k]) / 100;
    for (const k of MONEY_KEYS) values[k] = Math.round(Number(form[k]) * 10 ** decimals);
    save.mutate(values);
  };
  const field = (k: string, suffix?: string) => (
    <label key={k} className="label">{t(`settings.keys.${k}`)}
      <span className="flex items-center gap-2">
        <input className="input" type="number" min={0} value={form[k] ?? ""} onChange={(e) => setForm({ ...form, [k]: e.target.value })} />
        {suffix && <span aria-hidden>{suffix}</span>}
      </span>
    </label>
  );
  return (
    <section className="card grid gap-3 sm:grid-cols-2" data-testid="threshold-settings">
      <h2 className="text-base font-semibold sm:col-span-2">{t("settings.thresholds")}</h2>
      <label className="label">{t("settings.keys.reminder_auto_approve")}
        <select className="input" value={form.reminder_auto_approve ?? "off"} onChange={(e) => setForm({ ...form, reminder_auto_approve: e.target.value })}>
          <option value="off">{t("settings.reminders_off")}</option>
          <option value="polite_only">{t("settings.reminders_polite")}</option>
        </select>
      </label>
      {MONEY_KEYS.map((k) => field(k, currency))}
      {NUMBER_KEYS.map((k) => field(k, k.endsWith("pct") ? "%" : undefined))}
      {FRACTION_KEYS.map((k) => field(k, "%"))}
      <div className="flex items-end gap-3 sm:col-span-2">
        <button className="btn-primary" disabled={save.isPending} onClick={submit}>{t("common.save")}</button>
        {msg && <span className="text-sm text-ink-700" role="status">{msg}</span>}
      </div>
    </section>
  );
}

function Users() {
  const { t } = useTranslation();
  const qc = useQueryClient();
  const err = useErrorText();
  const q = useQuery({ queryKey: ["users"], queryFn: () => api.get<{ users: any[] }>("/users") });
  const [draft, setDraft] = useState({ username: "", password: "", role: "staff" as Role });
  const [msg, setMsg] = useState<string | null>(null);
  const done = () => { setMsg(t("settings.saved")); void qc.invalidateQueries({ queryKey: ["users"] }); };
  const patch = useMutation({ mutationFn: ({ id, body }: { id: string; body: unknown }) => api.patch(`/users/${id}`, body), onSuccess: done, onError: (e) => setMsg(err(e)) });
  const create = useMutation({ mutationFn: () => api.post("/users", draft), onSuccess: () => { setDraft({ username: "", password: "", role: "staff" }); done(); }, onError: (e) => setMsg(err(e)) });
  return (
    <section className="card flex flex-col gap-3" data-testid="users">
      <h2 className="text-base font-semibold">{t("settings.users")}</h2>
      <table className="table">
        <thead><tr><th>{t("login.username")}</th><th>{t("settings.role")}</th><th>{t("settings.active")}</th></tr></thead>
        <tbody>
          {(q.data?.users ?? []).map((u) => (
            <tr key={u.id}>
              <td>{u.username}{u.telegram_linked && <span className="badge ms-2 bg-brand-50 text-brand-700">Telegram</span>}</td>
              <td>
                <select className="input w-auto" value={u.role} onChange={(e) => patch.mutate({ id: u.id, body: { role: e.target.value } })}>
                  {(["owner", "manager", "staff"] as Role[]).map((r) => <option key={r} value={r}>{t(`settings.roles.${r}`)}</option>)}
                </select>
              </td>
              <td><input type="checkbox" checked={u.active} onChange={(e) => patch.mutate({ id: u.id, body: { active: e.target.checked } })} aria-label={t("settings.active")} /></td>
            </tr>
          ))}
        </tbody>
      </table>
      <div className="flex flex-wrap items-end gap-2">
        <input className="input w-auto" placeholder={t("login.username")} value={draft.username} onChange={(e) => setDraft({ ...draft, username: e.target.value })} />
        <input className="input w-auto" type="password" placeholder={t("login.password")} value={draft.password} onChange={(e) => setDraft({ ...draft, password: e.target.value })} />
        <select className="input w-auto" value={draft.role} onChange={(e) => setDraft({ ...draft, role: e.target.value as Role })}>
          {(["owner", "manager", "staff"] as Role[]).map((r) => <option key={r} value={r}>{t(`settings.roles.${r}`)}</option>)}
        </select>
        <button className="btn-secondary" disabled={create.isPending || !draft.username || draft.password.length < 8} onClick={() => create.mutate()}>{t("settings.add_user")}</button>
      </div>
      {msg && <span className="text-sm text-ink-700" role="status">{msg}</span>}
    </section>
  );
}

function TelegramLink() {
  const { t } = useTranslation();
  const link = useMutation({ mutationFn: () => api.post<{ code: string; expires_at: string }>("/me/telegram-link-code") });
  return (
    <section className="card flex flex-wrap items-center gap-3" data-testid="telegram-link">
      <h2 className="text-base font-semibold">{t("settings.telegram")}</h2>
      <button className="btn-secondary" onClick={() => link.mutate()}>{t("settings.telegram_code")}</button>
      {link.data && <span className="text-sm">{t("settings.telegram_send", { code: link.data.code })}</span>}
    </section>
  );
}
