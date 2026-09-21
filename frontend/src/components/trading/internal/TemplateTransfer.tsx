import { useRef, useState } from "react";
import { Download, Upload } from "lucide-react";
import { api, ApiError } from "@/api/client";
import { Button } from "@/components/shared/Button";
import { Tooltip } from "@/components/shared/Tooltip";
import { asArray } from "@/lib/asArray";

/**
 * A File's text, without `File.prototype.text`.
 *
 * jsdom does not implement it, so the component that used it read every
 * import as "chosen.text is not a function" under test while working in the
 * browser -- a difference that hides exactly the kind of bug the tests are
 * there to catch. FileReader is the portable route and is what the app
 * actually needs: a file the operator picked, as a string.
 */
function readText(file: File): Promise<string> {
  if (typeof file.text === "function") return file.text();
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onerror = () => reject(new Error("That file could not be read."));
    reader.onload = () => resolve(String(reader.result ?? ""));
    reader.readAsText(file);
  });
}

interface TemplateTransferProps {
  /** Called once an import has written something, so the list reloads. */
  onImported: () => void;
}

interface ExportBody { content?: string; filename?: string }
interface ImportBody { added?: unknown; replaced?: unknown; skipped?: unknown }

/**
 * Take EA templates off this machine, and bring someone else's on.
 *
 * Owner, 2026-09-21. The service half has been in the tree since the NiceGUI
 * app (`ea_templates.export_templates` / `import_templates`, with the
 * envelope, the validate-everything-before-writing rule and the overwrite
 * guard); only the buttons were never ported, so a tuned set of templates
 * could not leave the machine it was tuned on.
 *
 * **Overwrite is opt-in and the skip list is reported.** A file from another
 * machine silently replacing a locally tuned template is the damage this
 * feature could do, so the default is the safe one and the screen says what
 * it left alone — silence there reads as "the file was empty", which is a
 * different and wrong conclusion.
 */
export function TemplateTransfer({ onImported }: TemplateTransferProps) {
  const fileRef = useRef<HTMLInputElement>(null);
  const [chosen, setChosen] = useState<File | null>(null);
  const [overwrite, setOverwrite] = useState(false);
  const [busy, setBusy] = useState<"export" | "import" | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [said, setSaid] = useState<string | null>(null);

  const failed = (e: unknown) =>
    setError(e instanceof ApiError || e instanceof Error ? e.message : String(e));

  async function exportAll() {
    setBusy("export"); setError(null); setSaid(null);
    try {
      const r = await api.get<ExportBody>("/api/trading/templates/export");
      // Everything that follows is the browser's own save-as. There is no
      // API for "write this file", so the anchor is the mechanism.
      const url = URL.createObjectURL(
        new Blob([r?.content ?? ""], { type: "application/json" }));
      const a = document.createElement("a");
      a.href = url;
      a.download = r?.filename ?? "ea_templates.eatpl.json";
      document.body.appendChild(a);
      a.click();
      a.remove();
      URL.revokeObjectURL(url);
    } catch (e) {
      failed(e);
    } finally {
      setBusy(null);
    }
  }

  async function importChosen() {
    if (!chosen) return;
    setBusy("import"); setError(null); setSaid(null);
    try {
      const content = await readText(chosen);
      const r = await api.post<ImportBody>(
        "/api/trading/templates/import", { content, overwrite });
      const added = asArray<string>(r?.added);
      const replaced = asArray<string>(r?.replaced);
      const skipped = asArray<string>(r?.skipped);
      const parts = [
        `${added.length} added`,
        `${replaced.length} replaced`,
      ];
      if (skipped.length) {
        parts.push(
          `${skipped.length} already here and left alone (${skipped.join(", ")}) — `
          + "tick Replace and import again to take them");
      }
      setSaid(parts.join(", "));
      if (added.length || replaced.length) onImported();
      setChosen(null);
      if (fileRef.current) fileRef.current.value = "";
    } catch (e) {
      failed(e);
    } finally {
      setBusy(null);
    }
  }

  return (
    <div className="flex flex-wrap items-center gap-2">
      <Button variant="ghost" onClick={() => void exportAll()} disabled={busy === "export"}>
        <Download size={13} />
        {busy === "export" ? "Exporting…" : "Export all"}
      </Button>

      <label className="flex items-center gap-1.5 text-[11px] text-ink-3">
        <Upload size={13} aria-hidden />
        <span className="sr-only">Template file</span>
        <Tooltip label="An .eatpl.json file exported from this app, here or on the other node. Nothing is written until you press Import, and the file is validated in full first — a bad one changes nothing at all.">
        <input
          ref={fileRef}
          type="file"
          aria-label="Template file"
          accept=".eatpl.json,.json,application/json"
          onChange={(e) => { setChosen(e.target.files?.[0] ?? null); setSaid(null); setError(null); }}
          className="max-w-[14rem] text-[11px] text-ink-2 file:mr-2 file:rounded file:border
                     file:border-line file:bg-surface-2 file:px-2 file:py-1 file:text-[11px]
                     file:text-ink-1"
        />
        </Tooltip>
      </label>

      <label className="flex items-center gap-1.5 text-[11px] text-ink-3">
        <Tooltip label="What to do about a template in the file that already exists here. Off, it is skipped and yours is kept; on, yours is replaced by the file's — including any settings you have changed since.">
          <input
            type="checkbox"
            aria-label="Replace templates of the same name"
            checked={overwrite}
            onChange={(e) => setOverwrite(e.target.checked)}
          />
        </Tooltip>
        Replace templates of the same name
      </label>

      <Button
        onClick={() => void importChosen()}
        disabled={!chosen || busy === "import"}
        disabledReason={!chosen ? "Choose an exported template file first." : undefined}
      >
        {busy === "import" ? "Importing…" : "Import"}
      </Button>

      {error && (
        <p role="alert" className="w-full rounded border border-warning/40 bg-warning/10
                                   px-3 py-2 text-xs text-warning">
          {error}
        </p>
      )}
      {said && (
        <p role="status" className="w-full text-[11px] text-ink-2">{said}</p>
      )}
    </div>
  );
}
