import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  reactStrictMode: true,
  poweredByHeader: false,
  // typedRoutes is off deliberately: every dynamic link in this app is built
  // from an id (`/workspaces/${id}`), which typed routes reject without a cast
  // at each call site. The casts would cost more safety than the checking buys.
  typedRoutes: false,
  // Lint runs as its own gate (`pnpm lint`) rather than inside the build, so a
  // lint failure is reported separately from a compile failure.
  eslint: { ignoreDuringBuilds: true },
  typescript: { ignoreBuildErrors: false },
};

export default nextConfig;
