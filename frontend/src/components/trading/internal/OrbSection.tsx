import { useCallback, useState } from "react";
import { CandlestickChart } from "lucide-react";
import { api, ApiError } from "@/api/client";
import { Button } from "@/components/shared/Button";
import { DialogShell } from "@/components/shared/DialogShell";
import { StatCard } from "@/components/shared/StatCard";
import { formatPrice } from "@/components/shared/format";
import { asObject } from "@/lib/asArray";
import { usePoll } from "@/hooks/usePoll";
import { OrbChart } from "./OrbChart";

interface OrbReport {
  direction: string;
  phase: string;
  current_price: number;
  asia_low: number; asia_high: number; asia_range: number;
  or_low: number; or_high: number; or_range: number;
  stop: number; target: number; target2: number | null; rr: number | null;
  position_note: string;
}

interface OrbState {
  report: OrbReport | null;
  /** Still served, and still used by the emailed report. The tab draws the
   *  live chart instead -- see OrbChart. */
  chart_png_base64: string | null;
  lot_size: number;
  auto_execute: boolean;
  control_target: string;
}

const STATUS: Record<string, { text: string; tone: string }> = {
  bullish: { text: "BREAKOUT — BULLISH", tone: "text-profit" },
  bearish: { text: "BREAKOUT — BEARISH", tone: "text-loss" },
  unconfirmed: { text: "BROKE OPENING RANGE — UNCONFIRMED", tone: "text-warning" },
  inside: { text: "INSIDE RANGE", tone: "text-ink-3" },
};

/**
 * The London opening-range breakout report, and the one press that trades it.
 *
 * Restored 2026-09-18. The NiceGUI Trading page had this card and the React
 * port dropped it, so the report, its chart, the Execute button, the lot size
 * and the unattended auto-execute were all unreachable.
 *
 * Classic ORB: the whole Asian session is a confirmation filter, the first
 * fifteen minutes of London is the traded range, and a breakout only counts
 * once price clears both in the same direction.
 *
 * **Execute opens a real position, so it asks first and the question names the
 * numbers.** The stop and target sent are the ones rendered here, not a fresh
 * read: the report moves as price does, and re-reading it on the way to the
 * broker would trade numbers that were never on screen.
 *
 * The chart is rendered by the backend. Building it here would be a second
 * implementation of the same maths, with a second chance to disagree with the
 * figures printed beside it.
 */
export function OrbSection() {
  const poll = usePoll<OrbState>(
    "trading/orb",
    useCallback(() => api.get<OrbState>("/api/trading/orb"), []),
    60_000,
  );
  const [asking, setAsking] = useState(false);
  const [busy, setBusy] = useState(false);
  const [outcome, setOutcome] = useState<{ ok: boolean; text: string } | null>(null);

  const data = poll.data;
  const report = data?.report ? asObject<OrbReport>(data.report) : null;

  async function save(body: Record<string, unknown>) {
    try {
      await api.put("/api/trading/orb/settings", body);
      await poll.refresh();
    } catch (e) {
      setOutcome({ ok: false, text: e instanceof ApiError ? e.message : String(e) });
    }
  }

  async function execute() {
    if (!report) return;
    setBusy(true);
    try {
      const res = await api.post<{ mt5_ticket?: number; entry_price?: number; where?: string }>(
        "/api/trading/orb/execute",
        {
          direction: report.direction === "bullish" ? "BUY" : "SELL",
          stop_loss: report.stop,
          take_profit: report.target,
        },
      );
      setOutcome({
        ok: true,
        text: `Opened at ${formatPrice(res.entry_price)} · ticket ${res.mt5_ticket ?? "—"}`
          + (res.where === "remote" ? " (on the remote node)" : ""),
      });
    } catch (e) {
      setOutcome({ ok: false, text: e instanceof ApiError ? e.message : String(e) });
    } finally {
      setBusy(false);
      setAsking(false);
    }
  }

  if (!data) return null;

  const status = STATUS[report?.direction ?? "inside"] ?? STATUS.inside;
  const confirmed = report?.direction === "bullish" || report?.direction === "bearish";
  const side = report?.direction === "bullish" ? "BUY" : "SELL";

  return (
    <section data-testid="orb-report" className="rounded border border-line p-3">
      <h3 className="flex items-center gap-2 text-xs font-semibold text-ink-1">
        <CandlestickChart size={13} className="text-warning" />
        London Open — ORB Report
      </h3>

      {!report ? (
        <p className="mt-1 text-[11px] text-ink-3">
          No ORB report available yet — this builds from the whole Asian session
          plus the first 15 minutes of London, and is only available from London
          open onward.
        </p>
      ) : (
        <>
          <div className="mt-1 flex flex-wrap items-center gap-3 text-[11px]">
            <span className="num text-ink-2">
              Current price {formatPrice(report.current_price)}
            </span>
            <span className={`font-semibold ${status.tone}`}>{status.text}</span>
          </div>

          {report.position_note && (
            <p className="mt-1 text-[11px] text-ink-3">{report.position_note}</p>
          )}

          <div className="mt-2 grid gap-2 sm:grid-cols-2">
            <StatCard
              label="Asian range (00:00–08:00 UTC)"
              value={`${formatPrice(report.asia_low)} – ${formatPrice(report.asia_high)}`}
              hint={`${report.asia_range?.toFixed(1)} pts`}
            />
            {report.phase !== "forming" && (
              <StatCard
                label="London opening range (08:00–08:15 UTC)"
                value={`${formatPrice(report.or_low)} – ${formatPrice(report.or_high)}`}
                hint={`${report.or_range?.toFixed(1)} pts`}
              />
            )}
          </div>

          {/* The live chart, not the server-rendered PNG.
              It was a matplotlib image base64'd into this payload: it could
              not be zoomed, panned or read against a moving price, and it
              looked nothing like the Chart tab three clicks away. The
              endpoint still returns the PNG for the emailed report, which is
              the one place a picture is the right answer. */}
          <div className="mt-3">
            <OrbChart bands={report} />
          </div>

          {confirmed && (
            <>
              <p className="mt-3 text-[10px] font-semibold uppercase tracking-wider text-warning">
                Breakout setup
              </p>
              <div className="mt-1 grid gap-2 sm:grid-cols-4">
                <StatCard label="Stop" value={formatPrice(report.stop)}
                  valueClassName="text-loss" />
                <StatCard label="Target (2:1)" value={formatPrice(report.target)}
                  valueClassName="text-profit" />
                <StatCard
                  label="Target 2 (3:1, info only)"
                  value={report.target2 ? formatPrice(report.target2) : "—"}
                />
                <StatCard label="R:R" value={report.rr ? `${report.rr.toFixed(2)}:1` : "—"} />
              </div>
              <p className="mt-1 text-[10px] text-ink-3">
                Stop is the midpoint of the London opening range. Target is 2x the
                resulting risk and is where the automated path closes fully — it
                does not manage a partial-close ladder. Target 2 is shown for
                reference only.
              </p>
            </>
          )}
        </>
      )}

      <div className="mt-3 flex flex-wrap items-end gap-3">
        <label className="text-xs text-ink-2">
          Lot size
          <input
            aria-label="ORB lot size"
            className="num mt-0.5 w-28 rounded border border-line bg-surface-1 px-2 py-1 text-ink-1"
            defaultValue={String(data.lot_size ?? 0)}
            onBlur={(e) => void save({ lot_size: Number(e.target.value) || 0 })}
          />
          <span className="mt-0.5 block text-[10px] text-ink-3">
            0 sizes it from your risk % and the stop distance.
          </span>
        </label>

        {confirmed && (
          <Button
            onClick={() => { setOutcome(null); setAsking(true); }}
            disabled={busy}
          >
            Execute {side} at market
          </Button>
        )}
      </div>

      <label className="mt-3 flex items-start gap-2 text-xs text-ink-2">
        <input
          type="checkbox"
          aria-label="Auto-execute this setup every morning (unattended)"
          className="accent-accent mt-0.5"
          checked={data.auto_execute === true}
          onChange={(e) => void save({ auto_execute: e.target.checked })}
        />
        <span>
          Auto-execute this setup every morning (unattended)
          <span className="mt-0.5 block text-[10px] text-ink-3">
            Places this trade automatically at 08:15 UK time each weekday using
            the recommendation above — no manual click. In Remote mode whichever
            node is the active trader executes it and the other stays silent, so
            there is no double trade. Off by default.
          </span>
        </span>
      </label>

      {outcome && (
        <p
          role={outcome.ok ? "status" : "alert"}
          className={`mt-2 text-[11px] ${outcome.ok ? "text-profit" : "text-loss"}`}
        >
          {outcome.text}
        </p>
      )}

      <DialogShell
        open={asking}
        onOpenChange={setAsking}
        title={`Open ${side} at market?`}
      >
        <div className="space-y-3 text-xs text-ink-2">
          <p className="rounded border border-warning/40 bg-warning/10 px-2 py-1.5 text-warning">
            This opens a real position on the {data.control_target === "remote"
              ? "remote node" : "account this app is pointed at"}.
          </p>
          {report && (
            <p className="num text-ink-3">
              {side} · stop {formatPrice(report.stop)} · target{" "}
              {formatPrice(report.target)} · lots{" "}
              {data.lot_size > 0 ? data.lot_size : "sized from risk %"}
            </p>
          )}
          <p className="text-ink-3">
            These are the numbers shown above, not a fresh reading — the report
            moves as price does.
          </p>
          <div className="flex justify-end gap-2 pt-1">
            <Button variant="ghost" onClick={() => setAsking(false)}>Cancel</Button>
            <Button disabled={busy} onClick={() => void execute()}>
              {busy ? "Opening…" : `Open ${side}`}
            </Button>
          </div>
        </div>
      </DialogShell>
    </section>
  );
}
