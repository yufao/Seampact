# syntax=docker/dockerfile:1
# build_platform.py prepares and validates the source-free environment image.
ARG DEPENDENCY_IMAGE
FROM ${DEPENDENCY_IMAGE}
WORKDIR /app
COPY pyproject.toml ./
COPY src ./src
ARG SEAMPACT_EXTRAS=
# Runtime and build dependencies have already been installed and frozen.
RUN --network=none python -m pip install --no-deps --no-build-isolation ".${SEAMPACT_EXTRAS:+[$SEAMPACT_EXTRAS]}" \
    && python -m pip check \
    && python -m pip freeze --all > /app/runtime-lock.txt \
    && chmod -R a+rX /app
# Release metadata cannot invalidate the dependency environment.
ARG SEAMPACT_REVISION=UNKNOWN
ARG SEAMPACT_VERSION=UNKNOWN
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 \
    SEAMPACT_RUNTIME_LOCK=/app/runtime-lock.txt \
    SEAMPACT_REVISION=${SEAMPACT_REVISION} SEAMPACT_EXTRAS=${SEAMPACT_EXTRAS}
LABEL org.opencontainers.image.version="${SEAMPACT_VERSION}" org.opencontainers.image.revision="${SEAMPACT_REVISION}"
EXPOSE 8880
ENTRYPOINT ["python", "-B", "-m", "seampact"]
CMD ["serve", "--home", "/data", "--host", "0.0.0.0", "--port", "8880"]
