import { describe, expect, it } from "vitest";
import { discountPercent, formatINR, relativeTime, splitBold } from "./format";

describe("formatINR", () => {
  it.each([
    [0, "₹0"],
    [999, "₹999"],
    [1000, "₹1,000"],
    [62990, "₹62,990"],
    [124990, "₹1,24,990"],
    [10000000, "₹1,00,00,000"],
    [-1500, "-₹1,500"],
  ])("formats %d as %s", (n, expected) => {
    expect(formatINR(n)).toBe(expected);
  });
});

describe("discountPercent", () => {
  it("computes rounded discounts and ignores bad MRPs", () => {
    expect(discountPercent(62990, 79990)).toBe(21);
    expect(discountPercent(100, 100)).toBe(0);
    expect(discountPercent(100, 50)).toBe(0);
  });
});

describe("splitBold", () => {
  it("splits **bold** segments without producing HTML", () => {
    expect(splitBold("Top pick is **HP Victus** at ₹67,990")).toEqual([
      { text: "Top pick is ", bold: false },
      { text: "HP Victus", bold: true },
      { text: " at ₹67,990", bold: false },
    ]);
    expect(splitBold("<img src=x onerror=alert(1)>")).toEqual([{ text: "<img src=x onerror=alert(1)>", bold: false }]);
  });
});

describe("relativeTime", () => {
  it("formats recent timestamps", () => {
    const now = new Date("2026-10-18T12:00:00Z");
    expect(relativeTime("2026-10-18T11:59:30Z", now)).toBe("just now");
    expect(relativeTime("2026-10-18T11:30:00Z", now)).toBe("30m ago");
    expect(relativeTime("2026-10-16T12:00:00Z", now)).toBe("2d ago");
  });
});
