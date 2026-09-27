import type { NextConfig } from "next";

function engineOrigin(): string {
  const raw = process.env.PA_ENGINE_URL || process.env.NEXT_PUBLIC_PA_ENGINE_URL || "";
  if (raw.startsWith("http://") || raw.startsWith("https://")) {
    return raw.replace(/\/$/, "");
  }
  // Local FastAPI during `next dev` / local `next start`.
  return "http://127.0.0.1:8000";
}

const nextConfig: NextConfig = {
  reactStrictMode: true,
  async rewrites() {
    // Browser calls `/engine/...`; proxy to the PA engine (local or hosted).
    return [{ source: "/engine/:path*", destination: `${engineOrigin()}/:path*` }];
  },
};

export default nextConfig;
