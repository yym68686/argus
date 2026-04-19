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
        "min-h-11 w-full resize-none rounded-[22px] border border-input/80 px-3.5 py-2.5 text-sm text-foreground tracking-[0.01em]",
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
