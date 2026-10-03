export interface Money {
  amount_minor: number;
  currency: string;
  decimals: number;
  display: string;
}

export type Role = "staff" | "manager" | "owner";
export type Lang = "en" | "ar";

export interface Me {
  user: { id: string; username: string; role: Role; language: Lang; business_id: string };
  csrf_token: string;
  business: { id: string; name: string; country: string; currency: string; decimals: number; demo_mode: boolean };
}

export interface ApprovalOption {
  key: string;
  label_en: string;
  label_ar: string;
  effect: string;
}

export interface ApprovalRequest {
  id: string;
  kind: "approval" | "question" | "alert";
  agent: string;
  text_en: string;
  text_ar: string;
  options: ApprovalOption[];
  required_role: Role;
  deadline: string | null;
  urgency: number;
  status: "pending" | "resolved" | "timed_out" | "superseded";
  resolved_option: string | null;
  resolved_by: string | null;
  resolved_via: string | null;
  resolved_at: string | null;
  action_id: string | null;
  context: Record<string, unknown>;
  created_at: string | null;
  allow_text: boolean;
}

export interface ClockState {
  mode: "real" | "simulated";
  current_date: string;
  last_run_date: string | null;
  advancing: boolean;
}

export type DataAsOf = Record<string, string | null>;
