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
    <section className={cn("argus-shell-panel-soft rounded-[22px] p-4 md:p-5", className)}>
      <div className="mb-4 border-b border-border/60 pb-3">
        <div className="flex flex-col gap-3">
          <div className="min-w-0">
            {eyebrow ? <div className="argus-surface-label">{eyebrow}</div> : null}
            <div
              className={cn(
                "text-[1.02rem] font-semibold tracking-[-0.02em] text-foreground",
                eyebrow ? "mt-2" : null,
              )}
            >
              {title}
            </div>
            <div className="mt-1 max-w-[72ch] text-sm leading-6 text-muted-foreground">{subtitle}</div>
          </div>
          {action ? <div className="w-full">{action}</div> : null}
        </div>
      </div>
      <div className={cn("min-w-0", contentClassName)}>{children}</div>
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
        "argus-metric-card rounded-[20px] px-4 py-4 md:px-[1.125rem] md:py-[1.125rem]",
        tone === "primary" ? "argus-metric-card-primary" : null,
        className,
      )}
    >
      <div className="argus-surface-label">{label}</div>
      <div className="mt-2 text-[clamp(1.5rem,2.1vw,2.3rem)] font-semibold tracking-[-0.05em] text-foreground">
        {value}
      </div>
      {hint ? <div className="mt-2 max-w-[24rem] text-xs leading-5 text-muted-foreground">{hint}</div> : null}
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
    <div className="rounded-[16px] border border-border/70 bg-background/24 px-3.5 py-3 shadow-[inset_0_1px_0_0_oklch(var(--foreground)/0.04)]">
      <div className="argus-surface-label">{label}</div>
      <div
        className={cn(
          "mt-2 break-words text-sm font-medium leading-6 text-foreground",
          mono ? "font-mono text-[12.5px]" : null,
        )}
      >
        {value}
      </div>
    </div>
  );
}

export function EmptyState({ title, body }: { title: string; body: string }) {
  return (
    <div className="rounded-[20px] border border-dashed border-border/72 bg-background/20 px-4 py-10 text-center">
      <div className="mx-auto h-9 w-9 rounded-lg border border-border/70 bg-background/35" />
      <div className="mt-4 text-base font-medium text-foreground">{title}</div>
      <div className="mx-auto mt-2 max-w-[36rem] text-sm leading-6 text-muted-foreground">{body}</div>
    </div>
  );
}

export function Skeleton({ className }: { className?: string }) {
  return <div aria-hidden className={cn("argus-skeleton rounded-xl", className)} />;
}

export function InlineError({ message }: { message: string }) {
  return (
    <div className="rounded-[18px] border border-destructive/36 bg-destructive/10 px-4 py-3 text-sm leading-6 text-destructive shadow-[inset_0_1px_0_0_oklch(var(--foreground)/0.04)]">
      <div className="argus-surface-label !text-destructive/90">Issue</div>
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
        "inline-flex items-center rounded-md border px-2 py-1 text-[10px] font-semibold uppercase tracking-[0.08em]",
        tone === "primary" ? "border-primary/28 bg-primary/10 text-primary" : null,
        tone === "success" ? "border-emerald-500/28 bg-emerald-500/10 text-emerald-400" : null,
        tone === "warning" ? "border-amber-500/28 bg-amber-500/10 text-amber-300" : null,
        tone === "default" ? "border-border/72 bg-background/36 text-muted-foreground" : null,
      )}
    >
      {children}
    </span>
  );
}

export function InfoPill({ label, value }: { label: string; value: string }) {
  return (
    <span className="inline-flex items-center gap-2 rounded-lg border border-border/70 bg-background/28 px-2.5 py-1 text-[10px] font-medium uppercase tracking-[0.08em] text-muted-foreground">
      <span className="text-muted-foreground/70">{label}</span>
      <span className="text-foreground/85">{value}</span>
    </span>
  );
}
