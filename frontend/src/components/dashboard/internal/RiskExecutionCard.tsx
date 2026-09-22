import { CircleCheck, CircleSlash, TriangleAlert } from "lucide-react";
import type { HeaderState } from "@/api/types";
import { formatPercent } from "@/components/shared/format";
import { asObject } from "@/lib/asArray";
import { cn } from "@/lib/cn";
import { DashCard, Pill, Reading } from "./DashCard";

interface RiskExecutionCardProps {
  risk: Record<string, unknown>;
  header: HeaderState | null;
}

function num(source: Record<string, unknown>, key: string): number | null {
  const raw = source[key];
  return typeof raw === "number" ? raw : null;
}

/**
 * The limits an order is checked against, and whether anything could be sent
 * at all.
 *
 * These are the STORED values — what the engines will actually read — not a
 * copy kept on this screen. The risk service clamps what is typed, so a card
 * showing the typed number would be describing limits the engine is not using.
 *
 * Nothing here is editable. The Trading tab's Risk section owns these, with
 * the wording that says what each one does; a spin box on a summary card is
 * how a loss limit gets changed by accident.
 *
 * The halt is the loudest thing on the card when there is one. A paused
 * trader that looks merely quiet is the failure this replaces: "why did it
 * not take that signal" has a one-line answer and it belongs on the screen
 * the operator is already looking at.
 */
export function RiskExecutionCard({ risk, header }: RiskExecutionCardProps) {
  const bridge = asObject(header?.bridge);
  const connected = bridge["connected"] === true;
  const pause = header?.pause;
  const halted = pause?.paused === true;
  const ea = header?.ea_badge ?? null;

  return (
    <DashCard
      title="Risk & execution"
      icon="risk"
      badge={halted
        ? <Pill tone="loss">halted</Pill>
        : <Pill tone={connected ? "profit" : "warning"}>
            {connected ? "MT5 connected" : "MT5 offline"}
          </Pill>}
      footnote="Stored limits, as the engines read them. Change them on the Trading tab's Risk section."
    >
      <dl className="grid grid-cols-2 gap-x-3 gap-y-2 sm:grid-cols-4">
        <Reading
          label="Risk / trade"
          value={formatPercent(num(risk, "risk_per_trade_pct"))}
          hint="Percentage of the account risked on each entry."
        />
        <Reading
          label="Daily loss cap"
          value={formatPercent(num(risk, "max_daily_loss_pct"))}
          hint="Measured from the day's opening balance."
        />
        <Reading
          label="Max open"
          value={num(risk, "max_open_trades") ?? "—"}
          hint="New entries are refused once this many positions are open."
        />
        <Reading
          label="Max lot"
          value={num(risk, "max_lot_size") ?? "—"}
          hint="A hard ceiling applied after sizing, whatever the risk maths asks for."
        />
      </dl>

      <div className="mt-2.5 space-y-1 border-t border-line/70 pt-2 text-[11px]">
        {halted && (
          <p
            data-testid="dash-halt"
            className="flex items-center gap-1.5 rounded border border-loss/40
                       bg-loss/10 px-2 py-1 text-loss"
          >
            <TriangleAlert size={12} className="shrink-0" aria-hidden />
            <span className="min-w-0">
              Trading is halted{pause?.reason ? `: ${pause.reason}` : ""}
              {pause?.source ? ` (${pause.source})` : ""}
            </span>
          </p>
        )}
        <State
          ok={connected}
          label="MT5 bridge"
          value={connected ? "connected" : "not answering"}
        />
        <State
          ok={ea ? !ea.stale : false}
          label="Expert Advisor"
          value={ea ? ea.text : "no report"}
        />
        <State
          ok
          label="Trading on"
          value={header?.active_trader ?? "—"}
        />
      </div>
    </DashCard>
  );
}

function State({ ok, label, value }: { ok: boolean; label: string; value: string }) {
  return (
    <p className="flex items-center gap-1.5">
      {ok
        ? <CircleCheck size={11} className="shrink-0 text-profit" aria-hidden />
        : <CircleSlash size={11} className="shrink-0 text-warning" aria-hidden />}
      <span className="text-ink-3">{label}</span>
      <span className={cn("ml-auto truncate text-right font-medium",
                          ok ? "text-ink-1" : "text-warning")}>
        {value}
      </span>
    </p>
  );
}
