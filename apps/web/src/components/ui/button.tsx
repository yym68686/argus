import * as React from "react";
import { cva, type VariantProps } from "class-variance-authority";

import { cn } from "@/lib/utils";

const buttonVariants = cva(
  cn(
    "inline-flex items-center justify-center gap-2 whitespace-nowrap rounded-xl border text-sm font-medium tracking-[0.01em]",
    "border-border/80 text-foreground",
    "bg-[linear-gradient(180deg,oklch(var(--surface-3)/0.86)_0%,oklch(var(--surface-2)/0.92)_100%)]",
    "shadow-[inset_0_1px_0_0_oklch(var(--foreground)/0.08)]",
    "transition-all duration-200",
    "hover:border-primary/36 hover:bg-[linear-gradient(180deg,oklch(var(--surface-3)/0.96)_0%,oklch(var(--surface-2)/0.98)_100%)]",
    "active:translate-y-px",
    "focus-visible:outline-none focus-visible:ring-4 focus-visible:ring-ring/20",
    "disabled:pointer-events-none disabled:opacity-45 disabled:shadow-none disabled:transform-none"
  ),
  {
    variants: {
      variant: {
        default: cn(
          "border-primary/35 bg-[linear-gradient(180deg,oklch(var(--primary)/0.18)_0%,oklch(var(--surface-2)/0.92)_100%)]",
          "hover:border-primary/48 hover:bg-[linear-gradient(180deg,oklch(var(--primary)/0.24)_0%,oklch(var(--surface-2)/0.96)_100%)]"
        ),
        outline: cn("bg-transparent border-border/80 hover:border-primary/30 hover:bg-background/40"),
        secondary: cn(
          "border-border/72 bg-[linear-gradient(180deg,oklch(var(--surface-2)/0.82)_0%,oklch(var(--surface-1)/0.96)_100%)]",
          "hover:border-primary/28 hover:bg-[linear-gradient(180deg,oklch(var(--surface-3)/0.88)_0%,oklch(var(--surface-2)/0.96)_100%)]"
        ),
        destructive: cn(
          "border-destructive/42 text-destructive",
          "bg-[linear-gradient(180deg,oklch(var(--destructive)/0.12)_0%,oklch(var(--surface-2)/0.92)_100%)]",
          "hover:border-destructive/64 hover:bg-[linear-gradient(180deg,oklch(var(--destructive)/0.18)_0%,oklch(var(--surface-2)/0.94)_100%)]"
        ),
        ghost: cn("border-transparent bg-transparent shadow-none hover:border-border/60 hover:bg-background/36 hover:-translate-y-0")
      },
      size: {
        default: "h-10 px-3.5 py-2",
        sm: "h-8 rounded-lg px-2.5 text-[13px]",
        lg: "h-11 rounded-xl px-[1.125rem]"
      }
    },
    defaultVariants: {
      variant: "default",
      size: "default"
    }
  }
);

export interface ButtonProps
  extends React.ButtonHTMLAttributes<HTMLButtonElement>,
    VariantProps<typeof buttonVariants> {}

export function Button({ className, variant, size, ...props }: ButtonProps) {
  return <button className={cn(buttonVariants({ variant, size }), className)} {...props} />;
}
