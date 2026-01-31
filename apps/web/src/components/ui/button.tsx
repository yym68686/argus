import * as React from "react";
import { cva, type VariantProps } from "class-variance-authority";

import { cn } from "@/lib/utils";

const buttonVariants = cva(
  cn(
    "inline-flex items-center justify-center gap-2 whitespace-nowrap rounded-xl text-sm font-medium",
    "border border-border bg-background/70 backdrop-blur-md text-foreground",
    "shadow-[0_0_0_1px_oklch(var(--border)/0.35),inset_0_1px_0_0_oklch(var(--foreground)/0.10)]",
    "transition-all duration-300 [transition-timing-function:cubic-bezier(0.16,1,0.3,1)]",
    "hover:-translate-y-1 hover:border-primary/30 hover:shadow-[0_0_0_1px_oklch(var(--primary)/0.22),0_12px_34px_oklch(var(--primary)/0.14),inset_0_1px_0_0_oklch(var(--foreground)/0.12)]",
    "focus-visible:outline-none focus-visible:ring-4 focus-visible:ring-ring/25",
    "disabled:pointer-events-none disabled:opacity-50 disabled:shadow-none disabled:transform-none"
  ),
  {
    variants: {
      variant: {
        default: "",
        secondary: cn("bg-secondary/40 border-border/70 hover:border-primary/25"),
        destructive: cn(
          "bg-destructive/15 border-destructive/45 text-destructive",
          "hover:border-destructive/70 hover:shadow-[0_0_0_1px_oklch(var(--destructive)/0.35),0_12px_34px_oklch(var(--destructive)/0.18),inset_0_1px_0_0_oklch(var(--foreground)/0.10)]"
        ),
        ghost: cn("border-transparent bg-transparent shadow-none hover:border-border/60 hover:bg-background/40 hover:-translate-y-0")
      },
      size: {
        default: "h-10 px-4 py-2",
        sm: "h-9 rounded-xl px-3",
        lg: "h-11 rounded-2xl px-6"
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

