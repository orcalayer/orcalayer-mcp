# orcalayer-mcp — stdio MCP server for Polymarket smart-money analytics.
#
# Why this file exists (21.08.2026): directories such as Glama build the server
# from a Dockerfile to run tools/list in a sandbox and fill their Tools index.
# Without one they infer a Dockerfile heuristically; that inference stopped
# producing a working build after v0.3.x and the index froze at 5 tools
# (market_consensus missing). A committed Dockerfile makes the build
# deterministic and re-runs on every commit.
#
# The server starts without any environment variable; ORCALAYER_API_KEY is
# optional and only unlocks the premium whale_alerts tool.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install .

# stdio transport: the MCP client talks over stdin/stdout
ENTRYPOINT ["orcalayer-mcp"]
