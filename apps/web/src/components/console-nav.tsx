"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { Gauge, MessagesSquare, Settings2, Users2 } from "lucide-react";

import { cn } from "@/lib/utils";

const NAV_ITEMS = [
  { href: "/", label: "Workbench", icon: MessagesSquare },
  { href: "/users", label: "Users", icon: Users2 },
  { href: "/usage", label: "Usage", icon: Gauge },
  { href: "/settings", label: "Settings", icon: Settings2 },
];

export function ConsoleNav({ compact = false }: { compact?: boolean }) {
  const pathname = usePathname() || "/";

  return (
    <nav className={cn("flex flex-col gap-1", compact ? "px-0" : "px-2")}>
      {NAV_ITEMS.map((item) => {
        const Icon = item.icon;
        const active = pathname === item.href;
        return (
          <Link
            key={item.href}
            href={item.href}
            className={cn(
              "group flex items-center gap-3 rounded-2xl border px-3 py-2.5 text-sm transition-colors",
              active
                ? "border-primary/25 bg-primary/10 text-foreground"
                : "border-transparent bg-transparent text-muted-foreground hover:border-border/60 hover:bg-background/50 hover:text-foreground"
            )}
          >
            <span
              className={cn(
                "flex h-9 w-9 items-center justify-center rounded-xl border border-border/60 bg-background/60",
                active ? "text-primary" : "text-muted-foreground group-hover:text-foreground"
              )}
            >
              <Icon className="h-4 w-4" />
            </span>
            <span className="truncate">{item.label}</span>
          </Link>
        );
      })}
    </nav>
  );
}
