# Образ админки. Контекст сборки — корень репозитория
# (нужен доступ к common/), см. docker-compose.dev.yml и .do/app.yaml.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    TZ=UTC

WORKDIR /app

COPY common ./common
COPY admin ./admin

RUN pip install --no-cache-dir -e ./common -e ./admin \
    && chmod +x /app/admin/docker/entrypoint.sh

EXPOSE 8080

CMD ["/app/admin/docker/entrypoint.sh"]
