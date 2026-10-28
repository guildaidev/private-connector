# syntax=docker/dockerfile:1
FROM ghcr.io/astral-sh/uv:0.13.4@sha256:9f130421e930bfbdc563c00740466603f8723158e90297d07957c2294640262b AS uv
FROM python:3.13.17-slim-bookworm@sha256:e32be0e55814a08977501ca863271d9bd02443710db8f2afaf8372e2c4508530 AS build
COPY --from=uv /uv /usr/local/bin/uv
WORKDIR /opt/connector
COPY pyproject.toml uv.lock ./
COPY guild_connector ./guild_connector
RUN uv sync --frozen --no-dev --no-editable --no-cache --python /usr/local/bin/python

FROM python:3.13.17-slim-bookworm@sha256:e32be0e55814a08977501ca863271d9bd02443710db8f2afaf8372e2c4508530
ARG VERSION=0.1.0
ARG REVISION=unknown
LABEL org.opencontainers.image.title="Guild private connector" \
      org.opencontainers.image.source="https://github.com/guildaidev/private-connector" \
      org.opencontainers.image.version=$VERSION \
      org.opencontainers.image.revision=$REVISION
ENV PATH="/opt/connector/.venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
RUN groupadd --gid 10001 connector && \
    useradd --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin connector && \
    install -d -o 10001 -g 10001 -m 0770 /var/lib/guild-connector
COPY --from=build /opt/connector/.venv /opt/connector/.venv
USER 10001:10001
WORKDIR /var/lib/guild-connector
STOPSIGNAL SIGTERM
HEALTHCHECK --interval=15s --timeout=5s --start-period=15s --retries=3 \
    CMD ["guild-private-connector-health"]
ENTRYPOINT ["guild-private-connector"]
CMD ["run", "--config", "/etc/guild/connector.json", "--health-file", "/tmp/connector-health.json"]
