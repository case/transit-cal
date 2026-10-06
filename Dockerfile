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

# Compiles the ical4j validator; the JDK stays in this stage
FROM eclipse-temurin:25-jdk-alpine-3.24@sha256:3fd2d245c4e0eba615fe366a71b8bd25f5db7104f53e4026b24bf508b880bd2a AS validator

SHELL ["/bin/ash", "-eo", "pipefail", "-c"]
WORKDIR /build
COPY tools/ics-validate.lock tools/IcsValidate.java ./

# Fetch exactly the locked jars and check every hash before compiling against them
RUN mkdir lib \
    && grep -E '^[0-9a-f]{64} ' ics-validate.lock | while read -r sum path; do \
        wget -q -O "lib/${path##*/}" "https://repo1.maven.org/maven2/${path}"; \
        echo "${sum}  lib/${path##*/}" >>SHA256SUMS; \
    done \
    && sha256sum -c SHA256SUMS \
    && javac --release 25 -cp 'lib/*' -d classes IcsValidate.java \
    && jar --create --file lib/ics-validate.jar -C classes .

FROM caddy:2.11.6@sha256:907efba736324e43f891ccb9d760fe5abe545e313419b3d18d63d4ec670dad8d AS caddy

FROM ghcr.io/astral-sh/uv:0.12.23@sha256:61d393e44e249f2e4b526b6c7ddcecce245946826e608e11c93ad4f5bba55b21 AS uv

# Alpine's Python, as in the final stage, so the virtualenv's interpreter path matches
FROM eclipse-temurin:25-jre-alpine-3.24@sha256:3c0a9084927a221ccd1d007fcaf614465672c0af37aaa834c5184483afe56d61 AS python

RUN apk add --no-cache 'python3~3.14'
COPY --from=uv /uv /usr/local/bin/uv
WORKDIR /build
COPY pyproject.toml uv.lock README.md ./
COPY src/ ./src/
ENV UV_PROJECT_ENVIRONMENT=/opt/transit-cal UV_PYTHON=/usr/bin/python3 UV_PYTHON_DOWNLOADS=never
RUN uv sync --locked --no-dev --no-editable --compile-bytecode

FROM eclipse-temurin:25-jre-alpine-3.24@sha256:3c0a9084927a221ccd1d007fcaf614465672c0af37aaa834c5184483afe56d61

RUN apk add --no-cache 'python3~3.14' 's6~2.15' \
    && addgroup -S -g 10001 caddy && adduser -S -D -H -u 10001 -G caddy caddy \
    && addgroup -S -g 10002 feeds && adduser -S -D -H -u 10002 -G feeds feeds \
    && install -d -o 10002 -g 10002 /feeds

COPY --from=caddy /usr/bin/caddy /usr/bin/caddy
COPY Caddyfile /etc/caddy/Caddyfile
COPY --from=build /app/_site /srv
COPY --from=validator /build/lib /opt/ics-validate/lib
COPY --from=python /opt/transit-cal /opt/transit-cal
COPY tools/ics-validate tools/build-feeds tools/entrypoint /usr/local/bin/
COPY tools/s6/ /etc/s6/

# Caddy's caddy user has no home; its data and config dirs live on /tmp
ENV PATH=/opt/transit-cal/bin:$PATH \
    XDG_DATA_HOME=/tmp/caddy/data \
    XDG_CONFIG_HOME=/tmp/caddy/config
EXPOSE 8080
ENTRYPOINT ["entrypoint"]
