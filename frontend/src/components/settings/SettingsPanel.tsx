import * as Tabs from "@radix-ui/react-tabs";
import { PanelShell } from "@/components/shared/PanelShell";
import { cn } from "@/lib/cn";
import { ConnectionsTab } from "./tabs/ConnectionsTab";
import { DiagnosticsTab } from "./tabs/DiagnosticsTab";
import { Mt5Tab } from "./tabs/Mt5Tab";
import { RiskTab } from "./tabs/RiskTab";
import { TunablesTab } from "./tabs/TunablesTab";

/**
 * Settings, as one thin composer over one component per domain.
 *
 * The NiceGUI original reached 3,112 lines in a single module because every
 * feature that needed a setting was added to the same surface. Each tab here
 * owns one endpoint and knows nothing about the others.
 */
const TABS = [
  { id: "risk", label: "Risk", Panel: RiskTab },
  { id: "mt5", label: "MT5", Panel: Mt5Tab },
  { id: "connections", label: "Connections", Panel: ConnectionsTab },
  { id: "tunables", label: "Expert tunables", Panel: TunablesTab },
  { id: "diagnostics", label: "Diagnostics", Panel: DiagnosticsTab },
];

export function SettingsPanel() {
  return (
    <PanelShell title="Settings">
      <Tabs.Root defaultValue="risk" className="flex h-full min-h-0 flex-col">
        <Tabs.List className="mb-3 flex gap-1 border-b border-line">
          {TABS.map((t) => (
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
        {TABS.map(({ id, Panel }) => (
          <Tabs.Content key={id} value={id} className="min-h-0 flex-1 overflow-auto">
            <Panel />
          </Tabs.Content>
        ))}
      </Tabs.Root>
    </PanelShell>
  );
}
