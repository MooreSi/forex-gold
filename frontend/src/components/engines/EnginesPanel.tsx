import { FlaskConical } from "lucide-react";
import { Button } from "@/components/shared/Button";
import { EmptyState } from "@/components/shared/EmptyState";
import { PanelShell } from "@/components/shared/PanelShell";
import { asObject } from "@/lib/asArray";
import { useEnginesController } from "./hooks/useEnginesController";
import { CapabilitiesSection } from "./internal/CapabilitiesSection";
import { ControlTargetBanner } from "./internal/ControlTargetBanner";
import { EngineCard } from "./internal/EngineCard";
import { BreakoutSection } from "./internal/BreakoutSection";
import { ModelSection } from "./internal/ModelSection";
import { ShadowSection } from "./internal/ShadowSection";

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

          <section className="border-t border-line pt-3">
            {/* The Breakout engine's own panel. Its reads have existed since
                the restructure and nothing called them: this tab showed the
                Reversal engine in detail and said nothing about Breakout
                beyond its Start/Stop card. */}
            <h3 className="mb-2 text-xs font-semibold text-ink-1">
              Breakout engine
            </h3>
            <BreakoutSection />
          </section>

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
