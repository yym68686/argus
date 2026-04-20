import type { ReactNode } from "react";
import Link from "next/link";
import { PlugZap } from "lucide-react";

import { ConsoleNav } from "@/components/console-nav";
import { cn } from "@/lib/utils";

interface ConsoleFrameProps {
  header?: ReactNode;
  contextRail?: ReactNode;
  children: ReactNode;
  bodyClassName?: string;
  pageClassName?: string;
}

interface ConsoleShellProps {
  title: string;
  subtitle?: string;
  actions?: ReactNode;
  children: ReactNode;
}

export function ConsoleFrame({
  header,
  contextRail,
  children,
  bodyClassName,
  pageClassName,
}: ConsoleFrameProps) {
  return (
    <div className="min-h-dvh">
      <div className="min-h-dvh xl:grid xl:grid-cols-[216px_minmax(0,1fr)]">
        <ConsoleSidebar />

        <div className={cn("min-w-0 flex flex-col", contextRail ? "xl:grid xl:min-h-dvh xl:grid-cols-[304px_minmax(0,1fr)]" : "xl:min-h-dvh")}>
          {contextRail ? (
            <aside className="argus-subrail-surface min-h-[18rem] border-b border-border/68 xl:min-h-dvh xl:border-b-0 xl:border-r">
              {contextRail}
            </aside>
          ) : null}

          <section
            id="argus-main"
            className={cn("argus-page-surface min-w-0 flex min-h-dvh flex-col", pageClassName)}
          >
            {header ? <header className="border-b border-border/68 px-4 py-3 md:px-5 md:py-4">{header}</header> : null}
            <div className={cn("min-h-0 flex-1", bodyClassName ?? "p-4 md:p-5")}>{children}</div>
          </section>
        </div>
      </div>
    </div>
  );
}

export function ConsoleShell({ title, subtitle, actions, children }: ConsoleShellProps) {
  void subtitle;

  return (
    <ConsoleFrame
      header={
        <div className="flex flex-col gap-3 xl:flex-row xl:items-center xl:justify-between">
          <h1 className="min-w-0 text-[clamp(1.85rem,2.9vw,3rem)] font-semibold tracking-[-0.05em] text-foreground">{title}</h1>
          {actions ? <div className="w-full xl:w-auto xl:max-w-[48rem]">{actions}</div> : null}
        </div>
      }
    >
      {children}
    </ConsoleFrame>
  );
}

function ConsoleSidebar() {
  return (
    <aside className="argus-sidebar-surface border-b border-border/68 xl:sticky xl:top-0 xl:h-dvh xl:border-b-0 xl:border-r">
      <div className="flex h-full flex-col">
        <div className="px-4 py-5">
          <Link href="/" className="flex items-center gap-3">
            <span className="argus-shell-glyph flex h-10 w-10 shrink-0 items-center justify-center rounded-[14px] border border-border/72">
              <PlugZap className="h-[18px] w-[18px] text-primary" />
            </span>
            <span className="text-sm font-semibold uppercase tracking-[0.22em] text-foreground">Argus</span>
          </Link>
        </div>

        <div className="px-3 pb-5">
          <ConsoleNav />
        </div>
      </div>
    </aside>
  );
}
