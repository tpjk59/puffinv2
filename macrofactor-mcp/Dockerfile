FROM node:22-alpine AS build

RUN apk add --no-cache git python3 make g++
WORKDIR /app

RUN git clone --depth 1 https://github.com/sjawhar/macrofactor.git .
# Inject Referer header so Firebase accepts the web API key's HTTP-referrer restriction.
# Also inject X-Ios-Bundle-Identifier in case the key uses iOS-bundle restrictions.
RUN sed -i \
    "s|const BUNDLE_ID_HEADER = {|const BUNDLE_ID_HEADER = { 'Referer': 'https://sbs-diet-app.firebaseapp.com',|" \
    src/lib/api/auth.ts
RUN npm install -g pnpm@9
RUN pnpm install --no-frozen-lockfile
RUN pnpm run build:mcp

FROM node:22-alpine
WORKDIR /app
COPY --from=build /app/dist ./dist

EXPOSE 3001
CMD ["node", "dist/mcp/http.js"]
