/** @type {import('next').NextConfig} */
const backend = process.env.BACKEND_URL || "http://127.0.0.1:8000";

const nextConfig = {
  // Same-origin proxy: the browser calls /api/*, Next forwards to FastAPI.
  // Avoids CORS and keeps the API base configurable for deploys.
  async rewrites() {
    return [{ source: "/api/:path*", destination: `${backend}/api/:path*` }];
  },
};

export default nextConfig;
