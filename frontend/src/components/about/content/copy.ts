/**
 * The risk warning and the daily routine, transcribed from the NiceGUI About
 * page by parsing its source.
 *
 * The risk warning is a legal statement and its wording is not ours to
 * improve. The routine is the three things the owner checks each morning; it
 * lived in `getting_started.py` and was imported by the About home so the two
 * could not drift, and keeping it in one data module preserves that.
 */

export const RISK_WARNING: string[] = [
  "Trading leveraged financial instruments such as gold (XAUUSD) carries a high level of risk and may not be suitable for all investors. A significant proportion of retail trader accounts lose money when trading CFDs and similar products. You should not risk capital you cannot afford to lose.",
  "This software is provided as-is, without warranty of any kind. It may contain bugs or produce incorrect results. Automated execution does not guarantee profitability and can result in losses that exceed your initial deposit. You are solely responsible for monitoring all open positions and for any trading decisions made, whether manually or via automation. Do not leave automated trading running unattended for extended periods without reviewing open positions.",
  "Past performance is not indicative of future results. This tool does not constitute financial advice.",
];

export const DAILY_ROUTINE: string[] = [
  "Check the header: MT5 Connected (green) and no pause/news badge.",
  "Review pending signals and open positions on the Trading tab.",
  "Glance at Analysis for how today's closed trades went.",
];
