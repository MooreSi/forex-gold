import { ShieldAlert } from "lucide-react";
import { DAILY_ROUTINE, RISK_WARNING } from "../content/copy";

interface AboutHomeSectionProps {
  version: string | null;
  onOpen: (section: string) => void;
}

const CARDS = [
  { section: "glossary", title: "Glossary",
    desc: "Plain-English explanations of every trading term the app uses." },
  { section: "version", title: "Version history",
    desc: "Release notes and changelog for each version." },
];

// The risk warning has no card. It is rendered on this page, always, rather
// than filed behind a link somebody has to choose to open.

export function AboutHomeSection({ version, onOpen }: AboutHomeSectionProps) {
  return (
    <div className="space-y-5">
      <div>
        <h2 className="text-lg font-bold text-accent">FOREX Trader</h2>
        <p className="num text-xs text-ink-3">{version ?? "version unknown"}</p>
      </div>

      <section>
        <h3 className="mb-2 text-sm font-semibold text-ink-1">Every day</h3>
        <ol className="list-decimal space-y-1 pl-5 text-xs text-ink-2">
          {DAILY_ROUTINE.map((step) => (
            <li key={step}>{step}</li>
          ))}
        </ol>
      </section>

      <div className="grid gap-2 sm:grid-cols-2">
        {CARDS.map((card) => (
          <button
            key={card.section}
            onClick={() => onOpen(card.section)}
            className="rounded border border-line bg-surface-2 px-3 py-2.5 text-left transition-colors hover:border-accent/50"
          >
            <span className="block text-sm font-semibold text-ink-1">{card.title}</span>
            <span className="mt-0.5 block text-[11px] leading-snug text-ink-3">
              {card.desc}
            </span>
          </button>
        ))}
      </div>

      <section
        data-testid="risk-warning"
        className="rounded border border-warning/40 bg-warning/5 px-4 py-3"
      >
        <h3 className="mb-2 flex items-center gap-2 text-sm font-semibold text-warning">
          <ShieldAlert size={15} /> Risk warning
        </h3>
        {RISK_WARNING.map((para, i) => (
          <p
            key={i}
            className={
              i === RISK_WARNING.length - 1
                ? "mt-2 text-xs font-semibold leading-relaxed text-warning"
                : "mt-2 text-xs leading-relaxed text-ink-2"
            }
          >
            {para}
          </p>
        ))}
      </section>
    </div>
  );
}
