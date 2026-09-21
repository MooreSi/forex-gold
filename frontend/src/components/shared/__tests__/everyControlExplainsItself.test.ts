import { readdirSync, readFileSync, statSync } from "node:fs";
import { join, resolve } from "node:path";
import { describe, expect, it } from "vitest";

/**
 * Every field you can change says what it does when you hover it.
 *
 * Asked for on 2026-09-21: "add tooltips to all of the selectable, adjustable
 * fields/items when you hover over them". The sweep that followed touched
 * thirty-odd components, and a sweep is exactly the kind of thing that decays
 * one new field at a time — so this is the ratchet that stops it.
 *
 * **It counts, rather than checking each control individually.** A React key
 * leaves no trace and a JSX wrapper is not visible to a regex with any
 * confidence, so what this asserts is that a component rendering N raw inputs
 * or selects also renders at least N tooltips. That is weaker than "each one
 * is wrapped" and strong enough for what it is guarding: adding a field and
 * forgetting its help fails here.
 *
 * Controls rendered through `SettingsField`, `SettingsToggle` or the order
 * forms' own `Field` are covered by those components and never appear here,
 * which is the point of having them.
 *
 * `ALLOWED_BARE` is for a control that genuinely should not carry hover help.
 * It is empty, and adding to it should need a sentence saying why.
 */
// `process.cwd()`, not `import.meta.url`: under Vitest the module's own URL is
// not a file: URL, and `fileURLToPath` throws on it before a single test runs.
// Vitest's cwd is this project's root.
const COMPONENTS = resolve(process.cwd(), "src/components");

const ALLOWED_BARE: Record<string, string> = {};

function tsxFiles(dir: string): string[] {
  const out: string[] = [];
  for (const entry of readdirSync(dir)) {
    const full = join(dir, entry);
    if (statSync(full).isDirectory()) {
      if (entry === "__tests__") continue;
      out.push(...tsxFiles(full));
      continue;
    }
    if (entry.endsWith(".tsx")) out.push(full);
  }
  return out;
}

function count(text: string, pattern: RegExp): number {
  return text.match(pattern)?.length ?? 0;
}

describe("every adjustable field explains itself", () => {
  const files = tsxFiles(COMPONENTS);

  it("finds the components to check at all", () => {
    // Negative control. A path that stopped resolving would make every
    // assertion below vacuously true — the failure mode this repository has
    // already paid for once, in a guardrail that scanned a deleted directory
    // and printed "all good" for months.
    expect(files.length).toBeGreaterThan(50);
    expect(files.some((f) => f.endsWith("ActiveTradesSection.tsx"))).toBe(true);
  });

  it("wraps at least as many tooltips as it renders raw controls", () => {
    const bare: string[] = [];
    for (const file of files) {
      const short = file.slice(COMPONENTS.length + 1);
      if (short in ALLOWED_BARE) continue;
      const text = readFileSync(file, "utf-8");
      const controls = count(text, /<input|<select/g);
      if (controls === 0) continue;
      const tooltips = count(text, /<Tooltip/g);
      if (tooltips < controls) {
        bare.push(`${short}: ${controls} controls, ${tooltips} tooltips`);
      }
    }

    expect(bare).toEqual([]);
  });
});
