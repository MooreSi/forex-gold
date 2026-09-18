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

export interface NewsEvent {
  title: string;
  currency: string;
  impact: string;
  ts: number;
  forecast: string;
  previous: string;
  score: number;
}

export interface CurrentEvent extends NewsEvent {
  mins_remaining: number | null;
  mins_to_event: number | null;
}

export interface BlackoutSettings {
  enabled: boolean;
  impact: string;
  minutes_before: number;
  minutes_after: number;
}

export interface NewsState {
  events: NewsEvent[];
  current: CurrentEvent | null;
  blackout: BlackoutSettings;
  pause: Record<string, unknown>;
}

export interface BacktestOptions {
  strategies: Record<string, unknown>[];
  templates: Record<string, unknown>[];
  timeframes: string[];
  granularities: string[];
  min_trades_per_side: number;
  broker_tz_offset: number;
}

export interface StrategyResult {
  strategy: string;
  trades: number;
  wins: number;
  losses: number;
  win_rate: number;
  total_pnl: number;
  total_commission: number;
  avg_win: number;
  avg_loss: number;
  profit_factor: number;
  max_drawdown_pct: number;
  sharpe: number;
  final_balance: number;
  equity_curve: number[];
  unsupported_reason: string;
  [key: string]: unknown;
}

export interface BacktestResult {
  results: StrategyResult[];
  filtered: Record<string, unknown>;
  signals_loaded: number;
  candles_loaded: number;
  granularity: string;
  note: string | null;
}

export interface ParsingChannel {
  name: string;
  parser: Record<string, unknown>;
}

export interface ParsingState {
  reader: Record<string, unknown>;
  configured: boolean;
  settings: Record<string, unknown>;
  lexicons: Record<string, string[]>;
  lexicon_labels: Record<string, string>;
  lexicon_help: Record<string, string>;
  channels: ParsingChannel[];
}

export interface HourlyCell {
  weekday: number;
  hour: number;
  session: string;
  pnl: number;
  n: number;
  avg: number;
}

export interface HistoryState {
  days: number;
  performance: Record<string, unknown>;
  hourly: HourlyCell[];
  channels: Record<string, unknown>[];
  ladder: Record<string, Record<string, unknown>>;
}
