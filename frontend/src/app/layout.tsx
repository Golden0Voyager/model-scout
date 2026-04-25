import type { Metadata } from "next";
import { Fira_Sans, Fira_Code } from "next/font/google";
import "./globals.css";

const firaSans = Fira_Sans({ 
  subsets: ["latin"], 
  weight: ["300", "400", "500", "600", "700"],
  variable: '--font-fira-sans',
});

const firaCode = Fira_Code({ 
  subsets: ["latin"], 
  weight: ["400", "500", "600", "700"],
  variable: '--font-fira-code',
});

export const metadata: Metadata = {
  title: "ModelScout | LLM Model Availability Monitor",
  description: "Track free and paid LLM model performance with real-time latency and stability reports.",
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html lang="en" className={`${firaSans.variable} ${firaCode.variable}`}>
      <body className="font-sans antialiased">{children}</body>
    </html>
  );
}
