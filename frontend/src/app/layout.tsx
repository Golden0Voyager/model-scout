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
  title: "ModelScout | LLM 免费模型实时监控",
  description: "追踪全球最强免费模型性能，提供实时延迟与稳定性报告",
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html lang="zh-CN" className={`${firaSans.variable} ${firaCode.variable}`}>
      <body className="font-sans antialiased">{children}</body>
    </html>
  );
}
