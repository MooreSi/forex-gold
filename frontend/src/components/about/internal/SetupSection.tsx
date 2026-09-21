import { useState } from "react";
import * as Tabs from "@radix-ui/react-tabs";
import { cn } from "@/lib/cn";
import { COMMON_SETUP, PLATFORMS, type SetupCard } from "../content/setup";

/**
 * How to get this install working, per platform.
 *
 * Ported from the NiceGUI About page on 2026-09-21 — see `content/setup.ts`
 * for what was brought up to date and why.
 *
 * The platform-independent parts (the EA, both halves of Telegram, email, the
 * API key, the licence, going live) sit BELOW the tabs rather than inside
 * one. Filed under Windows, a Mac user would never read them.
 */
function Card({ card, index }: { card: SetupCard; index: number }) {
  return (
    <section
      data-testid={`setup-card-${index}`}
      className="rounded border border-line bg-surface-2 px-4 py-3"
    >
      <h4 className="text-sm font-semibold text-accent">{card.title}</h4>
      <ol className="mt-2 space-y-1.5">
        {card.steps.map((step, i) => (
          <li key={i} className="flex gap-2.5 text-xs leading-relaxed text-ink-2">
            <span className="num w-4 shrink-0 text-right text-ink-3">{i + 1}.</span>
            <span className="flex-1">{step}</span>
          </li>
        ))}
      </ol>
    </section>
  );
}

export function SetupSection() {
  const [platform, setPlatform] = useState(PLATFORMS[0].id);

  return (
    <div className="space-y-4">
      <p className="text-xs text-ink-3">
        Start with your platform, then work down the sections below it — those
        apply wherever the app is running.
      </p>

      <Tabs.Root value={platform} onValueChange={setPlatform}>
        <Tabs.List className="mb-3 flex gap-1 border-b border-line">
          {PLATFORMS.map((p) => (
            <Tabs.Trigger
              key={p.id}
              value={p.id}
              className={cn(
                "-mb-px border-b-2 px-3 py-1.5 text-xs transition-colors",
                "border-transparent text-ink-3 hover:text-ink-2",
                "data-[state=active]:border-accent data-[state=active]:text-ink-1",
              )}
            >
              {p.label}
            </Tabs.Trigger>
          ))}
        </Tabs.List>

        {PLATFORMS.map((p) => (
          <Tabs.Content key={p.id} value={p.id} className="space-y-3">
            {p.cards.map((card, i) => <Card key={card.title} card={card} index={i} />)}
          </Tabs.Content>
        ))}
      </Tabs.Root>

      <div className="space-y-3 border-t border-line pt-4">
        <h3 className="text-sm font-semibold text-ink-1">
          The same on every platform
        </h3>
        {COMMON_SETUP.map((card, i) => (
          <Card key={card.title} card={card} index={PLATFORMS.length * 10 + i} />
        ))}
      </div>
    </div>
  );
}
