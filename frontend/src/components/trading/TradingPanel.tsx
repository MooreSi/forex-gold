import * as Tabs from "@radix-ui/react-tabs";
import { useState } from "react";
import { Plus, TriangleAlert } from "lucide-react";
import { Button } from "@/components/shared/Button";
import { PanelShell } from "@/components/shared/PanelShell";
import { useTradingController } from "./hooks/useTradingController";
import { ActiveTradesSection } from "./internal/ActiveTradesSection";
import { SignalsSection } from "./internal/SignalsSection";
import { PlaceOrderDialog } from "./PlaceOrderDialog";
import { cn } from "@/lib/cn";
import { asArray } from "@/lib/asArray";
import type { Trade } from "@/api/types";

const SUB_TABS = [
  { id: "positions", label: "Positions" },
  { id: "signals", label: "Signals" },
];

/** Thin wrapper: composition, one piece of local UI state (which dialog is
 *  open), and nothing else. */
export function TradingPanel() {
  const c = useTradingController();
  const [placing, setPlacing] = useState(false);

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
        <Tabs.Content value="signals" className="min-h-0 flex-1 overflow-auto">
          <SignalsSection signals={asArray<Record<string, unknown>>(c.signals.data)} />
        </Tabs.Content>
      </Tabs.Root>

      <PlaceOrderDialog
        open={placing}
        onOpenChange={setPlacing}
        onPlaced={() => void c.refreshAll()}
        disabledReason={c.disabledReason}
      />
    </PanelShell>
  );
}
