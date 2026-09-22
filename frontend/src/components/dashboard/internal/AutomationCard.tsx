import { Circle, CirclePlay } from "lucide-react";
import { cn } from "@/lib/cn";
import { DashCard, Pill } from "./DashCard";

export interface EngineRow {
  id: string;
  label: string;
  running: boolean;
  built: boolean;
}

/**
 * Which engines are generating signals, and which are merely installed.
 *
 * "Running" and "built" are different states and both are shown. An engine
 * slot with no service behind it is a real thing on an install that never
 * enabled one, and it reads differently from an engine that is built and
 * stopped — showing either as simply "off" would hide which of the two needs
 * a switch and which needs a build.
 *
 * Starting an engine is not placing an order, but it is the switch that lets
 * one be placed with nobody watching, so this card only reports. The Signal
 * Generator tab owns the controls.
 */
export function AutomationCard({ engines }: { engines: EngineRow[] }) {
  const running = engines.filter((e) => e.running).length;

  return (
    <DashCard
      title="Engines"
      icon="flask"
      badge={<Pill tone={running ? "profit" : "neutral"}>
        {running} of {engines.length} running
      </Pill>}
      footnote="Start or stop an engine on the Signal Generator tab."
    >
      {engines.length === 0 ? (
        <p className="text-[11px] text-ink-3">No engines are built on this install.</p>
      ) : (
        <ul className="space-y-1">
          {engines.map((e) => (
            <li key={e.id} className="flex items-center gap-2 text-[11px]">
              {e.running
                ? <CirclePlay size={12} className="shrink-0 text-profit" aria-hidden />
                : <Circle size={12} className="shrink-0 text-ink-3" aria-hidden />}
              <span className="text-ink-1">{e.label}</span>
              <span className={cn("ml-auto text-[10px]",
                                  e.running ? "text-profit" : "text-ink-3")}>
                {e.running ? "running" : e.built ? "stopped" : "not built here"}
              </span>
            </li>
          ))}
        </ul>
      )}
    </DashCard>
  );
}
