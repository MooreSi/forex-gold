import { useCallback, useEffect, useState } from "react";
import { api, ApiError } from "@/api/client";
import { Button } from "@/components/shared/Button";
import { DialogShell } from "@/components/shared/DialogShell";
import { asObject } from "@/lib/asArray";

interface EaStatus {
  stale: boolean;
  detail: string;
  binary_shipped: boolean;
  platform: string;
}

interface InstallResult {
  report: Record<string, unknown>;
  needs_compile: boolean;
  compiled: boolean;
  next_step: string;
}

interface EaDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** The badge's tooltip, shown immediately so the dialog is never blank. */
  detail: string;
}

/**
 * What is stale about the EA, and the one button that fixes it.
 *
 * **It never claims a compile it did not perform.** MetaEditor exits 0 on a
 * build it never ran, and it cannot be driven headlessly under CrossOver at
 * all, so on macOS the honest flow is: copy the EA in, then one instruction to
 * press F7. A green "done" over an unchanged .ex5 would be exactly the silent
 * staleness this whole area exists to end.
 *
 * When the build ships a pre-compiled `.ex5` there is nothing for MetaEditor
 * to do and the install really is one click, on any platform.
 */
export function EaDialog({ open, onOpenChange, detail }: EaDialogProps) {
  const [status, setStatus] = useState<EaStatus | null>(null);
  const [result, setResult] = useState<InstallResult | null>(null);
  const [installing, setInstalling] = useState(false);
  const [refusal, setRefusal] = useState("");

  const load = useCallback(async () => {
    try {
      setStatus(await api.get<EaStatus>("/api/settings/ea"));
    } catch {
      // The badge's own detail is already on screen; a failed read of the
      // extra context is not worth an error over.
    }
  }, []);

  useEffect(() => {
    if (!open) return;
    setResult(null);
    setRefusal("");
    void load();
  }, [open, load]);

  const install = useCallback(async () => {
    setInstalling(true);
    setRefusal("");
    try {
      setResult(await api.post<InstallResult>("/api/settings/ea/install", {}));
    } catch (e) {
      setRefusal(e instanceof ApiError ? e.message : String(e));
    } finally {
      setInstalling(false);
    }
  }, []);

  const report = asObject(result?.report);
  const errors = (report["errors"] as string[] | undefined) ?? [];

  return (
    <DialogShell
      open={open}
      onOpenChange={onOpenChange}
      title="The EA on the chart is not the one this app ships"
      footer={
        <>
          <Button variant="ghost" onClick={() => onOpenChange(false)}>Close</Button>
          <Button
            variant="success"
            onClick={() => void install()}
            disabled={installing}
          >
            {installing ? "Installing..." : "Install the current EA"}
          </Button>
        </>
      }
    >
      <p className="text-xs leading-relaxed text-ink-1">{detail}</p>

      {status && !result && (
        <p className="mt-2 text-[11px] leading-relaxed text-ink-3">
          {status.binary_shipped
            ? "This build ships a compiled EA, so installing it needs no "
              + "MetaEditor: the file is copied into every MetaTrader on this "
              + "machine and the chart reloads it by itself."
            : "This build ships the EA source only, so after it is copied in "
              + "you will need to compile it once in MetaEditor (F7). "
              + "MetaEditor cannot be driven automatically on this platform, "
              + "and reporting a compile that did not happen is the exact "
              + "problem this warning exists to catch."}
        </p>
      )}

      {result && (
        <div className="mt-3 space-y-1.5">
          <p
            data-testid="ea-install-report"
            className="num text-[11px] text-ink-2"
          >
            {String(report["deployed"] ?? 0)} terminal(s) updated,
            {" "}{String(report["already_current"] ?? 0)} already current
            {" "}of {((report["targets"] as unknown[] | undefined) ?? []).length} found.
          </p>
          <p className={`text-xs leading-relaxed ${
            result.needs_compile ? "text-warning" : "text-profit"}`}>
            {result.next_step}
          </p>
          {errors.length > 0 && (
            <ul className="list-disc space-y-0.5 pl-4 text-[11px] text-loss">
              {errors.map((e, i) => <li key={i}>{e}</li>)}
            </ul>
          )}
        </div>
      )}

      {refusal && <p className="mt-2 text-[11px] text-loss">{refusal}</p>}
    </DialogShell>
  );
}
