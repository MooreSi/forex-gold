import { useEffect, useRef } from "react";

/**
 * TradingView's own chart, embedded with their free Advanced Chart widget.
 *
 * Asked for on 2026-09-28: every instrument TradingView lists, history back a
 * week or more, and drawing tools for planning. The broker chart has none of
 * those -- one symbol, a 1000-candle ceiling on the API, and lightweight-charts
 * has no drawing primitives -- so this sits beside it rather than replacing it.
 *
 * What it is NOT: the broker's prices. The feed is TradingView's (OANDA's gold
 * to start with), so a level read here can sit a few tenths away from where
 * MT5 fills. It also knows nothing of our open trades or fair-value gaps.
 *
 * The widget is a script that reads its settings from its own body and
 * replaces itself with an iframe. It is built once per page and kept alive;
 * see "One widget for the whole page" below.
 */
export const TRADINGVIEW_SCRIPT =
  "https://s3.tradingview.com/external-embedding/embed-widget-advanced-chart.js";

/**
 * The same chart on tradingview.com, in a new window. The embedded widget
 * cannot keep drawings (no setting for it, and its iframe is not ours to
 * read); tradingview.com keeps them for a logged-in account.
 */
export const TRADINGVIEW_FULL_CHART =
  "https://www.tradingview.com/chart/?symbol=OANDA%3AXAUUSD";

export function tradingViewConfig(theme: "light" | "dark") {
  return {
    autosize: true,
    symbol: "OANDA:XAUUSD",
    interval: "5",
    // UTC. Not the broker chart's clock: that axis shows MT5 server time,
    // which ran three hours ahead of UTC on 2026-09-28.
    timezone: "Etc/UTC",
    theme,
    style: "1",
    locale: "en",
    allow_symbol_change: true,
    // The drawing tools are the side toolbar.
    hide_side_toolbar: false,
    // The 1D / 5D / 1M ... buttons under the chart: the quickest way back a week.
    withdateranges: true,
    save_image: true,
    // No instrument side panel: it took ~40% of the width on 2026-09-28, and
    // the width is what drawing needs.
    details: false,
    support_host: "https://www.tradingview.com",
  };
}

/**
 * The theme the page is actually showing. `auto` is resolved the way
 * index.css resolves it: dark unless the machine prefers light.
 */
function currentTheme(): "light" | "dark" {
  const chosen = document.documentElement.getAttribute("data-theme");
  if (chosen === "light" || chosen === "dark") return chosen;
  try {
    return matchMedia("(prefers-color-scheme: light)").matches ? "light" : "dark";
  } catch {
    return "dark";
  }
}

// ── One widget for the whole page ──────────────────────────────────────────
// Asked for on 2026-09-28: drawings made on this chart must survive leaving
// the page. They cannot be saved: the widget sends the page one message
// (openChartInPopup) and exposes no chart state, and tradingview.com refuses
// to be framed (frame-ancestors 'none'). They live inside the widget, so the
// widget is never destroyed: built once, on first use, in a host at the end
// of <body>, and laid over the chart slot whenever the slot is on screen.
// Moving an iframe in the DOM reloads it, which is why the host is
// positioned over the slot rather than moved into it.
//
// A page reload still starts a fresh widget. The theme is fixed when it is
// built: re-theming means rebuilding, and rebuilding loses the drawings.

/** Under DialogShell (z-50) and Tooltip (z-80): an order dialog opened from
 *  the Chart tab must never sit behind the chart. */
const HOST_Z = 40;

let host: HTMLDivElement | null = null;

function ensureHost(): HTMLDivElement {
  if (host && host.isConnected) return host;
  host = document.createElement("div");
  host.dataset.testid = "tradingview-host";
  host.className = "tradingview-widget-container";
  Object.assign(host.style, {
    position: "fixed", left: "0px", top: "0px", width: "0px", height: "0px",
    zIndex: String(HOST_Z), visibility: "hidden", pointerEvents: "none",
  });
  const widget = document.createElement("div");
  widget.className = "tradingview-widget-container__widget";
  widget.style.height = "100%";
  widget.style.width = "100%";
  const script = document.createElement("script");
  script.type = "text/javascript";
  script.src = TRADINGVIEW_SCRIPT;
  script.async = true;
  script.innerHTML = JSON.stringify(tradingViewConfig(currentTheme()));
  host.append(widget, script);
  document.body.append(host);
  return host;
}

/** Tests only: forget the page's widget, as a page reload would. */
export function resetTradingViewHost(): void {
  host?.remove();
  host = null;
}

/** Keep the host exactly over `slot` until the returned function is called,
 *  which hides it again. */
function showOver(slot: HTMLElement): () => void {
  const h = ensureHost();
  const place = () => {
    const r = slot.getBoundingClientRect();
    Object.assign(h.style, {
      left: `${r.left}px`, top: `${r.top}px`, width: `${r.width}px`, height: `${r.height}px`,
    });
  };
  place();
  h.style.visibility = "visible";
  h.style.pointerEvents = "auto";
  // The slot moves without resizing when the page scrolls, and resizes when
  // the chart/positions divider is dragged or the window changes.
  const ro = typeof ResizeObserver === "function" ? new ResizeObserver(place) : null;
  ro?.observe(slot);
  window.addEventListener("resize", place);
  window.addEventListener("scroll", place, true);
  return () => {
    ro?.disconnect();
    window.removeEventListener("resize", place);
    window.removeEventListener("scroll", place, true);
    h.style.visibility = "hidden";
    h.style.pointerEvents = "none";
  };
}

/** The slot the page-wide widget is laid over. */
export function TradingViewChart() {
  const slot = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!slot.current) return;
    return showOver(slot.current);
  }, []);

  return <div ref={slot} data-testid="tradingview-chart" className="h-full w-full" />;
}
