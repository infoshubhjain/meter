import type { Metadata } from "next";
import {
  IBM_Plex_Sans,
  IBM_Plex_Mono,
  IBM_Plex_Sans_Condensed,
} from "next/font/google";
import "../(marketing)/marketing.css";
import "./predictor.css";
import "../product.css";

// Same self-hosted fonts and design system as the marketing surface, so this page
// reads as part of the same product. It gets its own root layout (like the
// dashboard does) purely so it can scroll normally — the marketing layout pins
// `no-scroll` on <body> for its timed intro, which this page has no reason to run.
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
  title: "Meter — How the predictive engine works",
  description:
    "Meter prices an LLM call before it runs. Here is exactly how the "
    + "output-token predictor turns a prompt into a dollar figure, step by step.",
};

export default function PredictorRootLayout({
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
