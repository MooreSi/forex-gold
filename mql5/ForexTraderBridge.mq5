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
#property version   "1.05"
#property strict

// ── Version handshake (2026-08-05) ────────────────────────────────────────
// Sent to Python in the "hello" below. Python parses this exact #define out
// of the repo's own copy of this file (ea_bridge._expected_ea_version) and
// compares the two, because the repo source and the terminal's compiled .ex5
// are completely unlinked — the failure documented at the top of
// tools/deploy_ea.sh, where a day of correct fixes was spent against a build
// from three weeks earlier that nothing reported as stale.
//
// Bump this on every change to the wire protocol or to management behaviour,
// and keep it identical to #property version above (MQL won't let a #define
// stand in for the literal there, so the two are duplicated by necessity).
#define EA_VERSION "1.05"

#include <Trade\Trade.mqh>

input string InpHost      = "127.0.0.1";
input int    InpPort      = 9111;  // this checkout's isolated EA-bridge port -- see backend/src/config/__init__.py's ea_bridge_port

// ── Port candidates (2026-08-07) ──────────────────────────────────────────
// InpPort above is only the *default*. MetaTrader stores the input's value
// inside the chart file and a persisted value always wins over a recompiled
// default -- so a chart restored after a terminal crash brings back whatever
// port was saved the last time that chart was written to disk, which can be
// days old. That is exactly how a crash on 2026-08-07 left this EA retrying
// port 9101 every 2.3s while the app listened on 9111, for four hours, with
// nothing in either log admitting it.
//
// So the port is treated as a guess, not a fact: on repeated failure the EA
// walks these candidates until Python actually answers. Python listens on
// all of them for the same reason (ea_bridge._LEGACY_PORTS).
#define EA_PORT_PRIMARY  9111   // current app default
#define EA_PORT_FALLBACK 9101   // what this EA shipped with before 2026-08-05
input ulong  InpMagic     = 20260706;
input double InpDefaultTrailStopPts = 5.0;   // used by trail_stop if the open_trade message omits trail_dist
input double InpConservativeTrailPts = 3.0;  // used by conservative/scalp_runner if omitted
input bool   InpShowPanel = true;            // draw the on-chart control panel

CTrade trade;
int    g_socket = INVALID_HANDLE;
bool   g_connected = false;
string g_recvBuffer = "";
// ── Link timers run on GetTickCount64(), never TimeCurrent() (2026-08-07) ──
// TimeCurrent() is the time of the LAST QUOTE RECEIVED, not a clock. It stops
// advancing the moment the market stops ticking, so on TimeCurrent() the
// heartbeat below simply stopped every weekend and in the daily gap between
// the New York close and the Asian open: "TimeCurrent() - g_lastPingSent >= 2"
// can never come true when TimeCurrent() is frozen. The socket stayed open and
// this EA was fine, but Python saw nothing arrive inside its 8s window and
// reported the EA offline for the whole of every closed session.
//
// GetTickCount64() is milliseconds since the machine booted: monotonic,
// quote-independent, and 64-bit so it does not wrap the way GetTickCount()
// does at ~49.7 days. Hence these are ulong milliseconds, not datetime
// seconds -- the thresholds below are in ms accordingly. TimeCurrent()
// remains correct everywhere else in this file (order expiry, VWAP windows,
// panel timestamps), all of which genuinely mean market time.
ulong  g_lastPingSent = 0;
ulong  g_lastRecv = 0;
// Socket link state. g_connected only means SocketConnect returned true --
// under Wine that happens even when nothing is listening, which is why the
// EA used to print "connected" on every one of its retries. g_linkConfirmed
// is the honest one: Python has actually sent us something back.
int      g_ports[];              // candidate ports, in the order they're tried
int      g_portIdx = 0;
int      g_activePort = 0;
bool     g_linkConfirmed = false;
ulong    g_lastLinkDownLog = 0;   // 0 = never logged; see NextPort
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
   double   tplOrigSlDist;      // |entry - the SL actually sent at open|, for
                                 // use_emergency_sl's backstop distance
   bool     tplCancelLevelDone; // cancel_pending_level's cancel already fired once

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
   double   tplOrigSlDist;   // see ManagedTrade.tplOrigSlDist
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

// ── Panel geometry ──────────────────────────────────────────────────────
// One 4-column grid per side. Every x/y below is derived from these, so a
// layout change is a define change, not a hunt through 90 call sites.
#define PNL_X       10          // left edge of the whole panel
#define PNL_Y       20          // top edge
#define PNL_GAP     3           // gap between cells
#define PNL_ROW     22          // standard cell height
#define PNL_LW      440         // left (copier) column width
#define PNL_RW      430         // right (dashboard) column width
#define PNL_MID     8           // gap between the two columns
#define PNL_PAD     6           // padding inside the backing rectangle
#define PNL_RX      (PNL_X + PNL_LW + PNL_MID)   // right column's left edge

// ── Palette ─────────────────────────────────────────────────────────────
#define CLR_BG      C'18,21,26'
#define CLR_CELL    C'32,38,46'
#define CLR_CELL2   C'25,30,37'
#define CLR_BORDER  C'52,60,72'
#define CLR_TEXT    C'214,222,232'
#define CLR_DIM     C'126,138,152'
#define CLR_CYAN    C'79,195,247'
#define CLR_GREEN   C'0,230,118'
#define CLR_GREENBG C'13,59,35'
#define CLR_RED     C'255,82,82'
#define CLR_REDBG   C'66,26,26'
#define CLR_YELLOW  C'255,213,79'
#define CLR_AMBER   C'255,143,0'
#define CLR_GRIDY   C'255,230,0'
#define CLR_TEAL    C'0,131,143'
#define CLR_BLUE    C'21,101,192'
#define CLR_STEEL   C'91,141,184'
#define CLR_OFF     C'43,49,56'

bool   g_panelBuilt    = false;

// ── Template state (the left column) ────────────────────────────────────
// g_panelCfg is the last complete set_template payload. Every numeric field
// and toggle on the left column is read out of it with TplD/TplI/TplB/TplS
// at paint time rather than being unpacked into a named global -- so a field
// added to core_ea_templates.DEFAULTS shows up here with one line in the
// layout and no new global, no new parse, no protocol change.
string g_panelCfg      = "";
string g_panelTemplate = "";       // template name last seen from the app
string g_panelLastSig  = "";       // most recent signal line, for context

// ── Channel roster (panel_context) ──────────────────────────────────────
#define PNL_MAX_CH  3
string g_chName[PNL_MAX_CH];
string g_chId[PNL_MAX_CH];
string g_chTemplate[PNL_MAX_CH];
bool   g_chActive[PNL_MAX_CH];
int    g_chCount   = 0;
int    g_chSel     = 0;
bool   g_tgActive  = false;
bool   g_tgCmd     = false;

// ── Signal dashboard (panel_signal) ─────────────────────────────────────
// Everything here is computed app-side from forex_trader/reversal_engine and
// pushed; the EA never recomputes an ICT pattern. What the EA DOES compute
// locally is the rest of the right column -- bid/ask/spread/P&L, the M5-D1
// trend row, ATR, session countdown, VWAP -- because those need a ticking
// clock and this chart's own series (see core_panel_signal.py's docstring).
string g_sigBias      = "NEUTRAL";
string g_sigScanner   = "WAITING...";
string g_sigHeadline  = "";
int    g_sigBuyConf   = 0;
int    g_sigSellConf  = 0;
string g_sigBuyGrade  = "-";
string g_sigSellGrade = "-";
bool   g_critBiasAlign = false;
bool   g_critFvg       = false;
bool   g_critSweep     = false;
bool   g_critDisp      = false;
bool   g_critOb        = false;
bool   g_critKz        = false;
bool   g_critVwapBuy   = false;
bool   g_critVwapSell  = false;
double g_lvlPrice[6];
string g_lvlKind[6];
string g_lvlDir[6];
int    g_lvlCount = 0;

// ── System log strip ────────────────────────────────────────────────────
// Ring buffer, newest first. Fed from both sides: PanelLog() for what this
// EA does, and the app's panel_log for what happens out of the terminal's
// sight. One strip, so the ordering between them is real.
#define PNL_LOG_N   5
string g_logLine[PNL_LOG_N];
int    g_logCount = 0;

// ── Local view state ────────────────────────────────────────────────────
// These four are the only panel state the EA owns, and deliberately so:
// none of them changes how a trade is placed or managed. They are which tab
// is showing, which half of the TP ladder is on screen, whether clicking a
// market button is armed, and whether alerts beep. Everything that DOES
// affect behaviour lives in the app and arrives via set_template.
string g_panelTab    = "signals";   // "trades" / "levels" / "signals"
string g_tpType      = "anchor";    // which TP ladder the grid is editing
bool   g_panelManual = false;       // arms the Entry Management buttons
bool   g_panelSound  = true;
double g_limitOffset = 41.0;        // pips from price for SELL/BUY LIMIT

// Edit boxes must not be overwritten under a user's fingers. PnlEdit records
// the text it last wrote; on the next paint, if the box no longer holds that
// text, the user is mid-edit and the repaint skips it. Without this a 1Hz
// repaint erases every keystroke before Enter.
#define PNL_MAX_EDIT   40
#define PNL_EDIT_GRACE_S 60      // how long an uncommitted edit is protected
string   g_editName[PNL_MAX_EDIT];
string   g_editLast[PNL_MAX_EDIT];
datetime g_editDivergedAt[PNL_MAX_EDIT];
int      g_editCount = 0;

// Cached iMA handles for the M5-D1 trend row -- created once, not per paint.
ENUM_TIMEFRAMES g_tfRow[6] = {PERIOD_M5, PERIOD_M15, PERIOD_M30,
                              PERIOD_H1, PERIOD_H4, PERIOD_D1};
string g_tfName[6] = {"M5", "M15", "M30", "H1", "H4", "D1"};
int    g_tfMa[6]   = {-1, -1, -1, -1, -1, -1};
int    g_atrHandle = -1;


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
// Build the candidate port list: the chart's own input first (it is right in
// every normal case), then the current app default, then the legacy one.
// Duplicates dropped so the common case is a single-entry list and the EA
// keeps hammering the one correct port.
void BuildPortList()
{
   int cand[3];
   cand[0] = InpPort;
   cand[1] = EA_PORT_PRIMARY;
   cand[2] = EA_PORT_FALLBACK;
   ArrayResize(g_ports, 0);
   for(int i = 0; i < 3; i++)
   {
      if(cand[i] <= 0) continue;
      bool dup = false;
      for(int j = 0; j < ArraySize(g_ports); j++)
         if(g_ports[j] == cand[i]) { dup = true; break; }
      if(dup) continue;
      int n = ArraySize(g_ports);
      ArrayResize(g_ports, n + 1);
      g_ports[n] = cand[i];
   }
   g_portIdx = 0;
}

// Move on to the next candidate port and say so — throttled to once every
// 30s, because the retry itself runs every couple of seconds and the point
// of this line is to be findable in the log, not to fill it.
void NextPort(const string reason)
{
   int n = ArraySize(g_ports);
   if(n > 1) g_portIdx = (g_portIdx + 1) % n;
   // The != 0 guard matters now these are milliseconds-since-boot rather than
   // epoch seconds: a terminal started less than 30s ago has a tick count
   // below the throttle window, which would have swallowed the first and most
   // useful "NO LINK" line of the session.
   if(g_lastLinkDownLog != 0 && GetTickCount64() - g_lastLinkDownLog < 30000) return;
   g_lastLinkDownLog = GetTickCount64();
   Print("[EABridge] NO LINK to Python on ", InpHost, ":", g_activePort,
         " (", reason, ") — retrying on port ", g_ports[g_portIdx],
         ". Trades are being managed by the app, not this EA.");
}

void EnsureConnected()
{
   if(g_connected) return;
   if(g_socket != INVALID_HANDLE) { SocketClose(g_socket); g_socket = INVALID_HANDLE; }
   if(ArraySize(g_ports) == 0) BuildPortList();
   g_activePort = g_ports[g_portIdx];
   g_socket = SocketCreate();
   if(g_socket == INVALID_HANDLE) return;
   if(!SocketConnect(g_socket, InpHost, g_activePort, 2000))
   {
      SocketClose(g_socket);
      g_socket = INVALID_HANDLE;
      NextPort("connect refused");
      return;
   }
   g_connected = true;
   // Not confirmed yet: SocketConnect returning true is not proof anything is
   // on the other end. PollSocket promotes this once Python replies.
   g_linkConfirmed = false;
   g_lastRecv = GetTickCount64();
   // __DATETIME__ is stamped by MetaEditor at compile time in local time, and
   // Python compares it against the repo source file's own local mtime. That
   // is only meaningful because the two always run on the same machine (see
   // the header note) — never send this to a remote comparator.
   SendJson("{\"type\":\"hello\",\"account\":" + (string)AccountInfoInteger(ACCOUNT_LOGIN) +
            ",\"symbol\":\"" + _Symbol + "\"" +
            ",\"ea_version\":\"" + EA_VERSION + "\"" +
            ",\"compiled\":\"" + TimeToString(__DATETIME__, TIME_DATE | TIME_SECONDS) + "\"" +
            ",\"mql_build\":" + (string)__MQL5BUILD__ +
            ",\"terminal_build\":" + (string)TerminalInfoInteger(TERMINAL_BUILD) + "}");
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
      // Failing on the very first write means the port was never really
      // open — try the next candidate rather than this one forever.
      if(!g_linkConfirmed) NextPort("send failed");
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
         g_lastRecv = GetTickCount64();
         if(!g_linkConfirmed)
         {
            // First byte back from Python — only now is the link real.
            g_linkConfirmed = true;
            g_lastLinkDownLog = 0;
            Print("[EABridge] connected to ", InpHost, ":", g_activePort,
                  " (v", EA_VERSION, ", compiled ",
                  TimeToString(__DATETIME__, TIME_DATE | TIME_SECONDS), ")");
            if(g_activePort != InpPort)
               Print("[EABridge] NOTE: reached the app on port ", g_activePort,
                     " but this chart's InpPort input says ", InpPort,
                     " — set InpPort=", g_activePort,
                     " in the EA's properties and save the chart, or the next "
                     "terminal restart starts this search over.");
         }
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
   // Milliseconds off GetTickCount64(), so this keeps beating through a closed
   // market -- see the note on g_lastPingSent's declaration.
   if(GetTickCount64() - g_lastPingSent >= 2000)
   {
      SendJson("{\"type\":\"ping\"}");
      g_lastPingSent = GetTickCount64();
   }
   if(g_connected && GetTickCount64() - g_lastRecv > 10000)
   {
      g_connected = false;
      SocketClose(g_socket);
      g_socket = INVALID_HANDLE;
      if(g_linkConfirmed)
      {
         // A link that worked and went quiet: the app is restarting or has
         // stalled. Same port, it will be back.
         Print("[EABridge] no data from Python in 10s — reconnecting on port ",
               g_activePort);
      }
      else
      {
         // Never got a single byte on this port. Something accepted the
         // connection (or Wine pretended it did) but it is not the app.
         NextPort("no reply in 10s");
      }
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
   if(type == "restore_trade") { HandleRestoreTrade(line); return; }
   if(type == "cancel_pending_order") { HandleCancelPendingOrder(line); return; }
   if(type == "set_global_config") { HandleSetGlobalConfig(line); return; }
   // Panel feeds -- display only, see the panel section at the bottom.
   if(type == "panel_context") { HandlePanelContext(line); return; }
   if(type == "panel_signal")  { HandlePanelSignal(line); return; }
   if(type == "panel_log")     { PanelLog(JsonGetString(line, "text")); return; }
}

//+------------------------------------------------------------------+
//| panel_context -- the CH tabs and link lamps.                      |
//|                                                                    |
//| Pure mirror of app state: the terminal never writes a channel name |
//| or id back. Selecting a tab only asks the app which template that  |
//| channel uses, which then arrives as an ordinary set_template.      |
//+------------------------------------------------------------------+
void HandlePanelContext(const string json)
{
   g_chCount = (int)JsonGetLong(json, "channel_count", 0);
   if(g_chCount > PNL_MAX_CH) g_chCount = PNL_MAX_CH;
   for(int i = 0; i < PNL_MAX_CH; i++)
   {
      string p = "ch" + (string)(i + 1) + "_";
      g_chName[i]     = JsonGetString(json, p + "name", "");
      g_chId[i]       = JsonGetString(json, p + "id", "");
      g_chTemplate[i] = JsonGetString(json, p + "template", "");
      g_chActive[i]   = JsonGetLong(json, p + "active", 0) != 0;
   }
   g_chSel    = (int)JsonGetLong(json, "active_slot", g_chSel);
   if(g_chSel < 0 || g_chSel >= PNL_MAX_CH) g_chSel = 0;
   g_tgActive = JsonGetLong(json, "tg_active", 0) != 0;
   g_tgCmd    = JsonGetLong(json, "tg_cmd", 0) != 0;
   if(InpShowPanel) PanelUpdate();
}

//+------------------------------------------------------------------+
//| panel_signal -- the ICT half of the right column.                  |
//+------------------------------------------------------------------+
void HandlePanelSignal(const string json)
{
   g_sigBias      = JsonGetString(json, "bias", "NEUTRAL");
   g_sigScanner   = JsonGetString(json, "scanner", "WAITING...");
   g_sigHeadline  = JsonGetString(json, "headline", "");
   g_sigBuyConf   = (int)JsonGetLong(json, "buy_conf", 0);
   g_sigSellConf  = (int)JsonGetLong(json, "sell_conf", 0);
   g_sigBuyGrade  = JsonGetString(json, "buy_grade", "-");
   g_sigSellGrade = JsonGetString(json, "sell_grade", "-");
   g_critBiasAlign = JsonGetLong(json, "bias_align", 0) != 0;
   g_critFvg       = JsonGetLong(json, "fvg", 0) != 0;
   g_critSweep     = JsonGetLong(json, "sweep", 0) != 0;
   g_critDisp      = JsonGetLong(json, "displacement", 0) != 0;
   g_critOb        = JsonGetLong(json, "order_block", 0) != 0;
   g_critKz        = JsonGetLong(json, "killzone", 0) != 0;
   g_critVwapBuy   = JsonGetLong(json, "vwap_buy_ok", 0) != 0;
   g_critVwapSell  = JsonGetLong(json, "vwap_sell_ok", 0) != 0;

   g_lvlCount = (int)JsonGetLong(json, "level_count", 0);
   if(g_lvlCount > 6) g_lvlCount = 6;
   for(int i = 0; i < 6; i++)
   {
      string p = "lvl" + (string)(i + 1) + "_";
      g_lvlPrice[i] = JsonGetDouble(json, p + "price", 0.0);
      g_lvlKind[i]  = JsonGetString(json, p + "kind", "");
      g_lvlDir[i]   = JsonGetString(json, p + "dir", "");
   }
   // No repaint here: OnTimer's ~1s repaint picks this up, and a push
   // arriving every 3s does not need its own ChartRedraw.
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
   // chart and app can never silently disagree. Storing the payload whole
   // (rather than unpacking named fields) is what lets the panel show any
   // template field without an EA change: see g_panelCfg's declaration.
   g_panelTemplate = name;
   g_panelCfg      = json;
   if(InpShowPanel) PanelUpdate();

   Print("[EABridge] set_template '", name, "' applied to ", updated, " live item(s)");
}

// An exact-match lookup (FindManagedByTradeId) silently found nothing for
// any grid leg, because a leg's OWN trade_id is the signal's base id plus
// "-a<n>"/"-g<n>" (HandleOpenTemplateGrid) -- Python's own follow-up
// correction (core_instant_followup.py's IME flow, update_trade) sends
// only the base id, since a signal can have several legs and the
// correction applies to all of them. Confirmed live 2026-08-04, trade
// 8fc78ea9 ("GD VIP - Grid"): the anchor filled on a provisional
// open with no real TP ladder yet, the real TP1-7 arrived a minute later
// via update_trade, and FindManagedByTradeId("8fc78ea9-...") never matched
// "8fc78ea9-...-a1" -- so the EA kept managing the position with
// essentially nothing to check, meaning TP2/TP3 clearing did nothing even
// though price actually reached them.
bool TradeIdMatchesBase(const string full, const string base)
{
   if(full == base) return true;
   int blen = StringLen(base);
   if(StringLen(full) <= blen + 2) return false;
   if(StringSubstr(full, 0, blen) != base) return false;
   string sep = StringSubstr(full, blen, 2);
   return (sep == "-a" || sep == "-g");
}

void HandleUpdateTrade(const string json)
{
   string trade_id = JsonGetString(json, "trade_id");
   int matched = 0;

   for(int idx = 0; idx < ArraySize(g_trades); idx++)
   {
      if(!TradeIdMatchesBase(g_trades[idx].trade_id, trade_id)) continue;
      matched++;
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
      // Corrected STOP (2026-08-04). Python skips its own modify_order
      // while this EA is healthy so nothing races the SL progression, which
      // meant a corrected stop previously reached neither the broker nor
      // here -- an IME anchor could sit on its wide provisional stop
      // forever while the app reported the tight corrected one (ticket
      // 1705825177: 123 pips real vs 68 reported).
      //
      // NOT routed through MoveSl: that only ever permits a tighter stop,
      // which is right for BE/trailing but wrong here. This is a
      // correction, and a correction may legitimately move the stop either
      // way. The broker's own stops-level is still respected.
      if(JsonHasKey(json, "stop_loss"))
      {
         double newSl = JsonGetDouble(json, "stop_loss");
         if(newSl > 0.0 && PositionSelectByTicket(g_trades[idx].ticket))
         {
            double curSl = PositionGetDouble(POSITION_SL);
            if(MathAbs(curSl - newSl) > _Point)
            {
               double curTp = PositionGetDouble(POSITION_TP);
               if(trade.PositionModify(g_trades[idx].ticket, newSl, curTp))
               {
                  // Keep the emergency-SL backstop measuring from the
                  // corrected distance, not the discarded provisional one.
                  g_trades[idx].tplOrigSlDist = MathAbs(g_trades[idx].entry_price - newSl);
                  Print("[EABridge] update_trade SL corrected: ticket=", g_trades[idx].ticket,
                        " ", DoubleToString(curSl, _Digits), " -> ", DoubleToString(newSl, _Digits));
               }
               else
               {
                  Print("[EABridge] update_trade SL correction REJECTED ticket=",
                        g_trades[idx].ticket, " -> ", DoubleToString(newSl, _Digits),
                        " err=", trade.ResultRetcodeDescription());
               }
            }
         }
      }

      Print("[EABridge] update_trade applied: trade_id=", g_trades[idx].trade_id,
            " ticket=", g_trades[idx].ticket, " fields_updated=", updated);
   }

   // A resting (not yet filled) grid leg has no ticket/price to manage yet,
   // but should still fill with the corrected TP ladder rather than the
   // stale one it was staged with.
   for(int pidx = 0; pidx < ArraySize(g_pending); pidx++)
   {
      if(!TradeIdMatchesBase(g_pending[pidx].trade_id, trade_id)) continue;
      matched++;
      for(int i = 0; i < MAX_TPS; i++)
      {
         string key = "tp" + (string)(i + 1);
         if(JsonHasKey(json, key))
         {
            g_pending[pidx].tp[i]    = JsonGetDouble(json, key);
            g_pending[pidx].hasTp[i] = true;
         }
      }
      Print("[EABridge] update_trade applied to resting leg: trade_id=", g_pending[pidx].trade_id,
            " ticket=", g_pending[pidx].ticket);
   }

   if(matched == 0)
      Print("[EABridge] update_trade: unknown/untracked trade_id=", trade_id);
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
   //
   // tpl_close_full_on_last=false is the exception: that flag exists
   // specifically so the last defined level closes only its OWN pct and
   // leaves a genuine runner (see ManageTemplate()'s own partial-close
   // loop) -- but a native broker TP sitting at that exact price defeats
   // the whole point. A resting broker order executes the instant price
   // touches it, server-side; the EA's own tick-based partial close can
   // only react on its next OnTick, after the broker has already gone --
   // so the native "backstop" doesn't just backstop, it always wins the
   // race and flattens the entire remaining position right there, same as
   // if close_full_on_last had been left on. Confirmed live 2026-08-04,
   // ticket 1702805808 ("Conservative Trial", close_full_on_last off,
   // tp4_pct=20%): TP1-TP3 closed correctly via the EA's own partial-close
   // (blank-comment deals), but the whole remaining 0.05 lot closed in one
   // shot at TP4 with broker comment "[tp 4064.19]" -- the native order,
   // not the intended 20% partial. Templates with close_full_on_last=false
   // skip the native TP entirely here and rely purely on ManageTemplate()
   // for every level including the last, same as "stealth" mode already
   // does for a different reason.
   double brokerTp = 0.0;
   string tplTpslModeEarly = isTemplate ? JsonGetString(json, "tpl_tpsl_mode", "on") : "";
   bool tplCloseFullOnLastEarly = isTemplate
      ? (JsonGetLong(json, "tpl_close_full_on_last", 1) != 0) : true;
   // fixed_rr joins be_runner in getting a genuine broker TP -- it is the
   // one strategy that is deliberately NOT managed after open at all, so
   // MT5 must hold both the stop and the target itself. Python sends the
   // computed target as tp1 (core_open_trade.py).
   if(strategy == "be_runner" || strategy == "fixed_rr" ||
      (isTemplate && tplTpslModeEarly == "on" && tplCloseFullOnLastEarly))
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
   // slippage (2026-08-04 -- existed as a template field with no
   // implementation): CTrade's own default deviation applied unconditionally
   // otherwise. 0/absent leaves that default untouched.
   if(isTemplate)
   {
      int _tplSlip = (int)JsonGetLong(json, "tpl_slippage", 0);
      if(_tplSlip > 0) trade.SetDeviationInPoints(_tplSlip);
   }
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
      // A template's absolute tp{n} was computed by Python from price at
      // signal time, which can be stale by the time this order actually
      // fills (the whole ack round-trip, or -- for a signal staged ahead
      // of price reaching it -- much longer). Recomputing from THIS
      // trade's own fillPrice when the template sent pips keeps the
      // target genuinely N pips of reward from the real entry instead of
      // from whatever price Python happened to see earlier -- the same
      // principle the grid's pending legs already use via their own
      // legPrice. Non-template callers (isTemplate false, no
      // tpl_tp{n}_pips on the wire) are unaffected: they fall straight
      // through to the absolute value, unchanged.
      double _anchorPips = isTemplate ? JsonGetDouble(json, "tpl_tp" + (string)(i + 1) + "_pips", 0.0) : 0.0;
      if(_anchorPips > 0.0)
      {
         double _off = PipsToPrice(_anchorPips);
         mt.hasTp[i] = true;
         mt.tp[i] = (direction == "BUY") ? fillPrice + _off : fillPrice - _off;
      }
      else
      {
         mt.hasTp[i] = JsonHasKey(json, key);
         mt.tp[i] = mt.hasTp[i] ? JsonGetDouble(json, key) : 0.0;
      }
      mt.triggered[i] = false;
      mt.pcts[i] = JsonGetDouble(json, "pct" + (string)(i + 1), 0.0);
   }
   mt.beAtPos = JsonHasKey(json, "be_at_pos") ? (int)JsonGetLong(json, "be_at_pos") : -1;
   mt.trailMode = JsonHasKey(json, "trail_mode") ? JsonGetString(json, "trail_mode") : "";
   // Every non-template open_trade() caller wants the original (always
   // close everything on the last level) behaviour. A template can opt out
   // via tpl_close_full_on_last=0 -- core_ea_templates.py's
   // close_full_on_last -- to leave a runner past its last defined TP
   // level instead of ManageTemplate() flattening it there.
   mt.closeFullOnLast = isTemplate
      ? (JsonGetLong(json, "tpl_close_full_on_last", 1) != 0) : true;

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
   mt.tplCancelLevelDone = false;
   mt.tplHarvestEnabled = isTemplate && JsonGetLong(json, "tpl_harvest_enabled", 0) != 0;
   mt.tplHarvestThreshold = isTemplate ? JsonGetDouble(json, "tpl_harvest_threshold", 50.0) : 0.0;
   mt.tplOrigSlDist = isTemplate ? MathAbs(fillPrice - sl) : 0.0;
   // Keep the whole payload so any tpl_* key the named fields above don't
   // cover can still be read later via TplS/TplD/TplI/TplB.
   mt.tplCfg = isTemplate ? json : "";
   if(isTemplate)
   {
      // Keep the panel current from ordinary trade opens too, not only
      // from an explicit set_template push.
      g_panelTemplate = StringSubstr(strategy, 9);   // strip "template:"
      g_panelCfg      = json;
      g_panelLastSig  = direction + " " + DoubleToString(fillPrice, _Digits);
      PanelLog(direction + " " + DoubleToString(fillPrice, _Digits) +
               " (" + g_panelTemplate + ")");
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
// Re-adopt an already-open POSITION after this EA restarted (2026-08-04).
// g_trades[] is pure in-memory state, so every recompile/terminal restart
// silently dropped management of every live template trade: no partial
// closes, no breakeven, no trailing, and no trade_closed message, which
// left the app's own row stuck 'open' forever. That became actively
// dangerous once close_full_on_last=false legitimately started leaving
// positions with NO broker-side TP -- an orphan then has nothing to close
// it at all. Python pushes every open EA-managed row on "hello"; see
// ea_bridge.restore_trade().
void HandleRestoreTrade(const string json)
{
   ulong  ticket   = (ulong)JsonGetLong(json, "ticket");
   string trade_id = JsonGetString(json, "trade_id");

   if(!PositionSelectByTicket(ticket))
   {
      // Closed at the broker while this EA was away. Say so rather than
      // staying silent: the app still believes it is open, and this is the
      // only moment either side can notice.
      Print("[EABridge] restore_trade: ticket ", ticket, " is no longer open at the broker");
      SendJson("{\"type\":\"trade_closed\",\"trade_id\":\"" + JsonEsc(trade_id) +
               "\",\"ticket\":" + (string)ticket +
               ",\"reason\":\"closed_while_disconnected\"}");
      return;
   }
   if(FindManagedByTicket(ticket) >= 0)
      return;   // already tracked (duplicate hello / double restore) -- no-op

   string direction = JsonGetString(json, "direction");
   string strategy  = JsonGetString(json, "strategy");
   bool   isTemplate = (StringFind(strategy, "template:") == 0);
   double entry     = JsonGetDouble(json, "entry_price", 0.0);
   double origLots  = JsonGetDouble(json, "orig_lots", 0.0);
   double remaining = JsonGetDouble(json, "remaining_lots", origLots);
   if(entry <= 0.0)    entry    = PositionGetDouble(POSITION_PRICE_OPEN);
   if(origLots <= 0.0) origLots = PositionGetDouble(POSITION_VOLUME);

   ManagedTrade mt;
   mt.ticket = ticket;
   mt.trade_id = trade_id;
   mt.strategy = strategy;
   mt.direction = direction;
   mt.entry_price = entry;
   mt.orig_lots = origLots;
   mt.trailing_active = false;
   mt.last_step = 0;
   mt.trail_dist = InpDefaultTrailStopPts;

   for(int i = 0; i < MAX_TPS; i++)
   {
      string key = "tp" + (string)(i + 1);
      mt.hasTp[i] = JsonHasKey(json, key);
      mt.tp[i] = mt.hasTp[i] ? JsonGetDouble(json, key) : 0.0;
      mt.triggered[i] = false;
      mt.pcts[i] = JsonGetDouble(json, "pct" + (string)(i + 1), 0.0);
   }

   // Work out how much of the ladder ALREADY fired, from how much of the
   // original position is already gone. Without this, restoring would mark
   // every level untriggered and re-run partial closes that have already
   // happened -- closing the position down far faster than the ladder ever
   // intended. Compares against cumulative pcts, which is the same basis
   // DoPartialClose sizes from, so the two cannot disagree.
   double closedFrac = (origLots > 0.0) ? (origLots - remaining) / origLots : 0.0;
   if(closedFrac > 0.0001)
   {
      double cum = 0.0;
      int compactPos = 0;
      for(int i = 0; i < MAX_TPS; i++)
      {
         if(!mt.hasTp[i]) continue;
         cum += mt.pcts[compactPos];
         // 1e-4 tolerance: broker volume rounding means the arithmetic
         // rarely lands exactly on the boundary.
         if(cum <= closedFrac + 0.0001)
            mt.triggered[i] = true;
         compactPos++;
      }
   }

   mt.beAtPos = JsonHasKey(json, "be_at_pos") ? (int)JsonGetLong(json, "be_at_pos") : -1;
   mt.trailMode = JsonHasKey(json, "trail_mode") ? JsonGetString(json, "trail_mode") : "";
   mt.closeFullOnLast = isTemplate
      ? (JsonGetLong(json, "tpl_close_full_on_last", 1) != 0) : true;
   mt.isTemplate = isTemplate;
   mt.tplTpslMode = isTemplate ? JsonGetString(json, "tpl_tpsl_mode", "on") : "";
   mt.tplAnchor = isTemplate ? JsonGetString(json, "tpl_anchor", "unified") : "unified";
   mt.tplAnchorPrice = entry;
   mt.tplTrailMode = isTemplate ? JsonGetString(json, "tpl_trail_mode", "off") : "off";
   mt.tplBeMode = isTemplate ? JsonGetString(json, "tpl_be_mode", "entry") : "entry";
   mt.tplBeBufferPts = isTemplate ? JsonGetDouble(json, "tpl_be_buffer_pts", 1.0) : 0.0;
   mt.tplBeTrigger = isTemplate ? (int)JsonGetLong(json, "tpl_be_trigger", 1) : 1;
   // A stop already at/past entry means breakeven has been applied, so
   // don't re-apply (and don't let it look un-done to the BE block).
   double _liveSl = PositionGetDouble(POSITION_SL);
   mt.tplBeDone = (_liveSl > 0.0) &&
                  ((direction == "BUY") ? (_liveSl >= entry) : (_liveSl <= entry));
   mt.tplCancelPending = isTemplate && JsonGetLong(json, "tpl_cancel_pending", 0) != 0;
   mt.tplGridGroup = -1;   // sibling grouping is not recoverable after a restart
   mt.tplGroupTpAction = isTemplate && JsonGetLong(json, "tpl_group_tp_action", 0) != 0;
   mt.tplGroupActionDone = false;
   mt.tplCancelLevelDone = false;
   mt.tplHarvestEnabled = isTemplate && JsonGetLong(json, "tpl_harvest_enabled", 0) != 0;
   mt.tplHarvestThreshold = isTemplate ? JsonGetDouble(json, "tpl_harvest_threshold", 50.0) : 0.0;
   mt.tplOrigSlDist = (_liveSl > 0.0) ? MathAbs(entry - _liveSl) : 0.0;
   mt.tplCfg = isTemplate ? json : "";

   int n = ArraySize(g_trades);
   ArrayResize(g_trades, n + 1);
   g_trades[n] = mt;

   int already = 0;
   for(int i = 0; i < MAX_TPS; i++) if(mt.triggered[i]) already++;
   Print("[EABridge] restore_trade: re-adopted ticket=", ticket, " strategy=", strategy,
         " entry=", DoubleToString(entry, _Digits), " remaining=", remaining,
         " of ", origLots, " (", already, " TP level(s) treated as already hit)");
}

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
   // pending_mode (2026-08-04): "step" opts out of zone-spanning entirely
   // and falls through to the grid_step_pts staging below, which is what
   // the reference copier does (its LADDER STEP). Chosen per template
   // because zone mode, while it honours the levels the signal actually
   // named, silently drops any leg that lands on the wrong side of the
   // market -- and since the anchor now always fires at market regardless
   // of the zone, price is often already past it, so the anchor fills and
   // the resting leg never appears. Step mode is measured away from the
   // current base price, so it cannot land on the wrong side.
   if(TplS(json, "pending_mode", "zone") == "step") useZone = false;

   // A pending limit must rest on the correct side of the market by at least
   // the broker's stops level, or it is rejected outright.
   double minDist = (double)SymbolInfoInteger(_Symbol, SYMBOL_TRADE_STOPS_LEVEL) * _Point;

   trade.SetExpertMagicNumber(InpMagic);
   // slippage -- see HandleOpenTrade's own copy of this for the full note.
   int _tplGridSlip = (int)JsonGetLong(json, "tpl_slippage", 0);
   if(_tplGridSlip > 0) trade.SetDeviationInPoints(_tplGridSlip);
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
         // "distributed" mode's whole point is a leg's own fill price
         // deciding its own target -- the pending legs below already do
         // this via legPrice. The anchor never did: it always trusted
         // Python's absolute tp{n}, computed from price at signal
         // creation, which for a grid staged ahead of price reaching the
         // zone can be well stale by the time this market fill actually
         // happens. Confirmed live 2026-08-04, ticket 1701202501: a SELL
         // anchor filled 4062.48 against a wired "TP1" of 4065.30 --
         // above entry, an instant loss the moment price nominally
         // "hit" it. Recompute from aFill instead whenever the template
         // sent pips; "unified" mode (or no pips sent, e.g. the
         // Telegram-anchor-ladder path) keeps the absolute wire value
         // unchanged, same as before.
         double _anchorPips = JsonGetDouble(json, "tpl_tp" + (string)(i + 1) + "_pips", 0.0);
         if(tplAnchorMode == "distributed" && _anchorPips > 0.0)
         {
            double _off = PipsToPrice(_anchorPips);
            am.hasTp[i] = true;
            am.tp[i] = (direction == "BUY") ? aFill + _off : aFill - _off;
         }
         else
         {
            am.hasTp[i] = JsonHasKey(json, k);
            am.tp[i] = am.hasTp[i] ? JsonGetDouble(json, k) : 0.0;
         }
         am.triggered[i] = false;
         am.pcts[i] = JsonGetDouble(json, "pct" + (string)(i + 1), 0.0);
      }
      am.beAtPos = -1;
      am.trailMode = "";
      am.closeFullOnLast = (JsonGetLong(json, "tpl_close_full_on_last", 1) != 0);
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
      am.tplCancelLevelDone = false;
      am.tplHarvestEnabled = (JsonGetLong(json, "tpl_harvest_enabled", 0) != 0);
      am.tplHarvestThreshold = JsonGetDouble(json, "tpl_harvest_threshold", 50.0);
      am.tplOrigSlDist = MathAbs(aFill - sl);
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
   int rescued = 0;   // zone legs pulled back to the market side (see below)
   for(int leg = 1; leg <= legs; leg++)
   {
      double span = zoneHigh - zoneLow;
      double legPrice;
      if(useZone)
      {
         // Leg 1 sits at the edge of the zone price reaches FIRST (the top
         // for a BUY, the bottom for a SELL); the last leg sits at the far
         // edge, with the rest spread evenly between.
         //
         // A SINGLE-leg grid takes the zone MIDPOINT, not that near edge
         // (2026-08-05). The near edge is by definition the point price is
         // closest to, so a lone leg staged there either fills instantly or
         // -- when price has already crossed it -- is dropped as wrong_side,
         // which is the failure this template shape kept hitting. The
         // midpoint is what the reference copier stages for its one pending
         // leg, and it is the only placement a 1-leg grid can make that is
         // genuinely "inside" the zone rather than on its boundary.
         double frac = (legs > 1) ? ((double)(leg - 1) / (double)(legs - 1)) : 0.5;
         legPrice = (direction == "BUY") ? (zoneHigh - span * frac)
                                         : (zoneLow  + span * frac);
      }
      else
      {
         legPrice = (direction == "BUY")
            ? basePrice - stepPrice * leg
            : basePrice + stepPrice * leg;
      }
      // gold_half_pip_anchor (2026-08-04 -- existed as a template field with
      // no implementation): some gold feeds quote at half-pip granularity
      // (0.05 here, half of the 0.10 pip this EA otherwise uses -- see
      // PipsToPrice's own note on the pip/point convention); anchor a
      // staged leg's price to that grid instead of the raw computed value
      // when the template asks for it.
      if(JsonGetLong(json, "tpl_gold_half_pip_anchor", 0) != 0)
         legPrice = MathRound(legPrice / (5.0 * _Point)) * (5.0 * _Point);
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

      // Wrong-side rescue, zone mode only (2026-08-05). Price moving partway
      // into the zone before the legs are staged is normal -- the anchor
      // fires at market on arrival regardless of where price sits -- and
      // dropping the leg for it means the grid silently degenerates into a
      // single market order, the exact behaviour this template was chosen to
      // avoid. When the zone still has room on the correct side of the
      // market, pull the leg back to just past the broker's stops level
      // instead of discarding it. Successive rescued legs are spaced one
      // slot deeper so they don't all stack on one price.
      //
      // Deliberately NOT applied when the whole zone is on the wrong side
      // (price has left it entirely): a limit there would need to be a stop
      // order, which is a different instruction than the signal gave. Nor to
      // step mode, whose prices are measured from basePrice and so cannot be
      // wrong side to begin with.
      if(wrongSide && !beyondSl && useZone)
      {
         double standoff = MathMax(minDist, 5 * _Point);
         double spacing  = MathMax(span / (double)legs, standoff);
         double clamped  = (direction == "BUY")
            ? (basePrice - standoff - spacing * rescued)
            : (basePrice + standoff + spacing * rescued);
         clamped = NormalizeDouble(clamped, _Digits);
         bool inZone      = (clamped >= zoneLow && clamped <= zoneHigh);
         bool clampPastSl = (sl > 0.0) && ((direction == "BUY") ? (clamped <= sl)
                                                                : (clamped >= sl));
         if(inZone && !clampPastSl)
         {
            Print("[EABridge] grid leg ", leg, "/", legs, " pulled back ",
                  DoubleToString(legPrice, _Digits), " -> ",
                  DoubleToString(clamped, _Digits),
                  " (price already inside zone, base=",
                  DoubleToString(basePrice, _Digits), ")");
            legPrice  = clamped;
            wrongSide = false;
            rescued++;
         }
      }

      if(wrongSide || beyondSl)
      {
         string skipReason = wrongSide ? "wrong_side" : "beyond_sl";
         Print("[EABridge] grid leg ", leg, "/", legs, " skipped @ ", legPrice,
               (wrongSide ? " (wrong side of market, base=" + DoubleToString(basePrice, _Digits) + ")"
                          : " (beyond SL " + DoubleToString(sl, _Digits) + ")"));
         // Report it (2026-08-04). This Print alone went only to the
         // terminal's own Experts log, which nothing in the app reads --
         // so a grid that placed an anchor and silently lost its resting
         // leg looked identical to one that never had a leg configured.
         // That invisibility is precisely what let the zone-mode skip go
         // unnoticed. Python logs this as a warning; an unknown type is a
         // harmless debug no-op on older app builds.
         SendJson("{\"type\":\"grid_leg_skipped\",\"trade_id\":\"" + JsonEsc(trade_id) +
                  "\",\"leg\":" + (string)leg + ",\"of\":" + (string)legs +
                  ",\"price\":" + DoubleToString(legPrice, _Digits) +
                  ",\"base\":" + DoubleToString(basePrice, _Digits) +
                  ",\"reason\":\"" + skipReason + "\"}");
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
      p.closeFullOnLast = (JsonGetLong(json, "tpl_close_full_on_last", 1) != 0);
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
      p.tplOrigSlDist = MathAbs(legPrice - sl);
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
   //
   // legs_placed (2026-08-03): how many legs actually went out (anchors +
   // pending, after the wrong-side/beyond-SL skips above) -- the ONE piece
   // of grid-shape Python otherwise has no way to learn (only this EA's own
   // g_pending[]/g_trades[] know it). Stored on the placeholder row as
   // grid_legs_total so ea_bridge._on_grid_leg_cancelled can tell "one
   // sibling of several cancelled, others may still fill" apart from "every
   // leg this grid ever had is now gone with none filled" -- without this,
   // that second case left the row open at $0 forever. See
   // core_template_placeholder_repair.py's docstring for the adopt/close
   // paths this complements.
   SendJson("{\"type\":\"trade_opened\",\"trade_id\":\"" + JsonEsc(trade_id) +
            "\",\"ticket\":0,\"fill_price\":0,\"legs_placed\":" + (string)placed + "}");
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

bool MoveSl(ManagedTrade &t, const double newSl_in, const string reason, const int tpIdx = -1,
            const bool clearTp = false)
{
   double newSl = newSl_in;

   // Guard/safety clamp (2026-08-04). guard_pips/safety_cap_pips have
   // existed on every template since their schema fields were added, but
   // nothing ever read them -- an SL move landing too close to price is
   // rejected outright by the broker as an invalid stop, the failure
   // documented against ticket 1663956102 (-$100, a rejected breakeven
   // move). guard_pips is the configured safety margin; the broker's own
   // SYMBOL_TRADE_STOPS_LEVEL is the hard minimum regardless of what's
   // configured, so it always applies even when guard_pips is 0.
   // safety_cap_pips is a second, independent floor the EA will never
   // tighten inside even if trailing math computes something closer --
   // kept separate rather than folded into guard_pips since a template may
   // want a looser day-to-day margin but a hard floor that never narrows
   // further no matter what. Empty tplCfg (non-template trades) reads 0
   // for both, so only the broker's own minimum applies there, unchanged
   // from before this existed.
   double guardPips = TplD(t.tplCfg, "guard_pips", 0.0);
   double capPips    = TplD(t.tplCfg, "safety_cap_pips", 0.0);
   double brokerMinDist = (double)SymbolInfoInteger(_Symbol, SYMBOL_TRADE_STOPS_LEVEL) * _Point;
   double minDist = MathMax(brokerMinDist, MathMax(PipsToPrice(guardPips), PipsToPrice(capPips)));
   if(minDist > 0.0)
   {
      MqlTick gtick;
      if(SymbolInfoTick(_Symbol, gtick))
      {
         double refPx = (t.direction == "BUY") ? gtick.bid : gtick.ask;
         if(t.direction == "BUY" && newSl > refPx - minDist)
            newSl = refPx - minDist;
         else if(t.direction == "SELL" && newSl < refPx + minDist)
            newSl = refPx + minDist;
      }
   }

   double curSl = 0.0;
   if(PositionSelectByTicket(t.ticket)) curSl = PositionGetDouble(POSITION_SL);
   bool better = (t.direction == "BUY") ? (newSl > curSl) : (newSl < curSl);
   if(!better) return false;
   double curTp = PositionSelectByTicket(t.ticket) ? PositionGetDouble(POSITION_TP) : 0.0;
   double newTp = clearTp ? 0.0 : curTp;
   if(!trade.PositionModify(t.ticket, newSl, newTp))
   {
      Print("[EABridge] modify SL failed ticket=", t.ticket, " err=", trade.ResultRetcodeDescription());
      return false;
   }
   ReportSlMoved(t, newSl, reason, tpIdx);
   return true;
}

// Standard "Trailing Step" trail: keep SL `dist` behind price, only moving
// once the improvement is >= trail_step pips (0 = move on any improvement).
// Factored out of the trail_mode=="step" branch below (2026-08-10) so
// trail_mode=="staged" can reuse it verbatim once its own ratchet
// (sl_stage1..3) has cleared -- see ManageTemplate.
void ApplyTemplateStepTrail(ManagedTrade &t, const MqlTick &tick, const double dist, const string reason)
{
   double newSl = (t.direction == "BUY") ? tick.bid - dist : tick.ask + dist;
   double trailStepPips = TplD(t.tplCfg, "trail_step", 0.0);
   bool stepOk = true;
   if(trailStepPips > 0.0)
   {
      double curStepSl = PositionSelectByTicket(t.ticket) ? PositionGetDouble(POSITION_SL) : 0.0;
      double moveAmt = (t.direction == "BUY") ? (newSl - curStepSl) : (curStepSl - newSl);
      stepOk = (curStepSl == 0.0) || (moveAmt >= PipsToPrice(trailStepPips));
   }
   if(stepOk) MoveSl(t, newSl, reason);
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

   // ── Emergency SL backstop ────────────────────────────────────────────
   // "in case the primary stop is rejected or removed" -- use_emergency_sl/
   // emergency_sl_mult existed as template fields with no implementation
   // until 2026-08-04. Checked every tick: if the broker-side SL on this
   // position is missing entirely (0.0) or has drifted wider than
   // emergency_sl_mult x the trade's own original SL distance, force it
   // back to that emergency distance from entry. Runs ahead of every other
   // management branch below so a missing stop never survives a full tick
   // unprotected. Ordinary MoveSl() calls elsewhere are always tighter
   // than this (BE/trailing only ever move a stop closer), so this never
   // fights them -- it only ever fires when something has gone wrong.
   if(TplB(t.tplCfg, "use_emergency_sl", false) && t.tplOrigSlDist > 0.0)
   {
      double _emMult = TplD(t.tplCfg, "emergency_sl_mult", 2.0);
      double _emDist = t.tplOrigSlDist * MathMax(1.0, _emMult);
      double _emSl = (t.direction == "BUY") ? t.entry_price - _emDist : t.entry_price + _emDist;
      double _liveSl = PositionGetDouble(POSITION_SL);
      bool _missing = (_liveSl == 0.0);
      bool _tooWide = (t.direction == "BUY") ? (_liveSl < _emSl) : (_liveSl > _emSl);
      if(_missing || _tooWide)
      {
         if(trade.PositionModify(t.ticket, _emSl, PositionGetDouble(POSITION_TP)))
            Print("[EABridge] emergency SL applied ticket=", t.ticket,
                  " sl=", DoubleToString(_emSl, _Digits), " (was ", DoubleToString(_liveSl, _Digits), ")");
      }
   }

   // ── Anchor TP: partial closes at each cleared level, full close on the
   // last defined level WHEN t.closeFullOnLast is set (the default, and the
   // only behaviour before that field became template-configurable -- see
   // core_ea_templates.py's close_full_on_last) -- same mechanism
   // ManageLadder() uses for the built-in ladder strategies (t.pcts[]/
   // t.closeFullOnLast), so a template's own Anchor TP %-close ladder
   // (core_ea_templates.py's tp{n}_pct fields) actually takes effect
   // instead of being silently ignored (2026-07-24 -- ManageTemplate()
   // never read t.pcts[] at all before this). A template with
   // close_full_on_last=false instead partial-closes the last level's own
   // pct too and leaves whatever remains open, managed from there by
   // Trail/BE -- for a ladder whose pct sum well under 100 and is meant to
   // leave a genuine runner. Runs for "on"/"stealth" only, never "off" --
   // "off" means no TP tracking whatsoever (SL/harvest/trail-only),
   // unchanged from before. For "stealth" this replaces the old
   // last-TP-only check: when
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

            // cancel_pending_level (2026-08-04 -- existed as a template
            // field with no implementation, meant to supersede the older
            // cancel_pending boolean's "only on first fill" limitation):
            // cancel every still-resting sibling once THIS specific TP
            // level clears, on any leg. 0 = never (default, matches every
            // template saved before this existed). Independent of Group TP
            // Action above -- that also moves live siblings to breakeven;
            // this only cancels resting ones, and at a level the template
            // chooses rather than always the first.
            int tplCancelLevel = TplI(t.tplCfg, "cancel_pending_level", 0);
            if(tplCancelLevel > 0 && !t.tplCancelLevelDone && t.tplGridGroup >= 0
               && (tplIdx + 1) == tplCancelLevel)
            {
               CancelGridSiblings(t.tplGridGroup, t.ticket);
               t.tplCancelLevelDone = true;
            }

            double tplRemaining = RemainingLots(t.ticket);
            if(tplRemaining <= 0) break;
            bool tplIsLast = (tplCompactPos == tplN - 1);

            // partials=false (2026-08-04 -- existed as a template field with
            // no implementation): skip every partial close at a non-last
            // level entirely, so nothing closes until the final defined TP,
            // which then closes the position outright -- "False = single
            // close at the final level" per core_ea_templates.py's own
            // comment for this field.
            bool tplPartialsOn = TplB(t.tplCfg, "partials", true);
            bool tplClosedAll = false;
            if(tplIsLast && (t.closeFullOnLast || !tplPartialsOn))
               tplClosedAll = DoCloseAll(t, tplIdx);
            else if(tplPartialsOn)
               DoPartialClose(t, tplIdx, t.pcts[tplCompactPos]);
            else
               break; // partials off, not the last level yet -- wait

            if(tplClosedAll) return; // position gone -- CheckForClosures reports it
            tplCompactPos++;
         }
      }
   }

   // ── Harvest: bank profit once floating P&L reaches the threshold ────
   if(t.tplHarvestEnabled)
   {
      double profit = PositionGetDouble(POSITION_PROFIT);
      // harvest_pips (2026-08-04 -- existed as a template field with no
      // implementation): a second, independent trigger alongside the
      // dollar-based harvest_threshold above -- "distinct from
      // harvest_threshold, which is in account currency" per its own
      // comment. 0 = off, matches every template saved before this
      // existed.
      double harvestPips = TplD(t.tplCfg, "harvest_pips", 0.0);
      double favMove = (t.direction == "BUY") ? (tick.bid - t.entry_price)
                                               : (t.entry_price - tick.ask);
      bool pipsHarvest = (harvestPips > 0.0) && (favMove >= PipsToPrice(harvestPips));
      if(profit >= t.tplHarvestThreshold || pipsHarvest)
      {
         trade.PositionClose(t.ticket);
         Print("[EABridge] template harvest triggered ($", profit,
               (pipsHarvest ? ", pips-move" : ""), "), closing ticket=", t.ticket);
         return;
      }
   }

   // ── Breakeven ────────────────────────────────────────────────────────
   // Anchor: "unified" uses tplAnchorPrice (the single price the whole grid
   // group was staged from, shared by every leg) so a multi-leg fill still
   // converges on one common breakeven; "distributed" (the default, and
   // the only behaviour before this field was wired in) uses this leg's
   // own entry_price, same as every other strategy's breakeven already did.
   //
   // Arms off triggered[] -- the LATCH DoPartialClose()/DoCloseAll() set when
   // the level actually traded -- and only falls back to the live TpCleared()
   // price test when nothing has latched (tpsl_mode="off", or partials=false,
   // where no level ever closes and so nothing ever sets triggered[]).
   //
   // TpCleared() alone was the bug (2026-08-21). It re-asks "is price beyond
   // the TP *right now*", so the whole block is skipped the moment price
   // retraces -- and MoveSl() returns false on a broker rejection or when the
   // guard clamp pushes the stop back inside the current one, leaving
   // tplBeDone false and the retry depending on a condition that has since
   // gone away. The clamp added against ticket 1663956102 fixed the rejection
   // itself but not the give-up: after one failed attempt the stop stayed at
   // its original full width for the rest of the trade. Measured over 21 Jul
   // - 21 Aug 2026: 141 trades reached a TP, never moved to breakeven, and
   // closed a mean 66.7 pips BELOW entry for -$7,562 -- 2.5x the account's
   // total loss for the period.
   //
   // Latching fixes both halves: a level that has traded stays armed, so
   // every subsequent tick retries the move until it succeeds, wherever
   // price has gone in the meantime. It cannot arm a level that never
   // traded, and MoveSl()'s own `better` test still refuses to widen a stop.
   if(!t.tplBeDone)
   {
      int beIdx = t.tplBeTrigger - 1;
      if(beIdx >= 0 && beIdx < MAX_TPS
         && (t.triggered[beIdx] || TpCleared(t, beIdx, tick)))
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
   // tp1_trigger_level (2026-08-04 -- existed as a template field with no
   // implementation): an alternate, TP-level-based arm condition, OR'd
   // with the pip-based trail_activation above -- whichever comes first.
   // Lets trailing start once a specific rung of the ladder clears instead
   // of only after a flat pip distance that can sit well beyond every
   // defined TP -- confirmed live on "Asian - Grid": trail_activation's
   // default (100 pips) is deeper than its own last defined TP (50 pips),
   // so the runner never armed at all and just sat at its breakeven stop,
   // capping every winning trade at the same ~$43 regardless of how far
   // price actually ran.
   if(!trailArmed)
   {
      int tplTrigLevel = TplI(t.tplCfg, "tp1_trigger_level", 0);
      if(tplTrigLevel > 0 && tplTrigLevel <= MAX_TPS)
         trailArmed = t.triggered[tplTrigLevel - 1];
   }

   if(t.tplTrailMode == "step" && trailArmed)
   {
      ApplyTemplateStepTrail(t, tick, trailDist + trailPad, "template_trail_step");
   }
   else if(t.tplTrailMode == "staged")
   {
      // Staged SL ratchet (2026-08-10) -- see core_ea_templates.DEFAULTS'
      // SL_STAGE_COUNT comment. Each rung is independent: on every tick,
      // walk sl_stage1..3 in order and, for any rung whose trigger_pips
      // has been reached, try to move SL to its target_pips (signed --
      // negative still risks a loss, 0 = breakeven, positive locks
      // profit). MoveSl's own better-than-current-SL check makes this
      // idempotent and self-healing across an EA/terminal restart with no
      // per-trade "which rung already fired" state to carry: a rung whose
      // target the stop has already passed (via an earlier rung, price
      // gapping through several triggers on one tick, or a prior session)
      // is simply rejected as not-better and costs nothing. Processing
      // ascending means a multi-rung gap still converges on the furthest
      // rung reached, applied last in the same tick.
      double favMovePx = (t.direction == "BUY") ? (tick.bid - t.entry_price)
                                                  : (t.entry_price - tick.ask);
      double inProfitPips = favMovePx / (10.0 * _Point);
      double lastTrigger = 0.0;
      for(int s = 1; s <= 3; s++)
      {
         string pfx = "sl_stage" + IntegerToString(s) + "_";
         double trigPips = TplD(t.tplCfg, pfx + "trigger_pips", 0.0);
         if(trigPips <= 0.0) continue; // rung unused
         if(trigPips > lastTrigger) lastTrigger = trigPips;
         if(inProfitPips < trigPips) continue; // not reached yet
         double targetPips = TplD(t.tplCfg, pfx + "target_pips", 0.0);
         double sign = (t.direction == "BUY") ? 1.0 : -1.0;
         double targetSl = t.entry_price + sign * PipsToPrice(targetPips);
         bool removeTp = TplB(t.tplCfg, pfx + "remove_tp", false);
         MoveSl(t, targetSl, "template_sl_stage" + IntegerToString(s), -1, removeTp);
      }
      // Every configured rung has cleared -- hand off to the same
      // constant-distance step trail trail_mode=="step" uses, so "trail
      // every N pips" past the ratchet needs no rung-specific code.
      if(lastTrigger > 0.0 && inProfitPips >= lastTrigger)
         ApplyTemplateStepTrail(t, tick, trailDist + trailPad, "template_sl_stage_trail");
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
         mt.tplCancelLevelDone = false;
         mt.tplHarvestEnabled = g_pending[i].tplHarvestEnabled;
         mt.tplHarvestThreshold = g_pending[i].tplHarvestThreshold;
         mt.tplOrigSlDist = g_pending[i].tplOrigSlDist;

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
//| ON-CHART COPIER PANEL                                             |
//|                                                                   |
//| The full control surface on the chart: the copier panel on the    |
//| left (channel, entries and lots, TP ladder, strategy switches,     |
//| entry management) and the live signal dashboard on the right.     |
//|                                                                   |
//| ONE AUTHORITY. Nothing here holds trading state. Every control     |
//| that changes behaviour sends panel_action to the app, which writes |
//| the SAVED template and pushes the result back as set_template --   |
//| and that reply is what actually moves the display. A chart edit    |
//| and an app edit therefore cannot diverge: both are the same write, |
//| made in the same place, and the chart only ever renders the        |
//| result. The four exceptions (which tab, which TP ladder, MANUAL    |
//| arming, sound) are local by design and change nothing about how a  |
//| trade is placed or managed.                                        |
//|                                                                    |
//| WHAT IS DRAWN FROM WHERE. The left column reads g_panelCfg, the    |
//| last set_template payload, through TplD/TplI/TplB/TplS -- so a new |
//| field in core_ea_templates.DEFAULTS needs one layout line here and |
//| nothing else. The right column's ICT criteria and confidence come  |
//| from the app (panel_signal, computed by core_panel_signal.py from  |
//| the reversal engine); the price, spread, P&L, trend row, ATR,      |
//| session countdown and VWAP are computed here, because they need a  |
//| ticking clock and this chart's own series and would only be made   |
//| worse by a socket hop.                                             |
//|                                                                    |
//| ORDERS. The Entry Management buttons do NOT place orders from      |
//| here. They send panel_action and the app places them through its   |
//| normal paths, so a trade started at the chart is risk-checked,     |
//| tracked and reported exactly like any other. The one deliberate    |
//| exception is the disconnected case -- see OnChartEvent's CLOSE ALL |
//| and CANCEL LIMITS handling.                                        |
//+------------------------------------------------------------------+

//+------------------------------------------------------------------+
//| Drawing primitives                                                |
//+------------------------------------------------------------------+
void PnlRect(const string name, const int x, const int y, const int w,
             const int h, const color bg, const color border = CLR_BORDER)
{
   string obj = PNL_PREFIX + name;
   if(ObjectFind(0, obj) < 0)
   {
      ObjectCreate(0, obj, OBJ_RECTANGLE_LABEL, 0, 0, 0);
      ObjectSetInteger(0, obj, OBJPROP_CORNER, CORNER_LEFT_UPPER);
      ObjectSetInteger(0, obj, OBJPROP_SELECTABLE, false);
      ObjectSetInteger(0, obj, OBJPROP_HIDDEN, true);
      ObjectSetInteger(0, obj, OBJPROP_BACK, false);
      ObjectSetInteger(0, obj, OBJPROP_BORDER_TYPE, BORDER_FLAT);
   }
   ObjectSetInteger(0, obj, OBJPROP_XDISTANCE, x);
   ObjectSetInteger(0, obj, OBJPROP_YDISTANCE, y);
   ObjectSetInteger(0, obj, OBJPROP_XSIZE, w);
   ObjectSetInteger(0, obj, OBJPROP_YSIZE, h);
   ObjectSetInteger(0, obj, OBJPROP_BGCOLOR, bg);
   ObjectSetInteger(0, obj, OBJPROP_COLOR, border);
}

// Anchored text. anchor == ANCHOR_CENTER is used for every cell caption so
// a value changing width (7 -> 100) doesn't visibly shift within its box.
void PnlLabel(const string name, const int x, const int y, const string text,
              const color clr, const int size = 8,
              const string font = "Arial",
              const ENUM_ANCHOR_POINT anchor = ANCHOR_CENTER)
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
   ObjectSetInteger(0, obj, OBJPROP_ANCHOR, anchor);
   ObjectSetInteger(0, obj, OBJPROP_XDISTANCE, x);
   ObjectSetInteger(0, obj, OBJPROP_YDISTANCE, y);
   ObjectSetInteger(0, obj, OBJPROP_COLOR, clr);
   ObjectSetInteger(0, obj, OBJPROP_FONTSIZE, size);
   ObjectSetString(0, obj, OBJPROP_FONT, font);
   ObjectSetString(0, obj, OBJPROP_TEXT, text);
}

// A read-only cell: backing rectangle plus centred caption.
void PnlCell(const string name, const int x, const int y, const int w,
             const int h, const string text, const color bg,
             const color fg, const int size = 8,
             const string font = "Arial")
{
   PnlRect(name + "_bg", x, y, w, h, bg);
   PnlLabel(name + "_tx", x + w / 2, y + h / 2, text, fg, size, font);
}

void PnlButton(const string name, const int x, const int y, const int w,
               const int h, const string text, const color bg,
               const color fg = clrWhite, const int size = 8)
{
   string obj = PNL_PREFIX + name;
   if(ObjectFind(0, obj) < 0)
   {
      ObjectCreate(0, obj, OBJ_BUTTON, 0, 0, 0);
      ObjectSetInteger(0, obj, OBJPROP_CORNER, CORNER_LEFT_UPPER);
      ObjectSetInteger(0, obj, OBJPROP_SELECTABLE, false);
      ObjectSetInteger(0, obj, OBJPROP_HIDDEN, true);
   }
   ObjectSetInteger(0, obj, OBJPROP_XDISTANCE, x);
   ObjectSetInteger(0, obj, OBJPROP_YDISTANCE, y);
   ObjectSetInteger(0, obj, OBJPROP_XSIZE, w);
   ObjectSetInteger(0, obj, OBJPROP_YSIZE, h);
   ObjectSetInteger(0, obj, OBJPROP_BGCOLOR, bg);
   ObjectSetInteger(0, obj, OBJPROP_COLOR, fg);
   ObjectSetInteger(0, obj, OBJPROP_BORDER_COLOR, CLR_BORDER);
   ObjectSetInteger(0, obj, OBJPROP_FONTSIZE, size);
   ObjectSetInteger(0, obj, OBJPROP_STATE, false);   // buttons are momentary
   ObjectSetString(0, obj, OBJPROP_TEXT, text);
}

//+------------------------------------------------------------------+
//| Edit boxes                                                        |
//|                                                                   |
//| The repaint runs about once a second and would otherwise overwrite |
//| a half-typed value on every tick of the clock. PnlEdit remembers   |
//| the exact text it last wrote; if the box no longer holds that      |
//| text, the user is typing and the repaint leaves it alone until     |
//| they commit with Enter (CHARTEVENT_OBJECT_ENDEDIT), which sends    |
//| the value to the app and lets the set_template reply reset the     |
//| baseline. Losing keystrokes to your own repaint is the classic way |
//| an MQL5 panel becomes unusable, so this is not optional.           |
//+------------------------------------------------------------------+
int PnlEditSlot(const string name)
{
   for(int i = 0; i < g_editCount; i++)
      if(g_editName[i] == name) return i;
   if(g_editCount >= PNL_MAX_EDIT) return -1;
   g_editName[g_editCount] = name;
   g_editLast[g_editCount] = "";
   g_editDivergedAt[g_editCount] = 0;
   g_editCount++;
   return g_editCount - 1;
}

void PnlEdit(const string name, const int x, const int y, const int w,
             const int h, const string text, const color bg = CLR_CELL2,
             const color fg = CLR_YELLOW, const int size = 8)
{
   string obj = PNL_PREFIX + name;
   bool fresh = ObjectFind(0, obj) < 0;
   if(fresh)
   {
      ObjectCreate(0, obj, OBJ_EDIT, 0, 0, 0);
      ObjectSetInteger(0, obj, OBJPROP_CORNER, CORNER_LEFT_UPPER);
      ObjectSetInteger(0, obj, OBJPROP_SELECTABLE, false);
      ObjectSetInteger(0, obj, OBJPROP_HIDDEN, true);
      ObjectSetInteger(0, obj, OBJPROP_ALIGN, ALIGN_CENTER);
   }
   ObjectSetInteger(0, obj, OBJPROP_XDISTANCE, x);
   ObjectSetInteger(0, obj, OBJPROP_YDISTANCE, y);
   ObjectSetInteger(0, obj, OBJPROP_XSIZE, w);
   ObjectSetInteger(0, obj, OBJPROP_YSIZE, h);
   ObjectSetInteger(0, obj, OBJPROP_BGCOLOR, bg);
   ObjectSetInteger(0, obj, OBJPROP_COLOR, fg);
   ObjectSetInteger(0, obj, OBJPROP_BORDER_COLOR, CLR_BORDER);
   ObjectSetInteger(0, obj, OBJPROP_FONTSIZE, size);

   int slot = PnlEditSlot(name);
   if(slot < 0) { ObjectSetString(0, obj, OBJPROP_TEXT, text); return; }
   if(!fresh && ObjectGetString(0, obj, OBJPROP_TEXT) != g_editLast[slot])
   {
      // Mid-edit: leave the user's text alone -- but not forever. Abandoning
      // an edit without pressing Enter (click away, switch chart) may not
      // produce an ENDEDIT on every build, and a box frozen on uncommitted
      // text while the app holds a different value is worse than losing the
      // typing: it shows a setting the EA is not actually using. So the
      // guard expires and the box resyncs with the app.
      if(g_editDivergedAt[slot] == 0) g_editDivergedAt[slot] = TimeCurrent();
      if(TimeCurrent() - g_editDivergedAt[slot] < PNL_EDIT_GRACE_S) return;
   }
   ObjectSetString(0, obj, OBJPROP_TEXT, text);
   g_editLast[slot] = text;
   g_editDivergedAt[slot] = 0;
}

// Called after the app confirms a value, so the next repaint is allowed to
// write into the box again.
void PnlEditCommitted(const string name)
{
   int slot = PnlEditSlot(name);
   if(slot >= 0)
   {
      g_editLast[slot] = ObjectGetString(0, PNL_PREFIX + name, OBJPROP_TEXT);
      g_editDivergedAt[slot] = 0;
   }
}

//+------------------------------------------------------------------+
//| Log ring                                                          |
//+------------------------------------------------------------------+
void PanelLog(const string text)
{
   if(text == "") return;
   for(int i = PNL_LOG_N - 1; i > 0; i--) g_logLine[i] = g_logLine[i - 1];
   g_logLine[0] = "[" + TimeToString(TimeCurrent(), TIME_SECONDS) + "] " + text;
   if(g_logCount < PNL_LOG_N) g_logCount++;
}

//+------------------------------------------------------------------+
//| Locally-computed market context                                   |
//+------------------------------------------------------------------+

// Trend per timeframe: close above a 50-period EMA on that timeframe. A
// single settled average, not a pattern read -- the pattern work all lives
// app-side (see the header). Returns false and sets ok=false while a
// timeframe's history is still loading, so "no data yet" never renders as
// "bearish".
bool TfBullish(const int idx, bool &ok)
{
   ok = false;
   if(idx < 0 || idx >= 6) return false;
   if(g_tfMa[idx] < 0)
   {
      g_tfMa[idx] = iMA(_Symbol, g_tfRow[idx], 50, 0, MODE_EMA, PRICE_CLOSE);
      if(g_tfMa[idx] == INVALID_HANDLE) { g_tfMa[idx] = -1; return false; }
   }
   double buf[];
   if(CopyBuffer(g_tfMa[idx], 0, 0, 1, buf) < 1) return false;
   double c = iClose(_Symbol, g_tfRow[idx], 0);
   if(c <= 0.0) return false;
   ok = true;
   return c > buf[0];
}

double PanelAtr()
{
   if(g_atrHandle < 0)
   {
      g_atrHandle = iATR(_Symbol, PERIOD_M15, 14);
      if(g_atrHandle == INVALID_HANDLE) { g_atrHandle = -1; return 0.0; }
   }
   double buf[];
   if(CopyBuffer(g_atrHandle, 0, 0, 1, buf) < 1) return 0.0;
   return buf[0];
}

// Session VWAP from midnight UTC, over M5 bars. Used only to say whether
// price sits above or below its own volume-weighted mean -- the same test
// core_panel_signal.py scores, computed here so the two lamps stay live
// between pushes.
// Cached: the panel repaints about once a second and this walks a day of M5
// bars. VWAP moves slowly enough that a 10s-old value is indistinguishable
// from a live one, and the two lamps it feeds only compare it to price.
double   g_vwapCache = 0.0;
datetime g_vwapAt    = 0;

double PanelVwap()
{
   if(g_vwapAt != 0 && TimeCurrent() - g_vwapAt < 10) return g_vwapCache;
   MqlRates r[];
   int n = CopyRates(_Symbol, PERIOD_M5, 0, 288, r);
   if(n <= 0) return 0.0;
   datetime midnight = (datetime)((long)TimeGMT() / 86400 * 86400);
   double num = 0.0, den = 0.0, plain = 0.0;
   int cnt = 0;
   for(int i = 0; i < n; i++)
   {
      if(r[i].time < midnight) continue;
      double tp = (r[i].high + r[i].low + r[i].close) / 3.0;
      double v  = (double)r[i].tick_volume;
      num += tp * v; den += v; plain += tp; cnt++;
   }
   if(cnt == 0) return 0.0;
   // Some feeds report zero volume on XAUUSD; an unweighted mean of the same
   // typical prices is still a usable reference, and matches the same
   // fallback in core_panel_signal._vwap so the two never disagree about
   // which side of it price is on.
   g_vwapCache = den > 0.0 ? num / den : plain / cnt;
   g_vwapAt    = TimeCurrent();
   return g_vwapCache;
}

// Session/killzone name plus the countdown to the next one. The windows are
// the same UTC blocks core_panel_signal.KILLZONES scores against -- kept in
// two places because the countdown has to tick locally while only the
// boolean crosses the wire. A drift between the two can move a label; it
// cannot move the score.
string PanelSession()
{
   int kzStart[3] = {0, 7, 12};
   int kzEnd[3]   = {6, 10, 15};
   string kzName[3];
   kzName[0] = "ASIA"; kzName[1] = "LONDON"; kzName[2] = "NEW YORK";

   datetime now = TimeGMT();
   MqlDateTime dt;
   TimeToStruct(now, dt);
   int h = dt.hour;

   string cur = "OUT OF SESSION";
   for(int i = 0; i < 3; i++)
      if(h >= kzStart[i] && h < kzEnd[i]) cur = kzName[i] + " KZ";

   // Next window start, today or tomorrow.
   datetime dayStart = (datetime)((long)now / 86400 * 86400);
   datetime best = 0;
   string bestName = "";
   for(int i = 0; i < 3; i++)
   {
      datetime t = dayStart + kzStart[i] * 3600;
      if(t <= now) t += 86400;
      if(best == 0 || t < best) { best = t; bestName = kzName[i]; }
   }
   int secs = (int)(best - now);
   return "SESSION: " + cur + " | NEXT: " + bestName + " in " +
          StringFormat("%02d:%02d:%02d", secs / 3600, (secs / 60) % 60, secs % 60);
}

// Positions this EA opened that already have their stop at or beyond entry.
int PanelRiskFreeCount()
{
   int n = 0;
   for(int i = 0; i < PositionsTotal(); i++)
   {
      ulong tk = PositionGetTicket(i);
      if(tk == 0) continue;
      if(PositionGetInteger(POSITION_MAGIC) != (long)InpMagic) continue;
      double sl = PositionGetDouble(POSITION_SL);
      if(sl <= 0.0) continue;
      double entry = PositionGetDouble(POSITION_PRICE_OPEN);
      bool isBuy = PositionGetInteger(POSITION_TYPE) == POSITION_TYPE_BUY;
      if((isBuy && sl >= entry) || (!isBuy && sl <= entry)) n++;
   }
   return n;
}

double PanelFloatingPl()
{
   double pl = 0.0;
   for(int i = 0; i < PositionsTotal(); i++)
      if(PositionGetTicket(i) > 0) pl += PositionGetDouble(POSITION_PROFIT);
   return pl;
}

//+------------------------------------------------------------------+
//| Template accessors for the panel's own config                     |
//+------------------------------------------------------------------+
double PnlD(const string key, const double def) { return TplD(g_panelCfg, key, def); }
int    PnlI(const string key, const int def)    { return TplI(g_panelCfg, key, def); }
bool   PnlB(const string key, const bool def)   { return TplB(g_panelCfg, key, def); }
string PnlS(const string key, const string def) { return TplS(g_panelCfg, key, def); }

// The TP grid edits whichever ladder the TYPE button is showing: Anchor legs
// use tp<n>_pips/_pct, pending legs tp_pen<n>_pips/_pct. One grid, two field
// families, so the panel never needs 10 more cells to show both at once.
string PnlTpKey(const int n, const bool pct)
{
   string base = (g_tpType == "pending") ? "tp_pen" : "tp";
   return base + (string)n + (pct ? "_pct" : "_pips");
}

//+------------------------------------------------------------------+
//| Build / destroy                                                    |
//+------------------------------------------------------------------+
void PanelDestroy()
{
   ObjectsDeleteAll(0, PNL_PREFIX);
   g_panelBuilt = false;
   g_editCount  = 0;
   ChartRedraw(0);
}

void PanelBuild()
{
   // Two backing rectangles, created before anything else so every cell and
   // caption drawn later sits on top of them (MT5 z-orders chart objects by
   // creation order once OBJPROP_BACK is false).
   PnlRect("BGL", PNL_X - PNL_PAD, PNL_Y - PNL_PAD,
           PNL_LW + PNL_PAD * 2, 660, CLR_BG);
   PnlRect("BGR", PNL_RX - PNL_PAD, PNL_Y - PNL_PAD,
           PNL_RW + PNL_PAD * 2, 660, CLR_BG);
   g_panelBuilt = true;
}

//+------------------------------------------------------------------+
//| Left column — the copier panel                                    |
//+------------------------------------------------------------------+
int PanelDrawLeft()
{
   int x = PNL_X;
   int y = PNL_Y;
   const int W  = PNL_LW;
   const int H  = PNL_ROW;
   const int S  = PNL_ROW + PNL_GAP;          // row stride
   const int W2 = (W - PNL_GAP) / 2;          // half width
   const int W3 = (W - PNL_GAP * 2) / 3;      // third
   const int W4 = (W - PNL_GAP * 3) / 4;      // quarter

   // ── Header ───────────────────────────────────────────────────────
   PnlCell("hdr", x, y, 184, H, "COPIER PANEL", CLR_CELL, CLR_TEXT, 9, "Arial Bold");
   PnlButton("refresh", x + 188, y, 34, H, "R", CLR_CELL, CLR_CYAN);
   PnlButton("tabtrades", x + 226, y, 86, H, "TRADES",
             g_panelTab == "trades" ? CLR_AMBER : CLR_CELL,
             g_panelTab == "trades" ? clrBlack : CLR_TEXT);
   PnlButton("sound", x + 316, y, 34, H, g_panelSound ? "S" : "s",
             CLR_CELL, g_panelSound ? CLR_CYAN : CLR_DIM);
   // MANUAL arms the market/limit buttons below. Disarmed by default: a
   // stray click on a chart should never be able to send a live order.
   PnlButton("manual", x + 354, y, 86, H, "MANUAL",
             g_panelManual ? CLR_AMBER : CLR_CELL,
             g_panelManual ? clrBlack : CLR_DIM);
   y += S;

   // ── Link lamps ───────────────────────────────────────────────────
   // g_linkConfirmed, not "the socket handle exists": an open handle to a
   // port nothing is serving showed this lamp lit for four hours on
   // 2026-08-07 while the app had no EA at all. The lamp now means the same
   // thing the app's own EA badge means.
   bool linked = g_linkConfirmed;
   PnlCell("tg", x, y, W2, H,
           "TELEGRAM: " + (g_tgActive ? "ACTIVE" : "OFF"),
           g_tgActive ? CLR_GREENBG : CLR_REDBG,
           g_tgActive ? CLR_GREEN : CLR_RED);
   PnlCell("tgcmd", x + W2 + PNL_GAP, y, W2, H,
           "TG CMD: " + (g_tgCmd ? "ACTIVE" : "OFF"),
           g_tgCmd ? CLR_GREENBG : CLR_REDBG,
           g_tgCmd ? CLR_GREEN : CLR_RED);
   y += S;

   // ── Channel tabs ─────────────────────────────────────────────────
   for(int i = 0; i < PNL_MAX_CH; i++)
   {
      string cap = "CH" + (string)(i + 1);
      if(g_chName[i] != "") cap += " (" + g_chName[i] + ")";
      PnlButton("ch" + (string)i, x + i * (W3 + PNL_GAP), y, W3, H, cap,
                i == g_chSel ? CLR_TEAL : CLR_CELL,
                i == g_chSel ? clrWhite : CLR_DIM, 7);
   }
   y += S;

   // ── Selected channel: name and id, both read-only ────────────────
   // The terminal never writes channel identity -- see core_panel_context.
   string selName = (g_chSel < PNL_MAX_CH) ? g_chName[g_chSel] : "";
   string selId   = (g_chSel < PNL_MAX_CH) ? g_chId[g_chSel] : "";
   bool   selOn   = (g_chSel < PNL_MAX_CH) && g_chActive[g_chSel];
   PnlRect("chbox_bg", x, y, W2, 44, CLR_CELL);
   PnlLabel("chbox_h", x + W2 / 2, y + 12,
            selOn ? "CHANNEL: ACTIVE" : "CHANNEL: IDLE",
            selOn ? CLR_GREEN : CLR_DIM, 7);
   PnlLabel("chbox_v", x + W2 / 2, y + 31,
            selName == "" ? "(none)" : selName, CLR_TEXT, 8, "Arial Bold");
   PnlRect("chid_bg", x + W2 + PNL_GAP, y, W2, 44, CLR_CELL);
   PnlLabel("chid_h", x + W2 + PNL_GAP + W2 / 2, y + 12, "CHANNEL ID", CLR_DIM, 7);
   PnlLabel("chid_v", x + W2 + PNL_GAP + W2 / 2, y + 31,
            selId == "" ? "-" : selId, CLR_TEXT, 8);
   y += 44 + PNL_GAP;

   // ── Template being edited ────────────────────────────────────────
   PnlCell("tpl", x, y, W, H,
           "TEMPLATE: " + (g_panelTemplate == "" ? "(none — app has not pushed one)"
                                                 : g_panelTemplate),
           CLR_CELL2, g_panelTemplate == "" ? CLR_RED : CLR_CYAN, 8);
   y += S;

   // ── Equity protect ───────────────────────────────────────────────
   PnlCell("eplbl", x, y, W2, H, "EQUITY PROTECT ($)", CLR_CELL, CLR_TEXT);
   PnlEdit("f_equity_protect", x + W2 + PNL_GAP, y, W2, H,
           DoubleToString(PnlD("equity_protect", 0.0), 2));
   y += S;

   // ── Entries and lots ─────────────────────────────────────────────
   PnlCell("aclbl", x, y, W4, H, "ANCHOR COUNT:", CLR_CELL, CLR_TEXT, 7);
   PnlEdit("f_anchors", x + W4 + PNL_GAP, y, W4, H, (string)PnlI("anchors", 1));
   PnlCell("allbl", x + (W4 + PNL_GAP) * 2, y, W4, H, "ANCHOR LOT:", CLR_CELL, CLR_TEXT, 7);
   PnlEdit("f_lot_anchor", x + (W4 + PNL_GAP) * 3, y, W4, H,
           DoubleToString(PnlD("lot_anchor", 0.01), 2));
   y += S;

   PnlCell("pclbl", x, y, W4, H, "PENDING COUNT:", CLR_CELL, CLR_TEXT, 7);
   PnlEdit("f_pendings", x + W4 + PNL_GAP, y, W4, H, (string)PnlI("pendings", 1));
   PnlCell("pllbl", x + (W4 + PNL_GAP) * 2, y, W4, H, "PENDING LOT:", CLR_CELL, CLR_TEXT, 7);
   PnlEdit("f_lot_pending", x + (W4 + PNL_GAP) * 3, y, W4, H,
           DoubleToString(PnlD("lot_pending", 0.01), 2));
   y += S;

   PnlCell("lslbl", x, y, W4, H, "LADDER STEP:", CLR_CELL, CLR_TEXT, 7);
   PnlEdit("f_grid_step_pts", x + W4 + PNL_GAP, y, W4, H,
           DoubleToString(PnlD("grid_step_pts", 10.0), 1));
   PnlCell("sllbl", x + (W4 + PNL_GAP) * 2, y, W4, H, "STOP LOSS:", CLR_CELL, CLR_TEXT, 7);
   PnlEdit("f_sl_pips", x + (W4 + PNL_GAP) * 3, y, W4, H,
           DoubleToString(PnlD("sl_pips", 50.0), 1));
   y += S;

   // ── Risk summary ─────────────────────────────────────────────────
   PnlCell("risk", x, y, W, H, PanelRiskLine(), CLR_CELL2, CLR_YELLOW, 8);
   y += S;

   // ── TP ladder ────────────────────────────────────────────────────
   PnlCell("tphdr", x, y, 250, H, "TAKE PROFIT TARGETS TP1-TP5", CLR_CELL, CLR_CYAN, 8);
   PnlButton("tptype", x + 254, y, W - 254, H,
             "TYPE: " + (g_tpType == "pending" ? "PENDING" : "ANCHOR"),
             CLR_CYAN, clrBlack);
   y += S;

   const int TPW = 85, TPS = 88;
   for(int i = 0; i < 5; i++)
      PnlCell("tph" + (string)i, x + i * TPS, y, TPW, H,
              "TP" + (string)(i + 1), CLR_CELL, CLR_TEXT, 8);
   y += S;
   for(int i = 0; i < 5; i++)
      PnlEdit("f_" + PnlTpKey(i + 1, false), x + i * TPS, y, TPW, H,
              DoubleToString(PnlD(PnlTpKey(i + 1, false), 0.0), 1));
   y += S;
   for(int i = 0; i < 5; i++)
      PnlEdit("f_" + PnlTpKey(i + 1, true), x + i * TPS, y, TPW, H,
              DoubleToString(PnlD(PnlTpKey(i + 1, true), 0.0), 0) + "%",
              CLR_CELL2, CLR_CYAN);
   y += S;

   // ── Channel strategy switches ────────────────────────────────────
   PnlCell("cshdr", x, y, W, H, "CHANNEL STRATEGY", CLR_CELL2, CLR_CYAN, 8);
   y += S;

   bool harvest = PnlB("harvest_enabled", false);
   PnlButton("b_harvest", x, y, W3, H, harvest ? "HARVEST ON" : "HARVEST OFF",
             harvest ? CLR_TEAL : CLR_OFF, harvest ? clrWhite : CLR_DIM);
   string mode = PnlS("mode", "single");
   PnlButton("b_mode", x + W3 + PNL_GAP, y, W3, H,
             mode == "grid" ? "GRID" : "SINGLE",
             mode == "grid" ? CLR_GRIDY : CLR_OFF,
             mode == "grid" ? clrBlack : CLR_DIM);
   string tpsl = PnlS("tpsl_mode", "on");
   PnlButton("b_tpsl", x + (W3 + PNL_GAP) * 2, y, W3, H,
             "TP/SL " + (tpsl == "off" ? "OFF" : (tpsl == "stealth" ? "STEALTH" : "ON")),
             tpsl == "off" ? CLR_REDBG : CLR_TEAL,
             tpsl == "off" ? CLR_RED : clrWhite);
   y += S;

   // "PEN TP" is the pending legs' TP anchoring: unified = every leg takes
   // the anchor's targets, distributed = each leg gets its own.
   string anc = PnlS("anchor", "unified");
   PnlButton("b_anchor", x, y, W3, H,
             "PEN TP : " + (anc == "unified" ? "ANCHOR" : "OWN"),
             CLR_AMBER, clrBlack);
   string beMode = PnlS("be_mode", "entry");
   PnlButton("b_bemode", x + W3 + PNL_GAP, y, W3, H,
             "BE: " + (beMode == "entry_buffer" ? "BUFFER" : "ENTRY"),
             C'216,67,21', clrWhite);
   int delLvl = PnlI("cancel_pending_level", 0);
   PnlButton("b_dellimits", x + (W3 + PNL_GAP) * 2, y, W3, H,
             delLvl <= 0 ? "DEL LIMITS OFF" : "DEL LIMITS TP" + (string)delLvl,
             delLvl <= 0 ? CLR_OFF : CLR_STEEL,
             delLvl <= 0 ? CLR_DIM : clrWhite);
   y += S;

   PnlButton("b_betrig", x, y, W3, H,
             "BREAKEVEN TP" + (string)PnlI("be_trigger", 1), CLR_BLUE, clrWhite);
   string trail = PnlS("trail_mode", "off");
   PnlButton("b_trail", x + W3 + PNL_GAP, y, W3, H,
             "TRAIL: " + (trail == "off" ? "OFF" : StringSubstr(trail, 0, 7)),
             trail == "off" ? CLR_OFF : C'110,40,150',
             trail == "off" ? CLR_DIM : clrWhite);
   bool sg = PnlB("sig_guard", false);
   double sgp = PnlD("sig_guard_pips", 0.0);
   PnlButton("b_sigguard", x + (W3 + PNL_GAP) * 2, y, W3, H,
             !sg ? "SIG GUARD: OFF"
                 : (sgp <= 0.0 ? "SIG GUARD: ALL"
                               : "SIG GUARD: " + DoubleToString(sgp, 0) + "p"),
             sg ? C'229,57,53' : CLR_OFF, sg ? clrWhite : CLR_DIM);
   y += S;

   // ── Entry management ─────────────────────────────────────────────
   PnlCell("emhdr", x, y, W, H,
           g_panelManual ? "ENTRY MANAGEMENT" : "ENTRY MANAGEMENT (press MANUAL to arm)",
           CLR_CELL2, g_panelManual ? CLR_CYAN : CLR_DIM, 8);
   y += S;

   color sellBg = g_panelManual ? C'229,57,53' : CLR_OFF;
   color buyBg  = g_panelManual ? C'0,200,83'  : CLR_OFF;
   color armFg  = g_panelManual ? clrWhite : CLR_DIM;
   PnlButton("b_selllimit", x, y, W3, H, "SELL LIMIT", sellBg, armFg);
   PnlEdit("f_limit_offset", x + W3 + PNL_GAP, y, W3, H,
           DoubleToString(g_limitOffset, 0));
   PnlButton("b_buylimit", x + (W3 + PNL_GAP) * 2, y, W3, H, "BUY LIMIT", buyBg, armFg);
   y += S;

   PnlButton("b_sell", x, y, W4, H, "SELL", sellBg, armFg, 9);
   PnlButton("b_cancel", x + W4 + PNL_GAP, y, W4, H, "CANCEL LIMITS",
             CLR_YELLOW, clrBlack, 7);
   PnlButton("b_closeall", x + (W4 + PNL_GAP) * 2, y, W4, H, "CLOSE ALL",
             clrBlack, clrWhite, 9);
   PnlButton("b_buy", x + (W4 + PNL_GAP) * 3, y, W4, H, "BUY", buyBg, armFg, 9);
   y += S;

   int rf = PanelRiskFreeCount();
   PnlCell("riskfree", x, y, W, H,
           "RISK FREE (BE+" + DoubleToString(PnlD("be_buffer_pts", 1.0), 1) + "): " +
           (string)rf + " TRADE" + (rf == 1 ? "" : "S") +
           (linked ? "" : "   ·   NO APP LINK"),
           CLR_CELL2, linked ? CLR_YELLOW : CLR_RED, 8);
   y += S;

   return y;
}

//+------------------------------------------------------------------+
//| Risk summary line                                                 |
//|                                                                   |
//| Derived from the template's own lots and stop, priced through the  |
//| symbol's tick value rather than a hardcoded $/pip -- the same      |
//| number the account actually risks, on any symbol this EA is        |
//| attached to. R:R uses the LAST configured TP level, which is the   |
//| target the ladder is actually built around.                        |
//+------------------------------------------------------------------+
string PanelRiskLine()
{
   double lots = PnlI("anchors", 1) * PnlD("lot_anchor", 0.01) +
                 PnlI("pendings", 1) * PnlD("lot_pending", 0.01);
   double slPips = PnlD("sl_pips", 50.0);
   if(lots <= 0.0 || slPips <= 0.0) return "RISK: not enough template data";

   double tickVal  = SymbolInfoDouble(_Symbol, SYMBOL_TRADE_TICK_VALUE);
   double tickSize = SymbolInfoDouble(_Symbol, SYMBOL_TRADE_TICK_SIZE);
   if(tickSize <= 0.0 || tickVal <= 0.0) return "RISK: no tick value from broker";
   double perPipPerLot = PipsToPrice(1.0) / tickSize * tickVal;

   double risk = lots * slPips * perPipPerLot;
   double bal  = AccountInfoDouble(ACCOUNT_BALANCE);
   double pct  = bal > 0.0 ? risk / bal * 100.0 : 0.0;

   double lastTp = 0.0;
   for(int n = 1; n <= 5; n++)
   {
      double v = PnlD("tp" + (string)n + "_pips", 0.0);
      if(v > 0.0) lastTp = v;
   }
   double reward = lots * lastTp * perPipPerLot;

   string band = pct < 1.0 ? "LOW" : (pct < 3.0 ? "MODERATE" : "HIGH");
   string rr = lastTp > 0.0
      ? StringFormat("  |  R:R 1:%.1f $%.2f", lastTp / slPips, reward)
      : "  |  R:R -- (no TP set)";
   return StringFormat("RISK: %s (%.2f%%) $%.2f%s", band, pct, risk, rr);
}

//+------------------------------------------------------------------+
//| Right column — live dashboard                                     |
//+------------------------------------------------------------------+
string g_panelTabDrawn = "";      // which tab's objects currently exist

// Y/N criterion cell. Shared by every row of the breakdown so a criterion
// added later is one line, not a block.
void PnlCrit(const string name, const int x, const int y, const int w,
             const int h, const string label, const bool on)
{
   PnlCell(name, x, y, w, h, label + ": " + (on ? "Y" : "N"),
           CLR_CELL, on ? CLR_GREEN : CLR_DIM, 8);
}

int PanelDrawRight()
{
   int x = PNL_RX;
   int y = PNL_Y;
   const int W  = PNL_RW;
   const int H  = PNL_ROW;
   const int S  = PNL_ROW + PNL_GAP;
   const int W2 = (W - PNL_GAP) / 2;
   const int W3 = (W - PNL_GAP * 2) / 3;

   // Tab strip
   string tabs[3];  tabs[0] = "trades"; tabs[1] = "levels"; tabs[2] = "signals";
   string caps[3];  caps[0] = "TRADES"; caps[1] = "LEVELS"; caps[2] = "SIGNALS";
   for(int i = 0; i < 3; i++)
      PnlButton("rtab" + (string)i, x + i * (W3 + PNL_GAP), y, W3, H, caps[i],
                g_panelTab == tabs[i] ? C'90,98,86' : CLR_CELL,
                g_panelTab == tabs[i] ? clrWhite : CLR_DIM);
   y += S;

   // Price header — always visible, whichever tab is showing.
   double pl = PanelFloatingPl();
   MqlTick tk;
   bool haveTick = SymbolInfoTick(_Symbol, tk);
   double spreadPts = haveTick ? (tk.ask - tk.bid) / _Point : 0.0;

   PnlCell("r_profit", x, y, W2, H,
           StringFormat("Profit: $%.2f", pl), CLR_CELL,
           pl >= 0 ? CLR_GREEN : CLR_RED);
   PnlCell("r_spread", x + W2 + PNL_GAP, y, W2, H,
           StringFormat("Spread: %.0f", spreadPts), CLR_CELL, CLR_TEXT);
   y += S;
   PnlCell("r_bid", x, y, W2, H,
           "BID: " + (haveTick ? DoubleToString(tk.bid, _Digits) : "-"),
           CLR_CELL, CLR_RED);
   PnlCell("r_ask", x + W2 + PNL_GAP, y, W2, H,
           "ASK: " + (haveTick ? DoubleToString(tk.ask, _Digits) : "-"),
           CLR_CELL, CLR_GREEN);
   y += S;
   PnlCell("r_session", x, y, W, H, PanelSession(), CLR_CELL, CLR_GREEN, 8);
   y += S;

   // Tab content lives under its own object prefix so switching tabs is a
   // delete of that prefix rather than a hunt for every object to hide.
   if(g_panelTabDrawn != g_panelTab)
   {
      ObjectsDeleteAll(0, PNL_PREFIX + "T_");
      g_panelTabDrawn = g_panelTab;
   }

   if(g_panelTab == "trades")  return PanelDrawTradesTab(x, y, W, H, S);
   if(g_panelTab == "levels")  return PanelDrawLevelsTab(x, y, W, H, S);
   return PanelDrawSignalsTab(x, y, W, H, S, W2);
}

int PanelDrawTradesTab(const int x, int y, const int W, const int H, const int S)
{
   PnlCell("T_thdr", x, y, W, H, "OPEN POSITIONS (THIS EA)", CLR_CELL2, CLR_CYAN, 8);
   y += S;
   int shown = 0;
   for(int i = 0; i < PositionsTotal() && shown < 10; i++)
   {
      ulong tkt = PositionGetTicket(i);
      if(tkt == 0) continue;
      if(PositionGetInteger(POSITION_MAGIC) != (long)InpMagic) continue;
      bool isBuy = PositionGetInteger(POSITION_TYPE) == POSITION_TYPE_BUY;
      double p = PositionGetDouble(POSITION_PROFIT);
      PnlCell("T_tr" + (string)shown, x, y, W, H,
              StringFormat("%s %.2f @ %s   SL %s   $%.2f",
                           isBuy ? "BUY" : "SELL",
                           PositionGetDouble(POSITION_VOLUME),
                           DoubleToString(PositionGetDouble(POSITION_PRICE_OPEN), _Digits),
                           DoubleToString(PositionGetDouble(POSITION_SL), _Digits),
                           p),
              CLR_CELL, p >= 0 ? CLR_GREEN : CLR_RED, 8);
      y += S;
      shown++;
   }
   if(shown == 0)
   {
      PnlCell("T_none", x, y, W, H, "No open positions", CLR_CELL, CLR_DIM, 8);
      y += S;
   }
   return y;
}

int PanelDrawLevelsTab(const int x, int y, const int W, const int H, const int S)
{
   PnlCell("T_lhdr", x, y, W, H, "REVERSAL ENGINE CANDIDATE LEVELS", CLR_CELL2, CLR_CYAN, 8);
   y += S;
   if(g_lvlCount == 0)
   {
      PnlCell("T_lnone", x, y, W, H,
              "No levels — reversal engine idle or not running", CLR_CELL, CLR_DIM, 8);
      return y + S;
   }
   for(int i = 0; i < g_lvlCount; i++)
   {
      PnlCell("T_lv" + (string)i, x, y, W, H,
              StringFormat("%s  %s  %+.2f  %s",
                           DoubleToString(g_lvlPrice[i], _Digits),
                           g_lvlDir[i], g_lvlPrice[i] - SymbolInfoDouble(_Symbol, SYMBOL_BID),
                           g_lvlKind[i]),
              CLR_CELL, g_lvlDir[i] == "BUY" ? CLR_GREEN : CLR_RED, 8);
      y += S;
   }
   return y;
}

int PanelDrawSignalsTab(const int x, int y, const int W, const int H,
                        const int S, const int W2)
{
   PnlCell("T_dhdr", x, y, W, H, "LIVE MARKET SIGNAL DASHBOARD", CLR_CELL2, CLR_CYAN, 9);
   y += S;
   PnlCell("T_bias", x, y, W, H, "HTF TREND BIAS: " + g_sigBias, CLR_CELL,
           g_sigBias == "BULLISH" ? CLR_GREEN
                                  : (g_sigBias == "BEARISH" ? CLR_RED : CLR_DIM), 9);
   y += S;

   // M5-D1 trend row, computed locally (see TfBullish).
   const int TW = 69, TS = 72;
   for(int i = 0; i < 6; i++)
   {
      bool ok;
      bool up = TfBullish(i, ok);
      PnlCell("T_tf" + (string)i, x + i * TS, y, TW, H, g_tfName[i],
              CLR_CELL, !ok ? CLR_DIM : (up ? CLR_GREEN : CLR_RED), 8);
   }
   y += S;

   PnlCell("T_scan", x, y, W, H, "SCANNER: " + g_sigScanner, clrBlack, CLR_TEXT, 8);
   y += S;
   PnlCell("T_head", x, y, W, H,
           g_sigHeadline == "" ? "CURRENT MARKET SIGNAL: --"
                               : "CURRENT MARKET SIGNAL: " + g_sigHeadline,
           CLR_CELL, CLR_GREEN, 8);
   y += S;
   PnlCell("T_buyc", x, y, W2, H,
           StringFormat("BUY CONF: %s (%d pts)", g_sigBuyGrade, g_sigBuyConf),
           CLR_CELL, CLR_GREEN, 8);
   PnlCell("T_sellc", x + W2 + PNL_GAP, y, W2, H,
           StringFormat("SELL CONF: %s (%d pts)", g_sigSellGrade, g_sigSellConf),
           CLR_CELL, CLR_RED, 8);
   y += S;

   PnlCell("T_chdr", x, y, W, H, "SIGNAL CRITERIA BREAKDOWN", CLR_CELL2, CLR_DIM, 8);
   y += S;
   PnlCrit("T_c1", x, y, W2, H, "HTF Bias Align", g_critBiasAlign);
   PnlCrit("T_c2", x + W2 + PNL_GAP, y, W2, H, "Active FVG", g_critFvg);
   y += S;
   PnlCrit("T_c3", x, y, W2, H, "Liquidity Sweep", g_critSweep);
   PnlCrit("T_c4", x + W2 + PNL_GAP, y, W2, H, "Displacement", g_critDisp);
   y += S;
   PnlCrit("T_c5", x, y, W2, H, "Inside Killzone", g_critKz);
   PnlCrit("T_c6", x + W2 + PNL_GAP, y, W2, H, "In Order Block", g_critOb);
   y += S;
   // The VWAP lamps are recomputed here every paint rather than taken from
   // the push: they flip with price, and a 3s-old answer next to a live BID
   // reads as a bug.
   double vw = PanelVwap();
   double bid = SymbolInfoDouble(_Symbol, SYMBOL_BID);
   PnlCrit("T_c7", x, y, W2, H, "VWAP Buy OK",  vw > 0.0 && bid >= vw);
   PnlCrit("T_c8", x + W2 + PNL_GAP, y, W2, H, "VWAP Sell OK", vw > 0.0 && bid <= vw);
   y += S;

   // ATR with a proportional bar. Bands are relative to the instrument's own
   // price so they mean the same thing on gold as on a currency pair.
   double atrv = PanelAtr();
   double atrPips = atrv / PipsToPrice(1.0);
   string band = atrPips < 5.0 ? "LOW" : (atrPips < 15.0 ? "MODERATE" : "HIGH");
   PnlRect("T_atr_bg", x, y, W, H, CLR_CELL);
   int fill = (int)MathMin(W - 4, MathMax(2, (W - 4) * atrPips / 30.0));
   PnlRect("T_atr_fill", x + 2, y + 6, fill, H - 12, CLR_CYAN, CLR_CYAN);
   PnlLabel("T_atr_tx", x + W / 2, y + H / 2,
            StringFormat("ATR: %s (%.2f p)", band, atrPips), CLR_TEXT, 8);
   y += S;

   // ── System logs ──────────────────────────────────────────────────
   PnlCell("T_lghdr", x, y, W, H, "RECENT SYSTEM LOGS", CLR_CELL2, CLR_DIM, 8);
   y += S;
   for(int i = 0; i < PNL_LOG_N; i++)
   {
      PnlCell("T_lg" + (string)i, x, y, W, H,
              i < g_logCount ? g_logLine[i] : "", CLR_BG, CLR_YELLOW, 7);
      y += S;
   }
   return y;
}

//+------------------------------------------------------------------+
//| Paint                                                             |
//+------------------------------------------------------------------+
void PanelUpdate()
{
   if(!g_panelBuilt) PanelBuild();
   int yl = PanelDrawLeft();
   int yr = PanelDrawRight();
   // Resize the backing rectangles to whatever the layout actually used, so
   // a tab with fewer rows doesn't leave a slab of empty background.
   PnlRect("BGL", PNL_X - PNL_PAD, PNL_Y - PNL_PAD,
           PNL_LW + PNL_PAD * 2, yl - PNL_Y + PNL_PAD * 2, CLR_BG);
   PnlRect("BGR", PNL_RX - PNL_PAD, PNL_Y - PNL_PAD,
           PNL_RW + PNL_PAD * 2, yr - PNL_Y + PNL_PAD * 2, CLR_BG);
   ChartRedraw(0);
}

//+------------------------------------------------------------------+
//| Outbound actions                                                  |
//|                                                                   |
//| A click never mutates local trading state -- it asks the app to,   |
//| and the app's set_template reply is what actually moves the panel. |
//| One authority, so a chart click and an app edit cannot disagree.   |
//+------------------------------------------------------------------+
void PanelSendAction(const string action, const string value)
{
   if(g_socket == INVALID_HANDLE)
   {
      PanelLog("NO APP LINK — " + action + " not sent");
      return;
   }
   SendJson("{\"type\":\"panel_action\",\"action\":\"" + JsonEsc(action) +
            "\",\"value\":\"" + JsonEsc(value) +
            "\",\"template\":\"" + JsonEsc(g_panelTemplate) + "\"}");
}

// Order actions carry finished prices. The EA owns _Point and the pip
// convention (see PipsToPrice), so it does that arithmetic once here rather
// than having the app re-derive it from a second copy of the same rules.
void PanelSendOrder(const string action, const bool isBuy, const bool isLimit)
{
   if(g_socket == INVALID_HANDLE)
   {
      PanelLog("NO APP LINK — order not sent");
      return;
   }
   MqlTick tk;
   if(!SymbolInfoTick(_Symbol, tk)) { PanelLog("No tick — order not sent"); return; }

   double lots = isLimit ? PnlD("lot_pending", 0.01) : PnlD("lot_anchor", 0.01);
   double slPips = PnlD("sl_pips", 50.0);
   double px = isBuy ? tk.ask : tk.bid;
   if(isLimit)
      px = isBuy ? tk.bid - PipsToPrice(g_limitOffset)
                 : tk.ask + PipsToPrice(g_limitOffset);
   double sl = isBuy ? px - PipsToPrice(slPips) : px + PipsToPrice(slPips);

   string tps = "";
   for(int n = 1; n <= 5; n++)
   {
      double tpPips = PnlD((isLimit ? "tp_pen" : "tp") + (string)n + "_pips", 0.0);
      if(tpPips <= 0.0) continue;
      double tp = isBuy ? px + PipsToPrice(tpPips) : px - PipsToPrice(tpPips);
      tps += ",\"tp" + (string)n + "\":" + DoubleToString(tp, _Digits);
   }

   SendJson("{\"type\":\"panel_action\",\"action\":\"" + JsonEsc(action) +
            "\",\"value\":\"\",\"template\":\"" + JsonEsc(g_panelTemplate) +
            "\",\"price\":" + DoubleToString(px, _Digits) +
            ",\"sl\":" + DoubleToString(sl, _Digits) +
            ",\"lots\":" + DoubleToString(lots, 2) + tps + "}");
   PanelLog((isBuy ? "BUY" : "SELL") + (isLimit ? " LIMIT" : "") +
            " requested @ " + DoubleToString(px, _Digits));
}

//+------------------------------------------------------------------+
//| Input                                                             |
//+------------------------------------------------------------------+

// Numeric edits map 1:1 onto template fields by name: an object called
// "f_<field>" sends "<field>". That is why there is no per-field click
// handling below and why a new numeric field costs exactly one PnlEdit call
// in the layout.
void PanelHandleEdit(const string objName)
{
   string key = StringSubstr(objName, 2);      // strip "f_"
   string raw = ObjectGetString(0, PNL_PREFIX + objName, OBJPROP_TEXT);
   StringReplace(raw, "%", "");
   StringReplace(raw, " ", "");
   if(raw == "") return;

   if(key == "limit_offset")
   {
      // Local: how far from price a LIMIT is placed. Not a template field --
      // it is an intent for the next click, not saved configuration.
      g_limitOffset = MathMax(0.0, StringToDouble(raw));
      PnlEditCommitted(objName);
      return;
   }
   PanelSendAction(key, DoubleToString(StringToDouble(raw), 4));
   PnlEditCommitted(objName);
}

void OnChartEvent(const int id, const long &lparam, const double &dparam, const string &sparam)
{
   if(StringFind(sparam, PNL_PREFIX) != 0) return;
   string what = StringSubstr(sparam, StringLen(PNL_PREFIX));

   if(id == CHARTEVENT_OBJECT_ENDEDIT)
   {
      if(StringFind(what, "f_") == 0) PanelHandleEdit(what);
      return;
   }
   if(id != CHARTEVENT_OBJECT_CLICK) return;
   ObjectSetInteger(0, sparam, OBJPROP_STATE, false);   // momentary

   // ── Local view state ─────────────────────────────────────────────
   if(what == "sound")     { g_panelSound  = !g_panelSound;  PanelUpdate(); return; }
   if(what == "manual")    { g_panelManual = !g_panelManual; PanelUpdate(); return; }
   if(what == "tabtrades") { g_panelTab = "trades"; PanelUpdate(); return; }
   if(what == "rtab0")     { g_panelTab = "trades";  PanelUpdate(); return; }
   if(what == "rtab1")     { g_panelTab = "levels";  PanelUpdate(); return; }
   if(what == "rtab2")     { g_panelTab = "signals"; PanelUpdate(); return; }
   if(what == "tptype")
   {
      g_tpType = (g_tpType == "anchor") ? "pending" : "anchor";
      // The ten pips/pct boxes now address different template fields, so the
      // old objects are deleted rather than relabelled. PnlEdit treats a
      // recreated box as fresh and writes into it immediately, which is what
      // stops the mid-edit guard from seeing the other ladder's leftover
      // text and refusing to repaint. (Clearing the whole edit registry
      // instead would freeze every OTHER box on the panel for the same
      // reason -- their slots would be re-registered with no baseline.)
      ObjectsDeleteAll(0, PNL_PREFIX + "f_tp");
      PanelUpdate();
      return;
   }
   if(StringFind(what, "ch") == 0 && StringLen(what) == 3)
   {
      int slot = (int)StringToInteger(StringSubstr(what, 2));
      if(slot >= 0 && slot < PNL_MAX_CH)
      {
         g_chSel = slot;
         PanelSendAction("select_channel", (string)slot);
         PanelUpdate();
      }
      return;
   }
   if(what == "refresh") { PanelSendAction("refresh", ""); return; }

   // ── Template toggles: each sends the NEXT value, never applies it ──
   if(what == "b_harvest")
   { PanelSendAction("harvest_enabled", PnlB("harvest_enabled", false) ? "0" : "1"); return; }
   if(what == "b_mode")
   { PanelSendAction("mode", PnlS("mode", "single") == "grid" ? "single" : "grid"); return; }
   if(what == "b_tpsl")
   {
      string t = PnlS("tpsl_mode", "on");
      PanelSendAction("tpsl_mode",
         t == "on" ? "stealth" : (t == "stealth" ? "off" : "on"));
      return;
   }
   if(what == "b_anchor")
   {
      PanelSendAction("anchor",
         PnlS("anchor", "unified") == "unified" ? "distributed" : "unified");
      return;
   }
   if(what == "b_bemode")
   {
      PanelSendAction("be_mode",
         PnlS("be_mode", "entry") == "entry" ? "entry_buffer" : "entry");
      return;
   }
   if(what == "b_dellimits")
   {
      // 0 means "never cancel", so the cycle runs 0..5 and wraps -- matching
      // core_ea_templates' own floor of 0 for cancel_pending_level.
      int lvl = PnlI("cancel_pending_level", 0);
      PanelSendAction("cancel_pending_level", (string)((lvl + 1) % 6));
      return;
   }
   if(what == "b_betrig")
   {
      int be = PnlI("be_trigger", 1);
      PanelSendAction("be_trigger", (string)(be >= 5 ? 1 : be + 1));
      return;
   }
   if(what == "b_trail")
   {
      string t = PnlS("trail_mode", "off");
      string nxt = "off";
      if(t == "off")          nxt = "tp";
      else if(t == "tp")      nxt = "step";
      else if(t == "step")    nxt = "candle";
      else if(t == "candle")  nxt = "fractal";
      PanelSendAction("trail_mode", nxt);
      return;
   }
   if(what == "b_sigguard")
   {
      // One button, two fields: cycles OFF -> ALL -> 10p -> 20p -> 50p. The
      // pips value only means anything with the guard on, so the two are
      // always sent as a pair rather than left in a state the app would
      // have to interpret.
      bool on = PnlB("sig_guard", false);
      double p = PnlD("sig_guard_pips", 0.0);
      if(!on)               { PanelSendAction("sig_guard", "1");
                              PanelSendAction("sig_guard_pips", "0"); }
      else if(p <= 0.0)       PanelSendAction("sig_guard_pips", "10");
      else if(p < 20.0)       PanelSendAction("sig_guard_pips", "20");
      else if(p < 50.0)       PanelSendAction("sig_guard_pips", "50");
      else                    PanelSendAction("sig_guard", "0");
      return;
   }

   // ── Entry management ─────────────────────────────────────────────
   // Armed by MANUAL so a misclick on a chart cannot open a position.
   if(what == "b_sell" || what == "b_buy" ||
      what == "b_selllimit" || what == "b_buylimit")
   {
      if(!g_panelManual) { PanelLog("Press MANUAL to arm entry buttons"); PanelUpdate(); return; }
      if(what == "b_sell")       PanelSendOrder("market_sell", false, false);
      else if(what == "b_buy")   PanelSendOrder("market_buy",  true,  false);
      else if(what == "b_selllimit") PanelSendOrder("limit_sell", false, true);
      else                       PanelSendOrder("limit_buy",  true,  true);
      PanelUpdate();
      return;
   }
   if(what == "b_cancel" || what == "b_closeall")
   {
      string act = (what == "b_cancel") ? "cancel_limits" : "close_all";
      if(g_socket != INVALID_HANDLE)
      {
         // Normal path: the app does it, so its own rows close out with it.
         PanelSendAction(act, "");
         return;
      }
      // Disconnected fallback. These two are the emergency controls -- a
      // dead socket is exactly when they matter most, so they still act
      // locally rather than becoming inert. Magic-filtered: never touches
      // anything this EA did not place. The app reconciles on reconnect.
      int n = 0;
      if(what == "b_cancel")
      {
         for(int i = OrdersTotal() - 1; i >= 0; i--)
         {
            ulong tkt = OrderGetTicket(i);
            if(tkt == 0) continue;
            if(OrderGetInteger(ORDER_MAGIC) != (long)InpMagic) continue;
            if(trade.OrderDelete(tkt)) n++;
         }
         PanelLog("OFFLINE: cancelled " + (string)n + " pending");
      }
      else
      {
         for(int i = PositionsTotal() - 1; i >= 0; i--)
         {
            ulong tkt = PositionGetTicket(i);
            if(tkt == 0) continue;
            if(PositionGetInteger(POSITION_MAGIC) != (long)InpMagic) continue;
            if(trade.PositionClose(tkt)) n++;
         }
         PanelLog("OFFLINE: closed " + (string)n + " position(s)");
      }
      Print("[EABridge][Panel] offline ", act, ": ", n);
      PanelUpdate();
      return;
   }
}


int OnInit()
{
   EventSetMillisecondTimer(200);
   BuildPortList();
   EnsureConnected();
   if(InpShowPanel) PanelUpdate();
   return(INIT_SUCCEEDED);
}

void OnDeinit(const int reason)
{
   EventKillTimer();
   if(g_socket != INVALID_HANDLE) SocketClose(g_socket);
   g_socket = INVALID_HANDLE;
   g_connected = false;
   g_linkConfirmed = false;
   // The panel's trend row and ATR gauge hold indicator handles. A chart
   // reload calls OnInit again and would otherwise leak one handle per
   // timeframe per reload, until iMA starts returning INVALID_HANDLE and the
   // row silently goes grey.
   for(int i = 0; i < 6; i++)
      if(g_tfMa[i] >= 0) { IndicatorRelease(g_tfMa[i]); g_tfMa[i] = -1; }
   if(g_atrHandle >= 0) { IndicatorRelease(g_atrHandle); g_atrHandle = -1; }
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
