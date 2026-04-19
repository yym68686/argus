import type { ReactNode } from "react";

import { cn } from "@/lib/utils";

export function PanelCard({
  eyebrow,
  title,
  subtitle,
  action,
  children,
  className,
  contentClassName,
}: {
  eyebrow?: string;
  title: string;
  subtitle: string;
  action?: ReactNode;
  children: ReactNode;
  className?: string;
  contentClassName?: string;
}) {
  return (
    <section className={cn("argus-shell-panel-soft rounded-[30px] p-5 md:p-6", className)}>
      <div className="mb-4 flex flex-col gap-3 xl:flex-row xl:items-end xl:justify-between">
        <div className="min-w-0">
          {eyebrow ? <div className="argus-surface-label">{eyebrow}</div> : null}
          <div className={cn("text-lg font-semibold tracking-[-0.02em] text-foreground", eyebrow && "mt-2")}>{title}</div>
          <div className="mt-1 text-sm leading-6 text-muted-foreground">{subtitle}</div>
        </div>
        {action ? <div className="shrink-0">{action}</div> : null}
      </div>
      <div className={contentClassName}>{children}</div>
    </section>
  );
}

export function StatCard({
  label,
  value,
  tone = "default",
  hint,
  className,
}: {
  label: string;
  value: string;
  tone?: "primary" | "default";
  hint?: string;
  className?: string;
}) {
  return (
    <div
      className={cn(
        "argus-metric-card rounded-[26px] p-5 md:p-6",
        tone === "primary" && "argus-metric-card-primary",
        className
      )}
    >
      <div className="argus-surface-label">{label}</div>
      <div className="mt-4 text-[clamp(1.9rem,2.6vw,3.15rem)] font-semibold tracking-[-0.04em] text-foreground">
        {value}
      </div>
      {hint ? <div className="mt-3 max-w-[16rem] text-xs leading-6 text-muted-foreground">{hint}</div> : null}
    </div>
  );
}

export function Fact({
  label,
  value,
  mono = false,
}: {
  label: string;
  value: string;
  mono?: boolean;
}) {
  return (
    <div className="rounded-[22px] border border-border/60 bg-background/30 px-4 py-3.5 shadow-[inset_0_1px_0_0_oklch(var(--foreground)/0.04)]">
      <div className="argus-surface-label">{label}</div>
      <div className={cn("mt-2 break-words text-sm leading-6 text-foreground", mono && "font-mono text-[13px]")}>{value}</div>
    </div>
  );
}

export function EmptyState({ title, body }: { title: string; body: string }) {
  return (
    <div className="rounded-[24px] border border-dashed border-border/70 bg-background/20 px-4 py-10 text-center">
      <div className="mx-auto mb-4 h-12 w-12 rounded-full border border-border/60 bg-background/40" />
      <div className="text-base font-medium text-foreground">{title}</div>
      <div className="mx-auto mt-2 max-w-[28rem] text-sm leading-6 text-muted-foreground">{body}</div>
    </div>
  );
}

export function InlineError({ message }: { message: string }) {
  return (
    <div className="rounded-[22px] border border-destructive/40 bg-destructive/10 px-4 py-3.5 text-sm leading-6 text-destructive shadow-[inset_0_1px_0_0_oklch(var(--foreground)/0.04)]">
      <div className="argus-surface-label !text-destructive/90">Attention</div>
      <div className="mt-1">{message}</div>
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
        "rounded-full border px-2.5 py-1 text-[11px] font-medium tracking-[0.02em]",
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
    <span className="rounded-full border border-border/60 bg-background/40 px-2.5 py-1 text-[11px] tracking-[0.02em] text-muted-foreground">
      <span className="uppercase tracking-[0.18em] text-muted-foreground/80">{label}</span> {value}
    </span>
  );
}
