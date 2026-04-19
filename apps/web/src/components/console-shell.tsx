import type { ReactNode } from "react";
import { PlugZap } from "lucide-react";

import { ConsoleNav } from "@/components/console-nav";
import { cn } from "@/lib/utils";

const ARGUS_WEB_VERSION = process.env.NEXT_PUBLIC_ARGUS_VERSION || "0.0.0";

interface ConsoleShellProps {
  title: string;
  subtitle: string;
  actions?: ReactNode;
  children: ReactNode;
}

export function ConsoleShell({ title, subtitle, actions, children }: ConsoleShellProps) {
  return (
    <div className="relative min-h-dvh">
      <div className="pointer-events-none fixed inset-0 z-0 overflow-hidden">
        <div className="absolute inset-0 argus-landing-canvas" />
        <div className="absolute inset-0 argus-landing-grid-64 opacity-70" />
        <div className="absolute inset-0 argus-landing-noise" />
        <div className="absolute -left-[30%] -top-[25%] h-[70vh] w-[70vw] argus-landing-blob argus-landing-blob-a" />
        <div className="absolute -right-[30%] -top-[20%] h-[70vh] w-[70vw] argus-landing-blob argus-landing-blob-b" />
        <div className="absolute -bottom-[30%] -right-[10%] h-[80vh] w-[80vw] argus-landing-blob argus-landing-blob-c" />
      </div>

      <main className="relative z-[1] min-h-dvh w-full px-4 py-4 md:px-6">
        <div className="mx-auto grid min-h-[calc(100dvh-2rem)] max-w-[1600px] grid-cols-1 gap-4 lg:grid-cols-[280px_1fr]">
          <aside
            className={cn(
              "overflow-hidden rounded-[28px] border border-border/70 bg-card/82 backdrop-blur-xl",
              "shadow-[0_0_0_1px_oklch(var(--border)/0.55),0_18px_60px_oklch(0%_0_0/0.35)]"
            )}
          >
            <div className="border-b border-border/60 bg-background/70 p-5">
              <div className="flex items-end gap-3">
                <div
                  className={cn(
                    "flex h-12 w-12 items-center justify-center rounded-2xl border border-border",
                    "bg-background/70 shadow-[0_0_0_1px_oklch(var(--border)/0.55),0_14px_40px_oklch(0%_0_0/0.25)]"
                  )}
                >
                  <PlugZap className="h-5 w-5 text-primary" />
                </div>
                <div className="min-w-0">
                  <div className="truncate font-logo text-base tracking-wide text-foreground">Argus</div>
                  <div className="truncate text-sm text-muted-foreground">Operator console</div>
                </div>
              </div>
            </div>

            <div className="space-y-6 p-4">
              <div>
                <div className="mb-2 px-3 text-[11px] font-semibold uppercase tracking-[0.24em] text-muted-foreground">
                  Console
                </div>
                <ConsoleNav />
              </div>
            </div>

            <div className="border-t border-border/60 bg-background/55 px-5 py-4 text-xs text-muted-foreground">
              <div className="flex items-center justify-between gap-2">
                <span>Web</span>
                <code className="font-mono text-foreground/80">{ARGUS_WEB_VERSION}</code>
              </div>
            </div>
          </aside>

          <section
            className={cn(
              "min-w-0 overflow-hidden rounded-[32px] border border-border/70 bg-card/82 backdrop-blur-xl",
              "shadow-[0_0_0_1px_oklch(var(--border)/0.55),0_18px_60px_oklch(0%_0_0/0.35)]"
            )}
          >
            <header className="border-b border-border/60 bg-background/72 px-6 py-5 backdrop-blur-xl">
              <div className="flex flex-col gap-4 lg:flex-row lg:items-end lg:justify-between">
                <div className="min-w-0">
                  <div className="text-[11px] font-semibold uppercase tracking-[0.24em] text-primary">Argus Console</div>
                  <h1 className="mt-2 text-2xl font-semibold tracking-tight text-foreground">{title}</h1>
                  <p className="mt-2 max-w-3xl text-sm leading-relaxed text-muted-foreground">{subtitle}</p>
                </div>
                {actions ? <div className="shrink-0">{actions}</div> : null}
              </div>
            </header>

            <div className="min-h-[calc(100dvh-12rem)] p-6">{children}</div>
          </section>
        </div>
      </main>
    </div>
  );
}
