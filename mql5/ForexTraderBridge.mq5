//+------------------------------------------------------------------+
//|                                          ForexTraderBridge.mq5   |
//| Companion EA for the FOREX Trader Python app.                    |
//|                                                                    |
//| Python remains the "brain" — it parses Telegram signals, runs ML  |
//| scoring, and decides direction/entry/SL/TP. This EA is the        |
//| "hands": it receives a fully-decided trade over a local TCP       |
//| socket, places the order with real broker-side SL, and then       |
//| manages the strategy's SL-trail / partial-close ladder tick-by-   |
//| tick inside MT5's own OnTick — no polling, no asyncio event loop,  |
//| no IPC hop on the hot path.                                       |
//|                                                                    |
//| Only 127.0.0.1 is ever used — the Python app and this EA always    |
//| run on the same machine (Mac's Wine MT5 talks to the Mac's own     |
//| Python; the VPS's native MT5 talks to the VPS's own Python), so    |
//| the exact same compiled .ex5 works unmodified on either side.      |
//|                                                                    |
//| DPM is NOT handled here — it continuously recomputes its           |
//| parameters from live ATR/session/momentum and a calibration        |
//| history the Python app holds in its own SQLite DB, which has no    |
//| MT5-native equivalent. DPM trades are never handed to this EA.     |
//|                                                                    |
//| One-time setup required in this terminal (Tools > Options >        |
//| Expert Advisors): tick "Allow WebRequest/Socket for listed         |
//| addresses" and add 127.0.0.1 to the list, or SocketConnect below   |
//| will always fail.                                                  |
//+------------------------------------------------------------------+
#property copyright "FOREX Trader"
#property version   "1.00"
#property strict

#include <Trade\Trade.mqh>

input string InpHost      = "127.0.0.1";
input int    InpPort      = 9111;  // this checkout's isolated EA-bridge port -- see forex_trader/config.py's ea_bridge_port
input ulong  InpMagic     = 20260706;
input double InpDefaultTrailStopPts = 5.0;   // used by trail_stop if the open_trade message omits trail_dist
input double InpConservativeTrailPts = 3.0;  // used by conservative/scalp_runner if omitted

CTrade trade;
int    g_socket = INVALID_HANDLE;
bool   g_connected = false;
string g_recvBuffer = "";
datetime g_lastPingSent = 0;
datetime g_lastRecv = 0;
// TEMPORARY DIAGNOSTIC (remove once the scalp_runner "silently orphaned
// trade" investigation is closed) — last time the tracked-trades heartbeat
// below was printed, and the count it saw, so a full g_trades wipe (e.g. an
// EA reload) shows up as a count drop with no matching CheckForClosures line.
datetime g_lastDiagHeartbeat = 0;
int      g_lastDiagCount = -1;

// ── Global Parameters > Harvest (2026-07-24) ─────────────────────────────
// Trading > Global Parameters, standing config pushed by
// ea_bridge.EABridge.push_global_config() -- once per connection ("hello")
// and again whenever the setting is saved. Unlike the old per-template
// tpl_harvest_enabled/tpl_harvest_threshold (a field on ManagedTrade/
// PendingOrder, sent once at open_trade time, only ever checked against
// that one EA-managed trade), this is checked in CheckGlobalHarvest()
// against EVERY open position on the symbol each tick -- including
// Python-only-managed trades this EA has no ManagedTrade/PendingOrder
// entry for at all -- so it applies account-wide regardless of how or
// when a position was opened.
bool     g_globalHarvestEnabled = false;
double   g_globalHarvestThresholdUsd = 50.0;

#define MAX_TPS 8

struct ManagedTrade
{
   ulong    ticket;
   string   trade_id;
   string   strategy;
   string   direction;      // "BUY" / "SELL"
   double   entry_price;
   double   orig_lots;
   double   tp[MAX_TPS];
   bool     hasTp[MAX_TPS];
   bool     triggered[MAX_TPS];
   double   trail_dist;     // for trail_stop / conservative / scalp_runner
   bool     trailing_active; // trail_stop: has TP1 activation happened
   int      last_step;      // be_runner: highest sl_ladder step already applied
   double   pcts[MAX_TPS];  // ladder strategies: close-% per TP, sent by Python
                             // at open time (pct1..pct8) — single source of
                             // truth lives in Python's _CLIMBER_PCTS/_GDVR_PCTS,
                             // not duplicated here (see ManageLadder).
   int      beAtPos;        // ladder strategies: compacted TP position where
                             // SL first moves to breakeven; -1 = not sent
                             // (falls back to the hardcoded table below).
   string   trailMode;      // ladder strategies: SL rule for every TP after
                             // beAtPos. "" (default, every strategy before
                             // adaptive_runner_2) = trail to the single
                             // immediately-previous TP price. "midpoint_lag2"
                             // (adaptive_runner_2) = trail to the midpoint of
                             // the two TPs before the one just hit. See
                             // ManageLadder().
   bool     closeFullOnLast; // ladder strategies: true (default, every
                             // strategy before limit_runner) = the last
                             // defined TP always closes 100% of whatever
                             // remains, regardless of its own pcts[] entry.
                             // false (limit_runner only, when the signal had
                             // a literal "TP OPEN" line) = the last defined
                             // TP only closes its own pcts[] share; whatever
                             // remains keeps riding on the trailing SL with
                             // no further TP to close it — see
                             // core_run_tp_ladder.run_tp_ladder's
                             // close_full_on_last parameter, which this must
                             // stay in lockstep with.

   // ── EA Template fields (strategy == "template:<name>") ──────────────
   // Sent flat as tpl_* on open_trade -- see core_ea_templates.py and
   // ea_bridge.EABridge.open_trade's docstring. Only meaningful when
   // isTemplate is true; every other strategy leaves these at their
   // zero-value defaults and ManageTrade() never reads them.
   bool     isTemplate;
   string   tplTpslMode;     // "off" / "on" / "stealth"
   string   tplTrailMode;    // "off" / "candle" / "step" / "fractal" / "tp"
   string   tplBeMode;       // "entry" / "entry_buffer"
   double   tplBeBufferPts;
   int      tplBeTrigger;    // 1-based TP# that arms breakeven
   bool     tplBeDone;       // breakeven already applied once
   bool     tplCancelPending;
   int      tplGridGroup;    // shared id for sibling grid legs, -1 if none
   bool     tplHarvestEnabled;
   double   tplHarvestThreshold;
};

ManagedTrade g_trades[];

// A resting BuyLimit/SellLimit order placed for the Limit Runner strategy
// (STRATEGY_LIMIT_RUNNER / "limit_runner") — the only strategy that places
// a genuine pending order instead of an immediate market fill. Tracked
// separately from g_trades[] (which only ever holds OPEN positions) until
// CheckPendingOrders() (called each OnTick, alongside CheckForClosures())
// observes it fill, expire, or get cancelled. Carries the same ladder
// fields ManagedTrade does so a fill can be promoted straight into
// g_trades[] with zero data loss.
struct PendingOrder
{
   ulong    ticket;
   string   trade_id;
   string   strategy;
   string   direction;      // "BUY" / "SELL"
   double   lots;
   double   tp[MAX_TPS];
   bool     hasTp[MAX_TPS];
   double   pcts[MAX_TPS];
   int      beAtPos;
   string   trailMode;
   bool     closeFullOnLast;

   // ── EA Template fields — see ManagedTrade's own copies for meaning.
   // Carried on the resting order so CheckPendingOrders() can populate a
   // complete ManagedTrade the moment a leg fills.
   bool     isTemplate;
   string   tplTpslMode;
   string   tplTrailMode;
   string   tplBeMode;
   double   tplBeBufferPts;
   int      tplBeTrigger;
   bool     tplCancelPending;
   int      tplGridGroup;    // shared id across this grid's sibling legs
   bool     tplHarvestEnabled;
   double   tplHarvestThreshold;
};

PendingOrder g_pending[];

//+------------------------------------------------------------------+
//| Minimal JSON helpers — the wire protocol is a fixed, flat set of  |
//| known keys (no nesting), so a general JSON library is unnecessary.|
//+------------------------------------------------------------------+
string JsonGetString(const string &json, const string key)
{
   string pattern = "\"" + key + "\"";
   int p = StringFind(json, pattern);
   if(p < 0) return "";
   p = StringFind(json, ":", p);
   if(p < 0) return "";
   p++;
   while(p < StringLen(json) && (StringGetCharacter(json, p) == ' ')) p++;
   if(p >= StringLen(json)) return "";
   if(StringGetCharacter(json, p) == '"')
   {
      int start = p + 1;
      int end = StringFind(json, "\"", start);
      if(end < 0) return "";
      return StringSubstr(json, start, end - start);
   }
   // unquoted (number/bool/null) — read until , or }
   int end2 = StringFind(json, ",", p);
   int end3 = StringFind(json, "}", p);
   int end = (end2 >= 0 && (end3 < 0 || end2 < end3)) ? end2 : end3;
   if(end < 0) end = StringLen(json);
   return StringSubstr(json, p, end - p);
}

// Overload with a default -- every existing call site before EA Templates
// only ever needed the bare 2-arg form (an empty string was itself a valid
// "not present" signal to those callers), so this is purely additive.
string JsonGetString(const string &json, const string key, const string def)
{
   string s = JsonGetString(json, key);
   return (s == "") ? def : s;
}

double JsonGetDouble(const string &json, const string key, double def = 0.0)
{
   string s = JsonGetString(json, key);
   if(s == "" || s == "null") return def;
   return StringToDouble(s);
}

long JsonGetLong(const string &json, const string key, long def = 0)
{
   string s = JsonGetString(json, key);
   if(s == "" || s == "null") return def;
   return StringToInteger(s);
}

bool JsonHasKey(const string &json, const string key)
{
   string s = JsonGetString(json, key);
   return s != "" && s != "null";
}

string JsonEsc(const string s)
{
   string r = s;
   StringReplace(r, "\\", "\\\\");
   StringReplace(r, "\"", "\\\"");
   return r;
}

//+------------------------------------------------------------------+
//| Socket handling                                                   |
//+------------------------------------------------------------------+
void EnsureConnected()
{
   if(g_connected) return;
   if(g_socket != INVALID_HANDLE) { SocketClose(g_socket); g_socket = INVALID_HANDLE; }
   g_socket = SocketCreate();
   if(g_socket == INVALID_HANDLE) return;
   if(!SocketConnect(g_socket, InpHost, InpPort, 2000))
   {
      SocketClose(g_socket);
      g_socket = INVALID_HANDLE;
      return;
   }
   g_connected = true;
   g_lastRecv = TimeCurrent();
   SendJson("{\"type\":\"hello\",\"account\":" + (string)AccountInfoInteger(ACCOUNT_LOGIN) +
            ",\"symbol\":\"" + _Symbol + "\"}");
   Print("[EABridge] connected to ", InpHost, ":", InpPort);
}

void SendJson(const string msg)
{
   if(g_socket == INVALID_HANDLE) return;
   string line = msg + "\n";
   uchar bytes[];
   int len = StringToCharArray(line, bytes, 0, StringLen(line));
   int sent = SocketSend(g_socket, bytes, len);
   if(sent < 0)
   {
      g_connected = false;
      SocketClose(g_socket);
      g_socket = INVALID_HANDLE;
   }
}

void PollSocket()
{
   if(g_socket == INVALID_HANDLE) { EnsureConnected(); return; }
   uint avail = SocketIsReadable(g_socket);
   if(avail > 0)
   {
      uchar buf[];
      int got = SocketRead(g_socket, buf, avail, 100);
      if(got > 0)
      {
         g_recvBuffer += CharArrayToString(buf, 0, got);
         g_lastRecv = TimeCurrent();
         int nl;
         while((nl = StringFind(g_recvBuffer, "\n")) >= 0)
         {
            string line = StringSubstr(g_recvBuffer, 0, nl);
            g_recvBuffer = StringSubstr(g_recvBuffer, nl + 1);
            if(StringLen(line) > 0) HandleMessage(line);
         }
      }
      else if(got < 0)
      {
         g_connected = false;
         SocketClose(g_socket);
         g_socket = INVALID_HANDLE;
      }
   }
   // Heartbeat every 2s; if nothing received for 10s, force a reconnect —
   // Python's own fallback watchdog uses a similar timeout on its side, so a
   // dead socket on either end reclaims management within a few seconds.
   if(TimeCurrent() - g_lastPingSent >= 2)
   {
      SendJson("{\"type\":\"ping\"}");
      g_lastPingSent = TimeCurrent();
   }
   if(g_connected && TimeCurrent() - g_lastRecv > 10)
   {
      Print("[EABridge] no data from Python in 10s — reconnecting");
      g_connected = false;
      SocketClose(g_socket);
      g_socket = INVALID_HANDLE;
   }
}

//+------------------------------------------------------------------+
//| Managed-trade array helpers                                       |
//+------------------------------------------------------------------+
int FindManagedByTradeId(const string trade_id)
{
   for(int i = 0; i < ArraySize(g_trades); i++)
      if(g_trades[i].trade_id == trade_id) return i;
   return -1;
}

int FindManagedByTicket(const ulong ticket)
{
   for(int i = 0; i < ArraySize(g_trades); i++)
      if(g_trades[i].ticket == ticket) return i;
   return -1;
}

void RemoveManaged(const int idx)
{
   int n = ArraySize(g_trades);
   for(int i = idx; i < n - 1; i++) g_trades[i] = g_trades[i + 1];
   ArrayResize(g_trades, n - 1);
}

int TpCount(const ManagedTrade &t)
{
   int n = 0;
   for(int i = 0; i < MAX_TPS; i++) if(t.hasTp[i]) n++;
   return n;
}

int LastTpIndex(const ManagedTrade &t)
{
   int last = -1;
   for(int i = 0; i < MAX_TPS; i++) if(t.hasTp[i]) last = i;
   return last;
}

//+------------------------------------------------------------------+
//| Inbound message handling                                          |
//+------------------------------------------------------------------+
void HandleMessage(const string line)
{
   string type = JsonGetString(line, "type");
   if(type == "pong") return;
   if(type == "ping") { SendJson("{\"type\":\"pong\"}"); return; }
   if(type == "open_trade") { HandleOpenTrade(line); return; }
   if(type == "update_trade") { HandleUpdateTrade(line); return; }
   if(type == "place_pending_order") { HandlePlacePendingOrder(line); return; }
   if(type == "restore_pending_order") { HandleRestorePendingOrder(line); return; }
   if(type == "set_global_config") { HandleSetGlobalConfig(line); return; }
}

// Trading > Global Parameters > Harvest -- see push_global_config() in
// ea_bridge.py and CheckGlobalHarvest() below. No ack sent (fire-and-forget,
// same as update_trade).
void HandleSetGlobalConfig(const string json)
{
   g_globalHarvestEnabled = JsonGetLong(json, "harvest_enabled", 0) != 0;
   g_globalHarvestThresholdUsd = JsonGetDouble(json, "harvest_threshold", 50.0);
   Print("[EABridge] global config updated: harvest_enabled=", g_globalHarvestEnabled,
         " harvest_threshold=", g_globalHarvestThresholdUsd);
}

// Corrects an already-tracked trade's TP levels — tp[]/hasTp[] are otherwise
// captured once at HandleOpenTrade and never refreshed, so an IME instant
// entry's provisional/missing TP1 stayed permanently stale once the real
// signal's TP arrived a moment later via a follow-up (Python's own DB had
// the corrected value; this EA's on-tick TpCleared() never saw it). Only
// updates untriggered levels are meaningful — leaves ticket/strategy/entry
// alone, and doesn't touch triggered[] so an already-hit TP can't refire.
void HandleUpdateTrade(const string json)
{
   string trade_id = JsonGetString(json, "trade_id");
   int idx = FindManagedByTradeId(trade_id);
   if(idx < 0)
   {
      Print("[EABridge] update_trade: unknown/untracked trade_id=", trade_id);
      return;
   }
   int updated = 0;
   for(int i = 0; i < MAX_TPS; i++)
   {
      string key = "tp" + (string)(i + 1);
      if(JsonHasKey(json, key))
      {
         g_trades[idx].tp[i]    = JsonGetDouble(json, key);
         g_trades[idx].hasTp[i] = true;
         updated++;
      }
   }
   Print("[EABridge] update_trade applied: trade_id=", trade_id,
         " ticket=", g_trades[idx].ticket, " fields_updated=", updated);
}

void HandleOpenTrade(const string json)
{
   string trade_id = JsonGetString(json, "trade_id");
   string direction = JsonGetString(json, "direction");
   double lots      = JsonGetDouble(json, "lot_size");
   double sl        = JsonGetDouble(json, "stop_loss");
   string strategy  = JsonGetString(json, "strategy");
   bool   isTemplate = (StringFind(strategy, "template:") == 0);

   // EA Template, Grid mode: places tpl_grid_legs resting BuyLimit/SellLimit
   // orders instead of a single market fill — a completely different
   // placement flow, handled by its own function so the market-order path
   // below stays exactly as every non-template strategy already relies on.
   if(isTemplate && JsonGetString(json, "tpl_mode") == "grid")
   {
      HandleOpenTemplateGrid(json);
      return;
   }

   // be_runner is the one strategy where Python's own bridge path sets a real
   // broker-side TP too (the highest defined TP, as a backstop in case the
   // ladder-management logic itself never catches the final level) — mirror
   // that here. Every other strategy manages TPs purely via partial closes,
   // so no broker TP is set for them (tp=0). EA Templates join be_runner
   // for tpsl_mode=="on" (full visible TP) but NOT for "off" (no target) or
   // "stealth" (target tracked internally in tp[], deliberately never
   // written to the broker order — see ManageTemplate()).
   double brokerTp = 0.0;
   string tplTpslModeEarly = isTemplate ? JsonGetString(json, "tpl_tpsl_mode", "on") : "";
   if(strategy == "be_runner" || (isTemplate && tplTpslModeEarly == "on"))
   {
      for(int i = MAX_TPS - 1; i >= 0; i--)
      {
         if(JsonHasKey(json, "tp" + (string)(i + 1)))
         {
            brokerTp = JsonGetDouble(json, "tp" + (string)(i + 1));
            break;
         }
      }
   }

   trade.SetExpertMagicNumber(InpMagic);
   bool ok;
   double price;
   MqlTick tick;
   SymbolInfoTick(_Symbol, tick);
   if(direction == "BUY")
   {
      ok = trade.Buy(lots, _Symbol, 0.0, sl, brokerTp, "ea:" + StringSubstr(trade_id, 0, 12));
      price = tick.ask;
   }
   else
   {
      ok = trade.Sell(lots, _Symbol, 0.0, sl, brokerTp, "ea:" + StringSubstr(trade_id, 0, 12));
      price = tick.bid;
   }

   if(!ok)
   {
      SendJson("{\"type\":\"trade_open_failed\",\"trade_id\":\"" + JsonEsc(trade_id) +
               "\",\"error\":\"" + JsonEsc(trade.ResultRetcodeDescription()) + "\"}");
      return;
   }

   ulong ticket = trade.ResultOrder();
   double fillPrice = trade.ResultPrice();
   if(fillPrice <= 0) fillPrice = price;

   ManagedTrade mt;
   mt.ticket = ticket;
   mt.trade_id = trade_id;
   mt.strategy = strategy;
   mt.direction = direction;
   mt.entry_price = fillPrice;
   mt.orig_lots = lots;
   mt.trailing_active = false;
   mt.last_step = 0;
   mt.trail_dist = JsonGetDouble(json, "trail_dist",
      (strategy == "trail_stop") ? InpDefaultTrailStopPts : InpConservativeTrailPts);
   for(int i = 0; i < MAX_TPS; i++)
   {
      string key = "tp" + (string)(i + 1);
      mt.hasTp[i] = JsonHasKey(json, key);
      mt.tp[i] = mt.hasTp[i] ? JsonGetDouble(json, key) : 0.0;
      mt.triggered[i] = false;
      mt.pcts[i] = JsonGetDouble(json, "pct" + (string)(i + 1), 0.0);
   }
   mt.beAtPos = JsonHasKey(json, "be_at_pos") ? (int)JsonGetLong(json, "be_at_pos") : -1;
   mt.trailMode = JsonHasKey(json, "trail_mode") ? JsonGetString(json, "trail_mode") : "";
   mt.closeFullOnLast = true; // every open_trade() caller wants the original behaviour

   mt.isTemplate = isTemplate;
   mt.tplTpslMode = isTemplate ? JsonGetString(json, "tpl_tpsl_mode", "on") : "";
   mt.tplTrailMode = isTemplate ? JsonGetString(json, "tpl_trail_mode", "off") : "off";
   mt.tplBeMode = isTemplate ? JsonGetString(json, "tpl_be_mode", "entry") : "entry";
   mt.tplBeBufferPts = isTemplate ? JsonGetDouble(json, "tpl_be_buffer_pts", 1.0) : 0.0;
   mt.tplBeTrigger = isTemplate ? (int)JsonGetLong(json, "tpl_be_trigger", 1) : 1;
   mt.tplBeDone = false;
   mt.tplCancelPending = isTemplate && JsonGetLong(json, "tpl_cancel_pending", 0) != 0;
   mt.tplGridGroup = -1; // single-mode template trades are never part of a grid group
   mt.tplHarvestEnabled = isTemplate && JsonGetLong(json, "tpl_harvest_enabled", 0) != 0;
   mt.tplHarvestThreshold = isTemplate ? JsonGetDouble(json, "tpl_harvest_threshold", 50.0) : 0.0;

   int n = ArraySize(g_trades);
   ArrayResize(g_trades, n + 1);
   g_trades[n] = mt;

   SendJson("{\"type\":\"trade_opened\",\"trade_id\":\"" + JsonEsc(trade_id) +
            "\",\"ticket\":" + (string)ticket +
            ",\"fill_price\":" + DoubleToString(fillPrice, _Digits) + "}");
   Print("[EABridge] opened ", direction, " ticket=", ticket, " strategy=", strategy,
         " @ ", fillPrice, " SL=", sl);
}

// Places a genuine resting BuyLimit/SellLimit order (Limit Runner) instead
// of an immediate market fill — does NOT add to g_trades[] (that only
// tracks OPEN positions); the order goes into g_pending[] instead, and
// CheckPendingOrders() promotes it to a managed trade once it actually
// fills. GTC-with-expiration (ORDER_TIME_SPECIFIED) so a signal whose zone
// never gets touched cleans itself up broker-side rather than resting
// forever — expire_minutes matches core_limit_order_signal.py's own 4h
// default (same window the Python-simulated zone-wait signals already use).
void HandlePlacePendingOrder(const string json)
{
   string trade_id  = JsonGetString(json, "trade_id");
   string direction = JsonGetString(json, "direction");
   double price     = JsonGetDouble(json, "price");
   double lots      = JsonGetDouble(json, "lot_size");
   double sl        = JsonGetDouble(json, "stop_loss");
   string strategy  = JsonGetString(json, "strategy");
   double expireMin = JsonGetDouble(json, "expire_minutes", 240.0);

   trade.SetExpertMagicNumber(InpMagic);
   datetime expiration = TimeCurrent() + (datetime)(expireMin * 60);
   string comment = "ea:" + StringSubstr(trade_id, 0, 12);
   bool ok;
   if(direction == "BUY")
      ok = trade.BuyLimit(lots, price, _Symbol, sl, 0.0, ORDER_TIME_SPECIFIED, expiration, comment);
   else
      ok = trade.SellLimit(lots, price, _Symbol, sl, 0.0, ORDER_TIME_SPECIFIED, expiration, comment);

   if(!ok)
   {
      SendJson("{\"type\":\"pending_order_open_failed\",\"trade_id\":\"" + JsonEsc(trade_id) +
               "\",\"error\":\"" + JsonEsc(trade.ResultRetcodeDescription()) + "\"}");
      return;
   }

   ulong ticket = trade.ResultOrder();

   PendingOrder p;
   p.ticket = ticket;
   p.trade_id = trade_id;
   p.strategy = strategy;
   p.direction = direction;
   p.lots = lots;
   for(int i = 0; i < MAX_TPS; i++)
   {
      string key = "tp" + (string)(i + 1);
      p.hasTp[i] = JsonHasKey(json, key);
      p.tp[i] = p.hasTp[i] ? JsonGetDouble(json, key) : 0.0;
      p.pcts[i] = JsonGetDouble(json, "pct" + (string)(i + 1), 0.0);
   }
   p.beAtPos = JsonHasKey(json, "be_at_pos") ? (int)JsonGetLong(json, "be_at_pos") : -1;
   p.trailMode = JsonHasKey(json, "trail_mode") ? JsonGetString(json, "trail_mode") : "";
   // Sent as an integer (0/1), not a native JSON bool -- this EA's minimal
   // JSON helpers only parse strings/numbers, matching every other flag in
   // this protocol (see be_at_pos above).
   p.closeFullOnLast = JsonHasKey(json, "close_full_on_last")
      ? (JsonGetLong(json, "close_full_on_last", 1) != 0) : true;
   // This is the Limit Runner / ORB pending-order path, never a template --
   // MQL5 zero-initialises struct members by default so this is already
   // implied (isTemplate=false, tplGridGroup=0), but set it explicitly so
   // ManageTrade()'s isTemplate dispatch check never depends on that.
   p.isTemplate = false;
   p.tplGridGroup = -1;

   int n = ArraySize(g_pending);
   ArrayResize(g_pending, n + 1);
   g_pending[n] = p;

   SendJson("{\"type\":\"pending_order_placed\",\"trade_id\":\"" + JsonEsc(trade_id) +
            "\",\"ticket\":" + (string)ticket + "}");
   Print("[EABridge] pending order placed ", direction, " ticket=", ticket,
         " strategy=", strategy, " price=", price, " SL=", sl, " expiresMin=", expireMin);
}

// Re-populates g_pending[] for a resting order this EA already knows about
// from a PREVIOUS connection -- g_pending[] is pure in-memory state with no
// persistence of its own, so any EA restart (recompile, terminal restart, a
// dropped socket that re-triggers OnInit) silently forgets every order that
// was still resting at the time, and CheckPendingOrders() then has nothing
// left to check: it can never again notice that order's eventual fill or
// broker-side expiry. Python is the durable source of truth for every field
// here (vantage_pending_orders) and now pushes one of these per still-
// "working" row the moment a fresh "hello" arrives (see ea_bridge.py's
// _dispatch), so a restart no longer orphans a resting Limit Runner/ORB
// order from EA-side tracking. Unlike HandlePlacePendingOrder, this never
// places a NEW broker order -- `ticket` already exists; the three outcomes
// are: still genuinely resting (re-added to g_pending[], picked up by the
// next CheckPendingOrders() cycle as normal), already filled while this EA
// was disconnected (promoted straight into g_trades[] so management resumes
// immediately instead of orphaning it a second time), or already gone with
// no resulting position (broker-side expiry or manual cancel while
// disconnected -- reported immediately instead of leaving Python waiting
// for a report that would otherwise never arrive).
void HandleRestorePendingOrder(const string json)
{
   ulong  ticket    = (ulong)JsonGetLong(json, "ticket");
   string trade_id  = JsonGetString(json, "trade_id");
   string direction = JsonGetString(json, "direction");
   string strategy  = JsonGetString(json, "strategy");
   double lots      = JsonGetDouble(json, "lot_size");

   if(OrderSelect(ticket))
   {
      // Still genuinely resting on the book -- rebuild the same PendingOrder
      // CheckPendingOrders() would have if this EA had never restarted.
      PendingOrder p;
      p.ticket = ticket;
      p.trade_id = trade_id;
      p.strategy = strategy;
      p.direction = direction;
      p.lots = lots;
      for(int i = 0; i < MAX_TPS; i++)
      {
         string key = "tp" + (string)(i + 1);
         p.hasTp[i] = JsonHasKey(json, key);
         p.tp[i] = p.hasTp[i] ? JsonGetDouble(json, key) : 0.0;
         p.pcts[i] = JsonGetDouble(json, "pct" + (string)(i + 1), 0.0);
      }
      p.beAtPos = JsonHasKey(json, "be_at_pos") ? (int)JsonGetLong(json, "be_at_pos") : -1;
      p.trailMode = JsonHasKey(json, "trail_mode") ? JsonGetString(json, "trail_mode") : "";
      p.closeFullOnLast = JsonHasKey(json, "close_full_on_last")
         ? (JsonGetLong(json, "close_full_on_last", 1) != 0) : true;
      p.isTemplate = false;
      p.tplGridGroup = -1;

      int n = ArraySize(g_pending);
      ArrayResize(g_pending, n + 1);
      g_pending[n] = p;

      Print("[EABridge] restored resting pending order ticket=", ticket,
            " trade_id=", trade_id, " strategy=", strategy);
      return;
   }

   if(PositionSelectByTicket(ticket))
   {
      // Filled while this EA was disconnected -- promote straight into
      // g_trades[] the same way CheckPendingOrders() does for a fill it
      // witnessed directly, so management resumes now instead of leaving
      // this trade permanently unmanaged on the EA side.
      double fillPrice = PositionGetDouble(POSITION_PRICE_OPEN);

      ManagedTrade mt;
      mt.ticket = ticket;
      mt.trade_id = trade_id;
      mt.strategy = strategy;
      mt.direction = direction;
      mt.entry_price = fillPrice;
      mt.orig_lots = lots;
      mt.trailing_active = false;
      mt.last_step = 0;
      mt.trail_dist = InpConservativeTrailPts;
      for(int j = 0; j < MAX_TPS; j++)
      {
         string key = "tp" + (string)(j + 1);
         mt.hasTp[j] = JsonHasKey(json, key);
         mt.tp[j] = mt.hasTp[j] ? JsonGetDouble(json, key) : 0.0;
         mt.triggered[j] = false;
         mt.pcts[j] = JsonGetDouble(json, "pct" + (string)(j + 1), 0.0);
      }
      mt.beAtPos = JsonHasKey(json, "be_at_pos") ? (int)JsonGetLong(json, "be_at_pos") : -1;
      mt.trailMode = JsonHasKey(json, "trail_mode") ? JsonGetString(json, "trail_mode") : "";
      mt.closeFullOnLast = JsonHasKey(json, "close_full_on_last")
         ? (JsonGetLong(json, "close_full_on_last", 1) != 0) : true;
      mt.isTemplate = false;
      mt.tplGridGroup = -1;
      mt.tplBeDone = false;
      mt.tplCancelPending = false;
      mt.tplHarvestEnabled = false;

      int n = ArraySize(g_trades);
      ArrayResize(g_trades, n + 1);
      g_trades[n] = mt;

      SendJson("{\"type\":\"pending_order_filled\",\"trade_id\":\"" + JsonEsc(trade_id) +
               "\",\"ticket\":" + (string)ticket +
               ",\"fill_price\":" + DoubleToString(fillPrice, _Digits) + "}");
      Print("[EABridge] restore found order already filled while disconnected -> managed ticket=",
            ticket, " strategy=", strategy, " @ ", fillPrice);
      return;
   }

   // Neither a resting order nor an open position -- gone (broker-side
   // expiry or manual cancel) while this EA was disconnected.
   SendJson("{\"type\":\"pending_order_cancelled\",\"trade_id\":\"" + JsonEsc(trade_id) +
            "\",\"reason\":\"expired_while_disconnected\"}");
   Print("[EABridge] restore found order already gone (expired/cancelled while disconnected) ticket=",
         ticket, " trade_id=", trade_id);
}

int g_nextGridGroup = 1;

// EA Template, Grid mode: places tpl_grid_legs resting BuyLimit/SellLimit
// orders staggered tpl_grid_step_pts apart, averaging INTO the position
// (BUY legs below current price, SELL legs above) rather than a single
// market fill. All legs share one tplGridGroup id -- when tplCancelPending
// is set and any leg fills (CheckPendingOrders), every other still-resting
// sibling leg is cancelled instead of also filling later and stacking
// exposure the template didn't intend.
void HandleOpenTemplateGrid(const string json)
{
   string trade_id  = JsonGetString(json, "trade_id");
   string direction = JsonGetString(json, "direction");
   double lots      = JsonGetDouble(json, "lot_size");
   double sl        = JsonGetDouble(json, "stop_loss");
   string strategy  = JsonGetString(json, "strategy");
   double stepPts   = JsonGetDouble(json, "tpl_grid_step_pts", 10.0);
   int    legs       = (int)JsonGetLong(json, "tpl_grid_legs", 3);
   if(legs < 1) legs = 1;
   // Same convention as every other *_pts field in this EA (trail_dist,
   // InpDefaultTrailStopPts, trail_stop_sl_pts) -- a raw price delta, not a
   // broker _Point-scaled value. See core_ea_templates.py's DEFAULTS.
   double stepPrice = stepPts;

   MqlTick tick;
   SymbolInfoTick(_Symbol, tick);
   double basePrice = (direction == "BUY") ? tick.bid : tick.ask;

   trade.SetExpertMagicNumber(InpMagic);
   int groupId = g_nextGridGroup++;
   int placed = 0;
   string expireMinKey = "tpl_grid_expire_minutes";
   double expireMin = JsonGetDouble(json, expireMinKey, 240.0);
   datetime expiration = TimeCurrent() + (datetime)(expireMin * 60);

   for(int leg = 1; leg <= legs; leg++)
   {
      double legPrice = (direction == "BUY")
         ? basePrice - stepPrice * leg
         : basePrice + stepPrice * leg;
      legPrice = NormalizeDouble(legPrice, _Digits);
      string legTradeId = trade_id + "-g" + (string)leg;
      string comment = "ea:" + StringSubstr(trade_id, 0, 10) + "g" + (string)leg;

      bool ok = (direction == "BUY")
         ? trade.BuyLimit(lots, legPrice, _Symbol, sl, 0.0, ORDER_TIME_SPECIFIED, expiration, comment)
         : trade.SellLimit(lots, legPrice, _Symbol, sl, 0.0, ORDER_TIME_SPECIFIED, expiration, comment);

      if(!ok)
      {
         Print("[EABridge] grid leg ", leg, "/", legs, " failed: ", trade.ResultRetcodeDescription());
         continue;
      }

      PendingOrder p;
      p.ticket = trade.ResultOrder();
      p.trade_id = legTradeId;
      p.strategy = strategy;
      p.direction = direction;
      p.lots = lots;
      for(int i = 0; i < MAX_TPS; i++)
      {
         string key = "tp" + (string)(i + 1);
         p.hasTp[i] = JsonHasKey(json, key);
         p.tp[i] = p.hasTp[i] ? JsonGetDouble(json, key) : 0.0;
         // Anchor TP (2026-07-24): was hardcoded to 0.0, silently discarding
         // whatever %-close ladder Python resolved (core_open_trade.py's
         // Anchor TP fallback) -- every grid leg only ever fully closed,
         // never partial-closed. Read the same generic pct{n} field every
         // other PendingOrder-building path already uses.
         p.pcts[i] = JsonGetDouble(json, "pct" + (string)(i + 1), 0.0);
      }
      p.beAtPos = -1;
      p.trailMode = "";
      p.closeFullOnLast = true;
      p.isTemplate = true;
      p.tplTpslMode = JsonGetString(json, "tpl_tpsl_mode", "on");
      p.tplTrailMode = JsonGetString(json, "tpl_trail_mode", "off");
      p.tplBeMode = JsonGetString(json, "tpl_be_mode", "entry");
      p.tplBeBufferPts = JsonGetDouble(json, "tpl_be_buffer_pts", 1.0);
      p.tplBeTrigger = (int)JsonGetLong(json, "tpl_be_trigger", 1);
      p.tplCancelPending = JsonGetLong(json, "tpl_cancel_pending", 0) != 0;
      p.tplGridGroup = groupId;
      p.tplHarvestEnabled = JsonGetLong(json, "tpl_harvest_enabled", 0) != 0;
      p.tplHarvestThreshold = JsonGetDouble(json, "tpl_harvest_threshold", 50.0);

      int n = ArraySize(g_pending);
      ArrayResize(g_pending, n + 1);
      g_pending[n] = p;
      placed++;
   }

   if(placed == 0)
   {
      SendJson("{\"type\":\"trade_open_failed\",\"trade_id\":\"" + JsonEsc(trade_id) +
               "\",\"error\":\"all grid legs failed to place\"}");
      return;
   }

   // Acked as a normal "trade_opened" (ticket=0, no immediate fill) so
   // Python's open_trade() caller doesn't need a third response shape --
   // the real per-leg fills/tickets arrive later via the existing
   // pending_order_placed-equivalent path once CheckPendingOrders()
   // promotes each leg. Python's open flow only checks ack.type=="trade_opened"
   // to mark the signal active; per-trade DB rows for individual legs are
   // not created by Python at all for grid mode -- the EA is the sole
   // source of truth for grid trades, matching "the EA should manage the
   // trade" for templates.
   SendJson("{\"type\":\"trade_opened\",\"trade_id\":\"" + JsonEsc(trade_id) +
            "\",\"ticket\":0,\"fill_price\":0}");
   Print("[EABridge] grid placed ", placed, "/", legs, " legs group=", groupId,
         " dir=", direction, " step=", stepPts, "pt");
}

//+------------------------------------------------------------------+
//| Outbound lifecycle events                                         |
//+------------------------------------------------------------------+
void ReportTpHit(ManagedTrade &t, const int tpIdx, const double price, const double lotsClosed, const double remaining)
{
   SendJson("{\"type\":\"tp_hit\",\"trade_id\":\"" + JsonEsc(t.trade_id) +
            "\",\"ticket\":" + (string)t.ticket +
            ",\"tp_num\":" + (string)(tpIdx + 1) +
            ",\"price\":" + DoubleToString(price, _Digits) +
            ",\"lots_closed\":" + DoubleToString(lotsClosed, 2) +
            ",\"remaining_lots\":" + DoubleToString(remaining, 2) + "}");
}

void ReportSlMoved(ManagedTrade &t, const double newSl, const string reason, const int tpIdx)
{
   // tpIdx: 0-based index of the TP that triggered this move, -1 if not tied
   // to a specific TP (a continuous distance-based trail). Sent as a 1-based
   // tp_cleared_num (0 meaning "n/a") so Python's fmt_sl_moved() can label the
   // Telegram message correctly instead of guessing — previously every call
   // site left this unreported and Python hardcoded a placeholder of 0,
   // which displayed as the misleading "TP0 cleared" on every EA-reported
   // breakeven lock (confirmed live on ticket 1556988985, a scalp_runner
   // trade whose TP2-triggered breakeven move showed "TP0" instead of "TP2").
   SendJson("{\"type\":\"sl_moved\",\"trade_id\":\"" + JsonEsc(t.trade_id) +
            "\",\"ticket\":" + (string)t.ticket +
            ",\"new_sl\":" + DoubleToString(newSl, _Digits) +
            ",\"reason\":\"" + JsonEsc(reason) + "\"" +
            ",\"tp_cleared_num\":" + (string)(tpIdx + 1) + "}");
}

void ReportTradeClosed(const string trade_id, const ulong ticket, const double closePrice, const string reason)
{
   SendJson("{\"type\":\"trade_closed\",\"trade_id\":\"" + JsonEsc(trade_id) +
            "\",\"ticket\":" + (string)ticket +
            ",\"close_price\":" + DoubleToString(closePrice, _Digits) +
            ",\"reason\":\"" + JsonEsc(reason) + "\"}");
}

//+------------------------------------------------------------------+
//| Shared primitives used by several strategy managers                |
//+------------------------------------------------------------------+
bool TpCleared(const ManagedTrade &t, const int idx, const MqlTick &tick)
{
   if(!t.hasTp[idx]) return false;
   double val = t.tp[idx];
   if(t.direction == "BUY")
   {
      if(val <= t.entry_price) return false;
      return tick.bid >= val;
   }
   else
   {
      if(val >= t.entry_price) return false;
      return tick.ask <= val;
   }
}

double RemainingLots(const ulong ticket)
{
   if(!PositionSelectByTicket(ticket)) return 0.0;
   return PositionGetDouble(POSITION_VOLUME);
}

bool DoPartialClose(ManagedTrade &t, const int tpIdx, const double pct)
{
   double remaining = RemainingLots(t.ticket);
   if(remaining <= 0.0) return false;
   double lots = NormalizeDouble(t.orig_lots * pct, 2);
   lots = MathMin(lots, remaining);
   if(lots <= 0.0) return false;
   MqlTick tick; SymbolInfoTick(_Symbol, tick);
   double price = (t.direction == "BUY") ? tick.bid : tick.ask;
   if(!trade.PositionClosePartial(t.ticket, lots))
   {
      Print("[EABridge] partial close failed ticket=", t.ticket, " tp=", tpIdx + 1,
            " err=", trade.ResultRetcodeDescription());
      return false;
   }
   double newRemaining = RemainingLots(t.ticket);
   t.triggered[tpIdx] = true;
   // DIAG: mirror the success path into the terminal log too — previously
   // only failures printed locally, so a successful partial close was
   // invisible in this log even though ReportTpHit told Python about it.
   Print("[EABridge][DIAG] partial close OK ticket=", t.ticket, " strategy=", t.strategy,
         " tp=", tpIdx + 1, " lots=", lots, " remaining=", newRemaining);
   ReportTpHit(t, tpIdx, price, lots, newRemaining);
   return true;
}

bool DoCloseAll(ManagedTrade &t, const int tpIdx)
{
   double remaining = RemainingLots(t.ticket);
   if(remaining <= 0.0) return false;
   MqlTick tick; SymbolInfoTick(_Symbol, tick);
   double price = (t.direction == "BUY") ? tick.bid : tick.ask;
   if(!trade.PositionClose(t.ticket))
   {
      Print("[EABridge] close-all failed ticket=", t.ticket, " err=", trade.ResultRetcodeDescription());
      return false;
   }
   t.triggered[tpIdx] = true;
   Print("[EABridge][DIAG] close-all OK ticket=", t.ticket, " strategy=", t.strategy,
         " tp=", tpIdx + 1, " lots=", remaining);
   ReportTpHit(t, tpIdx, price, remaining, 0.0);
   return true;
}

bool MoveSl(ManagedTrade &t, const double newSl, const string reason, const int tpIdx = -1)
{
   double curSl = 0.0;
   if(PositionSelectByTicket(t.ticket)) curSl = PositionGetDouble(POSITION_SL);
   bool better = (t.direction == "BUY") ? (newSl > curSl) : (newSl < curSl);
   if(!better) return false;
   double curTp = PositionSelectByTicket(t.ticket) ? PositionGetDouble(POSITION_TP) : 0.0;
   if(!trade.PositionModify(t.ticket, newSl, curTp))
   {
      Print("[EABridge] modify SL failed ticket=", t.ticket, " err=", trade.ResultRetcodeDescription());
      return false;
   }
   ReportSlMoved(t, newSl, reason, tpIdx);
   return true;
}

double GetAdx(const int period = 14)
{
   int h = iADX(_Symbol, PERIOD_M15, period);
   if(h == INVALID_HANDLE) return 0.0;
   double buf[];
   ArraySetAsSeries(buf, true);
   if(CopyBuffer(h, 0, 0, 1, buf) <= 0) return 0.0;
   return buf[0];
}

//+------------------------------------------------------------------+
//| Strategy: scale_out (default) — 40/30/20/10, last TP closes all;   |
//| SL -> entry (breakeven) at TP1.                                    |
//+------------------------------------------------------------------+
void ManageScaleOut(ManagedTrade &t, const MqlTick &tick)
{
   double pcts[4] = {0.40, 0.30, 0.20, 0.10};
   int lastIdx = LastTpIndex(t);
   for(int i = 0; i < MAX_TPS; i++)
   {
      if(!t.hasTp[i] || t.triggered[i]) continue;
      if(!TpCleared(t, i, tick)) break;
      bool closedAll = false;
      if(i == lastIdx)
         closedAll = DoCloseAll(t, i);
      else if(i < 4 && pcts[i] > 0)
         DoPartialClose(t, i, pcts[i]);
      else
         closedAll = DoCloseAll(t, i); // TP5+ with no defined pct — close all remaining, matches Python
      if(i == 0 && !closedAll)
      {
         bool sl_already = PositionSelectByTicket(t.ticket) &&
                            MathAbs(PositionGetDouble(POSITION_SL) - t.entry_price) < _Point;
         if(!sl_already) MoveSl(t, t.entry_price, "be", 0);
      }
      if(closedAll) return;
   }
}

//+------------------------------------------------------------------+
//| Strategy: be_runner — ADX>=25 gate (else fall back to scale_out).  |
//| No partial closes — SL only ever steps to [entry, tp1, tp2, ...]   |
//| at the highest cleared level.                                     |
//+------------------------------------------------------------------+
void ManageBeRunner(ManagedTrade &t, const MqlTick &tick)
{
   if(GetAdx() < 25.0) { ManageScaleOut(t, tick); return; }

   // Build sl_ladder = [entry] + [compacted, non-null TP prices in order] —
   // matches Python exactly. Gaps are skipped when compacting, not treated
   // as a stop, so a missing tp2 (say) doesn't stall progression at tp1.
   double ladder[MAX_TPS + 1];
   ladder[0] = t.entry_price;
   int total = 0;
   for(int i = 0; i < MAX_TPS; i++)
   {
      if(!t.hasTp[i]) continue;
      total++;
      ladder[total] = t.tp[i];
   }

   int bestStep = 0;
   int compactPos = 0;
   for(int i = 0; i < MAX_TPS; i++)
   {
      if(!t.hasTp[i]) continue;
      if(!TpCleared(t, i, tick)) break;
      compactPos++;
      bestStep = compactPos;
   }
   if(bestStep == 0) return;
   double target = ladder[bestStep - 1]; // one step BEHIND the highest cleared TP
   MoveSl(t, target, "step" + (string)bestStep);
}

//+------------------------------------------------------------------+
//| Strategy: trail_stop — SL -> entry at TP1, then continuously      |
//| trails bid/ask by trail_dist, floored/ceilinged at entry.          |
//+------------------------------------------------------------------+
void ManageTrailStop(ManagedTrade &t, const MqlTick &tick)
{
   if(!t.trailing_active)
   {
      if(!t.hasTp[0] || !TpCleared(t, 0, tick)) return;
      t.trailing_active = true;
      MoveSl(t, t.entry_price, "trail_activate", 0);
      return;
   }
   double newSl;
   if(t.direction == "BUY") newSl = MathMax(tick.bid - t.trail_dist, t.entry_price);
   else                     newSl = MathMin(tick.ask + t.trail_dist, t.entry_price);
   MoveSl(t, newSl, "trail");
}

//+------------------------------------------------------------------+
//| Strategy: protected_scale — TP1 skip, TP2 BE-lock, TP3-5 20% each. |
//+------------------------------------------------------------------+
void ManageProtectedScale(ManagedTrade &t, const MqlTick &tick)
{
   if(!t.triggered[0] && TpCleared(t, 0, tick)) t.triggered[0] = true; // TP1: mark only
   if(!t.triggered[1] && TpCleared(t, 1, tick))
   {
      t.triggered[1] = true;
      MoveSl(t, t.entry_price, "be", 1);
   }
   for(int i = 2; i <= 4; i++)
   {
      if(!t.hasTp[i] || t.triggered[i]) continue;
      if(!TpCleared(t, i, tick)) break;
      if(DoPartialClose(t, i, 0.20)) return;
   }
}

//+------------------------------------------------------------------+
//| Strategy: scalp_runner — TP1 (idx0) closes 50%, SL untouched; TP2  |
//| (idx1) moves SL to entry and starts the fixed-pt trail on the      |
//| remaining 50%. Own function now (previously shared a single-TP     |
//| 60/40 split with ManageConservativeLike) since the two TPs trigger |
//| genuinely different actions, not just a different close percent.  |
//+------------------------------------------------------------------+
void ManageScalpRunner(ManagedTrade &t, const MqlTick &tick)
{
   if(!t.triggered[0])
   {
      if(!t.hasTp[0] || !TpCleared(t, 0, tick)) return;
      DoPartialClose(t, 0, 0.50);
      return;
   }
   if(!t.triggered[1])
   {
      if(!t.hasTp[1] || !TpCleared(t, 1, tick)) return;
      t.triggered[1] = true;
      MoveSl(t, t.entry_price, "be", 1);
      return;
   }
   double newSl;
   if(t.direction == "BUY") newSl = MathMax(tick.bid - t.trail_dist, t.entry_price);
   else                     newSl = MathMin(tick.ask + t.trail_dist, t.entry_price);
   MoveSl(t, newSl, "trail");
}

//+------------------------------------------------------------------+
//| Strategy: conservative — TP1 partial close (80%) + SL->entry, then |
//| continuously trail the remainder floored at entry. unattended      |
//| reuses this with closePct=1.0 and no trail.                        |
//+------------------------------------------------------------------+
void ManageConservativeLike(ManagedTrade &t, const MqlTick &tick, const double closePct, const bool fullCloseNoTrail)
{
   if(!t.triggered[0])
   {
      if(!t.hasTp[0] || !TpCleared(t, 0, tick)) return;
      bool closedAll = false;
      if(fullCloseNoTrail) closedAll = DoCloseAll(t, 0);
      else DoPartialClose(t, 0, closePct);
      if(fullCloseNoTrail || closedAll) return;
      MoveSl(t, t.entry_price, "be", 0);
      return;
   }
   if(fullCloseNoTrail) return; // unattended: nothing left to manage after full close
   double newSl;
   if(t.direction == "BUY") newSl = MathMax(tick.bid - t.trail_dist, t.entry_price);
   else                     newSl = MathMin(tick.ask + t.trail_dist, t.entry_price);
   MoveSl(t, newSl, "trail");
}

//+------------------------------------------------------------------+
//| Strategy: conservative_trial — fixed 6-step ladder with two SL     |
//| moves (TP2->entry, TP4->TP2 price).                                |
//+------------------------------------------------------------------+
void ManageConservativeTrial(ManagedTrade &t, const MqlTick &tick)
{
   double pcts[6] = {0.05, 0.30, 0.20, 0.40, 0.05, -1.0}; // -1.0 = close all remaining
   for(int i = 0; i < 6; i++)
   {
      if(!t.hasTp[i] || t.triggered[i]) continue;
      if(!TpCleared(t, i, tick)) return; // sequential — matches Python's per-TP `if not cleared: return`

      bool closedAll = false;
      if(pcts[i] < 0) closedAll = DoCloseAll(t, i);
      else            DoPartialClose(t, i, pcts[i]);

      if(i == 1) MoveSl(t, t.entry_price, "be", 1); // TP2 -> breakeven
      if(i == 3 && t.hasTp[1]) MoveSl(t, t.tp[1], "tp2_level"); // TP4 -> TP2 price
      if(closedAll || i == 5) return;
   }
}

//+------------------------------------------------------------------+
//| Strategy: signal_climber / reversal_runner / adaptive_runner /       |
//| adaptive_runner_2 / limit_runner (_run_tp_ladder) — SL stays put    |
//| until beAtPos, then -> entry; every TP after that trails SL per    |
//| t.trailMode ("" = previous TP price, the original/default rule;    |
//| "midpoint_lag2" = midpoint of the two TPs before this one,         |
//| adaptive_runner_2 only). Close-% per TP, the BE trigger position,  |
//| the trail mode, and closeFullOnLast are sent by Python at open     |
//| time (t.pcts / t.beAtPos / t.trailMode / t.closeFullOnLast — see   |
//| ManagedTrade and HandleOpenTrade/HandlePlacePendingOrder), so a    |
//| tuning change to an existing strategy needs zero EA changes — only |
//| a Python-side edit. A genuinely new trail RULE (not just new       |
//| pcts/be_at_pos values) still needs an EA change, same as           |
//| adaptive_runner_2 itself did — see the trailMode branch below.     |
//| limit_runner's own closeFullOnLast=false path (only when the       |
//| signal had a literal "TP OPEN" line) is the second such case: the  |
//| last defined TP only closes its own pcts[] share instead of        |
//| everything, leaving the rest riding on the trailing SL with no     |
//| further TP to close it. Falls back to the hardcoded table below    |
//| only if be_at_pos wasn't sent by Python (a protocol-version-       |
//| mismatch safety net; shouldn't happen normally).                   |
//+------------------------------------------------------------------+
void ManageLadder(ManagedTrade &t, const MqlTick &tick)
{
   int n = TpCount(t);
   if(n == 0) return;
   double pcts[8];
   int beAtPos;
   if(t.beAtPos >= 0)
   {
      ArrayCopy(pcts, t.pcts);
      beAtPos = t.beAtPos;
   }
   else
   {
      bool isGdvr = (t.strategy != "signal_climber");
      GetLadderPcts(n, isGdvr, pcts);
      beAtPos = (t.strategy == "reversal_runner" || t.strategy == "adaptive_runner_2") ? 1 : 0;
      Print("[EABridge][WARN] ladder trade ", t.trade_id, " strategy=", t.strategy,
            " had no be_at_pos from Python — using hardcoded fallback table");
   }

   // compactPos tracks the position within the COMPACTED list of only-defined
   // TPs (matching Python's `enumerate(all_tps)`), since pcts_table and
   // be_at_pos are both indexed by that compacted position, not the raw
   // TP1-8 slot — a gap (tp2 missing but tp3+ present) would otherwise read
   // the wrong percentage/BE-step for every TP after the gap.
   int compactPos = 0;
   for(int idx = 0; idx < MAX_TPS; idx++)
   {
      if(!t.hasTp[idx]) continue; // gaps must not truncate the ladder
      if(t.triggered[idx]) { compactPos++; continue; }
      if(!TpCleared(t, idx, tick)) break;

      double remaining = RemainingLots(t.ticket);
      if(remaining <= 0) break;
      bool isLast = (compactPos == n - 1);

      bool closedAll = false;
      if(isLast && t.closeFullOnLast) closedAll = DoCloseAll(t, idx);
      else                            DoPartialClose(t, idx, pcts[compactPos]);

      if(compactPos >= beAtPos)
      {
         double newSl;
         string moveTag;
         if(compactPos == beAtPos)
         {
            newSl = t.entry_price;
            moveTag = "be";
         }
         else if(t.trailMode == "midpoint_lag2" && compactPos >= 2)
         {
            // adaptive_runner_2: SL steps to the midpoint of the two TPs
            // before the one just hit, not the single immediately-previous
            // TP price — see run_tp_ladder()'s sl_rule parameter
            // (core_run_tp_ladder.py) for the Python-side equivalent this
            // must stay in lockstep with.
            newSl = (CompactedTpPrice(t, compactPos - 2) + CompactedTpPrice(t, compactPos - 1)) / 2.0;
            moveTag = "trail_midpoint_lag2";
         }
         else
         {
            newSl = PrevTpPrice(t, idx);
            moveTag = "trail_prev_tp";
         }
         MoveSl(t, newSl, moveTag, idx);
      }
      if(closedAll) return;
      compactPos++;
   }
}

double PrevTpPrice(const ManagedTrade &t, const int pos)
{
   for(int j = pos - 1; j >= 0; j--) if(t.hasTp[j]) return t.tp[j];
   return t.entry_price;
}

// Price of the Nth (0-indexed) defined TP in compacted order — matching
// Python's all_tps[wantPos][1] (see run_tp_ladder()'s sl_rule callback).
// Unlike PrevTpPrice (which walks back from a raw slot to the nearest
// defined TP below it), this looks up an exact compacted position.
double CompactedTpPrice(const ManagedTrade &t, const int wantPos)
{
   int count = 0;
   for(int idx = 0; idx < MAX_TPS; idx++)
   {
      if(!t.hasTp[idx]) continue;
      if(count == wantPos) return t.tp[idx];
      count++;
   }
   return t.entry_price; // shouldn't happen -- wantPos is always derived from a real compactPos
}

void GetLadderPcts(const int n, const bool isGdvr, double &out[])
{
   ArrayInitialize(out, 0.0);
   if(!isGdvr)
   {
      // _CLIMBER_PCTS
      if(n==1){double p[]={1.00}; ArrayCopy(out,p);}
      else if(n==2){double p[]={0.40,0.60}; ArrayCopy(out,p);}
      else if(n==3){double p[]={0.30,0.30,0.40}; ArrayCopy(out,p);}
      else if(n==4){double p[]={0.20,0.25,0.25,0.30}; ArrayCopy(out,p);}
      else if(n==5){double p[]={0.20,0.15,0.15,0.20,0.30}; ArrayCopy(out,p);}
      else if(n==6){double p[]={0.20,0.15,0.15,0.15,0.20,0.15}; ArrayCopy(out,p);}
      else if(n==7){double p[]={0.20,0.10,0.10,0.15,0.15,0.20,0.10}; ArrayCopy(out,p);}
      else {double p[]={0.20,0.10,0.10,0.10,0.15,0.15,0.10,0.10}; ArrayCopy(out,p);}
   }
   else
   {
      // _GDVR_PCTS
      if(n==1){double p[]={1.00}; ArrayCopy(out,p);}
      else if(n==2){double p[]={0.30,0.70}; ArrayCopy(out,p);}
      else if(n==3){double p[]={0.15,0.25,0.60}; ArrayCopy(out,p);}
      else if(n==4){double p[]={0.10,0.15,0.25,0.50}; ArrayCopy(out,p);}
      else if(n==5){double p[]={0.10,0.10,0.15,0.25,0.40}; ArrayCopy(out,p);}
      else if(n==6){double p[]={0.08,0.08,0.12,0.17,0.20,0.35}; ArrayCopy(out,p);}
      else if(n==7){double p[]={0.07,0.07,0.10,0.13,0.13,0.20,0.30}; ArrayCopy(out,p);}
      else {double p[]={0.05,0.05,0.10,0.10,0.15,0.15,0.15,0.25}; ArrayCopy(out,p);}
   }
}

//+------------------------------------------------------------------+
//| Strategy: no_sl_scale (Trend Ratchet) — TP1 20% close; TP2 skip;   |
//| TP3 20% close + SL->TP1; TP4-7 skip + SL steps to TP(n-2); last    |
//| TP (or TP8) closes all remaining.                                  |
//+------------------------------------------------------------------+
void ManageNoSlScale(ManagedTrade &t, const MqlTick &tick)
{
   int lastIdx = LastTpIndex(t);
   if(lastIdx < 0) return;
   for(int i = 0; i <= lastIdx; i++)
   {
      if(!t.hasTp[i] || t.triggered[i]) continue;
      if(!TpCleared(t, i, tick)) break;

      if(i == lastIdx) { DoCloseAll(t, i); return; }

      if(i == 0) { DoPartialClose(t, 0, 0.20); }
      else if(i == 2) { DoPartialClose(t, 2, 0.20); if(t.hasTp[0]) MoveSl(t, t.tp[0], "tp1_level"); }
      else
      {
         // TP2, TP4, TP5, TP6, TP7 — skip (mark only), SL steps to TP(i-2)
         t.triggered[i] = true;
         if(i >= 3 && t.hasTp[i - 2]) MoveSl(t, t.tp[i - 2], "step_" + (string)(i + 1));
      }
   }
}

//+------------------------------------------------------------------+
//| Strategy: unattended — reuses ManageConservativeLike, full close   |
//| at TP1, no trail (matches _handle_conservative's _ua_on branch).   |
//+------------------------------------------------------------------+
void ManageUnattended(ManagedTrade &t, const MqlTick &tick)
{
   ManageConservativeLike(t, tick, 1.00, true);
}

//+------------------------------------------------------------------+
//| Strategy: orb_fixed — ORB/IVB Report trades. No management beyond  |
//| the reload-zone setup itself: full close (no partials, no BE-move, |
//| no trailing) the instant tp1 (the report's own target) is hit —    |
//| mirrors _handle_orb_fixed exactly.                                  |
//+------------------------------------------------------------------+
void ManageOrbFixed(ManagedTrade &t, const MqlTick &tick)
{
   if(t.triggered[0]) return;
   if(!TpCleared(t, 0, tick)) return;
   DoCloseAll(t, 0);
}

//+------------------------------------------------------------------+
//| EA Templates ("template:<name>" strategy) — see core_ea_templates.py |
//| and ManagedTrade's tpl* fields for what each setting means.        |
//+------------------------------------------------------------------+

// Trail-to level for tplTrailMode=="candle" -- lowest low (BUY) / highest
// high (SELL) of the last N closed M15 candles. Fixed 3-candle lookback
// (not itself template-configurable -- there is no UI field for it; a
// future template revision could expose it). Returns 0.0 if the candle
// data isn't available yet (no trail applied that tick).
double CandleTrailLevel(const ManagedTrade &t)
{
   const int lookback = 3;
   if(t.direction == "BUY")
   {
      int idx = iLowest(_Symbol, PERIOD_M15, MODE_LOW, lookback, 1);
      if(idx < 0) return 0.0;
      return iLow(_Symbol, PERIOD_M15, idx);
   }
   else
   {
      int idx = iHighest(_Symbol, PERIOD_M15, MODE_HIGH, lookback, 1);
      if(idx < 0) return 0.0;
      return iHigh(_Symbol, PERIOD_M15, idx);
   }
}

// Trail-to level for tplTrailMode=="fractal" -- the most recent confirmed
// Bill Williams fractal on the opposite side of price (a down-fractal
// beneath price for a BUY, an up-fractal above price for a SELL), scanned
// over the last 50 M15 bars. Returns 0.0 if none found yet.
double FractalTrailLevel(const ManagedTrade &t)
{
   int h = iFractals(_Symbol, PERIOD_M15);
   if(h == INVALID_HANDLE) return 0.0;
   double buf[];
   ArraySetAsSeries(buf, true);
   int lookback = 50;
   // Buffer 1 = lower fractals (trail target for a BUY, price rises toward
   // it from below); buffer 0 = upper fractals (trail target for a SELL).
   if(CopyBuffer(h, (t.direction == "BUY") ? 1 : 0, 0, lookback, buf) <= 0) return 0.0;
   for(int i = 2; i < lookback; i++) // skip the unconfirmed last 2 bars
   {
      double v = buf[i];
      if(v != 0.0 && v != EMPTY_VALUE) return v;
   }
   return 0.0;
}

void ManageTemplate(ManagedTrade &t, const MqlTick &tick)
{
   if(!PositionSelectByTicket(t.ticket)) return; // closed already this tick

   // ── Anchor TP: partial closes at each cleared level, full close on the
   // last defined level -- same mechanism ManageLadder() uses for the
   // built-in ladder strategies (t.pcts[]/t.closeFullOnLast), so a
   // template's own Anchor TP %-close ladder (core_ea_templates.py's
   // tp{n}_pct fields) actually takes effect instead of being silently
   // ignored (2026-07-24 -- ManageTemplate() never read t.pcts[] at all
   // before this). Runs for "on"/"stealth" only, never "off" -- "off" means
   // no TP tracking whatsoever (SL/harvest/trail-only), unchanged from
   // before. For "stealth" this replaces the old last-TP-only check: when
   // every pct is 0 (a template with no Anchor TP % configured, the case
   // for every template saved before this feature existed) DoPartialClose's
   // own 0-lots guard makes every non-last level a safe no-op, so only the
   // last defined TP ever actually closes anything -- identical outcome to
   // the old stealth-only block for every existing template.
   if(t.tplTpslMode != "off")
   {
      int tplN = TpCount(t);
      if(tplN > 0)
      {
         int tplCompactPos = 0;
         for(int tplIdx = 0; tplIdx < MAX_TPS; tplIdx++)
         {
            if(!t.hasTp[tplIdx]) continue;
            if(t.triggered[tplIdx]) { tplCompactPos++; continue; }
            if(!TpCleared(t, tplIdx, tick)) break;

            double tplRemaining = RemainingLots(t.ticket);
            if(tplRemaining <= 0) break;
            bool tplIsLast = (tplCompactPos == tplN - 1);

            bool tplClosedAll = false;
            if(tplIsLast && t.closeFullOnLast) tplClosedAll = DoCloseAll(t, tplIdx);
            else                               DoPartialClose(t, tplIdx, t.pcts[tplCompactPos]);

            if(tplClosedAll) return; // position gone -- CheckForClosures reports it
            tplCompactPos++;
         }
      }
   }

   // ── Harvest: bank profit once floating P&L reaches the threshold ────
   if(t.tplHarvestEnabled)
   {
      double profit = PositionGetDouble(POSITION_PROFIT);
      if(profit >= t.tplHarvestThreshold)
      {
         trade.PositionClose(t.ticket);
         Print("[EABridge] template harvest threshold reached ($", profit,
               " >= $", t.tplHarvestThreshold, "), closing ticket=", t.ticket);
         return;
      }
   }

   // ── Breakeven ────────────────────────────────────────────────────────
   if(!t.tplBeDone)
   {
      int beIdx = t.tplBeTrigger - 1;
      if(beIdx >= 0 && beIdx < MAX_TPS && TpCleared(t, beIdx, tick))
      {
         double beSl = t.entry_price;
         if(t.tplBeMode == "entry_buffer")
         {
            double sign = (t.direction == "BUY") ? 1.0 : -1.0;
            beSl = t.entry_price + sign * t.tplBeBufferPts;
         }
         if(MoveSl(t, beSl, "template_be", beIdx)) t.tplBeDone = true;
      }
   }

   // ── Trail ────────────────────────────────────────────────────────────
   if(t.tplTrailMode == "step")
   {
      double dist = InpDefaultTrailStopPts;
      double newSl = (t.direction == "BUY") ? tick.bid - dist : tick.ask + dist;
      MoveSl(t, newSl, "template_trail_step");
   }
   else if(t.tplTrailMode == "candle")
   {
      double newSl = CandleTrailLevel(t);
      if(newSl != 0.0) MoveSl(t, newSl, "template_trail_candle");
   }
   else if(t.tplTrailMode == "fractal")
   {
      double newSl = FractalTrailLevel(t);
      if(newSl != 0.0) MoveSl(t, newSl, "template_trail_fractal");
   }
   else if(t.tplTrailMode == "tp")
   {
      int bestIdx = -1;
      for(int i = 0; i < MAX_TPS; i++)
      {
         if(!t.hasTp[i]) continue;
         if(!TpCleared(t, i, tick)) break;
         bestIdx = i;
      }
      if(bestIdx >= 0) MoveSl(t, t.tp[bestIdx], "template_trail_tp", bestIdx);
   }
   // trail == "off": nothing further -- SL only ever moved by the
   // breakeven step above (if/when its trigger clears).
}

//+------------------------------------------------------------------+
//| Dispatch                                                           |
//+------------------------------------------------------------------+
void ManageTrade(ManagedTrade &t, const MqlTick &tick)
{
   if(t.isTemplate) { ManageTemplate(t, tick); return; }
   if(t.strategy == "be_runner") ManageBeRunner(t, tick);
   else if(t.strategy == "trail_stop") ManageTrailStop(t, tick);
   else if(t.strategy == "protected_scale") ManageProtectedScale(t, tick);
   else if(t.strategy == "conservative") ManageConservativeLike(t, tick, 0.80, false);
   else if(t.strategy == "scalp_runner") ManageScalpRunner(t, tick);
   else if(t.strategy == "conservative_trial") ManageConservativeTrial(t, tick);
   else if(t.strategy == "signal_climber" || t.strategy == "reversal_runner" ||
           t.strategy == "adaptive_runner" || t.strategy == "adaptive_runner_2" ||
           t.strategy == "limit_runner") ManageLadder(t, tick);
   else if(t.strategy == "no_sl_scale") ManageNoSlScale(t, tick);
   else if(t.strategy == "unattended") ManageUnattended(t, tick);
   else if(t.strategy == "orb_fixed") ManageOrbFixed(t, tick);
   else ManageScaleOut(t, tick); // default / scale_out
}

//+------------------------------------------------------------------+
//| Detect a managed position that no longer exists — closed by SL,   |
//| a final TP close-all, or manually — and report it.                |
//+------------------------------------------------------------------+
void CheckForClosures()
{
   for(int i = ArraySize(g_trades) - 1; i >= 0; i--)
   {
      if(PositionSelectByTicket(g_trades[i].ticket)) continue; // still open

      // TEMPORARY DIAGNOSTIC (remove once the scalp_runner "silently orphaned
      // trade" investigation is closed) — capture everything available at the
      // exact moment a tracked position is found missing, so a false-negative
      // from PositionSelectByTicket (vs a genuine close) is distinguishable
      // after the fact instead of guessed at from the DB's final state.
      int diagErr = GetLastError();
      ResetLastError();
      bool diagHistOk = HistorySelectByPosition(g_trades[i].ticket);
      int  diagDeals  = diagHistOk ? HistoryDealsTotal() : -1;
      Print("[EABridge][DIAG] position gone: ticket=", g_trades[i].ticket,
            " trade_id=", g_trades[i].trade_id, " strategy=", g_trades[i].strategy,
            " triggered0=", g_trades[i].triggered[0],
            " lastErr=", diagErr, " historySelectOk=", diagHistOk, " deals=", diagDeals);

      string reason = "SL";
      double closePrice = 0.0;
      if(diagHistOk)
      {
         int deals = diagDeals;
         if(deals > 0)
         {
            ulong dealTicket = HistoryDealGetTicket(deals - 1);
            closePrice = HistoryDealGetDouble(dealTicket, DEAL_PRICE);
            string comment = HistoryDealGetString(dealTicket, DEAL_COMMENT);
            long dealReason = HistoryDealGetInteger(dealTicket, DEAL_REASON);
            long dealEntry  = HistoryDealGetInteger(dealTicket, DEAL_ENTRY);
            // DIAG: MT5's own DEAL_REASON/DEAL_ENTRY enums, logged alongside the
            // comment-string heuristic below so we can see whether that
            // heuristic is actually matching what MT5 itself recorded.
            Print("[EABridge][DIAG] last deal: ticket=", dealTicket, " price=", closePrice,
                  " comment=\"", comment, "\" dealReason=", dealReason, " dealEntry=", dealEntry);
            string commentLower = comment;
            StringToLower(commentLower);
            if(StringFind(commentLower, "tp") >= 0) reason = "TP";
            else if(StringFind(commentLower, "sl") >= 0) reason = "SL";
            else reason = "MT5_close";
         }
         else
         {
            Print("[EABridge][DIAG] no deal history found for ticket=", g_trades[i].ticket,
                  " — position vanished with no matching deal");
         }
      }
      ReportTradeClosed(g_trades[i].trade_id, g_trades[i].ticket, closePrice, reason);
      RemoveManaged(i);
   }
}

//+------------------------------------------------------------------+
//| Detect a resting Limit Runner order that filled, expired, or was   |
//| cancelled — no MT5 event handler exists for pending-order fills    |
//| (OnTradeTransaction is not used anywhere in this EA; every other    |
//| lifecycle event here is polled, e.g. CheckForClosures() above), so |
//| this checks the same way: does the order still exist? If not, did  |
//| a position with the same ticket appear (filled) or not (gone)?     |
//+------------------------------------------------------------------+
// Cancels every other still-resting leg in a filled grid group. Only
// deletes the broker-side order here -- doesn't touch g_pending[] itself,
// so it's safe to call from inside CheckPendingOrders()'s own backward
// loop; the next tick's pass over g_pending[] finds each cancelled ticket
// already gone and cleans it up via the existing "expired_or_cancelled"
// path, no double-bookkeeping needed.
void CancelGridSiblings(const int groupId, const ulong filledTicket)
{
   for(int i = 0; i < ArraySize(g_pending); i++)
   {
      if(g_pending[i].tplGridGroup != groupId) continue;
      if(g_pending[i].ticket == filledTicket) continue;
      if(trade.OrderDelete(g_pending[i].ticket))
         Print("[EABridge] cancelled grid sibling ticket=", g_pending[i].ticket,
               " group=", groupId, " (leg filled ticket=", filledTicket, ")");
   }
}

// Trading > Global Parameters > Harvest -- sweeps EVERY open position on
// this symbol, closing any whose own floating profit has reached the
// configured threshold. Deliberately does NOT go through g_trades[]/
// PositionSelectByTicket(known ticket) like every other per-trade check in
// this file (ManageTrade, CheckForClosures) -- those only ever see
// positions this EA itself is managing. This iterates PositionsTotal()/
// PositionGetTicket() directly instead, so it also catches positions this
// EA has no tracking entry for at all (Python-bridge-managed trades,
// anything opened outside this app entirely) -- "regardless of how it was
// executed", per the Global Parameters card's own description. Called
// every OnTick regardless of g_trades/g_pending size (see OnTick below) --
// a Python-only-managed trade leaves both those arrays empty.
void CheckGlobalHarvest()
{
   if(!g_globalHarvestEnabled) return;
   for(int i = PositionsTotal() - 1; i >= 0; i--)
   {
      ulong ticket = PositionGetTicket(i);
      if(ticket == 0) continue;
      if(!PositionSelectByTicket(ticket)) continue;
      if(PositionGetString(POSITION_SYMBOL) != _Symbol) continue;
      double profit = PositionGetDouble(POSITION_PROFIT);
      if(profit >= g_globalHarvestThresholdUsd)
      {
         Print("[EABridge] global harvest threshold reached ($", profit,
               " >= $", g_globalHarvestThresholdUsd, "), closing ticket=", ticket);
         trade.PositionClose(ticket);
      }
   }
}

void CheckPendingOrders()
{
   for(int i = ArraySize(g_pending) - 1; i >= 0; i--)
   {
      ulong ticket = g_pending[i].ticket;
      if(OrderSelect(ticket)) continue;  // still resting on the book

      if(PositionSelectByTicket(ticket))
      {
         // Filled — a resting order that opens a brand-new position gets a
         // position ticket equal to its own order ticket (same assumption
         // HandleOpenTrade's trade.ResultOrder() already relies on for a
         // market fill). Promote straight into g_trades[] so this trade is
         // indistinguishable from any other EA-managed trade from here on.
         double fillPrice = PositionGetDouble(POSITION_PRICE_OPEN);

         ManagedTrade mt;
         mt.ticket = ticket;
         mt.trade_id = g_pending[i].trade_id;
         mt.strategy = g_pending[i].strategy;
         mt.direction = g_pending[i].direction;
         mt.entry_price = fillPrice;
         mt.orig_lots = g_pending[i].lots;
         mt.trailing_active = false;
         mt.last_step = 0;
         mt.trail_dist = InpConservativeTrailPts;
         for(int j = 0; j < MAX_TPS; j++)
         {
            mt.hasTp[j] = g_pending[i].hasTp[j];
            mt.tp[j] = g_pending[i].tp[j];
            mt.triggered[j] = false;
            mt.pcts[j] = g_pending[i].pcts[j];
         }
         mt.beAtPos = g_pending[i].beAtPos;
         mt.trailMode = g_pending[i].trailMode;
         mt.closeFullOnLast = g_pending[i].closeFullOnLast;

         mt.isTemplate = g_pending[i].isTemplate;
         mt.tplTpslMode = g_pending[i].tplTpslMode;
         mt.tplTrailMode = g_pending[i].tplTrailMode;
         mt.tplBeMode = g_pending[i].tplBeMode;
         mt.tplBeBufferPts = g_pending[i].tplBeBufferPts;
         mt.tplBeTrigger = g_pending[i].tplBeTrigger;
         mt.tplBeDone = false;
         mt.tplCancelPending = g_pending[i].tplCancelPending;
         mt.tplGridGroup = g_pending[i].tplGridGroup;
         mt.tplHarvestEnabled = g_pending[i].tplHarvestEnabled;
         mt.tplHarvestThreshold = g_pending[i].tplHarvestThreshold;

         int n = ArraySize(g_trades);
         ArrayResize(g_trades, n + 1);
         g_trades[n] = mt;

         SendJson("{\"type\":\"pending_order_filled\",\"trade_id\":\"" + JsonEsc(mt.trade_id) +
                  "\",\"ticket\":" + (string)ticket +
                  ",\"fill_price\":" + DoubleToString(fillPrice, _Digits) + "}");
         Print("[EABridge] pending order filled -> managed ticket=", ticket,
               " strategy=", mt.strategy, " @ ", fillPrice);

         // EA Template grid, Cancel Pending: this leg filled -- cancel every
         // other still-resting sibling leg in the same group so the position
         // doesn't keep averaging in beyond what already filled.
         if(mt.isTemplate && mt.tplCancelPending && mt.tplGridGroup >= 0)
            CancelGridSiblings(mt.tplGridGroup, ticket);
      }
      else
      {
         // Gone with no resulting position — either the broker-side
         // expiration we set fired, or it was cancelled manually in the
         // terminal. Can't reliably tell these apart from this observation
         // alone, so "reason" is best-effort, not authoritative.
         SendJson("{\"type\":\"pending_order_cancelled\",\"trade_id\":\"" + JsonEsc(g_pending[i].trade_id) +
                  "\",\"reason\":\"expired_or_cancelled\"}");
         Print("[EABridge] pending order gone (expired/cancelled) ticket=", ticket,
               " trade_id=", g_pending[i].trade_id);
      }

      int total = ArraySize(g_pending);
      for(int k = i; k < total - 1; k++) g_pending[k] = g_pending[k + 1];
      ArrayResize(g_pending, total - 1);
   }
}

//+------------------------------------------------------------------+
//| MT5 event handlers                                                 |
//+------------------------------------------------------------------+
int OnInit()
{
   EventSetMillisecondTimer(200);
   EnsureConnected();
   return(INIT_SUCCEEDED);
}

void OnDeinit(const int reason)
{
   EventKillTimer();
   if(g_socket != INVALID_HANDLE) SocketClose(g_socket);
}

void OnTimer()
{
   PollSocket();
   DiagHeartbeat();
}

// TEMPORARY DIAGNOSTIC (remove once the scalp_runner "silently orphaned
// trade" investigation is closed) — prints every 60s if the tracked-trade
// count changed, plus every tracked ticket/strategy. A count drop with no
// preceding "[EABridge][DIAG] position gone" line for that ticket means
// something removed it from g_trades outside CheckForClosures (e.g. a full
// EA reinit wiping the array), which would otherwise be invisible.
void DiagHeartbeat()
{
   int n = ArraySize(g_trades);
   if(TimeCurrent() - g_lastDiagHeartbeat < 60 && n == g_lastDiagCount) return;
   g_lastDiagHeartbeat = TimeCurrent();
   g_lastDiagCount = n;
   string list = "";
   for(int i = 0; i < n; i++)
      list += (string)g_trades[i].ticket + ":" + g_trades[i].strategy + " ";
   Print("[EABridge][DIAG] heartbeat tracked=", n, " [", list, "]");
}

void OnTick()
{
   // Global harvest runs even with g_trades/g_pending both empty -- it
   // sweeps PositionsTotal() directly, so it must not be skipped by the
   // early-return below (which exists for everything else here, all of
   // which only ever look at trades/orders this EA itself is tracking).
   if(g_globalHarvestEnabled) CheckGlobalHarvest();
   if(ArraySize(g_trades) == 0 && ArraySize(g_pending) == 0) return;
   MqlTick tick;
   if(!SymbolInfoTick(_Symbol, tick)) return;
   CheckPendingOrders();
   CheckForClosures();
   for(int i = 0; i < ArraySize(g_trades); i++)
      ManageTrade(g_trades[i], tick);
}
//+------------------------------------------------------------------+
