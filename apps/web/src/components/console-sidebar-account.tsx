"use client";

import { LogOut } from "lucide-react";

import { useAuth } from "@/components/admin-gate";
import { Button } from "@/components/ui/button";

export function ConsoleSidebarAccount() {
  const { user, logout } = useAuth();

  if (!user) return null;

  return (
    <div className="border-t border-border/68 px-3 py-3">
      <div className="flex items-center justify-between gap-3 rounded-lg border border-border/65 bg-background/18 px-3 py-2.5">
        <div className="min-w-0">
          <div className="truncate text-sm font-medium text-foreground">{user.email}</div>
          <div className="text-[11px] uppercase tracking-[0.12em] text-muted-foreground">
            {user.isAdmin ? "Admin" : "User"}
          </div>
        </div>
        <Button type="button" size="sm" variant="secondary" className="shrink-0" onClick={() => void logout()}>
          <LogOut className="h-4 w-4" />
          Sign out
        </Button>
      </div>
    </div>
  );
}
