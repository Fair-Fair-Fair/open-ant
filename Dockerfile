# syntax=docker/dockerfile:1
#
# Open-Ant application image (multi-stage, slim runtime).
#
# Build context: src/ — the git repo root (pyproject.toml, LICENSE, launchers
# and the `ant` package all live here and are versioned).  workspace/ and
# .env stay OUTSIDE the repo on purpose and are NEVER copied into the image:
#   - the workspace is bind-mounted by docker-compose (../workspace -> /workspace)
#   - credentials are injected at run time by compose `env_file` — environment
#     variables take precedence over .env files in pydantic-settings, which is
#     exactly the precedence InfraSettings relies on (src/ant/utils/settings.py).

# ── Stage 1: builder — resolve dependencies into a virtualenv ────────────
# Layer caching: this stage only re-runs `pip install` when pyproject.toml,
# README/LICENSE or the package source change.  The pip download cache is kept
# across builds via a BuildKit cache mount (Docker Desktop ships BuildKit by
# default), so rebuilds after small code edits stay fast.
FROM python:3.12-slim AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# src-layout repo: pyproject.toml sits at repo root (= /app in the image) and
# `packages = ["ant"]` means the package directory is the repo root too.  So
# `pip install .` (hatchling backend) needs pyproject + README (readme field)
# + LICENSE + the package source present — copy those before installing.
COPY pyproject.toml README.md LICENSE ./
COPY ant ./ant

RUN --mount=type=cache,target=/root/.cache/pip \
    python -m venv /opt/venv && \
    /opt/venv/bin/pip install .

# ── Stage 2: runtime — slim image, venv + code only ──────────────────────
FROM python:3.12-slim AS runtime

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

COPY --from=builder /opt/venv /opt/venv

# Rest of the repo: launchers, docs, tests, ...
COPY . .

# Liveness probe — /healthz lives in src/ant/server/app.py (Phase 4B).
# The slim image has no curl, so probe with python urllib.
# Readiness (real backend probes) is /readyz — use it from the host.
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=5 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=3)"]

# Workspace bind-mount and credentials (compose env_file) are injected at
# run time — see docker-compose.yml.
CMD ["open-ant", "server", "--workspace", "/workspace"]
