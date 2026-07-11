# Open-Ant Docker Sandbox — isolated execution environment.
#
# Build:
#   docker build -t open-ant-sandbox .
#
# Architecture:
#   /workspace-ro  ← bind-mounted real workspace (read-only)
#   /workspace     ← tmpfs copy (writable, destroyed on container exit)
#   /tmp           ← tmpfs scratch space
#
# On start, entrypoint.sh copies /workspace-ro → /workspace.
# The container can freely read/write/delete /workspace — the real
# files on the host are never touched.

FROM alpine:3.20

RUN adduser -D sandbox \
    && apk add --no-cache \
        bash \
        coreutils \
        grep \
        sed \
        findutils \
        curl \
        wget \
        jq

COPY entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

USER sandbox
WORKDIR /workspace

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["sh"]
