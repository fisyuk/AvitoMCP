FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

RUN addgroup --system app && adduser --system --ingroup app app \
    && mkdir -p /data && chown app:app /data

COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir .

USER app
EXPOSE 8000

CMD ["python", "-m", "avito_mcp"]

