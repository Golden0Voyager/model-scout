import type { NextConfig } from "next";

// Overridable so a second checkout can proxy to its own backend instead of whichever
// instance already holds 8000.
const BACKEND_URL = process.env.MODELSCOUT_BACKEND_URL ?? "http://127.0.0.1:8000";

const nextConfig: NextConfig = {
  async rewrites() {
    return [
      {
        source: "/api/:path*",
        destination: `${BACKEND_URL}/api/:path*`,
      },
    ];
  },
};

export default nextConfig;
