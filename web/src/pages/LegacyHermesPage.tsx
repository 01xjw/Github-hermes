import { Link } from "react-router";
import {
  BarChart3,
  BookOpen,
  Clock,
  Cpu,
  FileText,
  FolderOpen,
  KeyRound,
  Package,
  Plug,
  Puzzle,
  Radio,
  Settings,
  ShieldCheck,
  Users,
  Webhook,
  Wrench,
  type LucideIcon,
} from "lucide-react";

interface LegacyTool {
  path: string;
  title: string;
  description: string;
  icon: LucideIcon;
}

const LEGACY_TOOLS: LegacyTool[] = [
  {
    path: "/sessions",
    title: "Sessions",
    description: "Inspect complete Hermes session history and transcripts.",
    icon: FileText,
  },
  {
    path: "/files",
    title: "Files",
    description: "Browse and manage files exposed by the Hermes host.",
    icon: FolderOpen,
  },
  {
    path: "/analytics",
    title: "Analytics",
    description: "Review token usage, model activity, and estimated cost.",
    icon: BarChart3,
  },
  {
    path: "/models",
    title: "Models",
    description: "Configure primary, auxiliary, and routed model providers.",
    icon: Cpu,
  },
  {
    path: "/cron",
    title: "Cron",
    description: "Create and monitor scheduled Hermes jobs.",
    icon: Clock,
  },
  {
    path: "/skills",
    title: "Skills",
    description: "Manage built-in and operator-provided skills.",
    icon: Package,
  },
  {
    path: "/plugins",
    title: "Plugins",
    description: "Install, configure, and inspect dashboard plugins.",
    icon: Puzzle,
  },
  {
    path: "/mcp",
    title: "MCP",
    description: "Manage Model Context Protocol servers and credentials.",
    icon: Plug,
  },
  {
    path: "/channels",
    title: "Channels",
    description: "Configure messaging and delivery channels.",
    icon: Radio,
  },
  {
    path: "/webhooks",
    title: "Webhooks",
    description: "Inspect webhook integrations and delivery configuration.",
    icon: Webhook,
  },
  {
    path: "/pairing",
    title: "Pairing",
    description: "Review and approve trusted client connections.",
    icon: ShieldCheck,
  },
  {
    path: "/profiles",
    title: "Profiles",
    description: "Manage isolated Hermes runtime profiles.",
    icon: Users,
  },
  {
    path: "/config",
    title: "Configuration",
    description: "Edit the complete Hermes configuration surface.",
    icon: Settings,
  },
  {
    path: "/env",
    title: "Keys",
    description: "Manage provider keys and environment values.",
    icon: KeyRound,
  },
  {
    path: "/logs",
    title: "Logs",
    description: "Inspect runtime, gateway, and error logs.",
    icon: FileText,
  },
  {
    path: "/system",
    title: "System",
    description: "Run updates, restarts, diagnostics, and host operations.",
    icon: Wrench,
  },
  {
    path: "/docs",
    title: "Documentation",
    description: "Open the bundled Hermes operator documentation.",
    icon: BookOpen,
  },
];

export default function LegacyHermesPage() {
  return (
    <div className="space-y-5 py-4 text-[#1e293b]">
      <section className="rounded-lg border border-[#e2e8f0] bg-white px-5 py-5 shadow-[0_2px_8px_rgba(0,0,0,0.06)]">
        <p className="text-xs font-semibold uppercase tracking-[0.14em] text-[#3b82f6]">
          Encapsulated Hermes surfaces
        </p>
        <h2 className="mt-2 text-2xl font-semibold">Hermes Tools</h2>
        <p className="mt-2 max-w-3xl text-sm leading-6 text-[#64748b]">
          These original dashboard modules remain available and fully routed,
          but they are intentionally grouped outside the primary ProjectHermes
          workflow. Chat stays in the main navigation because it is the
          operator-facing reasoning surface.
        </p>
      </section>

      <section className="grid gap-4 sm:grid-cols-2 xl:grid-cols-3">
        {LEGACY_TOOLS.map(({ path, title, description, icon: Icon }) => (
          <Link
            to={path}
            key={path}
            className="group rounded-lg border border-[#e2e8f0] bg-white p-4 shadow-[0_2px_8px_rgba(0,0,0,0.06)] transition hover:-translate-y-0.5 hover:border-[#3b82f6]"
          >
            <div className="flex items-start gap-3">
              <span className="flex h-9 w-9 shrink-0 items-center justify-center rounded-md bg-[#f1f5f9] text-[#1e293b] transition group-hover:bg-[#eff6ff] group-hover:text-[#3b82f6]">
                <Icon className="h-4 w-4" />
              </span>
              <div>
                <h3 className="text-sm font-semibold">{title}</h3>
                <p className="mt-1 text-xs leading-5 text-[#64748b]">
                  {description}
                </p>
              </div>
            </div>
          </Link>
        ))}
      </section>
    </div>
  );
}
