import type { Metadata, Viewport } from "next";
import "./globals.css";
import AuthBootstrap from "./AuthBootstrap";

export const metadata: Metadata = {
  title: {
    default: "ThinkSync",
    template: "%s · ThinkSync",
  },
  description: "AI-powered server operations, workspace execution, and deployment.",
};

export const viewport: Viewport = {
  width: "device-width",
  initialScale: 1,
  maximumScale: 5,
  userScalable: true,
  viewportFit: "cover",
};

export default function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <html lang="en" className="scroll-smooth bg-slate-50">
      <body className="app-shell bg-slate-50 text-slate-900 antialiased">
        <AuthBootstrap>{children}</AuthBootstrap>
      </body>
    </html>
  );
}
