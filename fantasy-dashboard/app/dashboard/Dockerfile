# ── Fantasy Dashboard — Node.js Dashboard ───────────────────
# Build context: fantasy-dashboard/app/dashboard/
# Entrypoint:   node server.js  (Express, port 3000)
# ─────────────────────────────────────────────────────────────

FROM node:20-bookworm-slim AS deps

WORKDIR /app

# Copy lockfile first for layer caching
COPY package.json package-lock.json ./
RUN npm ci --omit=dev

# ── Final image ───────────────────────────────────────────────
FROM node:20-bookworm-slim AS final

WORKDIR /app

# Copy installed modules from deps stage
COPY --from=deps /app/node_modules ./node_modules

# Copy application source
COPY . .

# Non-root runtime user (node uid 1000 exists in this image)
USER node

EXPOSE 3000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD node -e "require('http').get('http://localhost:3000/health', r => process.exit(r.statusCode===200?0:1)).on('error',()=>process.exit(1))"

CMD ["node", "server.js"]
