import "@/app/globals.css";

import type { Metadata } from "next";
import type { CSSProperties } from "react";
import { IBM_Plex_Mono, IBM_Plex_Sans, Instrument_Serif } from "next/font/google";
import { Toaster } from "sonner";

import { AdminGate } from "@/components/admin-gate";
import { cn } from "@/lib/utils";

const sans = IBM_Plex_Sans({
  subsets: ["latin"],
  weight: ["400", "500", "600", "700"],
  display: "swap",
});

const mono = IBM_Plex_Mono({
  subsets: ["latin"],
  weight: ["400", "500"],
  display: "swap",
});

const display = Instrument_Serif({
  subsets: ["latin"],
  weight: ["400"],
  style: ["normal", "italic"],
  display: "swap",
});

const ROOT_FONT_VARS = {
  "--argus-font-mono": mono.style.fontFamily,
  "--argus-font-display": display.style.fontFamily,
} as CSSProperties;

const THEME_INIT_SCRIPT = `
(function () {
  try {
    var storageKey = "argus-theme";
    var saved = localStorage.getItem(storageKey);
    var theme = saved === "light" || saved === "dark" ? saved : "dark";
    var root = document.documentElement;
    root.classList.toggle("dark", theme === "dark");

    var ua = navigator.userAgent || "";
    var isWebKit =
      ua.indexOf("AppleWebKit") !== -1 &&
      ua.indexOf("Chrome") === -1 &&
      ua.indexOf("Chromium") === -1 &&
      ua.indexOf("Edg") === -1 &&
      ua.indexOf("OPR") === -1;
    var mask = isWebKit || ua.indexOf("Firefox") !== -1 ? "circle" : "blur";
    if (mask === "circle") {
      root.setAttribute("data-argus-vt-mask", "circle");
    } else {
      root.removeAttribute("data-argus-vt-mask");
    }
  } catch (e) {}
})();
`.trim();

export const metadata: Metadata = {
  title: "Argus",
  description: "Gateway operator console"
};

interface RootLayoutProps {
  children: React.ReactNode;
}

export default function RootLayout({ children }: RootLayoutProps) {
  return (
    <html
      lang="en"
      className="dark"
      style={ROOT_FONT_VARS}
      suppressHydrationWarning
    >
      <head>
        <script dangerouslySetInnerHTML={{ __html: THEME_INIT_SCRIPT }} />
      </head>
      <body className={cn(sans.className, "min-h-dvh bg-background text-foreground antialiased")}>
        <a
          href="#argus-main"
          className="sr-only focus:not-sr-only focus:fixed focus:left-4 focus:top-4 focus:z-[100] focus:rounded-lg focus:border focus:border-border/80 focus:bg-background focus:px-3 focus:py-2 focus:text-sm focus:text-foreground"
        >
          Skip to content
        </a>
        <Toaster theme="dark" richColors closeButton />
        <AdminGate>{children}</AdminGate>
      </body>
    </html>
  );
}
