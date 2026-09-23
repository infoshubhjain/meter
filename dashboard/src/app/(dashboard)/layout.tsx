import type { Metadata } from "next";
import { IBM_Plex_Sans, IBM_Plex_Mono, IBM_Plex_Sans_Condensed } from "next/font/google";
import "../globals.css";

// Inter for everything that is prose or a number. Weights are explicit because the
// design gets its hierarchy from weight (600 headings against 400 body) rather than
// from size and tracking — Next only subsets what is asked for, and a missing 600
// silently synthesises a faux-bold that ruins the type.
const plexSans = IBM_Plex_Sans({
  variable: "--font-plex-sans",
  subsets: ["latin"],
  weight: ["300", "400", "500", "600", "700"],
});

// Mono carries machine strings: model ids, feature tags, timestamps, the agent
// feed. Same family and the same CSS variable name the marketing side uses, so the
// two halves of the product now read as one product.
const plexMono = IBM_Plex_Mono({
  variable: "--font-plex-mono",
  subsets: ["latin"],
  weight: ["400", "500"],
});

// Condensed is a display face and is used in exactly one place — the page title.
const plexCond = IBM_Plex_Sans_Condensed({
  variable: "--font-plex-cond",
  subsets: ["latin"],
  weight: ["600", "700"],
});

export const metadata: Metadata = {
  title: "Meter — Dashboard",
  description:
    "The autonomous inference treasurer — budget, analyze, and transact.",
};

/**
 * Root layout for the dashboard half of the product.
 *
 * A *root* layout (it renders <html>/<body>), not a nested one — the marketing side
 * has its own. The two remain separate root layouts, but they no longer look like
 * separate products: the dashboard now draws on the same type stack, the same ink
 * ramp and the same pill vocabulary as the homepage. What the split still buys is
 * isolation — the homepage's intro animation, snap scrolling and expressive layout
 * never reach an operations screen someone watches while production is live, and
 * neither stylesheet can leak into the other.
 *
 * The documented cost is that moving between them is a full page load rather than a
 * client transition. That is acceptable, and arguably right: going from a marketing
 * page into live financial data is a hard context switch.
 */
export default function DashboardRootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html
      lang="en"
      className={`${plexSans.variable} ${plexMono.variable} ${plexCond.variable} antialiased`}
    >
      <body className="min-h-screen bg-canvas">
        {children}
      </body>
    </html>
  );
}
