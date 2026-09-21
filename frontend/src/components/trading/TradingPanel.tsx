import * as Tabs from "@radix-ui/react-tabs";
import { useState } from "react";
import { Plus, TriangleAlert } from "lucide-react";
import { Button } from "@/components/shared/Button";
import { PanelShell } from "@/components/shared/PanelShell";
import { useTradingController } from "./hooks/useTradingController";
import { ActiveTradesSection } from "./internal/ActiveTradesSection";
import { SignalsSection } from "./internal/SignalsSection";
import { PlaceLimitOrderDialog } from "./PlaceLimitOrderDialog";
import { PlaceOrderDialog } from "./PlaceOrderDialog";
import { ScheduleSection } from "./internal/ScheduleSection";
import { OrbSection } from "./internal/OrbSection";
import { SetForgetPanel } from "@/components/setforget/SetForgetPanel";
import { RiskSection } from "./internal/RiskSection";
import { StrategySection } from "./internal/StrategySection";
import { TemplatesSection } from "./internal/TemplatesSection";
import { cn } from "@/lib/cn";
import { asArray } from "@/lib/asArray";
import type { Trade } from "@/api/types";

const SUB_TABS = [
  { id: "positions", label: "Positions" },
  { id: "setforget", label: "Set & Forget" },
  { id: "signals", label: "Signals" },
  { id: "schedule", label: "Schedule" },
  { id: "risk", label: "Risk" },
  { id: "strategy", label: "Strategy" },
  { id: "templates", label: "EA templates" },
  { id: "orb", label: "ORB report" },
];

/** Thin wrapper: composition, one piece of local UI state (which dialog is
 *  open), and nothing else. */
export function TradingPanel() {
  const c = useTradingController();
  const [placing, setPlacing] = useState(false);
  const [placingLimit, setPlacingLimit] = useState(false);

  return (
    <PanelShell
      title="Trading"
      subtitle="XAUUSD"
      actions={
        <>
          {c.disabledReason && (
            <span
              role="status"
              className="flex items-center gap-1 rounded border border-warning/40 bg-warning/10 px-2 py-0.5 text-[11px] text-warning"
            >
              <TriangleAlert size={12} />
              {c.disabledReason}
            </span>
          )}
          <Button
            variant="success"
            onClick={() => setPlacing(true)}
            disabledReason={c.disabledReason}
          >
            <Plus size={13} /> Market order
          </Button>
          <Button
            onClick={() => setPlacingLimit(true)}
            disabledReason={c.disabledReason}
          >
            <Plus size={13} /> Limit order
          </Button>
        </>
      }
    >
      <Tabs.Root defaultValue="positions" className="flex h-full min-h-0 flex-col">
        <Tabs.List className="mb-3 flex gap-1 border-b border-line">
          {SUB_TABS.map((t) => (
            <Tabs.Trigger
              key={t.id}
              value={t.id}
              className={cn(
                "-mb-px border-b-2 px-3 py-1.5 text-xs transition-colors",
                "border-transparent text-ink-3 hover:text-ink-2",
                "data-[state=active]:border-accent data-[state=active]:text-ink-1",
              )}
            >
              {t.label}
            </Tabs.Trigger>
          ))}
        </Tabs.List>

        <Tabs.Content value="positions" className="min-h-0 flex-1 overflow-auto">
          <ActiveTradesSection
            trades={asArray<Trade>(c.trades.data)}
            disabledReason={c.disabledReason}
            onChanged={() => void c.refreshAll()}
          />
        </Tabs.Content>
        <Tabs.Content value="setforget" className="min-h-0 flex-1 overflow-auto">
          <SetForgetPanel />
        </Tabs.Content>
        <Tabs.Content value="signals" className="min-h-0 flex-1 overflow-auto">
          <SignalsSection
            signals={asArray<Record<string, unknown>>(c.signals.data)}
            onChanged={() => void c.refreshAll()}
          />
        </Tabs.Content>
        <Tabs.Content value="schedule" className="min-h-0 flex-1 overflow-auto">
          <ScheduleSection
            state={c.schedule}
            onSetEnabled={(v) => void c.setScheduleEnabled(v)}
            onSetSchedule={(v) => void c.setSchedule(v)}
            onSetTarget={(v) => void c.setDailyTarget(v)}
            onResumeToday={() => void c.resumeToday()}
            onSetMarket={(m, v) => void c.setMarket(m, v)}
            onSetClockOffset={(v) => void c.setClockOffset(v)}
          />
        </Tabs.Content>
        <Tabs.Content value="strategy" className="min-h-0 flex-1 overflow-auto">
          {/* Which strategy each channel trades under. Never ported until
              2026-09-19; its endpoints were all there. */}
          <StrategySection />
        </Tabs.Content>
        <Tabs.Content value="risk" className="min-h-0 flex-1 overflow-auto">
          {/* Moved here from Settings on 2026-09-19: these are the sizing and
              loss limits the engines read before every order, not preferences. */}
          <RiskSection />
        </Tabs.Content>
        <Tabs.Content value="orb" className="min-h-0 flex-1 overflow-auto">
          <OrbSection />
        </Tabs.Content>
        <Tabs.Content value="templates" className="min-h-0 flex-1 overflow-auto">
          <TemplatesSection
            templates={c.templates}
            eaConnected={c.eaConnected}
            eaLastSeen={c.eaLastSeen}
            onSave={c.saveTemplate}
            onDelete={(name) => void c.deleteTemplate(name)}
            onInstallBuiltin={() => void c.installBuiltin()}
            onImported={() => void c.refreshTemplates()}
          />
        </Tabs.Content>
      </Tabs.Root>

      <PlaceOrderDialog
        open={placing}
        onOpenChange={setPlacing}
        onPlaced={() => void c.refreshAll()}
        disabledReason={c.disabledReason}
      />
      <PlaceLimitOrderDialog
        open={placingLimit}
        onOpenChange={setPlacingLimit}
        onPlaced={() => void c.refreshAll()}
        disabledReason={c.disabledReason}
      />
    </PanelShell>
  );
}
