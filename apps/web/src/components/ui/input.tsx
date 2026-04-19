import * as React from "react";

import { cn } from "@/lib/utils";

export interface InputProps extends React.InputHTMLAttributes<HTMLInputElement> {}

export const Input = React.forwardRef<HTMLInputElement, InputProps>(function Input(
  { className, type, ...props },
  ref
) {
  return (
    <input
      ref={ref}
      type={type}
      className={cn(
        "h-11 w-full rounded-2xl border border-input/80 px-3.5 py-2 text-sm text-foreground tracking-[0.01em]",
        "bg-[linear-gradient(180deg,oklch(var(--background)/0.84)_0%,oklch(var(--background)/0.62)_100%)]",
        "shadow-[inset_0_1px_0_0_oklch(var(--foreground)/0.06)]",
        "outline-none transition-shadow",
        "placeholder:text-muted-foreground/70",
        "focus-visible:border-primary/35 focus-visible:ring-4 focus-visible:ring-ring/20",
        "disabled:cursor-not-allowed disabled:opacity-50",
        className
      )}
      {...props}
    />
  );
});
