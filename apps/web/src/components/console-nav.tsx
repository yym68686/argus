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
    <nav className={cn("flex flex-col gap-1.5", compact ? "px-0" : "px-1")}>
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
              "group relative flex items-start gap-3 rounded-[18px] border px-3 py-3 text-sm",
              active
                ? "border-primary/26 bg-primary/10 text-foreground"
                : "border-transparent text-muted-foreground hover:border-border/70 hover:bg-background/30 hover:text-foreground",
            )}
          >
            <span
              className={cn(
                "mt-0.5 flex h-9 w-9 shrink-0 items-center justify-center rounded-xl border",
                active
                  ? "border-primary/24 bg-primary/12 text-primary"
                  : "border-border/65 bg-background/28 text-muted-foreground group-hover:border-border/85 group-hover:text-foreground",
              )}
            >
              <Icon className="h-4 w-4" />
            </span>
            <span className="min-w-0 flex-1">
              <span className="flex items-center justify-between gap-2">
                <span className="truncate font-medium text-foreground">{item.label}</span>
                {active ? <span className="h-1.5 w-1.5 rounded-full bg-primary" /> : null}
              </span>
              {!compact ? (
                <span className="mt-1 block text-xs leading-5 text-muted-foreground">{item.detail}</span>
              ) : null}
            </span>
          </Link>
        );
      })}
    </nav>
  );
}
