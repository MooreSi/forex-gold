import { useState } from "react";
import { Play, RefreshCw, Sparkles } from "lucide-react";
import { Button } from "@/components/shared/Button";
import { EmptyState } from "@/components/shared/EmptyState";
import { formatClock, formatPrice } from "@/components/shared/format";
import { useSetForgetController } from "./hooks/useSetForgetController";
import { ConfluenceSection } from "./internal/ConfluenceSection";
import { ExecuteSetupDialog } from "./internal/ExecuteSetupDialog";
import { IndicatorStrip } from "./internal/IndicatorStrip";
import { LotSizeSection } from "./internal/LotSizeSection";
import { NoSetupSection } from "./internal/NoSetupSection";
import { ReviewSection } from "./internal/ReviewSection";
import { SetupChart } from "./internal/SetupChart";
import { SetupSummarySection } from "./internal/SetupSummarySection";
import { StrategyBriefSection } from "./internal/StrategyBriefSection";

/**
 * Trading > Set & Forget. Composition and one piece of local UI state.
 *
 * The section reads the chart for free and only bills when Evaluate is
 * pressed; Execute is gated behind a confirmation that names every number.
 * Everything that decides anything lives in the controller hook and the
 * backend — this file is the arrangement.
 */
export function SetForgetPanel() {
  const c = useSetForgetController();
  const [confirming, setConfirming] = useState(false);
  const data = c.data;
  const candidate = c.candidate;

  const lots = c.lots;
  const riskMoney = lots && data?.risk_per_lot != null ? data.risk_per_lot * lots : null;
  const rewardMoney = lots && data?.reward_per_lot != null
    ? data.reward_per_lot * lots : null;

  // Refused outright, so the button must not be pressable. The reason travels
  // with it: a greyed-out Execute with no explanation reads as a broken one.
  const blocked = !candidate
    ? "There is no setup to place."
    : (data?.invalidations.length ?? 0) > 0
      ? "This setup does not meet the method's rules — see the reasons above."
      : null;

  return (
    <div className="space-y-3 pb-2">
      <header className="flex flex-wrap items-center justify-between gap-3">
        <div className="min-w-0">
          <h2 className="text-sm font-semibold text-ink-1">Set &amp; Forget</h2>
          <p className="text-[11px] text-ink-3">
            XAUUSD · {data?.evidence.entry_timeframe ?? "4H"} entries from the
            Weekly and Daily read
            {data?.price ? ` · ${formatPrice(data.price)}` : ""}
            {data?.generated_at
              ? ` · read at ${formatClock(data.generated_at)}`
              : ""}
          </p>
        </div>
        <div className="flex shrink-0 items-center gap-2">
          {/* Not `ghost`. A transparent, borderless control next to two
              bordered ones reads as decoration — "what does refresh do, does
              it do anything as i cant click it", 2026-09-22. It is a button,
              so it looks like one, and it spins while it is working. */}
          <Button
            onClick={() => void c.refresh()}
            disabled={c.refreshing}
            tooltip="Re-read the chart, the zones and the checklist. Free — no model is asked."
          >
            <RefreshCw size={12} className={c.refreshing ? "animate-spin" : undefined} />
            {c.refreshing ? "Refreshing…" : "Refresh"}
          </Button>
          <Button
            onClick={() => void c.evaluate()}
            disabled={c.evaluating}
            disabledReason={
              data && !data.ai_configured
                ? "No AI provider is configured — set one in Settings > AI. "
                  + "The rules-based reading on this page works without one."
                : null
            }
            title={data?.ai_model ? `Bills ${data.ai_model}` : undefined}
          >
            <Sparkles size={13} />
            {c.evaluating ? "Evaluating…" : "Evaluate the market"}
          </Button>
          <Button
            variant={candidate?.direction === "SELL" ? "danger" : "success"}
            onClick={() => { c.setOutcome(null); setConfirming(true); }}
            disabled={c.placing}
            disabledReason={blocked}
          >
            <Play size={12} />
            {candidate?.order_type === "limit" ? "Place limit order" : "Execute"}
          </Button>
        </div>
      </header>

      {/* Directly under the buttons, not at the foot of the page. Every one of
          these lines is the answer to something the operator just pressed, and
          the page below is long enough that an answer rendered last is an
          answer nobody scrolls to — which is how "it just flickers and doesn't
          appear to analyse the market" (2026-09-22) survived having a message
          written for it. */}
      {c.outcome && (
        <p
          role={c.outcome.ok ? "status" : "alert"}
          className={`rounded-md border px-3 py-2 text-[11px] ${
            c.outcome.ok
              ? "border-profit/40 bg-profit/10 text-profit"
              : "border-loss/40 bg-loss/10 text-loss"
          }`}
        >
          {c.outcome.text}
        </p>
      )}

      {data?.ai_configured === false && (
        <p className="rounded-md border border-line bg-surface-2/60 px-3 py-2
                      text-[11px] text-ink-3">
          Everything on this page is computed from the chart and costs nothing.
          Configure a provider in Settings &gt; AI to add a model&apos;s review
          on top.
        </p>
      )}

      <StrategyBriefSection />

      {!data ? (
        <EmptyState
          title={c.error ? "The chart could not be read" : "Reading the chart…"}
          hint={c.error?.message
            ?? "Weekly, Daily and 4H are being read from the bridge."}
        />
      ) : (
        <>
          <SetupChart
            candles={c.candles}
            overlays={c.overlays}
            zones={data.evidence.zones}
            fibLevels={data.evidence.fib_levels}
            candidate={candidate}
            riskMoney={riskMoney}
            rewardMoney={rewardMoney}
          />

          <IndicatorStrip evidence={data.evidence} />

          {candidate ? (
            <SetupSummarySection
              candidate={candidate}
              evidence={data.evidence}
              minRr={data.min_rr}
              riskMoney={riskMoney}
              rewardMoney={rewardMoney}
              invalidations={data.invalidations}
            />
          ) : (
            <NoSetupSection reason={data.no_setup_reason} evidence={data.evidence} />
          )}

          {data.ai && <ReviewSection review={data.ai} />}

          <div className="grid gap-3 lg:grid-cols-2">
            <LotSizeSection
              lots={lots}
              onPick={(v) => void c.saveLotSize(v)}
              suggested={data.suggested_lot}
              riskPerLot={data.risk_per_lot}
              rewardPerLot={data.reward_per_lot}
              riskPct={data.risk_per_trade_pct}
              balance={data.balance}
            />
            <ConfluenceSection confluence={data.confluence} />
          </div>
        </>
      )}

      {candidate && data && (
        <ExecuteSetupDialog
          open={confirming}
          onOpenChange={setConfirming}
          candidate={candidate}
          lots={lots}
          riskMoney={riskMoney}
          rewardMoney={rewardMoney}
          controlTarget={data.control_target}
          busy={c.placing}
          onConfirm={() => { setConfirming(false); void c.execute(); }}
        />
      )}
    </div>
  );
}
