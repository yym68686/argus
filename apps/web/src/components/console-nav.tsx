"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { Gauge, KeyRound, Laptop, MessagesSquare, RadioTower, Settings2, Users2 } from "lucide-react";

import { useAuth } from "@/components/admin-gate";
import { cn } from "@/lib/utils";

const NAV_ITEMS = [
  { href: "/", label: "Workbench", icon: MessagesSquare, adminOnly: false },
  { href: "/usage", label: "Usage", icon: Gauge, adminOnly: false },
  { href: "/api-keys", label: "API Keys", icon: KeyRound, adminOnly: false },
  { href: "/devices", label: "Devices", icon: Laptop, adminOnly: true },
  { href: "/users", label: "Users", icon: Users2, adminOnly: true },
  { href: "/nodes", label: "Nodes", icon: RadioTower, adminOnly: true },
  { href: "/settings", label: "Settings", icon: Settings2, adminOnly: true },
];

export function ConsoleNav({ compact = false }: { compact?: boolean }) {
  void compact;
  const pathname = usePathname() || "/";
  const { user } = useAuth();
  const items = NAV_ITEMS.filter((item) => !item.adminOnly || Boolean(user?.isAdmin));

  return (
    <nav className="flex flex-col gap-1.5">
      {items.map((item) => {
        const Icon = item.icon;
        const active =
          item.href === "/" ? pathname === "/" : pathname === item.href || pathname.startsWith(`${item.href}/`);

        return (
          <Link
            key={item.href}
            href={item.href}
            aria-current={active ? "page" : undefined}
            className={cn(
              "group relative flex items-center gap-3 rounded-lg px-2.5 py-2 text-sm transition-colors",
              active
                ? "bg-primary/10 text-foreground"
                : "text-muted-foreground hover:bg-background/30 hover:text-foreground",
            )}
          >
            {active ? <span className="absolute inset-y-2 left-0 w-px rounded-full bg-primary" /> : null}
            <span
              className={cn(
                "flex h-8 w-8 shrink-0 items-center justify-center rounded-lg border",
                active
                  ? "border-primary/24 bg-primary/12 text-primary"
                  : "border-border/65 bg-background/20 text-muted-foreground group-hover:border-border/85 group-hover:text-foreground",
              )}
            >
              <Icon className="h-4 w-4" />
            </span>
            <span className="truncate font-medium text-foreground">{item.label}</span>
          </Link>
        );
      })}
    </nav>
  );
}
