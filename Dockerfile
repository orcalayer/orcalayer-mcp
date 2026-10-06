# orcalayer-mcp — stdio MCP server for Polymarket smart-money analytics.
#
# A plain, reproducible container build for anyone who wants to run the server
# without uv/pip on the host. Note: Glama does NOT read this file — it generates
# its own Dockerfile from Admin -> Dockerfile -> Configuration (debian:trixie-slim
# + uv sync); this one is for self-hosting and for directories that do build
# from the repository.
#
# The server starts without any environment variable; ORCALAYER_API_KEY is
# optional and only unlocks the premium whale_alerts tool. Verified 21.08.2026:
# image builds, initialize + tools/list answer with all six tools.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install .

# stdio transport by default: the MCP client talks over stdin/stdout.
# For a hosted HTTP server run the image with:
#   docker run -p 8020:8020 <image> --http --host 0.0.0.0 --port 8020
# and put a reverse proxy with TLS in front (the endpoint is /mcp).
ENTRYPOINT ["orcalayer-mcp"]
