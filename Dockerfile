FROM python:3.12-slim
WORKDIR /app
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# Not root: a break-out from the app should not land on a root shell. Before
# the sync rather than after it -- a `chown -R` on a built venv copies every
# file into a second layer, which cost 100MB for nothing.
RUN useradd --create-home --uid 1000 app && chown app:app /app
USER app

COPY --chown=app:app . .
RUN uv sync --all-packages --no-dev

# Render, Koyeb and Fly pick the port and inject it; a hardcoded one is never
# the one they route to, so the first deploy fails its health check and rolls
# back. The default keeps a plain `docker run -p 8000:8000` working.
ENV PORT=8000
EXPOSE 8000

# python, not curl: the slim image ships no HTTP client. /health is the only
# route outside the auth router and is exempt from the rate limiter, so probing
# it every 30s cannot spend a real caller's budget.
HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
    CMD ["python", "-c", \
         "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:' + os.environ['PORT'] + '/health').read()"]

# The venv's uvicorn rather than `uv run`: uv re-checks the lockfile and writes
# to its cache on every start. exec so uvicorn is PID 1 and receives the
# platform's SIGTERM instead of a shell swallowing it.
CMD ["sh", "-c", \
     "exec /app/.venv/bin/uvicorn cowbird_server:create_app --factory --host 0.0.0.0 --port $PORT"]
