import * as React from "react";

import { cn } from "@/lib/utils";

export interface TextareaProps extends React.TextareaHTMLAttributes<HTMLTextAreaElement> {}

export const Textarea = React.forwardRef<HTMLTextAreaElement, TextareaProps>(function Textarea(
  { className, ...props },
  ref
) {
  return (
    <textarea
      ref={ref}
      className={cn(
        "min-h-10 w-full resize-none rounded-xl border border-input/82 px-3.5 py-2.5 text-sm text-foreground tracking-[0.01em]",
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
