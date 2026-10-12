import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  // 운영 이미지(apps/web/Dockerfile)가 .next/standalone만 옮겨 담는다(ADR-0010). 개발 서버(next dev)와 무관하다
  output: "standalone",
};

export default nextConfig;
