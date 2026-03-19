import { readFileSync } from "node:fs";

import type { NextConfig } from "next";

const DEFAULT_ARGUS_VERSION = "0.0.0";

function loadArgusVersion(): string {
  const override = (process.env.ARGUS_VERSION || "").trim();
  if (override) return override;
  try {
    const value = readFileSync(new URL("../../VERSION", import.meta.url), "utf8").trim();
    if (value) return value;
  } catch {
    // ignore
  }
  return DEFAULT_ARGUS_VERSION;
}

const nextConfig: NextConfig = {
  output: "export",
  reactStrictMode: true,
  env: {
    NEXT_PUBLIC_ARGUS_VERSION: loadArgusVersion()
  }
};

export default nextConfig;
