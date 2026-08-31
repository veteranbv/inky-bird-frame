# syntax=docker/dockerfile:1.7

ARG PYTHON_IMAGE=python:3.11.16-slim-bookworm@sha256:0bee7276f83efd4a1ee05bbbf4281d95ed28e079220a9457f25a93e3f1e3c31b
ARG UV_IMAGE=ghcr.io/astral-sh/uv:0.12.7@sha256:95f2aa1fe59274951cfe9b0cbc7972e879ff1004bc8945d130a32eb0dbd85945

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
ARG CODEX_VERSION=0.151.0
ARG CODEX_AMD64_SHA256=605b4b183f22c645f5def63a5b7191767407fb66a6feaec4eaf10b5b7e0058f6
ARG CODEX_ARM64_SHA256=c1cf2baf375e261c1469381a52dc2c8fd05b6fb45cfff83fed0988fd6c5369b6
ARG CODEX_LICENSE_SHA256=d17f227e4df5da1600391338865ce0f3055211760a36688f816941d58232d8dc
ARG CODEX_NOTICE_SHA256=9d71575ecfd9a843fc1677b0efb08053c6ba9fd686a0de1a6f5382fd3c220915
ARG GH_VERSION=2.98.0
ARG GH_AMD64_SHA256=3b8ac6b30336802fc1a858d7c084e11cdf24ac1a761ca90b68022d7d729208de
ARG GH_ARM64_SHA256=cf689084f3a3618f7eae4a2420d335d74626d65f5e594b9828d125d69f800d86
ARG GH_LICENSE_SHA256=6da4adc42392c8485e40b4251c7e332fc3352df1947c9ffade71dd60b14a7a4f
RUN apt-get update \
    && apt-get install --yes --no-install-recommends ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*
RUN set -eux; \
    case "${TARGETARCH}" in \
      amd64) codex_arch=x86_64; codex_sha="${CODEX_AMD64_SHA256}"; gh_arch=amd64; gh_sha="${GH_AMD64_SHA256}" ;; \
      arm64) codex_arch=aarch64; codex_sha="${CODEX_ARM64_SHA256}"; gh_arch=arm64; gh_sha="${GH_ARM64_SHA256}" ;; \
      *) echo "Unsupported architecture: ${TARGETARCH}" >&2; exit 1 ;; \
    esac; \
    mkdir -p /out/licenses/codex /out/licenses/github-cli; \
    codex_archive=/tmp/codex.tar.gz; \
    curl -fsSL "https://github.com/openai/codex/releases/download/rust-v${CODEX_VERSION}/codex-${codex_arch}-unknown-linux-musl.tar.gz" -o "${codex_archive}"; \
    printf '%s  %s\n' "${codex_sha}" "${codex_archive}" | sha256sum -c -; \
    tar -xzf "${codex_archive}" -C /out; \
    mv "/out/codex-${codex_arch}-unknown-linux-musl" /out/codex; \
    curl -fsSL "https://raw.githubusercontent.com/openai/codex/rust-v${CODEX_VERSION}/LICENSE" -o /out/licenses/codex/LICENSE; \
    curl -fsSL "https://raw.githubusercontent.com/openai/codex/rust-v${CODEX_VERSION}/NOTICE" -o /out/licenses/codex/NOTICE; \
    printf '%s  %s\n' "${CODEX_LICENSE_SHA256}" /out/licenses/codex/LICENSE | sha256sum -c -; \
    printf '%s  %s\n' "${CODEX_NOTICE_SHA256}" /out/licenses/codex/NOTICE | sha256sum -c -; \
    gh_archive=/tmp/gh.tar.gz; \
    curl -fsSL "https://github.com/cli/cli/releases/download/v${GH_VERSION}/gh_${GH_VERSION}_linux_${gh_arch}.tar.gz" -o "${gh_archive}"; \
    printf '%s  %s\n' "${gh_sha}" "${gh_archive}" | sha256sum -c -; \
    tar -xzf "${gh_archive}" -C /tmp; \
    mv "/tmp/gh_${GH_VERSION}_linux_${gh_arch}/bin/gh" /out/gh; \
    mv "/tmp/gh_${GH_VERSION}_linux_${gh_arch}/LICENSE" /out/licenses/github-cli/LICENSE; \
    printf '%s  %s\n' "${GH_LICENSE_SHA256}" /out/licenses/github-cli/LICENSE | sha256sum -c -; \
    chmod 0755 /out/codex /out/gh; \
    chmod 0644 /out/licenses/codex/LICENSE /out/licenses/codex/NOTICE /out/licenses/github-cli/LICENSE

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
    && apt-get install --yes --no-install-recommends bubblewrap ca-certificates git \
    && rm -rf /var/lib/apt/lists/* \
    && bwrap --version \
    && groupadd --gid 10001 inky \
    && useradd --uid 10001 --gid 10001 --create-home --shell /usr/sbin/nologin inky
COPY --from=build /app/.venv /app/.venv
COPY --from=tool-downloads /out/codex /usr/local/bin/codex
COPY --from=tool-downloads /out/gh /usr/local/bin/gh
COPY --from=tool-downloads /out/licenses/ /usr/share/licenses/inky-bird-frame/third-party/
COPY LICENSE THIRD_PARTY_NOTICES.md /usr/share/licenses/inky-bird-frame/
COPY --chown=10001:10001 catalog /app/catalog
RUN mkdir -p \
      /data/catalog/species \
      /data/public-catalog \
      /data/var/controller \
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
CMD ["serve", "--config", "/data/config.toml"]
