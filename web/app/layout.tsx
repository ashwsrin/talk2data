import type { Metadata } from "next";
import { Suspense } from "react";
import { Geist, Geist_Mono } from "next/font/google";
import "./globals.css";
import { ThemeProvider } from "./contexts/ThemeContext";
import { ApiConfigProvider } from "./contexts/ApiConfigContext";
import { NavigationProvider } from "./contexts/NavigationContext";

const geistSans = Geist({
  variable: "--font-geist-sans",
  subsets: ["latin"],
});

const geistMono = Geist_Mono({
  variable: "--font-geist-mono",
  subsets: ["latin"],
});

export const metadata: Metadata = {
  title: "Talk2Data",
  description: "Chat with AI and tools (Talk2Data)",
  icons: { icon: "/logo.png" },
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  const themeScript = `
(function() {
  var t = localStorage.getItem('theme');
  if (['light','dark','redwood-light','redwood-teal-light','redwood-dark','redwood-plum-dark'].indexOf(t) === -1) t = 'redwood-teal-light';
  document.documentElement.setAttribute('data-theme', t);
  if (t === 'dark' || t === 'redwood-dark' || t === 'redwood-plum-dark') document.documentElement.classList.add('dark');
  else document.documentElement.classList.remove('dark');
})();
`;

  return (
    <html lang="en" suppressHydrationWarning>
      <body
        className={`${geistSans.variable} ${geistMono.variable} antialiased`}
      >
        <script dangerouslySetInnerHTML={{ __html: themeScript }} />
        <ThemeProvider>
          <ApiConfigProvider>
            <NavigationProvider>
              <Suspense fallback={<div className="flex h-screen items-center justify-center bg-app-bg"><div className="animate-spin rounded-full h-8 w-8 border-b-2 border-app-accent" /></div>}>
                {children}
              </Suspense>
            </NavigationProvider>
          </ApiConfigProvider>
        </ThemeProvider>
      </body>
    </html>
  );
}
