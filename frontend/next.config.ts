import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  /* config options here */
  cacheComponents: true,
  // Sample-run documents download as files (the live ones come from the API with the same header).
  headers() {
    return [
      {
        source: "/samples/:path*",
        headers: [{ key: "Content-Disposition", value: "attachment" }],
      },
    ];
  },
  partialPrefetching: true,
  turbopack: {
    rules: {
      "*.css": {
        loaders: ["@tailwindcss/turbopack"],
        as: "*.css",
      },
    },
  },
};

export default nextConfig;
