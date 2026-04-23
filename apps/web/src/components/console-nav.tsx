"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { type LucideIcon, Gauge, KeyRound, Laptop, MessagesSquare, Server, Settings2, Shield, UserRound, Users2 } from "lucide-react";

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
  badge: string;
  icon: LucideIcon;
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
    badge: "User",
    icon: UserRound,
    items: NAV_ITEMS.filter((item) => !item.adminOnly),
  },
  {
    id: "admin",
    label: "Admin Pages",
    badge: "Admin",
    icon: Shield,
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
    <nav className="flex flex-col gap-3">
      {sections.map((section) => {
        const SectionIcon = section.icon;

        return (
          <section
            key={section.id}
            className={cn(
              "rounded-[20px] border px-2.5 py-2.5 shadow-[inset_0_1px_0_0_oklch(var(--foreground)/0.04)]",
              section.adminOnly
                ? "border-amber-500/28 bg-amber-500/10"
                : "border-border/68 bg-background/16",
            )}
          >
            <div className="mb-2 flex items-center justify-between gap-3 px-1.5">
              <div className="inline-flex items-center gap-2">
                <span
                  className={cn(
                    "flex h-7 w-7 shrink-0 items-center justify-center rounded-lg border",
                    section.adminOnly
                      ? "border-amber-500/28 bg-amber-500/12 text-amber-200"
                      : "border-border/72 bg-background/30 text-muted-foreground",
                  )}
                >
                  <SectionIcon className="h-3.5 w-3.5" />
                </span>
                <span
                  className={cn(
                    "text-[11px] font-semibold uppercase tracking-[0.18em]",
                    section.adminOnly ? "text-amber-200" : "text-muted-foreground",
                  )}
                >
                  {section.label}
                </span>
              </div>
              <span
                className={cn(
                  "rounded-md border px-2 py-1 text-[10px] font-semibold uppercase tracking-[0.12em]",
                  section.adminOnly
                    ? "border-amber-500/28 bg-amber-500/12 text-amber-200"
                    : "border-border/72 bg-background/32 text-muted-foreground",
                )}
              >
                {section.badge}
              </span>
            </div>

            <div className="flex flex-col gap-1.5">
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
            </div>
          </section>
        );
      })}
    </nav>
  );
}
