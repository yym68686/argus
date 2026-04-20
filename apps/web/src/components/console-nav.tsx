"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { Gauge, MessagesSquare, RadioTower, Settings2, Users2 } from "lucide-react";

import { cn } from "@/lib/utils";

const NAV_ITEMS = [
  { href: "/", label: "Workbench", icon: MessagesSquare, detail: "Sessions, threads, and live turns" },
  { href: "/users", label: "Users", icon: Users2, detail: "Agents, upstreams, and operator state" },
  { href: "/usage", label: "Usage", icon: Gauge, detail: "Requests, tokens, and spend" },
  { href: "/nodes", label: "Nodes", icon: RadioTower, detail: "node-host inventory and remote invoke" },
  { href: "/settings", label: "Settings", icon: Settings2, detail: "Gateway health, contracts, and deploy notes" },
];

export function ConsoleNav({ compact = false }: { compact?: boolean }) {
  const pathname = usePathname() || "/";

  return (
    <nav className={cn("flex flex-col gap-1", compact ? "px-0" : "px-0")}>
      {NAV_ITEMS.map((item) => {
        const Icon = item.icon;
        const active =
          item.href === "/" ? pathname === "/" : pathname === item.href || pathname.startsWith(`${item.href}/`);

        return (
          <Link
            key={item.href}
            href={item.href}
            aria-current={active ? "page" : undefined}
            className={cn(
              "group relative flex items-center gap-3 rounded-xl px-3 py-2.5 text-sm transition-colors",
              active
                ? "bg-primary/10 text-foreground"
                : "text-muted-foreground hover:bg-background/30 hover:text-foreground",
            )}
          >
            {active ? <span className="absolute inset-y-2 left-0 w-px rounded-full bg-primary" /> : null}
            <span
              className={cn(
                "flex h-9 w-9 shrink-0 items-center justify-center rounded-lg border",
                active
                  ? "border-primary/24 bg-primary/12 text-primary"
                  : "border-border/65 bg-background/20 text-muted-foreground group-hover:border-border/85 group-hover:text-foreground",
              )}
            >
              <Icon className="h-4 w-4" />
            </span>
            <span className="min-w-0 flex-1">
              <span className="block truncate font-medium text-foreground">{item.label}</span>
              {!compact ? <span className="mt-0.5 block text-[11px] leading-5 text-muted-foreground/80">{item.detail}</span> : null}
            </span>
          </Link>
        );
      })}
    </nav>
  );
}
