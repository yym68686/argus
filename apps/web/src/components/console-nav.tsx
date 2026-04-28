"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { type LucideIcon, Gauge, KeyRound, Laptop, MessagesSquare, Server, Settings2, Users2 } from "lucide-react";

import { useAuth } from "@/components/admin-gate";
import { cn } from "@/lib/utils";

type NavItem = {
  href: string;
  label: string;
  icon: LucideIcon;
  adminOnly: boolean;
};

type NavSection = {
  id: string;
  label: string;
  adminOnly?: boolean;
  items: NavItem[];
};

const NAV_ITEMS: NavItem[] = [
  { href: "/", label: "Workbench", icon: MessagesSquare, adminOnly: false },
  { href: "/api-keys", label: "API Keys", icon: KeyRound, adminOnly: false },
  { href: "/devices", label: "Devices", icon: Laptop, adminOnly: false },
  { href: "/usage", label: "Usage", icon: Gauge, adminOnly: true },
  { href: "/session-fleet", label: "Sessions", icon: Server, adminOnly: true },
  { href: "/users", label: "Users", icon: Users2, adminOnly: true },
  { href: "/settings", label: "Settings", icon: Settings2, adminOnly: true },
];

const NAV_SECTIONS: NavSection[] = [
  {
    id: "user",
    label: "User Pages",
    items: NAV_ITEMS.filter((item) => !item.adminOnly),
  },
  {
    id: "admin",
    label: "Admin Pages",
    adminOnly: true,
    items: NAV_ITEMS.filter((item) => item.adminOnly),
  },
];

export function ConsoleNav({ compact = false }: { compact?: boolean }) {
  void compact;
  const pathname = usePathname() || "/";
  const { user } = useAuth();
  const isAdmin = Boolean(user?.isAdmin);
  const sections = NAV_SECTIONS.filter((section) => !section.adminOnly || isAdmin);

  return (
    <nav className="flex flex-col gap-5">
      {sections.map((section) => {
        return (
          <div
            key={section.id}
            className={cn(
              section.adminOnly ? "border-t border-border/60 pt-4" : null,
            )}
          >
            <div
              className={cn(
                "px-3 text-[11px] font-semibold uppercase tracking-[0.16em]",
                section.adminOnly ? "text-amber-700 dark:text-amber-200" : "text-muted-foreground",
              )}
            >
              {section.label}
            </div>

            <div className="mt-2 flex flex-col gap-1">
              {section.items.map((item) => {
                const Icon = item.icon;
                const active =
                  item.href === "/" ? pathname === "/" : pathname === item.href || pathname.startsWith(`${item.href}/`);

                return (
                  <Link
                    key={item.href}
                    href={item.href}
                    aria-current={active ? "page" : undefined}
                    className={cn(
                      "group relative flex items-center gap-3 rounded-lg px-3 py-2.5 text-sm transition-colors",
                      active
                        ? "bg-primary/10 text-foreground"
                        : "text-muted-foreground hover:bg-background/30 hover:text-foreground",
                    )}
                  >
                    {active ? <span className="absolute inset-y-2 left-0 w-px rounded-full bg-primary" /> : null}
                    <Icon
                      className={cn(
                        "h-4 w-4 shrink-0",
                        active ? "text-primary" : "text-muted-foreground group-hover:text-foreground",
                      )}
                    />
                    <span className="truncate font-medium text-foreground">{item.label}</span>
                  </Link>
                );
              })}
            </div>
          </div>
        );
      })}
    </nav>
  );
}
