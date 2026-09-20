import type { Metadata, Viewport } from "next";
import { GeistSans } from "geist/font/sans";
import { JetBrains_Mono } from "next/font/google";
import "./globals.css";
// react-grid-layout chrome (drag/resize handles). Global package CSS belongs at the layout boundary.
import "react-grid-layout/css/styles.css";
import "react-resizable/css/styles.css";
import { THEME_BOOTSTRAP_SCRIPT } from "@/lib/theme";

const jetbrainsMono = JetBrains_Mono({
  variable: "--font-geist-mono",
  subsets: ["latin"],
  weight: ["400", "500", "600", "700"],
});

export const metadata: Metadata = {
  title: "kumo-trading-platform",
  description: "Manual-trading cockpit — render-only over the FastAPI/Nautilus bridge",
};

// Allow pinch-zoom (needed to inspect the chart) but DON'T let iOS auto-zoom on input focus — the latter is
// the actually-annoying behavior and is caused by inputs with font < 16px, which we prevent at the input
// level (all focusable fields are >=16px on mobile). So we no longer lock user-scaling: zoom is available
// when the user wants it, the search/order fields no longer yank the viewport on focus.
export const viewport: Viewport = {
  width: "device-width",
  initialScale: 1,
};

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return (
    <html
      lang="en"
      className={`${GeistSans.variable} ${jetbrainsMono.variable} h-full antialiased dark`}
      suppressHydrationWarning
    >
      <head>
        {/* Apply the stored theme BEFORE paint so a light/system user never flashes the dark default. */}
        <script dangerouslySetInnerHTML={{ __html: THEME_BOOTSTRAP_SCRIPT }} />
      </head>
      <body className="min-h-full flex flex-col bg-ds-bg">{children}</body>
    </html>
  );
}
