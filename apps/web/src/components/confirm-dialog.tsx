"use client";

import * as React from "react";
import { createPortal } from "react-dom";
import { AlertTriangle } from "lucide-react";

import { AnimatedModal, useTransitionPresence } from "@/components/transitions";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";

export interface ConfirmOptions {
  title: string;
  body?: string;
  confirmLabel?: string;
  cancelLabel?: string;
  tone?: "default" | "destructive";
}

export function useConfirmDialog() {
  const [dialogState, setDialogState] = React.useState<{ open: boolean; options: ConfirmOptions | null }>({
    open: false,
    options: null,
  });
  const resolverRef = React.useRef<((confirmed: boolean) => void) | null>(null);

  const close = React.useCallback((confirmed: boolean) => {
    resolverRef.current?.(confirmed);
    resolverRef.current = null;
    setDialogState((current) => ({ ...current, open: false }));
  }, []);

  const confirm = React.useCallback((options: ConfirmOptions) => {
    return new Promise<boolean>((resolve) => {
      resolverRef.current?.(false);
      resolverRef.current = resolve;
      setDialogState({ open: true, options });
    });
  }, []);

  React.useEffect(() => {
    return () => {
      resolverRef.current?.(false);
      resolverRef.current = null;
    };
  }, []);

  return {
    confirm,
    confirmDialog: (
      <ConfirmDialog
        open={dialogState.open}
        options={dialogState.options}
        onCancel={() => close(false)}
        onConfirm={() => close(true)}
      />
    ),
  };
}

function ConfirmDialog({
  open,
  options,
  onCancel,
  onConfirm,
}: {
  open: boolean;
  options: ConfirmOptions | null;
  onCancel: () => void;
  onConfirm: () => void;
}) {
  const overlay = useTransitionPresence(open, "--modal-close-dur", 150);
  const title = options?.title ?? "";
  const body = options?.body;
  const tone = options?.tone ?? "destructive";

  React.useEffect(() => {
    if (!open) return undefined;
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") onCancel();
    };
    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, [onCancel, open]);

  if (typeof document === "undefined" || !overlay.present || !options) return null;

  return createPortal(
    <div className="fixed inset-0 z-[100] flex items-center justify-center px-4 py-6">
      <button
        type="button"
        className="argus-confirm-overlay absolute inset-0"
        data-open={overlay.active ? "true" : "false"}
        aria-label="Cancel"
        onClick={onCancel}
      />
      <AnimatedModal
        open={open}
        className="relative w-full max-w-md rounded-[22px] border border-border/78 bg-background/96 p-5 shadow-[0_24px_72px_oklch(0%_0_0/0.32)]"
      >
        <div className="flex items-start gap-3">
          <div
            className={cn(
              "flex h-10 w-10 shrink-0 items-center justify-center rounded-xl border",
              tone === "destructive"
                ? "border-destructive/36 bg-destructive/10 text-destructive"
                : "border-primary/28 bg-primary/10 text-primary",
            )}
          >
            <AlertTriangle className="h-5 w-5" />
          </div>
          <div className="min-w-0">
            <div className="text-base font-semibold tracking-[-0.02em] text-foreground">{title}</div>
            {body ? <div className="mt-2 text-sm leading-6 text-muted-foreground">{body}</div> : null}
          </div>
        </div>

        <div className="mt-5 flex justify-end gap-2">
          <Button type="button" variant="secondary" onClick={onCancel}>
            {options.cancelLabel ?? "Cancel"}
          </Button>
          <Button type="button" variant={tone === "destructive" ? "destructive" : "default"} onClick={onConfirm}>
            {options.confirmLabel ?? "Confirm"}
          </Button>
        </div>
      </AnimatedModal>
    </div>,
    document.body,
  );
}
