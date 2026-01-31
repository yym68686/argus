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
        "min-h-11 w-full resize-none rounded-2xl border border-input bg-background/70 px-3 py-2 text-sm text-foreground",
        "shadow-[inset_0_1px_0_0_oklch(var(--foreground)/0.06)]",
        "outline-none transition-shadow",
        "placeholder:text-muted-foreground/70",
        "focus-visible:ring-4 focus-visible:ring-ring/25",
        "disabled:cursor-not-allowed disabled:opacity-50",
        className
      )}
      {...props}
    />
  );
});

