import type { ReactNode } from "react";
import { asArray } from "@/lib/asArray";
import { cn } from "@/lib/cn";
import { ago, readerState, type ReaderTone } from "./reader_status";

interface ReaderStatusSectionProps {
  reader: Record<string, unknown>;
  configured: boolean;
  settings: Record<string, unknown>;
  /** "local" | "remote" | "centralized", from `/api/parsing/state`. */
  controlTarget: string;
}

const TONE: Record<ReaderTone, string> = {
  ok: "bg-profit",
  warn: "bg-warning",
  bad: "bg-loss",
  idle: "bg-ink-3",
};

function flag(settings: Record<string, unknown>, key: string, fallback: boolean): boolean {
  const raw = settings[key];
  return raw === undefined || raw === null ? fallback : Boolean(Number(raw));
}

function Tile({ label, children, hint }: { label: string; children: ReactNode; hint?: string }) {
  return (
    <div className="min-w-0 rounded-md border border-line bg-surface-2 px-3 py-2" title={hint}>
      <div className="text-[10px] uppercase tracking-wide text-ink-3">{label}</div>
      <div className="mt-0.5 text-xs text-ink-1">{children}</div>
    </div>
  );
}

/**
 * The one-glance answer to "is Telegram working, and where are its signals
 * traded?", above every sub-tab.
 *
 * **Where** matters since v0.612 (2026-09-29): the node that trades reads and
 * parses Telegram itself, and the other one keeps a standby copy of each
 * message and sends the channel set-up across. Nothing on this tab said so, so
 * a Mac in Remote mode looked like the machine placing the trades.
 */
export function ReaderStatusSection(
  { reader, configured, settings, controlTarget }: ReaderStatusSectionProps,
) {
  const st = readerState(reader, configured);
  const slots = asArray<Record<string, unknown>>(reader["slots"]);
  const listening = slots.filter((s) => s["listener_active"] || s["poller_active"]).length;
  const remote = controlTarget === "remote" || controlTarget === "centralized";
  const last = reader["last_message_at"];
  const errors = asArray<unknown>(reader["recent_errors"]).map(String).filter(Boolean);
  const accepting = flag(settings, "accept_tg_signals", true);
  const auto = flag(settings, "auto_execute_signals", false);

  return (
    <div data-testid="reader-status" className="mb-3 space-y-2">
      <div className="grid gap-2 sm:grid-cols-2 xl:grid-cols-4">
        <Tile label="Telegram reader" hint="The login and listeners on this machine.">
          <span className="flex items-center gap-1.5">
            <span aria-hidden className={cn("size-2 rounded-full", TONE[st.tone])} />
            <span className="font-semibold">{st.label}</span>
            {slots.length > 0 && (
              <span className="num text-ink-3">
                · {listening}/{slots.length} channels listening
              </span>
            )}
          </span>
        </Tile>
        <Tile
          label="Signals traded on"
          hint="Telegram follows the active trader: that node reads, parses and trades. The other keeps a standby copy of every message and sends its channel set-up, trigger phrases and manual channel pauses across."
        >
          {remote ? (
            <span>
              <span className="font-semibold text-remote">The VPS</span>
              <span className="text-ink-3"> · this machine keeps standby copies</span>
            </span>
          ) : (
            <span className="font-semibold">This machine</span>
          )}
        </Tile>
        <Tile label="Last message" hint="The newest message this machine's reader received.">
          <span className="font-semibold">
            {typeof last === "string" || typeof last === "number" ? ago(last) : "—"}
          </span>
          {reader["messages_stored_total"] !== undefined && (
            <span className="num text-ink-3">
              {" "}· {String(reader["messages_this_session"] ?? 0)} this session,{" "}
              {String(reader["messages_stored_total"])} stored
            </span>
          )}
        </Tile>
        <Tile
          label="Auto-execution"
          hint="Telegram Signals decides whether signals are created at all; Auto-Execution whether they are traded without a click. Both are switched below."
        >
          <span className={cn("font-semibold", accepting && auto ? "text-profit" : "text-ink-2")}>
            {!accepting ? "Telegram signals off" : auto ? "On" : "Off: manual only"}
          </span>
        </Tile>
      </div>
      {errors.length > 0 && (
        <p role="alert" className="rounded border border-warning/40 bg-warning/10 px-2 py-1.5 text-[11px] text-warning">
          Reader reported {errors.length} recent error{errors.length === 1 ? "" : "s"}:{" "}
          {errors[errors.length - 1]}
        </p>
      )}
    </div>
  );
}
