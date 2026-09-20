/**
 * The ONLY place money, percentages and timestamps are formatted.
 *
 * Prices formatted three different ways across screens is how people stop
 * trusting the numbers, and this codebase has already paid for a home-rolled
 * timestamp once: MT5 stamps are UTC+3 encoded as an epoch, and `_uk()` in the
 * NiceGUI `pages/trading/_shared.py` existed precisely so nobody re-derived
 * that. `formatBrokerTime` is its port. Do not re-derive it here either.
 */

/** MT5 encodes its server time (UTC+3) as if it were a UTC epoch. */
const MT5_UTC_OFFSET_SECONDS = 3 * 60 * 60;

/** Two decimals and thousands separators.
 *
 *  The grouping is not decoration. `HeaderStats` grew its own money formatter
 *  because this one produced "$1210.40" while the header showed "$4,378.31"
 *  two inches away -- which is the exact failure the docstring above warns
 *  about, committed inside the module that warns about it. There is one
 *  implementation again. */
function grouped(value: number): string {
  return Math.abs(value).toLocaleString("en-GB", {
    minimumFractionDigits: 2, maximumFractionDigits: 2,
  });
}

export function formatMoney(value: number | null | undefined, currency = "$"): string {
  if (value == null || !Number.isFinite(value)) return "—";
  const sign = value < 0 ? "-" : "";
  return `${sign}${currency}${grouped(value)}`;
}

/** A P&L number carries its own sign, always, so +12.00 cannot read as 12.00. */
export function formatSignedMoney(value: number | null | undefined, currency = "$"): string {
  if (value == null || !Number.isFinite(value)) return "—";
  const sign = value < 0 ? "-" : "+";
  return `${sign}${currency}${grouped(value)}`;
}

export function formatPrice(value: number | null | undefined, dp = 2): string {
  if (value == null || !Number.isFinite(value)) return "—";
  return value.toFixed(dp);
}

export function formatLots(value: number | null | undefined): string {
  if (value == null || !Number.isFinite(value)) return "—";
  return value.toFixed(2);
}

export function formatPercent(value: number | null | undefined, dp = 1): string {
  if (value == null || !Number.isFinite(value)) return "—";
  return `${value.toFixed(dp)}%`;
}

/**
 * An MT5 broker timestamp, rendered in UK local time.
 *
 * The stamp arrives as an epoch that is really UTC+3, so it is shifted back
 * before it is read as a real instant. Formatting it directly puts every
 * trade three hours into the future, which looks plausible and is wrong.
 */
export function formatBrokerTime(epochSeconds: number | null | undefined): string {
  if (epochSeconds == null || !Number.isFinite(epochSeconds)) return "—";
  const asRealInstant = (epochSeconds - MT5_UTC_OFFSET_SECONDS) * 1000;
  return new Intl.DateTimeFormat("en-GB", {
    day: "2-digit",
    month: "short",
    hour: "2-digit",
    minute: "2-digit",
    timeZone: "Europe/London",
  }).format(new Date(asRealInstant));
}

/**
 * A real UTC instant, rendered in UTC.
 *
 * For timestamps that are genuinely UTC — the economic calendar, Telegram
 * message stamps — as opposed to MT5's broker stamps, which are UTC+3 encoded
 * as an epoch and need `formatBrokerTime`.
 *
 * The news banner used `formatBrokerTime(ts + 3h)`: the added three hours
 * cancel that helper's shift, which leaves Europe/London local time. On a page
 * headed "times in UTC" that is an hour out for eight months of the year, and
 * plausible enough that nobody notices.
 */
export function formatUtcTime(epochSeconds: number | null | undefined): string {
  if (epochSeconds == null || !Number.isFinite(epochSeconds)) return "—";
  return new Intl.DateTimeFormat("en-GB", {
    day: "2-digit",
    month: "short",
    hour: "2-digit",
    minute: "2-digit",
    timeZone: "UTC",
  }).format(new Date(epochSeconds * 1000));
}

/** Clock time for a chart axis or a "last updated" line. */
export function formatClock(epochSeconds: number | null | undefined): string {
  if (epochSeconds == null || !Number.isFinite(epochSeconds)) return "—";
  return new Intl.DateTimeFormat("en-GB", {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    timeZone: "Europe/London",
  }).format(new Date(epochSeconds * 1000));
}

/** The class that colours a P&L number. Green profit, red loss, gray flat. */
export function pnlColour(value: number | null | undefined): string {
  if (value == null || !Number.isFinite(value) || value === 0) return "text-ink-2";
  return value > 0 ? "text-profit" : "text-loss";
}
