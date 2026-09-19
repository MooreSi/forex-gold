import { Monitor, Moon, Sun } from "lucide-react";
import { THEMES, useTheme, type Theme } from "@/contexts/ThemeContext";
import { cn } from "@/lib/cn";

/**
 * Light, dark, or follow the machine.
 *
 * **The only tab in Settings that writes nothing to the trading database.**
 * Everything else here is something an engine reads; a colour is not, so it
 * is kept in the browser and applied before the first paint. The trade-off is
 * that it does not follow the operator to another machine, and the tab says
 * so rather than letting that be a surprise.
 *
 * "Auto" reports what it currently resolves to, because it is the one option
 * whose effect an operator cannot otherwise confirm.
 */
const OPTIONS: { id: Theme; label: string; hint: string; Icon: typeof Sun }[] = [
  { id: "auto", label: "Auto", Icon: Monitor,
    hint: "Follow this computer's light/dark setting." },
  { id: "light", label: "Light", Icon: Sun,
    hint: "Always light. Accents darken so an 11px P&L column still passes contrast." },
  { id: "dark", label: "Dark", Icon: Moon,
    hint: "Always dark. What the app's colours were designed against." },
];

export function AppearanceTab() {
  const { theme, resolved, setTheme } = useTheme();

  return (
    <div className="max-w-2xl space-y-4">
      <div>
        <h3 className="text-sm font-semibold text-ink-1">Theme</h3>
        <p className="mt-0.5 text-xs text-ink-3">
          Kept in <strong>this browser</strong>, not on the account — it applies
          before the page draws, so there is no flash of the wrong colour, and
          it does not follow you to another machine.
        </p>
      </div>

      <div role="radiogroup" aria-label="Theme" className="grid gap-2 sm:grid-cols-3">
        {OPTIONS.map(({ id, label, hint, Icon }) => (
          <button
            key={id}
            type="button"
            role="radio"
            aria-checked={theme === id}
            aria-label={label}
            onClick={() => setTheme(id)}
            className={cn(
              "flex flex-col items-start gap-1 rounded-lg border p-3 text-left transition-colors",
              theme === id
                ? "border-accent bg-surface-2"
                : "border-line bg-surface-1 hover:bg-surface-2",
            )}
          >
            <span className="flex items-center gap-1.5 text-xs font-semibold text-ink-1">
              <Icon size={14} className={theme === id ? "text-accent" : "text-ink-3"} />
              {label}
            </span>
            <span className="text-[11px] leading-snug text-ink-3">{hint}</span>
          </button>
        ))}
      </div>

      <p className="text-[11px] text-ink-3">
        Currently showing{" "}
        <span data-testid="theme-resolved" className="font-semibold text-ink-2">
          {resolved}
        </span>
        {theme === "auto" && " — following this computer."}
      </p>

      <p className="text-[11px] text-ink-3">
        Profit green, loss red and warning amber keep their meaning in both
        themes. Only their lightness changes, so nothing you have learned to
        read at a glance moves.
      </p>

      {/* Referenced so the option list and the theme list cannot drift apart:
          a theme with no button here would be unreachable. */}
      {THEMES.length !== OPTIONS.length && (
        <p role="alert" className="text-[11px] text-loss">
          A theme exists with no way to choose it.
        </p>
      )}
    </div>
  );
}
