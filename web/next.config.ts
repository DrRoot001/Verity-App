import type { NextConfig } from "next";

/**
 * `next build` and `next dev` must not share an output directory. Running a
 * production build while the dev server is live overwrites the chunks the dev
 * server has already loaded, and it then fails at runtime with
 * "Cannot find module './NNN.js'" — a confusing error with no relation to the
 * code being edited. Giving the build its own distDir removes the collision.
 */
const isProductionBuild = process.env.NEXT_BUILD === "1";

const nextConfig: NextConfig = {
  distDir: isProductionBuild ? ".next-build" : ".next",
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
