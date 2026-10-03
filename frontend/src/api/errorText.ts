import i18n from "../i18n";
import { ApiError, errorKind } from "./client";

export function errorText(e: unknown): string {
  // http_502 and friends mean the body had no message (proxy page or empty).
  if (e instanceof ApiError && !e.code.startsWith("http_")) return e.message_for(i18n.language);
  return i18n.t(`errors.${errorKind(e)}`);
}
