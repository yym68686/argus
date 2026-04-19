export function formatInt(value: number | null | undefined): string {
  const safe = Number.isFinite(value) ? Number(value) : 0;
  return new Intl.NumberFormat("en-US").format(safe);
}

export function formatCompact(value: number | null | undefined): string {
  const safe = Number.isFinite(value) ? Number(value) : 0;
  return new Intl.NumberFormat("en-US", {
    notation: "compact",
    maximumFractionDigits: safe >= 1000 ? 1 : 0
  }).format(safe);
}

export function formatUsd(value: number | null | undefined): string {
  const safe = Number.isFinite(value) ? Number(value) : 0;
  return new Intl.NumberFormat("en-US", {
    style: "currency",
    currency: "USD",
    minimumFractionDigits: safe >= 100 ? 0 : 2,
    maximumFractionDigits: safe >= 100 ? 0 : 4,
  }).format(safe);
}

export function formatWhen(ms: number | null | undefined): string {
  if (!ms || !Number.isFinite(ms)) return "—";
  try {
    return new Intl.DateTimeFormat(undefined, {
      month: "short",
      day: "numeric",
      hour: "2-digit",
      minute: "2-digit"
    }).format(new Date(ms));
  } catch {
    return "—";
  }
}

export function formatRelative(ms: number | null | undefined): string {
  if (!ms || !Number.isFinite(ms)) return "never";
  const diff = Date.now() - ms;
  if (diff < 60_000) return "just now";
  if (diff < 3_600_000) return `${Math.round(diff / 60_000)}m ago`;
  if (diff < 86_400_000) return `${Math.round(diff / 3_600_000)}h ago`;
  if (diff < 7 * 86_400_000) return `${Math.round(diff / 86_400_000)}d ago`;
  return formatWhen(ms);
}
