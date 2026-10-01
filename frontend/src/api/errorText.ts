import i18n from "../i18n";
import { ApiError, errorKind } from "./client";

/** Text for a failed call: the server's own wording when it sent one, otherwise a message by cause. */
export function errorText(e: unknown): string {
  // Codes like "http_502" mean the body carried no message of its own (a proxy page, or none at all).
  if (e instanceof ApiError && !e.code.startsWith("http_")) return e.message_for(i18n.language);
  return i18n.t(`errors.${errorKind(e)}`);
}
