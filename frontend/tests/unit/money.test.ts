import { formatMoney } from "../../src/lib/money";

describe("formatMoney", () => {
  it("uses the currency's own decimals", () => {
    expect(formatMoney({ amount_minor: 180000, currency: "EGP", decimals: 2, display: "" })).toBe("EGP 1,800.00");
    expect(formatMoney({ amount_minor: 36000, currency: "OMR", decimals: 3, display: "" })).toBe("OMR 36.000");
  });
  it("renders a dash for missing values", () => {
    expect(formatMoney(null)).toBe("—");
  });
});
