import {
  Activity, AlertTriangle, BarChart3, Bell, Bot, Brain, CalendarClock,
  CandlestickChart, ClipboardList, Clock, Flame, FlaskConical, Gauge, History,
  Info, KeyRound, LayoutGrid, LineChart, ListTree, Network, Newspaper, Percent,
  Radio, Send, Server, Settings, Shield, Sliders, Sparkles, Table2, Target,
  TrendingUp, Wrench,
  type LucideIcon,
} from "lucide-react";

/**
 * One name → one icon, for the whole dashboard.
 *
 * `tabs.ts` has carried an `icon` string since the port began and nothing
 * rendered it, because there was no agreed place to turn a name into a
 * component. Scattering `import { Bot }` through ten panels is how two
 * sections end up with the same icon meaning different things.
 *
 * The names are meanings, not pictures: `risk` is the shield wherever risk
 * appears, and changing what a shield looks like is one edit here.
 */
export const ICONS: Record<string, LucideIcon> = {
  // Tabs
  bot: Bot,
  candlestick: CandlestickChart,
  "trending-up": TrendingUp,
  send: Send,
  flask: FlaskConical,
  "bar-chart": BarChart3,
  history: History,
  settings: Settings,
  newspaper: Newspaper,
  info: Info,

  // Sections
  positions: LayoutGrid,
  signals: Radio,
  schedule: CalendarClock,
  templates: ClipboardList,
  risk: Shield,
  performance: Gauge,
  heatmap: Flame,
  channels: Network,
  ladder: ListTree,
  trades: Table2,
  equity: LineChart,
  ai: Brain,
  evidence: Sparkles,
  tunables: Sliders,
  diagnostics: Wrench,
  access: KeyRound,
  node: Server,
  alerts: Bell,
  clock: Clock,
  warning: AlertTriangle,
  target: Target,
  percent: Percent,
  activity: Activity,
};

/** The icon for a name, or null — never a wrong icon standing in for a missing
 *  one, which is worse than no icon at all. */
export function iconFor(name: string | null | undefined): LucideIcon | null {
  if (!name) return null;
  return ICONS[name] ?? null;
}
