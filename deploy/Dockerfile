# Platform image. The base image, extras and wheel index are build-time choices;
# no GPU/CUDA combination is fixed here.
ARG BASE_IMAGE=python:3.11-slim
FROM ${BASE_IMAGE}
WORKDIR /app

# Git is needed when the user supplies a remote Harness repository URL.
RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Empty SEAMPACT_EXTRAS keeps the light platform; "rl" adds the three learners.
ARG SEAMPACT_EXTRAS=
ARG SEAMPACT_REVISION=UNKNOWN
ARG SEAMPACT_PIP_EXTRA_INDEX_URL=
COPY pyproject.toml ./
COPY src ./src
RUN python -c "import sys; assert sys.version_info[:2] == (3, 11), 'platform image requires Python 3.11: ' + sys.version" \
    && python -m pip install --no-cache-dir \
       ${SEAMPACT_PIP_EXTRA_INDEX_URL:+--extra-index-url "$SEAMPACT_PIP_EXTRA_INDEX_URL"} \
       ".${SEAMPACT_EXTRAS:+[$SEAMPACT_EXTRAS]}" \
    && python -m pip freeze --all > /app/runtime-lock.txt \
    && chmod -R a+rX /app
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 \
    SEAMPACT_RUNTIME_LOCK=/app/runtime-lock.txt \
    SEAMPACT_REVISION=${SEAMPACT_REVISION} SEAMPACT_EXTRAS=${SEAMPACT_EXTRAS}
LABEL org.opencontainers.image.version="0.10.0" org.opencontainers.image.revision="${SEAMPACT_REVISION}"
EXPOSE 8880
ENTRYPOINT ["python", "-B", "-m", "seampact"]
CMD ["serve", "--home", "/data", "--host", "0.0.0.0", "--port", "8880"]
