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
};

ManagedTrade g_trades[];

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

   // be_runner is the one strategy where Python's own bridge path sets a real
   // broker-side TP too (the highest defined TP, as a backstop in case the
   // ladder-management logic itself never catches the final level) — mirror
   // that here. Every other strategy manages TPs purely via partial closes,
   // so no broker TP is set for them (tp=0).
   double brokerTp = 0.0;
   if(strategy == "be_runner")
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

   int n = ArraySize(g_trades);
   ArrayResize(g_trades, n + 1);
   g_trades[n] = mt;

   SendJson("{\"type\":\"trade_opened\",\"trade_id\":\"" + JsonEsc(trade_id) +
            "\",\"ticket\":" + (string)ticket +
            ",\"fill_price\":" + DoubleToString(fillPrice, _Digits) + "}");
   Print("[EABridge] opened ", direction, " ticket=", ticket, " strategy=", strategy,
         " @ ", fillPrice, " SL=", sl);
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
//| Strategy: signal_climber / gd_vip_runner / adaptive_runner /       |
//| adaptive_runner_2 (_run_tp_ladder) — SL stays put until beAtPos,   |
//| then -> entry; every TP after that trails SL per t.trailMode      |
//| ("" = previous TP price, the original/default rule; "midpoint_    |
//| lag2" = midpoint of the two TPs before this one, adaptive_        |
//| runner_2 only). Close-% per TP, the BE trigger position, and the  |
//| trail mode are sent by Python at open time (t.pcts / t.beAtPos /  |
//| t.trailMode — see ManagedTrade and HandleOpenTrade), so a tuning  |
//| change to an existing strategy needs zero EA changes — only a     |
//| Python-side edit. A genuinely new trail RULE (not just new pcts/  |
//| be_at_pos values) still needs an EA change, same as adaptive_     |
//| runner_2 itself did — see the trailMode branch below. Falls back  |
//| to the hardcoded table below only if be_at_pos wasn't sent by     |
//| Python (a protocol-version-mismatch safety net; shouldn't happen  |
//| normally).                                                        |
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
      beAtPos = (t.strategy == "gd_vip_runner" || t.strategy == "adaptive_runner_2") ? 1 : 0;
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
      if(isLast) closedAll = DoCloseAll(t, idx);
      else       DoPartialClose(t, idx, pcts[compactPos]);

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
//| Dispatch                                                           |
//+------------------------------------------------------------------+
void ManageTrade(ManagedTrade &t, const MqlTick &tick)
{
   if(t.strategy == "be_runner") ManageBeRunner(t, tick);
   else if(t.strategy == "trail_stop") ManageTrailStop(t, tick);
   else if(t.strategy == "protected_scale") ManageProtectedScale(t, tick);
   else if(t.strategy == "conservative") ManageConservativeLike(t, tick, 0.80, false);
   else if(t.strategy == "scalp_runner") ManageScalpRunner(t, tick);
   else if(t.strategy == "conservative_trial") ManageConservativeTrial(t, tick);
   else if(t.strategy == "signal_climber" || t.strategy == "gd_vip_runner" ||
           t.strategy == "adaptive_runner" || t.strategy == "adaptive_runner_2") ManageLadder(t, tick);
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
   if(ArraySize(g_trades) == 0) return;
   MqlTick tick;
   if(!SymbolInfoTick(_Symbol, tick)) return;
   CheckForClosures();
   for(int i = 0; i < ArraySize(g_trades); i++)
      ManageTrade(g_trades[i], tick);
}
//+------------------------------------------------------------------+
