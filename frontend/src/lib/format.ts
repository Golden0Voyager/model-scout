// Presentation helpers shared by the dashboard and the model modal.
// Both files previously carried their own copy, which is how the CNY/USD rate ended
// up written down twice; keep conversions here so there is one place to update.

// Static, not fetched: this only powers a rough price comparison in the UI.
// 7.2 was stale by ~7% against the 2026-10-03 market (er-api 6.7144, ECB 6.7046),
// which understated every USD equivalent of a CNY-priced model.
export const CNY_TO_USD = 6.71;

export function latencyColor(ms: number): string {
  if (ms < 200) return "#22c55e"; // emerald-500 - excellent
  if (ms < 500) return "#f59e0b"; // amber-500   - good
  if (ms < 1000) return "#f97316"; // orange-500  - fair
  return "#ef4444"; // red-500     - poor
}

export function formatContext(n: number): string {
  if (n >= 1000000) return `${(n / 1000000).toFixed(0)}M`;
  if (n >= 1000) return `${(n / 1000).toFixed(0)}K`;
  return `${n}`;
}

/**
 * Returns null when the price is unknown, so each surface can pick its own wording —
 * the modal sits under a "Pricing" heading and reads better as a bare "N/A".
 */
export function formatPrice(
  input?: number,
  output?: number,
  currency?: string
): string | null {
  if (input == null || output == null) return null;
  if (currency === "CNY" || !currency) {
    const inUsd = input / CNY_TO_USD;
    const outUsd = output / CNY_TO_USD;
    return `$${inUsd.toFixed(2)} / $${outUsd.toFixed(2)} per 1M tokens (¥${input.toFixed(2)} / ¥${output.toFixed(2)})`;
  }
  return `$${input.toFixed(2)} / $${output.toFixed(2)} per 1M tokens`;
}
