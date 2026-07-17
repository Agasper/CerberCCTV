# Образ агента для Raspberry Pi (arm64) и dev-машины (amd64).
# Контекст сборки — корень репозитория (нужен доступ к common/).
FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

ENV PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    DATA_DIR=/data

WORKDIR /app

COPY common ./common
COPY agent ./agent

RUN pip install --no-cache-dir -e ./common -e ./agent

VOLUME /data

CMD ["python", "-m", "cerber_agent.main"]
