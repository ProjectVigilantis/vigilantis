# Vigilantis 대시보드 운영 이미지 (ADR-0010)
# build context는 저장소 루트(.)다 — 루트 .dockerignore가 node_modules·.next를 빼 준다.
#   docker build -f apps/web/Dockerfile --build-arg NEXT_PUBLIC_API_BASE_URL=https://<배포 주소> -t <이미지> .
#
# NEXT_PUBLIC_API_BASE_URL은 **빌드할 때 번들에 박힌다**(Next.js 규칙). 그래서 배포 주소가
# 정해진 뒤에 빌드하고, 주소가 바뀌면 다시 빌드한다. 서버 컴포넌트의 API 호출도 같은 주소로
# 나가므로 운영에서는 배포 주소(CloudFront)를 거친다 — 그 경로가 허용 IP(WAF)에 막히지 않게
# 플랫폼 NAT의 공인 IP가 허용 목록에 들어 있다(ADR-0010).
FROM node:22-alpine AS deps
WORKDIR /app
COPY apps/web/package.json apps/web/package-lock.json ./
RUN npm ci

FROM node:22-alpine AS build
WORKDIR /app
ARG NEXT_PUBLIC_API_BASE_URL
ARG NEXT_PUBLIC_ORG_NAME
ENV NEXT_PUBLIC_API_BASE_URL=${NEXT_PUBLIC_API_BASE_URL}     NEXT_PUBLIC_ORG_NAME=${NEXT_PUBLIC_ORG_NAME}     NEXT_TELEMETRY_DISABLED=1
COPY --from=deps /app/node_modules ./node_modules
COPY apps/web ./
RUN test -n "$NEXT_PUBLIC_API_BASE_URL" || (echo "NEXT_PUBLIC_API_BASE_URL 빌드 인자가 필요하다" && exit 1)
RUN npm run build

# standalone 출력만 옮긴다(next.config.ts output: "standalone") — node_modules 전체가 들어가지 않는다
FROM node:22-alpine AS run
WORKDIR /app
ENV NODE_ENV=production     NEXT_TELEMETRY_DISABLED=1     PORT=3000     HOSTNAME=0.0.0.0
RUN addgroup -S app && adduser -S app -G app
COPY --from=build --chown=app:app /app/.next/standalone ./
COPY --from=build --chown=app:app /app/.next/static ./.next/static
COPY --from=build --chown=app:app /app/public ./public
USER app
EXPOSE 3000
CMD ["node", "server.js"]
