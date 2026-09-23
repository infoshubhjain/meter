import type { Metadata } from "next";
import {
  IBM_Plex_Sans,
  IBM_Plex_Mono,
  IBM_Plex_Sans_Condensed,
} from "next/font/google";
import "./marketing.css";
import "../product.css";

// Self-hosted through next/font rather than the design's <link> to Google Fonts:
// the link version blocks first paint on a third-party round trip, and this page
// opens with a timed animation where a late font swap is very visible.
const plexSans = IBM_Plex_Sans({
  variable: "--font-plex-sans",
  subsets: ["latin"],
  weight: ["400", "500", "600", "700"],
});

const plexMono = IBM_Plex_Mono({
  variable: "--font-plex-mono",
  subsets: ["latin"],
  weight: ["400", "500", "600"],
});

const plexCond = IBM_Plex_Sans_Condensed({
  variable: "--font-plex-cond",
  subsets: ["latin"],
  weight: ["600", "700"],
});

export const metadata: Metadata = {
  title: "Meter — AI infrastructure that pays its own bills",
  description:
    "Meter sits in the request path, meters every AI call your company makes, "
    + "enforces budgets, and tops up provider credit before production fails.",
};

/**
 * Root layout for the public surface — its own fonts and page stylesheet.
 */
export default function MarketingRootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html
      lang="en"
      className={`${plexSans.variable} ${plexMono.variable} ${plexCond.variable}`}
    >
      <body>{children}</body>
    </html>
  );
}
