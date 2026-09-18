import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { AboutPanel } from "../AboutPanel";
import { GLOSSARY } from "../content/glossary";
import { DAILY_ROUTINE, RISK_WARNING } from "../content/copy";

let fetchMock: ReturnType<typeof vi.fn>;

beforeEach(() => {
  fetchMock = vi.fn().mockResolvedValue({
    ok: true,
    status: 200,
    json: async () => ({
      version: "1.4.2",
      releases: [{ version: "1.4.2", date: "2026-09-18", notes: ["Ported the News tab"] }],
    }),
  });
  vi.stubGlobal("fetch", fetchMock);
});
afterEach(() => vi.unstubAllGlobals());

describe("the content that came across from the NiceGUI page", () => {
  it("kept all six glossary sections and all 47 terms", () => {
    // Transcribed by parsing the original source. If a section or a term ever
    // goes missing in an edit, this is what notices.
    expect(GLOSSARY.map((s) => s.title)).toEqual([
      "Order & Risk Basics",
      "Order Types",
      "Technical Indicators",
      "Automation & Risk Controls",
      "EA Template Terms",
      "Strategy Names",
    ]);
    expect(GLOSSARY.reduce((n, s) => n + s.terms.length, 0)).toBe(47);
  });

  it("has a definition for every term", () => {
    for (const section of GLOSSARY) {
      for (const t of section.terms) {
        expect(t.definition.length).toBeGreaterThan(20);
      }
    }
  });

  it("kept the three risk paragraphs and the three-step routine", () => {
    expect(RISK_WARNING).toHaveLength(3);
    expect(DAILY_ROUTINE).toHaveLength(3);
    expect(RISK_WARNING[2]).toContain("not constitute financial advice");
  });
});

describe("the home page", () => {
  it("shows the risk warning without anybody having to open it", async () => {
    render(<AboutPanel />);

    const warning = await screen.findByTestId("risk-warning");
    expect(warning).toHaveTextContent(/high level of risk/);
    expect(warning).toHaveTextContent(/not constitute financial advice/);
  });

  it("shows the daily routine", async () => {
    render(<AboutPanel />);

    expect(await screen.findByText(DAILY_ROUTINE[0])).toBeInTheDocument();
  });

  it("shows the running version", async () => {
    render(<AboutPanel />);

    expect(await screen.findByText("1.4.2")).toBeInTheDocument();
  });

  it("still renders when the changelog cannot be fetched", async () => {
    // Reference material failing to load must not take the risk warning down
    // with it.
    fetchMock.mockRejectedValue(new Error("offline"));
    render(<AboutPanel />);

    expect(await screen.findByTestId("risk-warning")).toBeInTheDocument();
  });
});

describe("the glossary", () => {
  it("opens from the home page and lists its sections", async () => {
    render(<AboutPanel />);

    await userEvent.click(await screen.findByRole("button", { name: /Glossary/ }));

    expect(screen.getByText("Order & Risk Basics")).toBeInTheDocument();
    expect(screen.getByText("DPM — Dynamic Position Management")).toBeInTheDocument();
  });

  it("filters on a term", async () => {
    render(<AboutPanel />);
    await userEvent.click(await screen.findByRole("button", { name: /Glossary/ }));

    await userEvent.type(screen.getByLabelText("Filter terms"), "kelly");

    expect(screen.getByText("Kelly Criterion Sizing")).toBeInTheDocument();
    expect(screen.queryByText("SL — Stop Loss")).toBeNull();
  });

  it("says so when nothing matches", async () => {
    render(<AboutPanel />);
    await userEvent.click(await screen.findByRole("button", { name: /Glossary/ }));

    await userEvent.type(screen.getByLabelText("Filter terms"), "zzzz");

    expect(screen.getByText(/No term matches/)).toBeInTheDocument();
  });

  it("goes back to the home page", async () => {
    render(<AboutPanel />);
    await userEvent.click(await screen.findByRole("button", { name: /Glossary/ }));

    await userEvent.click(screen.getByRole("button", { name: /Back/ }));

    expect(screen.getByTestId("risk-warning")).toBeInTheDocument();
  });
});

describe("the version history", () => {
  it("lists the releases and marks the one that is running", async () => {
    render(<AboutPanel />);

    await userEvent.click(await screen.findByRole("button", { name: /Version history/ }));

    expect(screen.getByText("Ported the News tab")).toBeInTheDocument();
    expect(screen.getByText("running")).toBeInTheDocument();
  });
});
