/**
 * Reading the Telegram reader's status (`/api/parsing/state` -> `reader`).
 *
 * The auth states are `services/telegram/reader_common.py`'s AUTH_* values.
 * Matched case-insensitively: the backend sends lower case, and an older
 * build or a test may not.
 */

export type ReaderTone = "ok" | "warn" | "bad" | "idle";

const STATES: Record<string, { label: string; tone: ReaderTone }> = {
  connected: { label: "Connected", tone: "ok" },
  reconnecting: { label: "Reconnecting", tone: "warn" },
  awaiting_code: { label: "Waiting for login code", tone: "warn" },
  awaiting_2fa: { label: "Waiting for 2FA password", tone: "warn" },
  failed: { label: "Login failed", tone: "bad" },
  disconnected: { label: "Disconnected", tone: "bad" },
};

export function readerState(reader: Record<string, unknown>, configured: boolean):
  { label: string; tone: ReaderTone } {
  const raw = String(reader["auth_state"] ?? "").toLowerCase();
  if (STATES[raw]) return STATES[raw];
  return configured ? { label: "Unknown", tone: "warn" } : { label: "Not set up", tone: "idle" };
}

/** "12s ago", "4 min ago", "3 h ago" from an ISO string or epoch seconds. */
export function ago(when: string | number, nowMs: number = Date.now()): string {
  const ms = typeof when === "number" ? when * 1000 : Date.parse(when);
  if (!Number.isFinite(ms)) return "—";
  const s = Math.max(0, (nowMs - ms) / 1000);
  if (s < 90) return `${Math.round(s)}s ago`;
  if (s < 90 * 60) return `${Math.round(s / 60)} min ago`;
  if (s < 48 * 3600) return `${Math.round(s / 3600)} h ago`;
  return `${Math.round(s / 86400)} d ago`;
}

/** The reader slot listening to `channel`, matched by its group name. Both
 *  come from the same channel set-up, so the names are identical (checked
 *  against the live payload, 2026-09-30). */
export function slotFor(slots: Record<string, unknown>[], channel: string):
  Record<string, unknown> | undefined {
  return slots.find((s) => s["group_name"] === channel);
}
