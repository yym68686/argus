"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { Gauge, MessagesSquare, Settings2, Users2 } from "lucide-react";

import { cn } from "@/lib/utils";

const NAV_ITEMS = [
  { href: "/", label: "Workbench", icon: MessagesSquare, detail: "Live sessions and threads" },
  { href: "/users", label: "Users", icon: Users2, detail: "Fleets, agents, channels" },
  { href: "/usage", label: "Usage", icon: Gauge, detail: "Requests, tokens, cost" },
  { href: "/settings", label: "Settings", icon: Settings2, detail: "Gateway state and contracts" },
];

export function ConsoleNav({ compact = false }: { compact?: boolean }) {
  const pathname = usePathname() || "/";

  return (
    <nav className={cn("flex flex-col gap-1", compact ? "px-0" : "px-2")}>
      {NAV_ITEMS.map((item) => {
        const Icon = item.icon;
        const active = item.href === "/" ? pathname === "/" : pathname === item.href || pathname.startsWith(`${item.href}/`);
        return (
          <Link
            key={item.href}
            href={item.href}
            aria-current={active ? "page" : undefined}
            className={cn(
              "group relative flex items-center gap-3 rounded-[22px] border px-3 py-3 text-sm transition-colors",
              active
                ? "border-primary/20 bg-primary/10 text-foreground shadow-[inset_0_1px_0_0_oklch(var(--foreground)/0.06)]"
                : "border-transparent bg-transparent text-muted-foreground hover:border-border/60 hover:bg-background/45 hover:text-foreground"
            )}
          >
            <span
              className={cn(
                "absolute bottom-3 left-0 top-3 w-px rounded-full bg-transparent transition-colors",
                active ? "bg-primary/70" : "group-hover:bg-border/70"
              )}
            />
            <span
              className={cn(
                "argus-shell-glyph flex h-10 w-10 items-center justify-center rounded-2xl border border-border/65",
                active ? "text-primary" : "text-muted-foreground group-hover:text-foreground"
              )}
            >
              <Icon className="h-4 w-4" />
            </span>
            <span className="min-w-0 flex-1">
              <span className="block truncate font-medium text-foreground">{item.label}</span>
              {!compact ? <span className="mt-0.5 block truncate text-xs text-muted-foreground">{item.detail}</span> : null}
            </span>
          </Link>
        );
      })}
    </nav>
  );
}
