import type { Config } from "tailwindcss";

export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        brand: { 50: "#f0fdfa", 100: "#ccfbf1", 500: "#14b8a6", 600: "#0d9488", 700: "#0f766e", 900: "#134e4a" },
        ink: { 400: "#94a3b8", 500: "#64748b", 700: "#334155", 900: "#0f172a" },
        warn: { 50: "#fffbeb", 500: "#f59e0b", 700: "#b45309" },
        bad: { 50: "#fef2f2", 500: "#ef4444", 700: "#b91c1c" },
        good: { 50: "#f0fdf4", 500: "#22c55e", 700: "#15803d" },
      },
      fontFamily: {
        sans: ["Inter", "Noto Sans Arabic", "system-ui", "Segoe UI", "Tahoma", "sans-serif"],
      },
    },
  },
  plugins: [],
} satisfies Config;
