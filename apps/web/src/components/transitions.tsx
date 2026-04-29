"use client";

import * as React from "react";

import { cn } from "@/lib/utils";

type DropdownOrigin =
  | "top-left"
  | "top-center"
  | "top-right"
  | "bottom-left"
  | "bottom-center"
  | "bottom-right";

function durationMs(cssVarName: string, fallback: number): number {
  if (typeof window === "undefined") return fallback;
  const raw = window.getComputedStyle(document.documentElement).getPropertyValue(cssVarName).trim();
  if (!raw) return fallback;
  if (raw.endsWith("ms")) {
    const value = Number.parseFloat(raw);
    return Number.isFinite(value) ? value : fallback;
  }
  if (raw.endsWith("s")) {
    const value = Number.parseFloat(raw);
    return Number.isFinite(value) ? value * 1000 : fallback;
  }
  const value = Number.parseFloat(raw);
  return Number.isFinite(value) ? value : fallback;
}

export function useTransitionPresence(
  open: boolean,
  closeDurationVar: string,
  fallbackCloseMs: number,
  opts?: { appear?: boolean },
) {
  const appear = opts?.appear ?? true;
  const [present, setPresent] = React.useState(open);
  const [active, setActive] = React.useState(open && !appear);
  const [closing, setClosing] = React.useState(false);

  React.useEffect(() => {
    if (open) {
      let activeFrame = 0;
      const presentFrame = window.requestAnimationFrame(() => {
        setPresent(true);
        setClosing(false);
        activeFrame = window.requestAnimationFrame(() => setActive(true));
      });
      return () => {
        window.cancelAnimationFrame(presentFrame);
        window.cancelAnimationFrame(activeFrame);
      };
    }

    if (!present) return undefined;

    let timer = 0;
    const frame = window.requestAnimationFrame(() => {
      setActive(false);
      setClosing(true);
      timer = window.setTimeout(() => {
        setClosing(false);
        setPresent(false);
      }, durationMs(closeDurationVar, fallbackCloseMs));
    });

    return () => {
      window.cancelAnimationFrame(frame);
      window.clearTimeout(timer);
    };
  }, [closeDurationVar, fallbackCloseMs, open, present]);

  return { active, closing, present };
}

export function AnimatedNumber({
  value,
  className,
}: {
  value: string | number;
  className?: string;
}) {
  const text = String(value);
  const chars = Array.from(text);

  return (
    <span key={text} className={cn("t-digit-group is-animating", className)}>
      {chars.map((ch, index) => {
        const stagger = index === chars.length - 2 ? "1" : index === chars.length - 1 ? "2" : undefined;
        return (
          <span key={`${index}:${ch}`} className="t-digit" data-stagger={stagger}>
            {ch}
          </span>
        );
      })}
    </span>
  );
}

export function TextSwap({
  value,
  className,
}: {
  value: string | number | null | undefined;
  className?: string;
}) {
  const text = String(value ?? "");
  const ref = React.useRef<HTMLSpanElement | null>(null);
  const [display, setDisplay] = React.useState(text);

  React.useEffect(() => {
    if (text === display) return undefined;
    const element = ref.current;
    const reduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    if (!element || reduced) {
      setDisplay(text);
      return undefined;
    }

    element.classList.add("is-exit");
    const timer = window.setTimeout(() => {
      setDisplay(text);
      window.requestAnimationFrame(() => {
        const current = ref.current;
        if (!current) return;
        current.classList.remove("is-exit");
        current.classList.add("is-enter-start");
        void current.offsetHeight;
        current.classList.remove("is-enter-start");
      });
    }, durationMs("--text-swap-dur", 200));

    return () => window.clearTimeout(timer);
  }, [display, text]);

  return (
    <span ref={ref} className={cn("t-text-swap", className)}>
      {display}
    </span>
  );
}

export function AnimatedDropdown({
  open,
  origin = "top-right",
  className,
  children,
}: {
  open: boolean;
  origin?: DropdownOrigin;
  className?: string;
  children: React.ReactNode;
}) {
  const presence = useTransitionPresence(open, "--dropdown-close-dur", 150);

  if (!presence.present) return null;

  return (
    <div
      className={cn(
        "t-dropdown",
        presence.active ? "is-open" : null,
        presence.closing ? "is-closing" : null,
        className,
      )}
      data-origin={origin}
    >
      {children}
    </div>
  );
}

export function AnimatedModal({
  open,
  className,
  children,
}: {
  open: boolean;
  className?: string;
  children: React.ReactNode;
}) {
  const presence = useTransitionPresence(open, "--modal-close-dur", 150);

  if (!presence.present) return null;

  return (
    <div
      className={cn("t-modal", presence.active ? "is-open" : null, presence.closing ? "is-closing" : null, className)}
      role="dialog"
      aria-modal="true"
    >
      {children}
    </div>
  );
}

export function PanelReveal({
  open,
  className,
  travel = "14px",
  children,
}: {
  open: boolean;
  className?: string;
  travel?: string;
  children: React.ReactNode;
}) {
  const presence = useTransitionPresence(open, "--panel-close-dur", 350);

  if (!presence.present) return null;

  return (
    <div
      className={cn("t-panel-slide", className)}
      data-open={presence.active ? "true" : "false"}
      style={{ "--panel-translate-y": travel } as React.CSSProperties}
    >
      {children}
    </div>
  );
}

export function PageSideBySide({
  page,
  pageOne,
  pageTwo,
  className,
}: {
  page: "1" | "2";
  pageOne: React.ReactNode;
  pageTwo: React.ReactNode;
  className?: string;
}) {
  return (
    <div className={cn("t-page-slide", className)} data-page={page}>
      <section className="t-page h-full min-h-0" data-page-id="1" aria-hidden={page !== "1"}>
        {pageOne}
      </section>
      <section className="t-page h-full min-h-0" data-page-id="2" aria-hidden={page !== "2"}>
        {pageTwo}
      </section>
    </div>
  );
}
