/** @type {import('next').NextConfig} */
const nextConfig = {
  // lightweight-charts can't survive StrictMode's dev mount→unmount→remount (the chart instance
  // gets torn down and the re-created one never sizes/paints). Disable until the chart lifecycle
  // is fully remount-proof.
  reactStrictMode: false,
  // Self-contained server bundle for the Docker image (#21) — deploy/Dockerfile.ui copies .next/standalone.
  output: "standalone",
  // Deterministic build id = the git SHA (passed as a Docker build arg). Next puts it in the /_next/<buildId>/
  // asset paths, so every deploy's static resources get a fresh URL → the browser can't serve stale chunks.
  // Falls back to Next's default random id in dev / when BUILD_ID isn't set.
  generateBuildId: async () => process.env.BUILD_ID || null,
  // Never cache the HTML document — mobile Safari was pinning an old bundle across deploys (the HTML
  // references hash-named chunks, so a stale HTML = stale app; every deploy looked like "nothing changed").
  // The hashed /_next/ chunks stay immutable (excluded), so this only forces a fresh HTML fetch → new chunk
  // refs → the deploy is seen immediately, no manual hard-refresh.
  async headers() {
    return [
      {
        source: "/:path((?!_next/).*)",
        headers: [{ key: "Cache-Control", value: "no-cache, no-store, must-revalidate" }],
      },
    ];
  },
};

export default nextConfig;
