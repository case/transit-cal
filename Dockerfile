FROM node:26.10.0-alpine@sha256:0b36e8c136b94cd4fcf02188228e76c31ad5872eef3fec8cbd2eee500cfd9e80 AS build

WORKDIR /app

# Manifests first, so dependency layers cache across content changes
COPY web/package.json web/pnpm-lock.yaml web/pnpm-workspace.yaml ./

# Install the pnpm that package.json pins
RUN PNPM_VERSION="$(node -p "require('./package.json').packageManager.split('@')[1]")" \
    && npm install -g "pnpm@${PNPM_VERSION:?packageManager missing from package.json}"

RUN pnpm install --frozen-lockfile --prod --ignore-scripts

COPY web/eleventy.config.js ./
COPY web/source/ ./source/
RUN pnpm build

FROM caddy:2.11.6@sha256:907efba736324e43f891ccb9d760fe5abe545e313419b3d18d63d4ec670dad8d

COPY Caddyfile /etc/caddy/Caddyfile
COPY --from=build /app/_site /srv

EXPOSE 8080
