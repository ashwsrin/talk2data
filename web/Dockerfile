FROM node:20-alpine AS base

# Install dependencies only when needed
FROM base AS builder
WORKDIR /app
RUN apk add --no-cache libc6-compat
COPY package.json package-lock.json* ./

# Workaround for corporate proxy/firewall TLS interception
ENV NODE_TLS_REJECT_UNAUTHORIZED=0
RUN npm config set strict-ssl false

# Install dependencies (including devDependencies so 'next' is available)
RUN \
  if [ -f package-lock.json ]; then npm install && npm install -g next; \
  else echo "Lockfile not found." && exit 1; \
  fi

COPY . .

# Disable telemetry during build
ENV NEXT_TELEMETRY_DISABLED=1

# Debug: Check if next exists
RUN ls -la node_modules/.bin/next || echo "next binary not found in node_modules/.bin"

# Build the app
RUN npm run build

# Production image, copy all the files and run next
FROM base AS runner
WORKDIR /app

ENV NODE_ENV=production
ENV NEXT_TELEMETRY_DISABLED=1

RUN addgroup --system --gid 1001 nodejs
RUN adduser --system --uid 1001 nextjs

COPY --from=builder /app/public ./public

# Set the correct permission for prerender cache
RUN mkdir .next
RUN chown nextjs:nodejs .next

# Automatically leverage output traces to reduce image size
COPY --from=builder --chown=nextjs:nodejs /app/.next/standalone ./
COPY --from=builder --chown=nextjs:nodejs /app/.next/static ./.next/static

USER nextjs

EXPOSE 3000

ENV PORT=3000

CMD ["node", "server.js"]
