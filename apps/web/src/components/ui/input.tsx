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
        "h-10 w-full rounded-xl border border-input/82 px-3.5 py-2 text-sm text-foreground tracking-[0.01em]",
        "bg-[linear-gradient(180deg,oklch(var(--surface-2)/0.84)_0%,oklch(var(--surface-1)/0.98)_100%)]",
        "shadow-[inset_0_1px_0_0_oklch(var(--foreground)/0.05)]",
        "outline-none transition-shadow",
        "placeholder:text-muted-foreground/70",
        "focus-visible:border-primary/38 focus-visible:ring-4 focus-visible:ring-ring/18",
        "disabled:cursor-not-allowed disabled:opacity-50",
        className
      )}
      {...props}
    />
  );
});
