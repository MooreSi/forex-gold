import { ConnectionSection, type ConnectionSpec } from "../internal/ConnectionSection";
import { TestSendRow } from "../internal/TestSendRow";

/**
 * The outbound connections, and the buttons that prove each one works.
 *
 * Three separate stores behind one tab, which is why they are three sections
 * rather than one form:
 *
 *   * **Email** is the `email_config` row — provider, schedule and credentials.
 *   * **Telegram alerts** is the `telegram_config` row — the bot that SENDS.
 *   * **Telegram reader** is `config.yaml` — the Telethon account that READS
 *     signals, and needs a restart to take effect.
 *
 * The first React port of this tab conflated the last two and invented field
 * names for both. Every `key` below is the name the store actually uses, and
 * `tests/api/test_settings_writes_reach_the_store.py` checks that against the
 * schema rather than trusting this comment.
 */
const EMAIL: ConnectionSpec = {
  path: "/api/settings/email",
  title: "Email",
  blurb:
    "Where the daily, weekly and morning ORB reports are sent, and how they get out.",
  fields: [
    {
      key: "send_provider",
      label: "Send reports via",
      kind: "choice",
      choices: [
        { value: "resend", label: "Resend (API — no SMTP needed)" },
        { value: "gmail", label: "Gmail (SMTP)" },
        { value: "outlook", label: "Outlook.com (SMTP)" },
        { value: "custom", label: "Custom SMTP" },
      ],
      hint: "Configure that provider's credentials below, then test it.",
    },
    { key: "to_addr", label: "Send reports to" },
    { key: "from_addr", label: "From address", hint: "Blank uses the provider's default." },
    {
      key: "resend_api_key",
      label: "Resend API key",
      kind: "password",
      hint: "Starts with re_ — from resend.com > API Keys.",
    },
    { key: "smtp_host", label: "SMTP host" },
    { key: "smtp_port", label: "SMTP port", kind: "number" },
    { key: "smtp_user", label: "SMTP username" },
    {
      key: "smtp_password",
      label: "SMTP password",
      kind: "password",
      hint: "An App Password, if the account has two-step verification.",
    },
    { key: "use_tls", label: "Use STARTTLS", kind: "toggle", hint: "Port 587." },
    { key: "send_time", label: "Send time", hint: "24-hour HH:MM, server clock." },
    { key: "daily_enabled", label: "Daily summary", kind: "toggle" },
    {
      key: "weekly_enabled",
      label: "Weekly summary",
      kind: "toggle",
      hint: "Fridays. On a Friday you get both, as two messages.",
    },
    {
      key: "orb_report_enabled",
      label: "Morning ORB / IVB report",
      kind: "toggle",
      hint: "08:15 Europe/London, independent of the send time above.",
    },
  ],
};

const TELEGRAM_BOT: ConnectionSpec = {
  path: "/api/settings/telegram",
  title: "Telegram alerts",
  blurb: "The bot that SENDS trade notifications — opens, closes and TP hits.",
  fields: [
    {
      key: "bot_token",
      label: "Bot token",
      kind: "password",
      // Written as bot_token, stored as bot_token_enc.
      storedAs: "bot_token_enc",
      hint: "From @BotFather.",
    },
    { key: "chat_id", label: "Chat ID", hint: "From @userinfobot or @RawDataBot." },
    { key: "enabled", label: "Send Telegram alerts", kind: "toggle" },
  ],
};

const TELEGRAM_READER: ConnectionSpec = {
  path: "/api/settings/telegram-reader",
  title: "Telegram reader",
  blurb:
    "The Telethon account that READS signal channels. Saved to config.yaml — restart the app for a change to take effect.",
  fields: [
    { key: "telegram_api_id", label: "API ID", hint: "From my.telegram.org/apps." },
    { key: "telegram_api_hash", label: "API hash", kind: "password" },
    { key: "telegram_phone", label: "Phone", hint: "International format, e.g. +441234567890." },
  ],
};

export function ConnectionsTab() {
  return (
    <div className="space-y-4">
      <ConnectionSection spec={EMAIL}>
        {() => (
          <TestSendRow
            actions={[
              { label: "Test delivery", path: "/api/notifications/test-email" },
              { label: "Send test ORB report", path: "/api/notifications/test-orb-report" },
            ]}
          />
        )}
      </ConnectionSection>

      <ConnectionSection spec={TELEGRAM_BOT}>
        {() => (
          <TestSendRow
            actions={[{ label: "Test alert", path: "/api/notifications/test-telegram" }]}
          />
        )}
      </ConnectionSection>

      <ConnectionSection spec={TELEGRAM_READER} />
    </div>
  );
}
