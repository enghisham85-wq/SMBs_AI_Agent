import i18n from "i18next";
import { initReactI18next } from "react-i18next";
import ar from "./ar.json";
import en from "./en.json";

const STORAGE_KEY = "lang";

// The last language used in this browser, so the first paint already has the right text and direction
// instead of flipping to right-to-left once /me answers.
function storedLanguage(): "en" | "ar" {
  try {
    return localStorage.getItem(STORAGE_KEY) === "ar" ? "ar" : "en";
  } catch {
    return "en"; // storage blocked (privacy mode, sandboxed frame)
  }
}

function applyDirection(lang: "en" | "ar"): void {
  document.documentElement.lang = lang;
  document.documentElement.dir = lang === "ar" ? "rtl" : "ltr";
}

const initial = storedLanguage();
applyDirection(initial);

void i18n.use(initReactI18next).init({
  resources: { en: { translation: en }, ar: { translation: ar } },
  lng: initial,
  fallbackLng: "en",
  interpolation: { escapeValue: false },
});

/** Switch language and page direction (Arabic is right-to-left). */
export function applyLanguage(lang: "en" | "ar"): void {
  void i18n.changeLanguage(lang);
  applyDirection(lang);
  try {
    localStorage.setItem(STORAGE_KEY, lang);
  } catch {
    /* not remembered; the server-side preference still applies after /me */
  }
}

export default i18n;
