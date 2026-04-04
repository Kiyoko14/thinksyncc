/** @type {import('next').NextConfig} */
const nextConfig = {
  reactStrictMode: true,
  output: "standalone",

  // Proxy /api/v1/* → backend so the browser never needs to reach the API
  // domain directly (avoids CORS entirely).
  // INTERNAL_API_URL is set at runtime by docker-compose / the host env.
  async rewrites() {
    const dest = process.env.INTERNAL_API_URL ?? "http://localhost:8000";
    return [
      {
        source: "/api/v1/:path*",
        destination: `${dest}/api/v1/:path*`,
      },
    ];
  },
};

module.exports = nextConfig;