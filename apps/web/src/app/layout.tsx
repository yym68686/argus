import "@/app/globals.css";
import "@fontsource/press-start-2p/latin-400.css";

import type { Metadata } from "next";
import { GeistMono } from "geist/font/mono";
import { GeistSans } from "geist/font/sans";
import { Toaster } from "sonner";

import { cn } from "@/lib/utils";

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
  description: "Gateway chat UI"
};

interface RootLayoutProps {
  children: React.ReactNode;
}

export default function RootLayout({ children }: RootLayoutProps) {
  return (
    <html
      lang="en"
      className={cn("dark", GeistSans.variable, GeistMono.variable)}
      suppressHydrationWarning
    >
      <head>
        <script dangerouslySetInnerHTML={{ __html: THEME_INIT_SCRIPT }} />
      </head>
      <body className={cn("min-h-dvh bg-background font-sans text-foreground")}>
        <Toaster theme="dark" richColors closeButton />
        {children}
      </body>
    </html>
  );
}

