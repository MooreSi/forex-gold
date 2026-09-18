import * as Tabs from "@radix-ui/react-tabs";
import { EmptyState } from "@/components/shared/EmptyState";
import { PanelShell } from "@/components/shared/PanelShell";
import { asObject } from "@/lib/asArray";
import { cn } from "@/lib/cn";
import { useParsingController } from "./hooks/useParsingController";
import { ChannelsSection } from "./internal/ChannelsSection";
import { DecisionLogSection } from "./internal/DecisionLogSection";
import { LexiconSection } from "./internal/LexiconSection";
import { MessageFeedSection } from "./internal/MessageFeedSection";
import { ParsingSettingsSection } from "./internal/ParsingSettingsSection";
import { UnrecognisedSection } from "./internal/UnrecognisedSection";

const SUB_TABS = [
  { id: "settings", label: "Settings" },
  { id: "channels", label: "Channels" },
  { id: "phrases", label: "Trigger phrases" },
  { id: "feed", label: "Feed" },
  { id: "questions", label: "Questions" },
  { id: "decisions", label: "Decision log" },
];

export function ParsingPanel() {
  const c = useParsingController();
  const data = c.state.data;

  return (
    <PanelShell
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
        <Tabs.Root defaultValue="settings" className="flex h-full min-h-0 flex-col">
          <Tabs.List className="mb-3 flex gap-1 border-b border-line">
            {SUB_TABS.map((t) => (
              <Tabs.Trigger
                key={t.id}
                value={t.id}
                className={cn(
                  "-mb-px border-b-2 px-3 py-1.5 text-xs transition-colors",
                  "border-transparent text-ink-3 hover:text-ink-2",
                  "data-[state=active]:border-accent data-[state=active]:text-ink-1",
                )}
              >
                {t.label}
              </Tabs.Trigger>
            ))}
          </Tabs.List>

          <Tabs.Content value="settings" className="min-h-0 flex-1 overflow-auto">
            <ParsingSettingsSection settings={asObject(data.settings)} onSave={c.saveSetting} />
          </Tabs.Content>
          <Tabs.Content value="channels" className="min-h-0 flex-1 overflow-auto">
            <ChannelsSection channels={data.channels} onToggle={c.setChannelEnabled} />
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
      )}
    </PanelShell>
  );
}
