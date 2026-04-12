/** @type {import('next').NextConfig} */
const nextConfig = {
  reactStrictMode: true,
  output: "standalone",

  // Proxy /api/* → backend so the browser never needs to reach the API
  // domain directly (avoids CORS entirely).
  // INTERNAL_API_URL is set at runtime by docker-compose / the host env.
  async rewrites() {
    const dest = process.env.INTERNAL_API_URL ?? "http://localhost:8000";
    return [
      {
        source: "/api/:path*",
        destination: `${dest}/:path*`,
      },
    ];
  },
};

module.exports = nextConfig;
