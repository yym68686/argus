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
        <div className="mx-auto grid min-h-[calc(100dvh-2rem)] max-w-[1680px] grid-cols-1 gap-4 lg:grid-cols-[304px_1fr]">
          <aside
            className={cn(
              "argus-shell-panel overflow-hidden rounded-[32px] backdrop-blur-xl"
            )}
          >
            <div className="border-b border-border/60 bg-background/58 p-5">
              <div className="mb-4 flex items-center justify-between gap-3">
                <div className="argus-surface-label">Operator console</div>
                <div className="rounded-full border border-border/60 bg-background/45 px-2.5 py-1 font-mono text-[10px] uppercase tracking-[0.22em] text-muted-foreground">
                  v{ARGUS_WEB_VERSION}
                </div>
              </div>

              <div className="flex items-end gap-3">
                <div
                  className={cn(
                    "argus-shell-glyph flex h-14 w-14 items-center justify-center rounded-[22px] border border-border/70"
                  )}
                >
                  <PlugZap className="h-5 w-5 text-primary" />
                </div>
                <div className="min-w-0">
                  <div className="truncate font-mono text-[15px] font-medium uppercase tracking-[0.22em] text-foreground">
                    Argus
                  </div>
                  <div className="mt-1 max-w-[16rem] text-sm leading-relaxed text-muted-foreground">
                    Quiet control surface for sessions, fleets, usage, and gateway health.
                  </div>
                </div>
              </div>
            </div>

            <div className="space-y-6 p-4">
              <div className="argus-shell-panel-soft rounded-[26px] p-4">
                <div className="argus-surface-label">Intent</div>
                <div className="mt-3 text-sm leading-relaxed text-foreground/90">
                  Read the system quickly, act with precision, and keep the noisy parts in the background.
                </div>
              </div>

              <div>
                <div className="mb-2 px-3 argus-surface-label">Sections</div>
                <ConsoleNav />
              </div>
            </div>

            <div className="border-t border-border/60 bg-background/55 px-5 py-4 text-xs text-muted-foreground">
              <div className="argus-shell-panel-soft rounded-[22px] p-4">
                <div className="argus-surface-label">Operating posture</div>
                <div className="mt-3 flex items-start justify-between gap-4">
                  <div className="max-w-[11rem] text-xs leading-relaxed text-muted-foreground">
                    Self-hosted, high-signal, and built for operators rather than end-user vanity metrics.
                  </div>
                  <div className="space-y-2 text-right font-mono text-[11px]">
                    <div className="text-foreground/85">console</div>
                    <div className="text-muted-foreground">live</div>
                  </div>
                </div>
              </div>
            </div>
          </aside>

          <section
            className={cn(
              "argus-shell-panel min-w-0 overflow-hidden rounded-[36px] backdrop-blur-xl"
            )}
          >
            <header className="argus-data-grid border-b border-border/60 bg-background/72 px-6 py-6 backdrop-blur-xl">
              <div className="flex flex-col gap-5 lg:flex-row lg:items-end lg:justify-between">
                <div className="min-w-0">
                  <div className="argus-kicker">Argus Console</div>
                  <h1 className="mt-3 text-[clamp(1.9rem,2.7vw,3rem)] font-semibold tracking-[-0.03em] text-foreground">
                    {title}
                  </h1>
                  <p className="mt-3 max-w-3xl text-sm leading-7 text-muted-foreground">{subtitle}</p>
                </div>
                {actions ? (
                  <div className="argus-shell-panel-soft shrink-0 rounded-[26px] p-3 md:p-4">{actions}</div>
                ) : null}
              </div>
            </header>

            <div className="min-h-[calc(100dvh-12rem)] p-6 md:p-7">{children}</div>
          </section>
        </div>
      </main>
    </div>
  );
}
