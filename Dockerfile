# syntax=docker/dockerfile:1.7

ARG PYTHON_IMAGE=python:3.11.13-slim-bookworm@sha256:86adf8dbadc3d6e82ee5dd2c74bec2e1c2467cdad47886280501df722372d2e1
ARG UV_IMAGE=ghcr.io/astral-sh/uv:0.5.10@sha256:e4c08963c249b0e07d88e9313374d00491e69eed0c99ca5ee443e5c234a16a38

FROM ${UV_IMAGE} AS uv

FROM ${PYTHON_IMAGE} AS build
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
COPY --from=uv /uv /usr/local/bin/uv
WORKDIR /app
COPY pyproject.toml uv.lock README.md LICENSE ./
COPY src ./src
RUN uv sync --locked --no-dev --extra controller --no-editable

FROM ${PYTHON_IMAGE} AS tool-downloads
ARG TARGETARCH
ARG CODEX_VERSION=0.139.0
ARG CODEX_AMD64_SHA256=12ebf70df41dc831061862912ab5e7eacdd112bb17e8ce9b2098cb3d92180081
ARG CODEX_ARM64_SHA256=2b7407643e0e74c525d84347c9eecec4b3d275af0382142ac42216508bb0b2a2
ARG GH_VERSION=2.95.0
ARG GH_AMD64_SHA256=25d1e4729e8808c9ed3d613e96ebd3f3e44446f2d368c89d878a71a36ddb3d8c
ARG GH_ARM64_SHA256=d41e0b3b6218e5741c8bb4db39b16e53a59e0e06299a8489bd38f623ef7ebaae
RUN apt-get update \
    && apt-get install --yes --no-install-recommends ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*
RUN set -eux; \
    case "${TARGETARCH}" in \
      amd64) codex_arch=x86_64; codex_sha="${CODEX_AMD64_SHA256}"; gh_arch=amd64; gh_sha="${GH_AMD64_SHA256}" ;; \
      arm64) codex_arch=aarch64; codex_sha="${CODEX_ARM64_SHA256}"; gh_arch=arm64; gh_sha="${GH_ARM64_SHA256}" ;; \
      *) echo "Unsupported architecture: ${TARGETARCH}" >&2; exit 1 ;; \
    esac; \
    mkdir -p /out; \
    codex_archive=/tmp/codex.tar.gz; \
    curl -fsSL "https://github.com/openai/codex/releases/download/rust-v${CODEX_VERSION}/codex-${codex_arch}-unknown-linux-musl.tar.gz" -o "${codex_archive}"; \
    printf '%s  %s\n' "${codex_sha}" "${codex_archive}" | sha256sum -c -; \
    tar -xzf "${codex_archive}" -C /out; \
    mv "/out/codex-${codex_arch}-unknown-linux-musl" /out/codex; \
    gh_archive=/tmp/gh.tar.gz; \
    curl -fsSL "https://github.com/cli/cli/releases/download/v${GH_VERSION}/gh_${GH_VERSION}_linux_${gh_arch}.tar.gz" -o "${gh_archive}"; \
    printf '%s  %s\n' "${gh_sha}" "${gh_archive}" | sha256sum -c -; \
    tar -xzf "${gh_archive}" -C /tmp; \
    mv "/tmp/gh_${GH_VERSION}_linux_${gh_arch}/bin/gh" /out/gh; \
    chmod 0755 /out/codex /out/gh

FROM ${PYTHON_IMAGE} AS runtime
ARG VCS_REF=unknown
ARG BUILD_DATE=unknown
LABEL org.opencontainers.image.title="Inky Bird Frame Controller" \
      org.opencontainers.image.description="Discovery, generation, review, publication, and catalog serving for Inky Bird Frame" \
      org.opencontainers.image.source="https://github.com/veteranbv/inky-bird-frame" \
      org.opencontainers.image.revision="${VCS_REF}" \
      org.opencontainers.image.created="${BUILD_DATE}" \
      org.opencontainers.image.licenses="MIT"
RUN apt-get update \
    && apt-get install --yes --no-install-recommends ca-certificates git \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 inky \
    && useradd --uid 10001 --gid 10001 --create-home --shell /usr/sbin/nologin inky
COPY --from=build /app/.venv /app/.venv
COPY --from=tool-downloads /out/codex /usr/local/bin/codex
COPY --from=tool-downloads /out/gh /usr/local/bin/gh
COPY --chown=10001:10001 catalog /app/catalog
RUN mkdir -p \
      /data/catalog/species \
      /data/public-catalog \
      /data/state \
      /data/workspace \
      /home/inky/.codex \
      /home/inky/.config/git \
      /home/inky/.config/gh \
    && chown -R inky:inky /data /home/inky
ENV PATH=/app/.venv/bin:/usr/local/bin:/usr/bin:/bin \
    HOME=/home/inky \
    CODEX_HOME=/home/inky/.codex \
    GIT_CONFIG_GLOBAL=/home/inky/.config/git/config \
    PYTHONUNBUFFERED=1
USER 10001:10001
WORKDIR /data
EXPOSE 8793
ENTRYPOINT ["inky-bird-frame"]
CMD ["serve", "--config", "/config/config.toml"]
