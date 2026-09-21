import * as Tabs from "@radix-ui/react-tabs";
import { FlaskConical } from "lucide-react";
import { Button } from "@/components/shared/Button";
import { EmptyState } from "@/components/shared/EmptyState";
import { PanelShell } from "@/components/shared/PanelShell";
import { asObject } from "@/lib/asArray";
import { cn } from "@/lib/cn";
import { useEnginesController } from "./hooks/useEnginesController";
import { CapabilitiesSection } from "./internal/CapabilitiesSection";
import { ControlTargetBanner } from "./internal/ControlTargetBanner";
import { EngineCard } from "./internal/EngineCard";
import { BreakoutSection } from "./internal/BreakoutSection";
import { ModelSection } from "./internal/ModelSection";
import { LearningChartSection } from "./internal/LearningChartSection";
import { ShadowSection } from "./internal/ShadowSection";

// Reversal first, and the one the page opens on. Owner, 2026-09-21: "this is
// the main one" -- it is the engine that trades, and the one whose model,
// switches and ledger are read daily. Breakout keeps its own tab, unchanged.
const SUB_TABS = [
  { id: "reversal", label: "Reversal engine" },
  { id: "breakout", label: "Breakout engine" },
];

export function EnginesPanel() {
  const c = useEnginesController();
  const model = asObject(c.state.data?.pro_model);

  return (
    <PanelShell title="Signal Generator" subtitle="the engines that produce signals" icon="flask">
      {!c.state.data ? (
        <EmptyState
          title={c.state.error ? "Could not load the engines" : "Loading"}
          hint={c.state.error?.message}
        />
      ) : (
        <div className="space-y-4">
          <ControlTargetBanner target={String(c.state.data.control_target ?? "local")} />

          <div className="grid gap-2 sm:grid-cols-3">
            {c.engines.map((e) => (
              <EngineCard
                key={e.id}
                engine={e}
                busy={c.busy === e.id}
                onSetRunning={(id, running) => void c.setRunning(id, running)}
              />
            ))}
          </div>

          {c.refusal && (
            <p role="alert" className="rounded border border-warning/40 bg-warning/10 px-3 py-2 text-xs text-warning">
              {c.refusal}
            </p>
          )}

          {/* One engine at a time, as the NiceGUI Signal Generator tab had
              it (`frontend/pages/test_panel/__init__.py`: a Breakout tab and
              a Reversal Engine tab). The port put both engines' detail on one
              scrolling page, so reading the Breakout splits meant scrolling
              past the reversal model, its switches and its ledger. Start and
              Stop stay ABOVE the tabs: stopping an engine should not mean
              finding the right tab first. */}
          <Tabs.Root defaultValue="reversal" className="flex min-h-0 flex-1 flex-col">
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

            <Tabs.Content value="breakout">
              {/* Its reads have existed since the restructure and nothing
                  called them: this tab showed the Reversal engine in detail
                  and said nothing about Breakout beyond a Start/Stop card. */}
              <BreakoutSection />
            </Tabs.Content>

            <Tabs.Content value="reversal" className="space-y-4">
              <LearningChartSection
                metrics={asObject(asObject(c.report.data?.ml)["metrics"])}
                summary={asObject(asObject(c.report.data?.ml)["summary"])}
              />

              <section className="border-t border-line pt-3">
                <h3 className="mb-2 text-xs font-semibold text-ink-1">
                  Reversal engine capabilities
                </h3>
                <CapabilitiesSection
                  settings={asObject(c.state.data.settings)}
                  onSave={(key, value) => void c.saveSetting(key, value)}
                />
              </section>

              <section className="border-t border-line pt-3">
                <ModelSection model={model} />
                <div className="mt-2 flex flex-wrap gap-2">
                  <Button onClick={() => void c.refit()} disabled={c.busy === "fit"}>
                    {c.busy === "fit" ? "Starting…" : "Retrain in the background"}
                  </Button>
                  <Button variant="ghost" onClick={() => void c.runStudy()} disabled={c.busy === "study"}>
                    <FlaskConical size={13} />
                    {c.busy === "study" ? "Running…" : "Run the research study"}
                  </Button>
                </div>
                <p className="mt-1 text-[11px] text-ink-3">
                  Retraining runs in the background. Doing it inline would freeze the
                  dashboard, the EA socket and the monitor loop together.
                </p>
              </section>

              <section className="border-t border-line pt-3">
                <ShadowSection
                  shadow={c.report.data?.shadow}
                  history={c.report.data?.history}
                  realised={asObject(c.report.data?.realised)}
                  edge={asObject<Record<string, unknown>>(c.report.data?.edge)}
                />
              </section>
            </Tabs.Content>
          </Tabs.Root>

          {c.study && (
            <pre
              data-testid="study-report"
              className="max-h-80 overflow-auto whitespace-pre-wrap rounded border border-line bg-surface-1 p-3 text-[11px] text-ink-2"
            >
              {c.study}
            </pre>
          )}
        </div>
      )}
    </PanelShell>
  );
}
