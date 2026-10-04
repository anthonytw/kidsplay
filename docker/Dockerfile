# Stage 1: Grab the latest static build of ffmpeg
FROM mwader/static-ffmpeg:latest AS ffmpeg

# Stage 2: Your actual application image
FROM python:3.12-slim-bookworm

# Copy the modern ffmpeg binaries directly from Stage 1
COPY --from=ffmpeg /ffmpeg /usr/local/bin/
COPY --from=ffmpeg /ffprobe /usr/local/bin/

# System dependencies:
#   curl    — used to install uv
#   ca-certificates - needed for secure downloads
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
    curl \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Install uv via the official installer.
RUN curl -LsSf https://astral.sh/uv/install.sh | sh
ENV PATH="/root/.local/bin:$PATH"

WORKDIR /app

# Copy workspace configuration and lock file first so dependency installation
# is cached as its own layer, separate from application code.
COPY pyproject.toml uv.lock ./

# Copy only the packages the server needs (no cli or device).
COPY packages/kidsplay-models/ packages/kidsplay-models/
COPY packages/kidsplay-server/ packages/kidsplay-server/
COPY packages/kidsplay-importer-ytdlp/ packages/kidsplay-importer-ytdlp/

# YouTube import is an optional plugin (yt-dlp + deno). It is installed by
# default; build with `--build-arg KIDSPLAY_WITH_YTDLP=0` for a core-only image
# without yt-dlp. Accepted values: 1/true/yes/on and 0/false/no/off; anything
# else fails the build. Syncing the plugin package pulls in the server it
# depends on.
ARG KIDSPLAY_WITH_YTDLP=1
COPY docker/select-server-package.sh /usr/local/bin/select-server-package

# Install production dependencies only.
RUN uv sync --no-dev --frozen --package "$(select-server-package)"

# Media data is always mounted at runtime — never baked into the image.
EXPOSE 80

# --no-sync: the environment was built above; without it, `uv run` would sync
# the dev group (pytest, ...) into the image on every container start.
ENTRYPOINT ["uv", "run", "--no-sync", "uvicorn", \
    "kidsplay_server.api.app:create_app_from_env", \
    "--factory", \
    "--no-proxy-headers", \
    "--host", "0.0.0.0", \
    "--port", "80"]
