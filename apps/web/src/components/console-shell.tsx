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
      <main className="relative z-[1] min-h-dvh px-3 py-3 md:px-4 md:py-4">
        <div className="mx-auto grid min-h-[calc(100dvh-1.5rem)] max-w-[1820px] grid-cols-1 gap-3 xl:grid-cols-[288px_minmax(0,1fr)]">
          <aside className={cn("argus-shell-panel flex min-h-[18rem] flex-col overflow-hidden rounded-[28px]")}>
            <div className="border-b border-border/68 px-4 py-4">
              <div className="flex items-center justify-between gap-3">
                <div className="argus-kicker">Argus operator console</div>
                <div className="rounded-md border border-border/70 bg-background/30 px-2 py-1 font-mono text-[10px] uppercase tracking-[0.12em] text-muted-foreground">
                  v{ARGUS_WEB_VERSION}
                </div>
              </div>

              <div className="mt-4 flex items-start gap-3">
                <div className="argus-shell-glyph flex h-12 w-12 shrink-0 items-center justify-center rounded-[16px] border border-border/72">
                  <PlugZap className="h-5 w-5 text-primary" />
                </div>
                <div className="min-w-0">
                  <div className="text-lg font-semibold tracking-[-0.04em] text-foreground">Argus</div>
                  <p className="mt-1 text-sm leading-6 text-muted-foreground">
                    Compact, state-first control plane for sessions, fleets, gateways, and runtime diagnostics.
                  </p>
                </div>
              </div>
            </div>

            <div className="border-b border-border/68 px-4 py-4">
              <div className="grid grid-cols-2 gap-3">
                <div className="argus-rail-stat rounded-[16px] px-3 py-3">
                  <div className="argus-surface-label">Focus</div>
                  <div className="mt-2 text-sm font-medium text-foreground">High signal</div>
                </div>
                <div className="argus-rail-stat rounded-[16px] px-3 py-3">
                  <div className="argus-surface-label">Density</div>
                  <div className="mt-2 text-sm font-medium text-foreground">Operator grade</div>
                </div>
              </div>
            </div>

            <div className="min-h-0 flex-1 overflow-y-auto px-2 py-3">
              <div className="px-3 pb-2 text-xs font-medium uppercase tracking-[0.12em] text-muted-foreground">
                Navigation
              </div>
              <ConsoleNav />
            </div>

            <div className="border-t border-border/68 px-4 py-4">
              <div className="rounded-[18px] border border-border/72 bg-background/24 px-3.5 py-3.5">
                <div className="argus-surface-label">Layout stance</div>
                <p className="mt-2 text-sm leading-6 text-muted-foreground">
                  System state first, filters close to the data they affect, and fewer decorative cards between the operator and the work.
                </p>
              </div>
            </div>
          </aside>

          <section
            id="argus-main"
            className={cn("argus-shell-panel min-w-0 overflow-hidden rounded-[30px]")}
          >
            <header className="border-b border-border/68 px-4 py-4 md:px-5 md:py-5">
              <div className="flex flex-col gap-4 xl:flex-row xl:items-start xl:justify-between">
                <div className="min-w-0">
                  <div className="argus-kicker">Argus / {title}</div>
                  <h1 className="argus-display mt-2 text-[clamp(2rem,3vw,3.4rem)] text-foreground">{title}</h1>
                  <p className="mt-2 max-w-[72ch] text-sm leading-6 text-muted-foreground">{subtitle}</p>
                </div>
                {actions ? (
                  <div className="argus-toolbar-band w-full rounded-[18px] p-3 xl:w-auto xl:max-w-[48rem]">
                    {actions}
                  </div>
                ) : null}
              </div>
            </header>

            <div className="min-h-[calc(100dvh-11rem)] p-4 md:p-5 lg:p-6">{children}</div>
          </section>
        </div>
      </main>
    </div>
  );
}
