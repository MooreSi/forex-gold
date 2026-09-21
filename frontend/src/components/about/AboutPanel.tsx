import { useEffect, useState } from "react";
import { ChevronLeft } from "lucide-react";
import { api } from "@/api/client";
import { Button } from "@/components/shared/Button";
import { PanelShell } from "@/components/shared/PanelShell";
import { asArray } from "@/lib/asArray";
import { AboutHomeSection } from "./internal/AboutHomeSection";
import { GlossarySection } from "./internal/GlossarySection";
import { SetupSection } from "./internal/SetupSection";
import { VersionSection } from "./internal/VersionSection";

const TITLES: Record<string, string> = {
  setup: "Setup instructions",
  glossary: "Glossary",
  version: "Version history",
};

/**
 * About: reference content, not live data.
 *
 * Fetched once rather than polled — the changelog ships with the build and
 * cannot change while the app is running, so a poll here would be a request
 * every few seconds for an answer that is already known.
 */
export function AboutPanel() {
  const [section, setSection] = useState<string | null>(null);
  const [version, setVersion] = useState<string | null>(null);
  const [releases, setReleases] = useState<Record<string, unknown>[]>([]);

  useEffect(() => {
    let cancelled = false;
    void api
      .get<{ version: string; releases: Record<string, unknown>[] }>("/api/system/releases")
      .then((body) => {
        if (cancelled) return;
        setVersion(body.version);
        setReleases(asArray(body.releases));
      })
      .catch(() => {
        // The changelog is reference material. Failing to load it must not
        // take the risk warning and the glossary down with it.
      });
    return () => {
      cancelled = true;
    };
  }, []);

  return (
    <PanelShell
      icon="info"
      title={section ? TITLES[section] : "About"}
      actions={
        section && (
          <Button variant="ghost" onClick={() => setSection(null)}>
            <ChevronLeft size={13} /> Back
          </Button>
        )
      }
    >
      {section === null && <AboutHomeSection version={version} onOpen={setSection} />}
      {section === "setup" && <SetupSection />}
      {section === "glossary" && <GlossarySection />}
      {section === "version" && <VersionSection version={version} releases={releases} />}
    </PanelShell>
  );
}
