import type { ReactNode } from "react";

import { cn } from "@/lib/utils";

export function PanelCard({
  title,
  subtitle,
  action,
  children,
}: {
  title: string;
  subtitle: string;
  action?: ReactNode;
  children: ReactNode;
}) {
  return (
    <section className="rounded-[28px] border border-border/70 bg-background/45 p-5 shadow-[inset_0_1px_0_0_oklch(var(--foreground)/0.05)]">
      <div className="mb-4 flex flex-col gap-3 xl:flex-row xl:items-end xl:justify-between">
        <div className="min-w-0">
          <div className="text-lg font-semibold text-foreground">{title}</div>
          <div className="mt-1 text-sm text-muted-foreground">{subtitle}</div>
        </div>
        {action ? <div className="shrink-0">{action}</div> : null}
      </div>
      {children}
    </section>
  );
}

export function StatCard({
  label,
  value,
  tone = "default",
}: {
  label: string;
  value: string;
  tone?: "primary" | "default";
}) {
  return (
    <div
      className={cn(
        "rounded-[24px] border p-5 shadow-[inset_0_1px_0_0_oklch(var(--foreground)/0.05)]",
        tone === "primary" ? "border-primary/25 bg-primary/10" : "border-border/70 bg-background/40"
      )}
    >
      <div className="text-[11px] font-semibold uppercase tracking-[0.24em] text-muted-foreground">{label}</div>
      <div className="mt-3 text-3xl font-semibold tracking-tight text-foreground">{value}</div>
    </div>
  );
}

export function Fact({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded-2xl border border-border/60 bg-background/40 px-4 py-3">
      <div className="text-[11px] font-semibold uppercase tracking-[0.22em] text-muted-foreground">{label}</div>
      <div className="mt-2 break-words text-sm text-foreground">{value}</div>
    </div>
  );
}

export function EmptyState({ title, body }: { title: string; body: string }) {
  return (
    <div className="rounded-2xl border border-dashed border-border/70 bg-background/25 px-4 py-10 text-center">
      <div className="font-medium text-foreground">{title}</div>
      <div className="mt-2 text-sm text-muted-foreground">{body}</div>
    </div>
  );
}

export function InlineError({ message }: { message: string }) {
  return (
    <div className="rounded-2xl border border-destructive/40 bg-destructive/10 px-4 py-3 text-sm text-destructive">
      {message}
    </div>
  );
}

export function Badge({
  children,
  tone,
}: {
  children: ReactNode;
  tone: "primary" | "success" | "warning" | "default";
}) {
  return (
    <span
      className={cn(
        "rounded-full border px-2 py-0.5 text-[11px] font-medium",
        tone === "primary" && "border-primary/25 bg-primary/10 text-primary",
        tone === "success" && "border-emerald-500/30 bg-emerald-500/10 text-emerald-400",
        tone === "warning" && "border-amber-500/30 bg-amber-500/10 text-amber-300",
        tone === "default" && "border-border/60 bg-background/40 text-muted-foreground",
      )}
    >
      {children}
    </span>
  );
}

export function InfoPill({ label, value }: { label: string; value: string }) {
  return (
    <span className="rounded-full border border-border/60 bg-background/40 px-2 py-0.5">
      {label}: {value}
    </span>
  );
}
