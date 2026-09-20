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
  /**
   * Equity minus net deposits: the account's whole life, including open
   * trades, swap and commission. Null when the deposit history cannot be
   * read -- showing the whole equity as profit is the most flattering
   * possible wrong answer, so the header shows nothing instead.
   */
  lifetime_pnl: number | null;
  active_trader: string | null;
  /**
   * Both halts at once, not just the risk governor's. The circuit breaker
   * writes a different key, so reading only the governor left a tripped
   * breaker invisible on every screen but Settings > Diagnostics.
   */
  pause: {
    paused: boolean;
    reason: string;
    /** Unix seconds, or null when the halt has no stored expiry. */
    until: number | null;
    /** "governor" | "circuit-breaker" | "both" | "" */
    source: string;
  };
  remote_connected: boolean;
  ea_badge: EaBadge | null;
  /**
   * Whether a newer build is waiting on GitHub, for the header's badge.
   *
   * Two facts only, and from the backend's CACHED check -- this payload is
   * polled every five seconds and a live check runs `git fetch`. What the
   * update actually contains is fetched once, when the popup opens.
   */
  update?: { available: boolean; commits: number; remote_sha: string } | null;
}

/** One commit in a pending update, as `/api/node/update` reports it. */
export interface UpdateCommit {
  sha: string;
  short_sha: string;
  summary: string;
}

/** The answer to "is there a newer build, and what is in it". */
export interface UpdateCheck {
  available: boolean;
  local_sha?: string;
  remote_sha?: string;
  commits?: UpdateCommit[];
  /**
   * True when this install has no `.git` at all, or could not be matched to a
   * commit. Not a failure: it is the normal state of an install that has
   * never updated, and the button next to it is what fixes it -- but it
   * force-checks-out, so it is never called "Update" without qualification.
   */
  bootstrap?: boolean;
  error?: string | null;
}

export interface UpdateStatus {
  current: string;
  /** Which repository and branch the check compared against, read from git. */
  tracking?: { repo_url: string; branch: string };
  update: UpdateCheck;
  /** Plain-English lines from the AI provider. Empty is normal and fine. */
  changes: string[];
  changes_error: string;
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
  /**
   * Which node a control reaches. The live-execution gates on this tab write
   * this node's own row and do NOT travel between nodes, so the tab says so
   * when the other machine is the one trading.
   */
  control_target?: string;
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

/* ── Set & Forget ─────────────────────────────────────────────────────────── */

/** A supply or demand band. `touches` is how many swing points merged into it. */
export interface Aoi {
  kind: "demand" | "supply";
  low: number;
  high: number;
  ts: number;
  touches: number;
}

export interface ConfluenceItem {
  id: string;
  label: string;
  weight: number;
  passed: boolean;
  /** Why it passed or failed, in words. The number is not the product. */
  detail: string;
}

export interface Confluence {
  items: ConfluenceItem[];
  score: number;
  max: number;
  pct: number;
  grade: "high" | "moderate" | "low";
}

export interface SetForgetCandidate {
  direction: "BUY" | "SELL";
  entry: number;
  stop_loss: number;
  take_profit: number;
  /** "stop" is a refusal, not an order type this section will place. */
  order_type: "market" | "limit" | "stop";
  risk: number;
  reward: number;
  rr: number | null;
  zone?: Aoi | null;
  target_zone?: Aoi | null;
}

/** One drawn retracement level. Priced by the backend, never re-derived here. */
export interface FibLevel {
  ratio: number;
  price: number;
}

export interface SetForgetEvidence {
  price: number | null;
  weekly_bias: string;
  daily_bias: string;
  entry_bias: string;
  entry_timeframe: string;
  zones: Aoi[];
  atr: number;
  ema_fast: number | null;
  ema_slow: number | null;
  rsi: number | null;
  fib: number | null;
  /** Empty when no leg has completed. Never a list of nulls. */
  fib_levels: FibLevel[];
  impulse: { start: number; end: number } | null;
  confirmation: { kind: string; direction: string } | null;
}

export interface SetForgetReview {
  verdict: "take" | "adjust" | "skip" | null;
  reasoning?: string;
  risks?: string;
  /** Rules the model's own levels broke. Non-empty means they were discarded. */
  levels_rejected?: string[];
  model?: string;
  error?: string | null;
}

export interface SetForgetState {
  generated_at: number;
  price: number | null;
  evidence: SetForgetEvidence;
  candidate: SetForgetCandidate | null;
  /** Which rule refused, when there is no candidate. */
  no_setup_reason: string;
  confluence: Confluence;
  invalidations: string[];
  ai: SetForgetReview | null;
  billed: boolean;
  /**
   * Cash for ONE lot. The browser multiplies; it does not re-derive P&L.
   * The figure comes from the backend's single `fees_sizing.pnl`.
   */
  risk_per_lot: number | null;
  reward_per_lot: number | null;
  suggested_lot: number | null;
  lot_size: number;
  risk_per_trade_pct: number;
  balance: number | null;
  min_rr: number;
  preferred_rr: number;
  strategy: string;
  source_name: string;
  control_target: string;
  ai_configured: boolean;
  ai_provider: string;
  ai_model: string;
}
