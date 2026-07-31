FROM python:3.12-slim

WORKDIR /app

COPY pyproject.toml ./
RUN pip install --no-cache-dir uv && uv pip install --system --no-cache -r pyproject.toml

COPY app ./app
COPY scripts ./scripts

CMD ["python", "-m", "app.main"]
