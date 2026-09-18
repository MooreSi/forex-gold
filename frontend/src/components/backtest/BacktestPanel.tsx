import { EmptyState } from "@/components/shared/EmptyState";
import { PanelShell } from "@/components/shared/PanelShell";
import { useBacktestController } from "./hooks/useBacktestController";
import { BacktestForm } from "./internal/BacktestForm";
import { BacktestResults } from "./internal/BacktestResults";

export function BacktestPanel() {
  const c = useBacktestController();

  return (
    <PanelShell title="Backtest" subtitle="walks recorded signals; places nothing">
      {!c.options ? (
        <EmptyState
          title={c.refusal ? "Could not load the backtest options" : "Loading"}
          hint={c.refusal ?? undefined}
        />
      ) : (
        <div className="space-y-5">
          <BacktestForm
            options={c.options}
            form={c.form}
            set={c.set}
            selected={c.selected}
            toggle={c.toggle}
            running={c.running}
            onRun={() => void c.run()}
          />
          {c.refusal && (
            <p
              role="alert"
              className="rounded border border-warning/40 bg-warning/10 px-3 py-2 text-xs text-warning"
            >
              {c.refusal}
            </p>
          )}
          {c.result && <BacktestResults result={c.result} />}
        </div>
      )}
    </PanelShell>
  );
}
