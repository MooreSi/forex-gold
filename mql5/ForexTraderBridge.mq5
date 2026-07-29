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
input bool   InpShowPanel = true;            // draw the on-chart control panel

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
   string   tplAnchor;       // "unified" / "distributed" -- see BE block in ManageTemplate
   double   tplAnchorPrice;  // grid mode: basePrice shared by every leg; single mode: own fill price
   string   tplTrailMode;    // "off" / "candle" / "step" / "fractal" / "tp"
   string   tplBeMode;       // "entry" / "entry_buffer"
   double   tplBeBufferPts;
   int      tplBeTrigger;    // 1-based TP# that arms breakeven
   bool     tplBeDone;       // breakeven already applied once
   bool     tplCancelPending;
   int      tplGridGroup;    // shared id for sibling grid legs, -1 if none
   bool     tplGroupTpAction;  // see ApplyGroupTpAction
   bool     tplGroupActionDone;
   bool     tplHarvestEnabled;
   double   tplHarvestThreshold;

   // Raw open_trade payload, kept verbatim so ANY tpl_* key can be read
   // later without a struct member or a parse line of its own.
   //
   // Every named tplXxx field above still exists and is still populated at
   // open -- the hot paths read them directly, and rewriting 126 live
   // call sites at once on code that manages real positions is not worth
   // the risk. What this adds is a second, generic route: a template
   // field newly added on the Python side (core_ea_templates.DEFAULTS,
   // forwarded generically by ea_bridge) arrives here automatically and
   // can be consumed with a single TplD/TplI/TplB/TplS call at the point
   // of use -- no new member, no new parse line, and crucially no
   // recompile just to carry the value. Only genuinely NEW behaviour
   // needs MQL5 code; new parameters for existing behaviour are free.
   string   tplCfg;
};

ManagedTrade g_trades[];

//+------------------------------------------------------------------+
//| Generic EA Template config accessors                              |
//|                                                                   |
//| Read a tpl_<key> straight out of the stored payload, falling back  |
//| to `def` when the key is absent -- which is what makes an older EA |
//| build safe against a newer app sending fields it has never heard   |
//| of, and a newer EA safe against an older app that omits them.      |
//+------------------------------------------------------------------+
string TplS(const string cfg, const string key, const string def)
{
   if(cfg == "") return def;
   return JsonGetString(cfg, "tpl_" + key, def);
}

double TplD(const string cfg, const string key, const double def)
{
   if(cfg == "") return def;
   return JsonGetDouble(cfg, "tpl_" + key, def);
}

int TplI(const string cfg, const string key, const int def)
{
   if(cfg == "") return def;
   return (int)JsonGetLong(cfg, "tpl_" + key, (long)def);
}

// Flags cross the wire as 1/0, never as native JSON booleans -- the
// minimal parser here cannot read `true`/`false`, and Python sending a
// bare bool silently evaluated false (confirmed live 2026-07-23, when
// harvest and cancel-pending never fired). ea_bridge coerces on the way
// out; this mirrors that on the way in.
bool TplB(const string cfg, const string key, const bool def)
{
   if(cfg == "") return def;
   return TplI(cfg, key, def ? 1 : 0) != 0;
}

//+------------------------------------------------------------------+
//| Pip -> raw price delta.                                           |
//|                                                                   |
//| MIND THE UNITS. Two conventions coexist here deliberately:        |
//|                                                                   |
//|   *_pts fields (grid_step_pts, be_buffer_pts, trail_dist,         |
//|   InpDefaultTrailStopPts) are RAW PRICE DELTAS -- grid_step_pts    |
//|   = 10.0 means a $10 move. That is this EA's long-standing         |
//|   convention and is unchanged.                                     |
//|                                                                   |
//|   *_pips fields (sl_pips, guard_pips, trail_distance, ...) mirror  |
//|   the copier panel's labels, where 50 means $5.00 -- so they MUST  |
//|   be converted before use. Confirmed two ways: the panel's own     |
//|   arithmetic (SL 50.0 at 0.01 lot = 1oz displays $5.00 risk) and   |
//|   the channel's wording ("TP1 HIT +20 PIPS (4022 TO 4024)" = 2.00  |
//|   of price). Both give 1 pip = 0.10 = 10 * _Point on this XAUUSD   |
//|   feed, where _Point is 0.01.                                      |
//|                                                                   |
//| Getting this wrong scales every stop and target by 10x, so the two |
//| conventions are kept visually distinct by the field NAME suffix    |
//| rather than by memory.                                             |
//+------------------------------------------------------------------+
double PipsToPrice(const double pips)
{
   return pips * 10.0 * _Point;
}

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
   string   tplAnchor;
   double   tplAnchorPrice;
   string   tplTrailMode;
   string   tplBeMode;
   double   tplBeBufferPts;
   int      tplBeTrigger;
   bool     tplCancelPending;
   int      tplGridGroup;    // shared id across this grid's sibling legs
   bool     tplGroupTpAction;  // see ApplyGroupTpAction
   bool     tplHarvestEnabled;
   double   tplHarvestThreshold;
   string   tplCfg;          // raw payload -- see ManagedTrade.tplCfg
};

PendingOrder g_pending[];

// ── On-chart panel state ────────────────────────────────────────────
// Declared here, with the other module globals, because MQL5 requires a
// global to be declared before it is referenced -- HandleOpenTrade and
// HandleSetTemplate both update these, and both appear long before the
// panel's own code further down.
datetime g_panelLastPaint = 0;

#define PNL_PREFIX  "FTB_PNL_"
#define PNL_X       12
#define PNL_Y       22
#define PNL_W       232
#define PNL_ROW     20

string g_panelTemplate = "";       // template name last seen from the app
string g_panelMode     = "single";
string g_panelTpsl     = "on";
string g_panelAnchor   = "unified";
string g_panelTrail    = "off";
int    g_panelBeTrig   = 1;
string g_panelLastSig  = "";       // most recent signal line, for context
bool   g_panelBuilt    = false;


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
   if(type == "set_template") { HandleSetTemplate(line); return; }
   if(type == "place_pending_order") { HandlePlacePendingOrder(line); return; }
   if(type == "restore_pending_order") { HandleRestorePendingOrder(line); return; }
   if(type == "cancel_pending_order") { HandleCancelPendingOrder(line); return; }
   if(type == "set_global_config") { HandleSetGlobalConfig(line); return; }
}

// Python-initiated cancel of a still-resting pending order (2026-07-28) --
// core_pending_order_revalidation.py periodically re-checks each resting
// order against current price action and pulls it here the moment the
// setup that justified it no longer holds, rather than leaving it to fill
// blind or wait out its full expire_minutes. Removes the ticket from
// g_pending[] directly and reports back immediately with the specific
// reason Python sent, instead of relying on CheckPendingOrders()'s own
// next cycle to notice it's gone and report a generic "expired_or_cancelled".
void HandleCancelPendingOrder(const string json)
{
   string trade_id = JsonGetString(json, "trade_id");
   string reason    = JsonGetString(json, "reason", "revalidation_failed");
   ulong ticket = (ulong)JsonGetLong(json, "ticket", 0);
   if(ticket == 0)
   {
      for(int i = 0; i < ArraySize(g_pending); i++)
         if(g_pending[i].trade_id == trade_id) { ticket = g_pending[i].ticket; break; }
   }
   if(ticket == 0)
   {
      Print("[EABridge] cancel_pending_order: no ticket found for trade_id=", trade_id);
      return;
   }
   if(!trade.OrderDelete(ticket))
   {
      Print("[EABridge] cancel_pending_order failed ticket=", ticket,
            " err=", trade.ResultRetcodeDescription());
      return;
   }
   for(int i = ArraySize(g_pending) - 1; i >= 0; i--)
   {
      if(g_pending[i].ticket == ticket)
      {
         int total = ArraySize(g_pending);
         for(int k = i; k < total - 1; k++) g_pending[k] = g_pending[k + 1];
         ArrayResize(g_pending, total - 1);
         break;
      }
   }
   SendJson("{\"type\":\"pending_order_cancelled\",\"trade_id\":\"" + JsonEsc(trade_id) +
            "\",\"reason\":\"" + JsonEsc(reason) + "\"}");
   Print("[EABridge] cancel_pending_order: cancelled ticket=", ticket, " reason=", reason);
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
//+------------------------------------------------------------------+
//| set_template -- apply a template's values to every OPEN trade      |
//| already running under it, without waiting for the next signal.     |
//|                                                                    |
//| Templates normally travel with each open_trade, so an edit made in  |
//| the app reaches the EA only on the NEXT trade. This is the app's    |
//| "Send to EA" button: it re-points the stored config of every live   |
//| trade at the new values, so a mid-session adjustment takes effect   |
//| on positions that are already open.                                 |
//|                                                                    |
//| Deliberately replaces tplCfg wholesale rather than merging fields:  |
//| the payload is a complete template, and a partial merge would let   |
//| a removed field keep its old value invisibly. The named tplXxx      |
//| members are refreshed alongside it so the hot paths that still read |
//| them stay consistent with the generic store.                        |
//+------------------------------------------------------------------+
void HandleSetTemplate(const string json)
{
   string name = JsonGetString(json, "template_name", "");
   int updated = 0;
   for(int i = 0; i < ArraySize(g_trades); i++)
   {
      if(!g_trades[i].isTemplate) continue;
      g_trades[i].tplCfg            = json;
      g_trades[i].tplTpslMode       = JsonGetString(json, "tpl_tpsl_mode", g_trades[i].tplTpslMode);
      g_trades[i].tplAnchor         = JsonGetString(json, "tpl_anchor", g_trades[i].tplAnchor);
      g_trades[i].tplTrailMode      = JsonGetString(json, "tpl_trail_mode", g_trades[i].tplTrailMode);
      g_trades[i].tplBeMode         = JsonGetString(json, "tpl_be_mode", g_trades[i].tplBeMode);
      g_trades[i].tplBeBufferPts    = JsonGetDouble(json, "tpl_be_buffer_pts", g_trades[i].tplBeBufferPts);
      g_trades[i].tplBeTrigger      = (int)JsonGetLong(json, "tpl_be_trigger", g_trades[i].tplBeTrigger);
      g_trades[i].tplCancelPending  = (JsonGetLong(json, "tpl_cancel_pending", g_trades[i].tplCancelPending ? 1 : 0) != 0);
      g_trades[i].tplGroupTpAction  = (JsonGetLong(json, "tpl_group_tp_action", g_trades[i].tplGroupTpAction ? 1 : 0) != 0);
      g_trades[i].tplHarvestEnabled = (JsonGetLong(json, "tpl_harvest_enabled", g_trades[i].tplHarvestEnabled ? 1 : 0) != 0);
      g_trades[i].tplHarvestThreshold = JsonGetDouble(json, "tpl_harvest_threshold", g_trades[i].tplHarvestThreshold);
      updated++;
   }
   for(int i = 0; i < ArraySize(g_pending); i++)
   {
      if(!g_pending[i].isTemplate) continue;
      g_pending[i].tplCfg = json;
      updated++;
   }
   // Mirror into the panel's display state. The panel deliberately holds
   // no authoritative state of its own -- a click asks the app to change
   // something, and this is the reply that actually moves the display, so
   // chart and app can never silently disagree.
   g_panelTemplate = name;
   g_panelMode     = JsonGetString(json, "tpl_mode", g_panelMode);
   g_panelTpsl     = JsonGetString(json, "tpl_tpsl_mode", g_panelTpsl);
   g_panelAnchor   = JsonGetString(json, "tpl_anchor", g_panelAnchor);
   g_panelTrail    = JsonGetString(json, "tpl_trail_mode", g_panelTrail);
   g_panelBeTrig   = (int)JsonGetLong(json, "tpl_be_trigger", g_panelBeTrig);
   if(InpShowPanel) PanelUpdate();

   Print("[EABridge] set_template '", name, "' applied to ", updated, " live item(s)");
}

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
   // fixed_rr joins be_runner in getting a genuine broker TP -- it is the
   // one strategy that is deliberately NOT managed after open at all, so
   // MT5 must hold both the stop and the target itself. Python sends the
   // computed target as tp1 (core_open_trade.py).
   if(strategy == "be_runner" || strategy == "fixed_rr" ||
      (isTemplate && tplTpslModeEarly == "on"))
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
   // Single mode has no siblings to share a reference with, so unified vs
   // distributed can't differ here -- this leg's own fill price either way.
   mt.tplAnchor = isTemplate ? JsonGetString(json, "tpl_anchor", "unified") : "unified";
   mt.tplAnchorPrice = fillPrice;
   mt.tplTrailMode = isTemplate ? JsonGetString(json, "tpl_trail_mode", "off") : "off";
   mt.tplBeMode = isTemplate ? JsonGetString(json, "tpl_be_mode", "entry") : "entry";
   mt.tplBeBufferPts = isTemplate ? JsonGetDouble(json, "tpl_be_buffer_pts", 1.0) : 0.0;
   mt.tplBeTrigger = isTemplate ? (int)JsonGetLong(json, "tpl_be_trigger", 1) : 1;
   mt.tplBeDone = false;
   mt.tplCancelPending = isTemplate && JsonGetLong(json, "tpl_cancel_pending", 0) != 0;
   mt.tplGridGroup = -1; // single-mode template trades are never part of a grid group
   mt.tplGroupTpAction = isTemplate && JsonGetLong(json, "tpl_group_tp_action", 0) != 0;
   mt.tplGroupActionDone = false;
   mt.tplHarvestEnabled = isTemplate && JsonGetLong(json, "tpl_harvest_enabled", 0) != 0;
   mt.tplHarvestThreshold = isTemplate ? JsonGetDouble(json, "tpl_harvest_threshold", 50.0) : 0.0;
   // Keep the whole payload so any tpl_* key the named fields above don't
   // cover can still be read later via TplS/TplD/TplI/TplB.
   mt.tplCfg = isTemplate ? json : "";
   if(isTemplate)
   {
      // Keep the panel current from ordinary trade opens too, not only
      // from an explicit set_template push.
      g_panelTemplate = StringSubstr(strategy, 9);   // strip "template:"
      g_panelMode     = JsonGetString(json, "tpl_mode", g_panelMode);
      g_panelTpsl     = mt.tplTpslMode;
      g_panelAnchor   = mt.tplAnchor;
      g_panelTrail    = mt.tplTrailMode;
      g_panelBeTrig   = mt.tplBeTrigger;
      g_panelLastSig  = direction + " " + DoubleToString(fillPrice, _Digits);
   }

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
      mt.tplAnchor = "unified";
      mt.tplAnchorPrice = fillPrice;
      mt.tplGridGroup = -1;
      mt.tplBeDone = false;
      mt.tplCancelPending = false;
      mt.tplGroupTpAction = false;
      mt.tplGroupActionDone = false;
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
   // Anchor/pending split (2026-07-29) -- the copier's actual structure,
   // observed live: on signal 25202 it opened "C2_LDBD_25202_ANC" at 4026
   // (a MARKET fill, a point outside the zone) alongside
   // "C2_LDBD_25202_PEN" at 4025 (a LIMIT resting at the zone edge). The
   // anchor takes part of the position immediately so a signal that never
   // retraces is not missed entirely; the pending legs wait for a better
   // fill inside the zone.
   //
   // tpl_grid_legs is the pre-split field and still drives the pending
   // count when the newer tpl_pendings is absent, so existing templates
   // behave exactly as before.
   int    legs       = (int)JsonGetLong(json, "tpl_pendings",
                              JsonGetLong(json, "tpl_grid_legs", 3));
   int    anchors    = (int)JsonGetLong(json, "tpl_anchors", 0);
   if(legs < 0) legs = 0;
   if(anchors < 0) anchors = 0;
   double lotAnchor  = JsonGetDouble(json, "tpl_lot_anchor", 0.0);
   double lotPending = JsonGetDouble(json, "tpl_lot_pending", 0.0);
   if(lotAnchor  <= 0.0) lotAnchor  = lots;
   if(lotPending <= 0.0) lotPending = lots;
   // Same convention as every other *_pts field in this EA (trail_dist,
   // InpDefaultTrailStopPts, trail_stop_sl_pts) -- a raw price delta, not a
   // broker _Point-scaled value. See core_ea_templates.py's DEFAULTS.
   double stepPrice = stepPts;

   MqlTick tick;
   SymbolInfoTick(_Symbol, tick);
   double basePrice = (direction == "BUY") ? tick.bid : tick.ask;

   // Zone-spanned staging (2026-07-28). When the signal states its own entry
   // zone (zone_low/zone_high, sent by ea_bridge.py), stage the legs ACROSS
   // that zone instead of stepping grid_step_pts away from the current price.
   // A "BUY LIMITS 4063/4068 AREA" message is itself already a grid
   // instruction, and its SL sits just beyond the zone (4062 here) -- fixed
   // stepping walks the legs straight through that stop (4057/4047/4037) and
   // the broker rejects every one as invalid stops, so the signal silently
   // places nothing at all. Spanning the zone keeps every leg inside the
   // signal's own structure, which is above its SL by construction. Falls
   // back to the original step-based staging whenever no usable zone is sent.
   double zoneLow  = JsonGetDouble(json, "zone_low", 0.0);
   double zoneHigh = JsonGetDouble(json, "zone_high", 0.0);
   bool   useZone  = (zoneLow > 0.0 && zoneHigh > zoneLow);

   // A pending limit must rest on the correct side of the market by at least
   // the broker's stops level, or it is rejected outright.
   double minDist = (double)SymbolInfoInteger(_Symbol, SYMBOL_TRADE_STOPS_LEVEL) * _Point;

   trade.SetExpertMagicNumber(InpMagic);
   int groupId = g_nextGridGroup++;
   int placed = 0;
   // 60min, not the old 240 -- these resting legs have no fill-time
   // re-check at all (this EA doesn't use OnTradeTransaction; every
   // lifecycle event here is polled), so they shouldn't sit on the book
   // any longer than core_limit_order_signal.py's own resting orders do
   // (see that file's _DEFAULT_EXPIRE_MINUTES for the full reasoning).
   // Python never actually sends tpl_grid_expire_minutes today, so this
   // default is the only value in effect.
   string expireMinKey = "tpl_grid_expire_minutes";
   double expireMin = JsonGetDouble(json, expireMinKey, 60.0);
   datetime expiration = TimeCurrent() + (datetime)(expireMin * 60);

   string tplAnchorMode = JsonGetString(json, "tpl_anchor", "unified");

   // ── Anchor leg(s): immediate market fill ──────────────────────────
   for(int a = 1; a <= anchors; a++)
   {
      MqlTick atick;
      if(!SymbolInfoTick(_Symbol, atick)) break;
      string aTradeId = trade_id + "-a" + (string)a;
      string aComment = "ea:" + StringSubstr(trade_id, 0, 10) + "a" + (string)a;
      bool aok = (direction == "BUY")
         ? trade.Buy(lotAnchor, _Symbol, 0.0, sl, 0.0, aComment)
         : trade.Sell(lotAnchor, _Symbol, 0.0, sl, 0.0, aComment);
      if(!aok)
      {
         Print("[EABridge] anchor leg ", a, "/", anchors, " failed: ",
               trade.ResultRetcodeDescription());
         continue;
      }
      double aFill = trade.ResultPrice();
      if(aFill <= 0.0) aFill = (direction == "BUY") ? atick.ask : atick.bid;

      ManagedTrade am;
      am.ticket = trade.ResultOrder();
      am.trade_id = aTradeId;
      am.strategy = strategy;
      am.direction = direction;
      am.entry_price = aFill;
      am.orig_lots = lotAnchor;
      am.trailing_active = false;
      am.last_step = 0;
      am.trail_dist = InpDefaultTrailStopPts;
      for(int i = 0; i < MAX_TPS; i++)
      {
         string k = "tp" + (string)(i + 1);
         am.hasTp[i] = JsonHasKey(json, k);
         am.tp[i] = am.hasTp[i] ? JsonGetDouble(json, k) : 0.0;
         am.triggered[i] = false;
         am.pcts[i] = JsonGetDouble(json, "pct" + (string)(i + 1), 0.0);
      }
      am.beAtPos = -1;
      am.trailMode = "";
      am.closeFullOnLast = true;
      am.isTemplate = true;
      am.tplTpslMode = JsonGetString(json, "tpl_tpsl_mode", "on");
      am.tplAnchor = JsonGetString(json, "tpl_anchor", "unified");
      am.tplAnchorPrice = basePrice;
      am.tplTrailMode = JsonGetString(json, "tpl_trail_mode", "off");
      am.tplBeMode = JsonGetString(json, "tpl_be_mode", "entry");
      am.tplBeBufferPts = JsonGetDouble(json, "tpl_be_buffer_pts", 1.0);
      am.tplBeTrigger = (int)JsonGetLong(json, "tpl_be_trigger", 1);
      am.tplBeDone = false;
      am.tplCancelPending = (JsonGetLong(json, "tpl_cancel_pending", 0) != 0);
      am.tplGridGroup = groupId;
      am.tplGroupTpAction = (JsonGetLong(json, "tpl_group_tp_action", 0) != 0);
      am.tplGroupActionDone = false;
      am.tplHarvestEnabled = (JsonGetLong(json, "tpl_harvest_enabled", 0) != 0);
      am.tplHarvestThreshold = JsonGetDouble(json, "tpl_harvest_threshold", 50.0);
      am.tplCfg = json;

      int sz = ArraySize(g_trades);
      ArrayResize(g_trades, sz + 1);
      g_trades[sz] = am;
      placed++;

      SendJson("{\"type\":\"trade_opened\",\"trade_id\":\"" + JsonEsc(aTradeId) +
               "\",\"ticket\":" + (string)am.ticket +
               ",\"fill_price\":" + DoubleToString(aFill, _Digits) + "}");
      Print("[EABridge] anchor leg ", a, "/", anchors, " filled @ ",
            DoubleToString(aFill, _Digits), " lots=", lotAnchor);
   }

   // ── Pending leg(s): resting limits inside the zone ────────────────
   for(int leg = 1; leg <= legs; leg++)
   {
      double legPrice;
      if(useZone)
      {
         // Leg 1 sits at the edge of the zone price reaches FIRST (the top
         // for a BUY, the bottom for a SELL); the last leg sits at the far
         // edge, with the rest spread evenly between. A single-leg grid just
         // takes that near edge.
         double span = zoneHigh - zoneLow;
         double frac = (legs > 1) ? ((double)(leg - 1) / (double)(legs - 1)) : 0.0;
         legPrice = (direction == "BUY") ? (zoneHigh - span * frac)
                                         : (zoneLow  + span * frac);
      }
      else
      {
         legPrice = (direction == "BUY")
            ? basePrice - stepPrice * leg
            : basePrice + stepPrice * leg;
      }
      legPrice = NormalizeDouble(legPrice, _Digits);

      // Skip rather than let the broker reject: price may already be partway
      // through the zone by the time this runs (the zone-wait path fires the
      // moment price ENTERS the zone), which leaves the near legs on the
      // wrong side of the market. Any leg beyond the signal's own stop is
      // skipped too -- that ordering can only be a mistake, never an entry.
      bool wrongSide = (direction == "BUY") ? (legPrice > basePrice - minDist)
                                            : (legPrice < basePrice + minDist);
      bool beyondSl  = (sl > 0.0) && ((direction == "BUY") ? (legPrice <= sl)
                                                           : (legPrice >= sl));
      if(wrongSide || beyondSl)
      {
         Print("[EABridge] grid leg ", leg, "/", legs, " skipped @ ", legPrice,
               (wrongSide ? " (wrong side of market, base=" + DoubleToString(basePrice, _Digits) + ")"
                          : " (beyond SL " + DoubleToString(sl, _Digits) + ")"));
         continue;
      }

      string legTradeId = trade_id + "-g" + (string)leg;
      string comment = "ea:" + StringSubstr(trade_id, 0, 10) + "g" + (string)leg;

      bool ok = (direction == "BUY")
         ? trade.BuyLimit(lotPending, legPrice, _Symbol, sl, 0.0, ORDER_TIME_SPECIFIED, expiration, comment)
         : trade.SellLimit(lotPending, legPrice, _Symbol, sl, 0.0, ORDER_TIME_SPECIFIED, expiration, comment);

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
      p.lots = lotPending;
      for(int i = 0; i < MAX_TPS; i++)
      {
         // Pending legs may carry their OWN ladder (tpl_tp_pen<n>_pips),
         // falling back to the anchor ladder when absent.
         //
         // Which price the offset measures FROM is decided by tpl_anchor,
         // the same flag that already governs breakeven:
         //   "unified"     -> basePrice, so every leg shares one TP PRICE
         //                    and a leg filled deeper in the zone earns
         //                    MORE points reaching it. This is what the
         //                    copier actually does -- signal 25246 put
         //                    both its legs on TP 4051 (13pt for the
         //                    anchor at 4038, 14pt for the pending at
         //                    4037); 25204 likewise shared TP 4019.
         //   "distributed" -> each leg's own price, i.e. equal DISTANCE.
         string key = "tp" + (string)(i + 1);
         double penPips = JsonGetDouble(json, "tpl_tp_pen" + (string)(i + 1) + "_pips", 0.0);
         if(penPips > 0.0)
         {
            double tpBase = (tplAnchorMode == "distributed") ? legPrice : basePrice;
            double off = PipsToPrice(penPips);
            p.hasTp[i] = true;
            p.tp[i] = (direction == "BUY") ? tpBase + off : tpBase - off;
         }
         else
         {
            p.hasTp[i] = JsonHasKey(json, key);
            p.tp[i] = p.hasTp[i] ? JsonGetDouble(json, key) : 0.0;
         }
         double penPct = JsonGetDouble(json, "tpl_tp_pen" + (string)(i + 1) + "_pct", 0.0);
         p.pcts[i] = (penPct > 0.0) ? penPct / 100.0
                                    : JsonGetDouble(json, "pct" + (string)(i + 1), 0.0);
      }
      p.beAtPos = -1;
      p.trailMode = "";
      p.closeFullOnLast = true;
      p.isTemplate = true;
      p.tplTpslMode = JsonGetString(json, "tpl_tpsl_mode", "on");
      // Anchor (2026-07-28) -- "unified": every leg's breakeven target is
      // THIS SAME basePrice (the price the whole grid was staged from),
      // not each leg's own fill price, so a group of legs filled at
      // different levels still converges on one shared breakeven when
      // triggered. "distributed" (the only behaviour before this): each
      // leg's own entry_price, exactly as ManageTemplate/ApplyGroupTpAction
      // already did. See core_ea_templates.py's DEFAULTS and the EA
      // Templates UI's own tooltip for this field's stated meaning.
      p.tplAnchor = JsonGetString(json, "tpl_anchor", "unified");
      p.tplAnchorPrice = basePrice;
      p.tplTrailMode = JsonGetString(json, "tpl_trail_mode", "off");
      p.tplBeMode = JsonGetString(json, "tpl_be_mode", "entry");
      p.tplBeBufferPts = JsonGetDouble(json, "tpl_be_buffer_pts", 1.0);
      p.tplBeTrigger = (int)JsonGetLong(json, "tpl_be_trigger", 1);
      p.tplCancelPending = JsonGetLong(json, "tpl_cancel_pending", 0) != 0;
      p.tplGridGroup = groupId;
      p.tplGroupTpAction = JsonGetLong(json, "tpl_group_tp_action", 0) != 0;
      p.tplHarvestEnabled = JsonGetLong(json, "tpl_harvest_enabled", 0) != 0;
      p.tplHarvestThreshold = JsonGetDouble(json, "tpl_harvest_threshold", 50.0);
      p.tplCfg = json;

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

// Cancels every other still-resting leg in a filled/TP'd grid group. Only
// deletes the broker-side order here -- doesn't touch g_pending[] itself,
// so it's safe to call from inside CheckPendingOrders()'s own backward
// loop (a filled sibling) or ManageTemplate's TP-clear check (a TP'd
// sibling, see ApplyGroupTpAction below); the next tick's pass over
// g_pending[] finds each cancelled ticket already gone and cleans it up
// via the existing "expired_or_cancelled" path, no double-bookkeeping
// needed. Moved above ManageTemplate (2026-07-28) so ApplyGroupTpAction
// can call it -- unchanged otherwise.
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

// EA Template, Group TP Action (2026-07-28) -- when a grid template has
// this enabled, the FIRST TP any leg of the group clears (ManageTemplate
// below) treats it as validation of the whole basket: every other
// still-resting sibling gets pulled (same mechanism Cancel Pending already
// uses on a fill, just triggered by a TP hit here instead) and every other
// already-live sibling's SL moves to breakeven -- caps risk on the rest of
// the basket instead of leaving unfilled legs to fill blind or open
// siblings sitting at their original, wider stop. Each sibling's own
// Anchor setting decides the breakeven reference: "distributed" is ITS OWN
// entry price (legs are staggered by grid_step_pts, so this differs per
// leg); "unified" is tplAnchorPrice, the single basePrice the whole group
// was staged from, so every leg converges on the same breakeven price.
void ApplyGroupTpAction(ManagedTrade &t)
{
   CancelGridSiblings(t.tplGridGroup, t.ticket);
   for(int i = 0; i < ArraySize(g_trades); i++)
   {
      if(g_trades[i].tplGridGroup != t.tplGridGroup) continue;
      if(g_trades[i].ticket == t.ticket) continue;
      double refPrice = (g_trades[i].tplAnchor == "unified")
         ? g_trades[i].tplAnchorPrice : g_trades[i].entry_price;
      double beSl = refPrice;
      if(g_trades[i].tplBeMode == "entry_buffer")
      {
         double sign = (g_trades[i].direction == "BUY") ? 1.0 : -1.0;
         beSl = refPrice + sign * g_trades[i].tplBeBufferPts;
      }
      if(MoveSl(g_trades[i], beSl, "group_tp_action"))
         g_trades[i].tplBeDone = true;
   }
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

            // Group TP Action -- fires once, on the FIRST TP this leg clears
            // (see ApplyGroupTpAction above). Independent of whether this
            // same level goes on to partial/full-close below.
            if(t.tplGroupTpAction && !t.tplGroupActionDone && t.tplGridGroup >= 0)
            {
               ApplyGroupTpAction(t);
               t.tplGroupActionDone = true;
            }

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
   // Anchor: "unified" uses tplAnchorPrice (the single price the whole grid
   // group was staged from, shared by every leg) so a multi-leg fill still
   // converges on one common breakeven; "distributed" (the default, and
   // the only behaviour before this field was wired in) uses this leg's
   // own entry_price, same as every other strategy's breakeven already did.
   if(!t.tplBeDone)
   {
      int beIdx = t.tplBeTrigger - 1;
      if(beIdx >= 0 && beIdx < MAX_TPS && TpCleared(t, beIdx, tick))
      {
         double refPrice = (t.tplAnchor == "unified") ? t.tplAnchorPrice : t.entry_price;
         double beSl = refPrice;
         if(t.tplBeMode == "entry_buffer")
         {
            double sign = (t.direction == "BUY") ? 1.0 : -1.0;
            beSl = refPrice + sign * t.tplBeBufferPts;
         }
         if(MoveSl(t, beSl, "template_be", beIdx)) t.tplBeDone = true;
      }
   }

   // ── Trail ────────────────────────────────────────────────────────────
   // Geometry comes from the template when supplied, falling back to the
   // EA input for older payloads that predate these fields. This is the
   // generic-config path in use: trail_distance/activation/padding were
   // added on the Python side and are consumed here with no struct member
   // and no parse line of their own.
   double trailAct  = PipsToPrice(TplD(t.tplCfg, "trail_activation", 0.0));
   double trailPad  = PipsToPrice(TplD(t.tplCfg, "trail_padding", 0.0));
   double trailDist = TplD(t.tplCfg, "trail_distance", 0.0) > 0.0
                      ? PipsToPrice(TplD(t.tplCfg, "trail_distance", 0.0))
                      : InpDefaultTrailStopPts;

   // trail_activation: leave the stop alone until the trade is this far in
   // profit. 0 keeps the previous always-on behaviour.
   bool trailArmed = true;
   if(trailAct > 0.0)
   {
      double inProfit = (t.direction == "BUY") ? (tick.bid - t.entry_price)
                                               : (t.entry_price - tick.ask);
      trailArmed = (inProfit >= trailAct);
   }

   if(t.tplTrailMode == "step" && trailArmed)
   {
      double dist = trailDist + trailPad;
      double newSl = (t.direction == "BUY") ? tick.bid - dist : tick.ask + dist;
      MoveSl(t, newSl, "template_trail_step");
   }
   else if(t.tplTrailMode == "candle" && trailArmed)
   {
      double newSl = CandleTrailLevel(t);
      if(newSl != 0.0) MoveSl(t, newSl, "template_trail_candle");
   }
   else if(t.tplTrailMode == "fractal" && trailArmed)
   {
      double newSl = FractalTrailLevel(t);
      if(newSl != 0.0) MoveSl(t, newSl, "template_trail_fractal");
   }
   else if(t.tplTrailMode == "tp" && trailArmed)
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
   // fixed_rr is intentionally unmanaged: its stop AND target are both
   // real broker orders, so there is nothing to poll. This branch must
   // exist -- without it the default below (ManageScaleOut) would
   // partial-close it against tp[] levels it does not use.
   else if(t.strategy == "fixed_rr") { return; }
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
         // EA Template grid legs are tracked in g_pending under a per-leg id
         // ("<original trade_id>-g<N>", set by HandleOpenTemplateGrid) so
         // CancelGridSiblings can tell them apart while resting. Python's
         // vantage_simulated_trades row, though, only ever exists under the
         // un-suffixed original id (_promote_grid_leg_fill's UPDATE targets
         // that, not the leg id) -- every later lifecycle message for this
         // now-live position (tp_hit/sl_moved/trade_closed) must therefore
         // report the stripped id, or ea_bridge.py's _fetch_trade() finds no
         // row, logs "unknown trade_id", and silently drops the event. This
         // left a filled/closed grid leg's trade permanently stuck showing
         // "open" in the app (confirmed live: Test Template, grid mode) --
         // manual closes were simply the easiest way to notice it, since
         // ManageTemplate()'s own partial-close ladder can mask the same
         // gap for TP/SL-triggered closes.
         int _gridSuffixPos = StringFind(g_pending[i].trade_id, "-g");
         mt.trade_id = (_gridSuffixPos >= 0)
            ? StringSubstr(g_pending[i].trade_id, 0, _gridSuffixPos)
            : g_pending[i].trade_id;
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
         mt.tplCfg      = g_pending[i].tplCfg;
         mt.tplAnchor = g_pending[i].tplAnchor;
         mt.tplAnchorPrice = g_pending[i].tplAnchorPrice;
         mt.tplTrailMode = g_pending[i].tplTrailMode;
         mt.tplBeMode = g_pending[i].tplBeMode;
         mt.tplBeBufferPts = g_pending[i].tplBeBufferPts;
         mt.tplBeTrigger = g_pending[i].tplBeTrigger;
         mt.tplBeDone = false;
         mt.tplCancelPending = g_pending[i].tplCancelPending;
         mt.tplGridGroup = g_pending[i].tplGridGroup;
         mt.tplGroupTpAction = g_pending[i].tplGroupTpAction;
         mt.tplGroupActionDone = false;
         mt.tplHarvestEnabled = g_pending[i].tplHarvestEnabled;
         mt.tplHarvestThreshold = g_pending[i].tplHarvestThreshold;

         int n = ArraySize(g_trades);
         ArrayResize(g_trades, n + 1);
         g_trades[n] = mt;

         // EA Template grid, tpsl_mode "on" -- give the position a real
         // broker-side TP now that it's live, matching what HandleOpenTrade
         // already does for be_runner/single-mode template trades at open
         // time. BuyLimit/SellLimit above always place the resting order
         // with tp=0.0 (a grid leg has no single TP to quote until we know
         // which leg actually fills), so "on" mode has to be applied here,
         // retroactively, once fill_price/ticket are known -- without this,
         // "on" silently behaved exactly like "stealth" (internal-only, no
         // visible broker target) for every grid-mode template.
         if(mt.isTemplate && mt.tplTpslMode == "on")
         {
            double gridBrokerTp = 0.0;
            for(int k = MAX_TPS - 1; k >= 0; k--)
            {
               if(mt.hasTp[k]) { gridBrokerTp = mt.tp[k]; break; }
            }
            if(gridBrokerTp > 0.0 && PositionSelectByTicket(ticket))
               trade.PositionModify(ticket, PositionGetDouble(POSITION_SL), gridBrokerTp);
         }

         // Deliberately the raw (possibly "-g<N>"-suffixed) pending id, NOT
         // mt.trade_id above -- ea_bridge.py's _on_pending_order_filled uses
         // the suffix itself to detect a grid-leg fill and route it to
         // _promote_grid_leg_fill (core_ea_templates.py's grid-leg-fill
         // promotion path). mt.trade_id is already stripped for every
         // message from here on, once this trade is a normal tracked
         // position.
         SendJson("{\"type\":\"pending_order_filled\",\"trade_id\":\"" + JsonEsc(g_pending[i].trade_id) +
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
//+------------------------------------------------------------------+
//| ON-CHART CONTROL PANEL                                            |
//|                                                                   |
//| A compact status/control surface on the chart, so the live         |
//| template can be read and the high-frequency switches flipped at    |
//| the terminal instead of only from the app.                         |
//|                                                                   |
//| SCOPE, deliberately. This shows state and carries the toggles and  |
//| emergency actions -- not the full 60-field editor. The TP grids    |
//| (10 levels x pips/% x anchor/pending = 40 numeric cells) belong in |
//| the app, where they are a real form; reproducing them as MQL5      |
//| chart objects would be large, fragile, and worse to use. What      |
//| genuinely benefits from being on the chart is what you reach for   |
//| WHILE watching price: mode, trail, breakeven, and close/cancel.    |
//|                                                                   |
//| TWO-WAY SYNC. A click sends panel_action to the app, which mutates |
//| the saved template and pushes it back via set_template -- so the   |
//| chart never holds authoritative state of its own. The app remains  |
//| the single source of truth; the panel is a view plus a remote      |
//| control. That ordering matters: if the panel owned state, an app   |
//| edit and a chart edit could silently diverge.                      |
//+------------------------------------------------------------------+

void PnlLabel(const string name, const int x, const int y, const string text,
              const color clr, const int size = 8, const string font = "Arial")
{
   string obj = PNL_PREFIX + name;
   if(ObjectFind(0, obj) < 0)
   {
      ObjectCreate(0, obj, OBJ_LABEL, 0, 0, 0);
      ObjectSetInteger(0, obj, OBJPROP_CORNER, CORNER_LEFT_UPPER);
      ObjectSetInteger(0, obj, OBJPROP_SELECTABLE, false);
      ObjectSetInteger(0, obj, OBJPROP_HIDDEN, true);
      ObjectSetInteger(0, obj, OBJPROP_BACK, false);
   }
   ObjectSetInteger(0, obj, OBJPROP_XDISTANCE, x);
   ObjectSetInteger(0, obj, OBJPROP_YDISTANCE, y);
   ObjectSetInteger(0, obj, OBJPROP_COLOR, clr);
   ObjectSetInteger(0, obj, OBJPROP_FONTSIZE, size);
   ObjectSetString(0, obj, OBJPROP_FONT, font);
   ObjectSetString(0, obj, OBJPROP_TEXT, text);
}

void PnlButton(const string name, const int x, const int y, const int w,
               const string text, const color bg, const color fg = clrWhite)
{
   string obj = PNL_PREFIX + name;
   if(ObjectFind(0, obj) < 0)
   {
      ObjectCreate(0, obj, OBJ_BUTTON, 0, 0, 0);
      ObjectSetInteger(0, obj, OBJPROP_CORNER, CORNER_LEFT_UPPER);
      ObjectSetInteger(0, obj, OBJPROP_SELECTABLE, false);
      ObjectSetInteger(0, obj, OBJPROP_HIDDEN, true);
      ObjectSetInteger(0, obj, OBJPROP_FONTSIZE, 7);
   }
   ObjectSetInteger(0, obj, OBJPROP_XDISTANCE, x);
   ObjectSetInteger(0, obj, OBJPROP_YDISTANCE, y);
   ObjectSetInteger(0, obj, OBJPROP_XSIZE, w);
   ObjectSetInteger(0, obj, OBJPROP_YSIZE, 16);
   ObjectSetInteger(0, obj, OBJPROP_BGCOLOR, bg);
   ObjectSetInteger(0, obj, OBJPROP_COLOR, fg);
   ObjectSetInteger(0, obj, OBJPROP_BORDER_COLOR, clrDimGray);
   ObjectSetInteger(0, obj, OBJPROP_STATE, false);
   ObjectSetString(0, obj, OBJPROP_TEXT, text);
}

void PanelDestroy()
{
   ObjectsDeleteAll(0, PNL_PREFIX);
   g_panelBuilt = false;
   ChartRedraw(0);
}

void PanelBuild()
{
   string bg = PNL_PREFIX + "BG";
   if(ObjectFind(0, bg) < 0)
   {
      ObjectCreate(0, bg, OBJ_RECTANGLE_LABEL, 0, 0, 0);
      ObjectSetInteger(0, bg, OBJPROP_CORNER, CORNER_LEFT_UPPER);
      ObjectSetInteger(0, bg, OBJPROP_SELECTABLE, false);
      ObjectSetInteger(0, bg, OBJPROP_HIDDEN, true);
      ObjectSetInteger(0, bg, OBJPROP_BACK, false);
   }
   ObjectSetInteger(0, bg, OBJPROP_XDISTANCE, PNL_X - 6);
   ObjectSetInteger(0, bg, OBJPROP_YDISTANCE, PNL_Y - 6);
   ObjectSetInteger(0, bg, OBJPROP_XSIZE, PNL_W);
   ObjectSetInteger(0, bg, OBJPROP_YSIZE, PNL_ROW * 9 + 18);
   ObjectSetInteger(0, bg, OBJPROP_BGCOLOR, C'22,26,34');
   ObjectSetInteger(0, bg, OBJPROP_BORDER_TYPE, BORDER_FLAT);
   ObjectSetInteger(0, bg, OBJPROP_COLOR, clrDimGray);
   g_panelBuilt = true;
}

void PanelUpdate()
{
   if(!g_panelBuilt) PanelBuild();

   int y = PNL_Y;
   PnlLabel("title", PNL_X, y, "FOREX TRADER  ·  BRIDGE", C'255,209,71', 9, "Arial Bold");
   y += PNL_ROW;

   bool linked = (g_socket != INVALID_HANDLE);
   PnlLabel("link", PNL_X, y,
            (linked ? "● LINKED" : "○ NO LINK") + "   " + _Symbol,
            linked ? clrLimeGreen : clrTomato, 8);
   y += PNL_ROW;

   PnlLabel("tpl", PNL_X, y,
            "Template: " + (g_panelTemplate == "" ? "(none)" : g_panelTemplate),
            clrGainsboro, 8);
   y += PNL_ROW;

   int openTrades = ArraySize(g_trades);
   int openPend   = ArraySize(g_pending);
   double flt = 0.0;
   for(int i = 0; i < PositionsTotal(); i++)
      if(PositionGetTicket(i) > 0) flt += PositionGetDouble(POSITION_PROFIT);
   PnlLabel("stat", PNL_X, y,
            StringFormat("Open %d   Pending %d   P/L %.2f", openTrades, openPend, flt),
            flt >= 0 ? clrLimeGreen : clrTomato, 8);
   y += PNL_ROW;

   PnlLabel("sig", PNL_X, y,
            "Sig: " + (g_panelLastSig == "" ? "-" : g_panelLastSig), clrSilver, 7);
   y += PNL_ROW;

   // Toggle rows -- each click round-trips through the app.
   PnlButton("mode", PNL_X, y, 70,
             g_panelMode == "grid" ? "GRID" : "SINGLE",
             g_panelMode == "grid" ? C'196,160,0' : C'55,62,74');
   PnlButton("tpsl", PNL_X + 74, y, 70,
             "TP/SL " + (g_panelTpsl == "off" ? "OFF" : (g_panelTpsl == "stealth" ? "STL" : "ON")),
             g_panelTpsl == "off" ? C'85,45,45' : C'0,110,110');
   PnlButton("anchor", PNL_X + 148, y, 70,
             g_panelAnchor == "unified" ? "UNIFIED" : "DISTRIB",
             C'190,110,20');
   y += PNL_ROW;

   PnlButton("trail", PNL_X, y, 70,
             "TR " + (g_panelTrail == "off" ? "OFF" : g_panelTrail),
             g_panelTrail == "off" ? C'55,62,74' : C'110,40,150');
   PnlButton("be", PNL_X + 74, y, 70,
             "BE TP" + (string)g_panelBeTrig, C'30,70,160');
   PnlButton("refresh", PNL_X + 148, y, 70, "REFRESH", C'55,62,74');
   y += PNL_ROW + 2;

   PnlButton("cancel", PNL_X, y, 106, "CANCEL LIMITS", C'150,110,0');
   PnlButton("closeall", PNL_X + 110, y, 108, "CLOSE ALL", C'150,30,30');

   ChartRedraw(0);
}

// A click never mutates local state directly -- it asks the app to, and the
// app's set_template reply is what actually moves the panel. One authority,
// so a chart click and an app edit cannot silently disagree.
void PanelSendAction(const string action, const string value)
{
   SendJson("{\"type\":\"panel_action\",\"action\":\"" + JsonEsc(action) +
            "\",\"value\":\"" + JsonEsc(value) +
            "\",\"template\":\"" + JsonEsc(g_panelTemplate) + "\"}");
}

void OnChartEvent(const int id, const long &lparam, const double &dparam, const string &sparam)
{
   if(id != CHARTEVENT_OBJECT_CLICK) return;
   if(StringFind(sparam, PNL_PREFIX) != 0) return;
   string what = StringSubstr(sparam, StringLen(PNL_PREFIX));
   ObjectSetInteger(0, sparam, OBJPROP_STATE, false);   // buttons are momentary

   if(what == "mode")
      PanelSendAction("mode", g_panelMode == "grid" ? "single" : "grid");
   else if(what == "tpsl")
      PanelSendAction("tpsl_mode",
         g_panelTpsl == "on" ? "stealth" : (g_panelTpsl == "stealth" ? "off" : "on"));
   else if(what == "anchor")
      PanelSendAction("anchor", g_panelAnchor == "unified" ? "distributed" : "unified");
   else if(what == "trail")
   {
      string nxt = "off";
      if(g_panelTrail == "off")          nxt = "tp";
      else if(g_panelTrail == "tp")      nxt = "step";
      else if(g_panelTrail == "step")    nxt = "candle";
      else if(g_panelTrail == "candle")  nxt = "fractal";
      PanelSendAction("trail_mode", nxt);
   }
   else if(what == "be")
      PanelSendAction("be_trigger", (string)((g_panelBeTrig % 10) + 1));
   else if(what == "refresh")
      PanelSendAction("refresh", "");
   else if(what == "cancel")
   {
      // Cancels only orders this EA placed (magic-filtered) -- it must not
      // touch anything the user or another EA left on the book.
      int n = 0;
      for(int i = OrdersTotal() - 1; i >= 0; i--)
      {
         ulong tk = OrderGetTicket(i);
         if(tk == 0) continue;
         if(OrderGetInteger(ORDER_MAGIC) != (long)InpMagic) continue;
         if(trade.OrderDelete(tk)) n++;
      }
      Print("[EABridge][Panel] cancelled ", n, " pending order(s)");
      PanelUpdate();
   }
   else if(what == "closeall")
   {
      int n = 0;
      for(int i = PositionsTotal() - 1; i >= 0; i--)
      {
         ulong tk = PositionGetTicket(i);
         if(tk == 0) continue;
         if(PositionGetInteger(POSITION_MAGIC) != (long)InpMagic) continue;
         if(trade.PositionClose(tk)) n++;
      }
      Print("[EABridge][Panel] closed ", n, " position(s)");
      PanelUpdate();
   }
}

int OnInit()
{
   EventSetMillisecondTimer(200);
   EnsureConnected();
   if(InpShowPanel) PanelUpdate();
   return(INIT_SUCCEEDED);
}

void OnDeinit(const int reason)
{
   EventKillTimer();
   if(g_socket != INVALID_HANDLE) SocketClose(g_socket);
   PanelDestroy();
}

void OnTimer()
{
   PollSocket();
   DiagHeartbeat();
   // Panel repaint is throttled to ~1s: OnTimer runs at 200ms, and redrawing
   // the chart five times a second to move a P/L figure is wasted work.
   if(InpShowPanel && TimeCurrent() != g_panelLastPaint)
   {
      g_panelLastPaint = TimeCurrent();
      PanelUpdate();
   }
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
