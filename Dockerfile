FROM python:3.12-slim
WORKDIR /app
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
COPY . .
RUN uv sync --all-packages --no-dev
EXPOSE 8000
CMD ["uv", "run", "uvicorn", "cowbird_server:create_app", "--factory", \
     "--host", "0.0.0.0", "--port", "8000"]
