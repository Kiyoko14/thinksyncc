import type { Metadata, Viewport } from "next";
import "./globals.css";
import AuthBootstrap from "./AuthBootstrap";

export const metadata: Metadata = {
  title: "ThinkSync",
  description: "AI DevOps Platform",
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
      <body className="min-h-screen bg-slate-50 text-slate-900 antialiased">
        <AuthBootstrap>{children}</AuthBootstrap>
      </body>
    </html>
  );
}
