/** Shapes the API returns. Mirrors backend/src/api/schemas/. */

export interface ApiErrorBody {
  error: { kind: string; message: string; ref: string | null };
}

export interface Candle {
  ts: number;
  open: number;
  high: number;
  low: number;
  close: number;
}

export interface Tick {
  bid: number;
  ask: number;
  mid: number;
  spread: number;
  spread_points: number;
  timestamp: number;
  source: string;
}

export interface FvgZone {
  ts: number;
  top: number;
  bottom: number;
  direction: string;
}

export interface Overlays {
  timeframe: string;
  count: number;
  emas: Record<string, (number | null)[]>;
  rsi: (number | null)[];
  fvgs: FvgZone[];
}

export interface Trade {
  id?: number | string;
  direction?: string;
  entry?: number;
  lots?: number;
  sl?: number;
  tp?: number;
  [key: string]: unknown;
}

export interface EaBadge {
  colour: string;
  text: string;
  tooltip: string;
  stale: boolean;
  scope: string;
}

export interface HeaderState {
  account: Record<string, unknown> | null;
  bridge: Record<string, unknown> | null;
  tick: Tick | null;
  active_trader: string | null;
  halt_reason: string;
  remote_connected: boolean;
  ea_badge: EaBadge | null;
}

export interface HaltState {
  reason: string;
  market_closed: boolean;
  circuit_breaker: Record<string, unknown>;
}

export interface SessionState {
  authenticated: boolean;
  needs_setup: boolean;
  auto_login: boolean;
  debug: boolean;
}

export interface MarketOrderRequest {
  direction: "BUY" | "SELL";
  stop_loss?: number | null;
  lot_size?: number | null;
  strategy?: string | null;
  take_profit?: number | null;
  source_name?: string;
}
