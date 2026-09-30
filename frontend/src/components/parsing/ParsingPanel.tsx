import * as Tabs from "@radix-ui/react-tabs";
import { EmptyState } from "@/components/shared/EmptyState";
import { PanelShell } from "@/components/shared/PanelShell";
import { asArray, asObject } from "@/lib/asArray";
import { cn } from "@/lib/cn";
import type { ParsingChannel } from "@/api/types";
import { useParsingController } from "./hooks/useParsingController";
import { ChannelsSection } from "./internal/ChannelsSection";
import { DecisionLogSection } from "./internal/DecisionLogSection";
import { LexiconSection } from "./internal/LexiconSection";
import { MessageFeedSection } from "./internal/MessageFeedSection";
import { ParsingSettingsSection } from "./internal/ParsingSettingsSection";
import { ReaderStatusSection } from "./internal/ReaderStatusSection";
import { SignalsSourcesSection } from "./internal/SignalsSourcesSection";
import { UnrecognisedSection } from "./internal/UnrecognisedSection";

/**
 * The Telegram side of the app: what the reader sees, how it reads it, and
 * whether it trades it.
 *
 * Sub-tab names are what the tests and the operator both find them by; a
 * count rides beside a name, never inside it (`aria-label` keeps the name).
 */
export function ParsingPanel() {
  const c = useParsingController();
  const data = c.state.data;
  const settings = asObject(data?.settings);
  const channels = asArray<ParsingChannel>(data?.channels);

  const tabs = [
    { id: "settings", label: "Settings" },
    { id: "channels", label: "Channels", count: data ? channels.length : undefined },
    { id: "phrases", label: "Trigger phrases" },
    { id: "feed", label: "Feed" },
    { id: "questions", label: "Questions", count: c.pending.length || undefined, alert: c.pending.length > 0 },
    { id: "decisions", label: "Decision log" },
  ];

  return (
    <PanelShell
      icon="send"
      title="Parsing"
      subtitle={
        data ? (data.configured ? "reader connected" : "reader not configured") : undefined
      }
      actions={
        c.pending.length > 0 && (
          <span className="rounded border border-warning/40 bg-warning/10 px-2 py-0.5 text-[11px] text-warning">
            {c.pending.length} unread message{c.pending.length === 1 ? "" : "s"}
          </span>
        )
      }
    >
      {!data ? (
        <EmptyState
          title={c.state.error ? "Could not load the parsing settings" : "Loading"}
          hint={c.state.error?.message}
        />
      ) : (
        <div className="flex h-full min-h-0 flex-col">
          <ReaderStatusSection
            reader={asObject(data.reader)}
            configured={data.configured}
            settings={settings}
            controlTarget={String(data.control_target ?? "local")}
          />
          <Tabs.Root defaultValue="settings" className="flex min-h-0 flex-1 flex-col">
            <Tabs.List className="mb-3 flex gap-1 overflow-x-auto rounded-md border border-line bg-surface-2/60 p-1">
              {tabs.map((t) => (
                <Tabs.Trigger
                  key={t.id}
                  value={t.id}
                  aria-label={t.label}
                  className={cn(
                    "flex shrink-0 items-center gap-1.5 rounded px-3 py-1.5 text-xs transition-colors",
                    "text-ink-3 hover:bg-surface-3 hover:text-ink-1",
                    "data-[state=active]:bg-surface-1 data-[state=active]:font-medium data-[state=active]:text-ink-1",
                    "data-[state=active]:shadow-sm data-[state=active]:ring-1 data-[state=active]:ring-line",
                  )}
                >
                  {t.label}
                  {t.count !== undefined && (
                    <span
                      aria-hidden
                      className={cn(
                        "num rounded-full px-1.5 text-[10px]",
                        t.alert ? "bg-warning/20 text-warning" : "bg-surface-1 text-ink-3",
                      )}
                    >
                      {t.count}
                    </span>
                  )}
                </Tabs.Trigger>
              ))}
            </Tabs.List>

            <Tabs.Content value="settings" className="min-h-0 flex-1 overflow-auto">
              <div className="space-y-4">
                {/* At the top, where it has been since 2026-07-22: these decide
                    whether a source opens real positions, and everything below
                    only decides how a message is READ. */}
                <SignalsSourcesSection
                  settings={settings}
                  onSave={c.saveSetting}
                  controlTarget={String(data.control_target ?? "local")}
                />
                <ParsingSettingsSection settings={settings} onSave={c.saveSetting} />
              </div>
            </Tabs.Content>
            <Tabs.Content value="channels" className="min-h-0 flex-1 overflow-auto">
              <ChannelsSection
                channels={channels}
                onToggle={c.setChannelEnabled}
                slots={asArray<Record<string, unknown>>(asObject(data.reader)["slots"])}
              />
            </Tabs.Content>
            <Tabs.Content value="phrases" className="min-h-0 flex-1 overflow-auto">
              <LexiconSection
                lexicons={asObject(data.lexicons)}
                labels={asObject(data.lexicon_labels)}
                help={asObject(data.lexicon_help)}
                onSave={c.saveLexicon}
              />
            </Tabs.Content>
            <Tabs.Content value="feed" className="min-h-0 flex-1 overflow-auto">
              <MessageFeedSection messages={c.messages} total={c.messageTotal} />
            </Tabs.Content>
            <Tabs.Content value="questions" className="min-h-0 flex-1 overflow-auto">
              <UnrecognisedSection pending={c.pending} onResolve={c.resolve} />
            </Tabs.Content>
            <Tabs.Content value="decisions" className="min-h-0 flex-1 overflow-auto">
              <DecisionLogSection />
            </Tabs.Content>
          </Tabs.Root>
        </div>
      )}
    </PanelShell>
  );
}
