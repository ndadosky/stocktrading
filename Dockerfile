FROM python:3.11-slim-bookworm

WORKDIR /app

ARG APP_VERSION=dev
ARG APP_GIT_COMMIT=unknown
ARG APP_BUILT_AT=

RUN apt-get update \
    && apt-get install -y --no-install-recommends fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN mkdir -p exports logs \
    && chmod +x docker/entrypoint.sh deploy/pi_pull_redeploy.sh deploy/pi_bootstrap.sh

ENV HOST=0.0.0.0 \
    PORT=80 \
    PYTHON=python3 \
    STOCK_IMAGE_PYTHON=python3 \
    TZ=America/New_York \
    APP_VERSION=$APP_VERSION \
    APP_GIT_COMMIT=$APP_GIT_COMMIT \
    APP_BUILT_AT=$APP_BUILT_AT

EXPOSE 80

ENTRYPOINT ["/app/docker/entrypoint.sh"]
