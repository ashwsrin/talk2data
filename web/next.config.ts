import type { NextConfig } from "next";

const backendUrl = process.env.API_URL || 'http://127.0.0.1:8001';

const nextConfig: NextConfig = {
  output: 'standalone',
  async rewrites() {
    return [
      {
        source: '/api/:path*',
        destination: `${backendUrl}/api/:path*`,
      },
    ];
  },
};

export default nextConfig;
