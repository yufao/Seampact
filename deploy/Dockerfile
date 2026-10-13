# Resolve this tag to a digest when recording a Docker run; the receipt records
# the actual image ID. No system package or ML runtime is required by control.
ARG PYTHON_BASE=python:3.11-slim@sha256:a3ab0b966bc4e91546a033e22093cb840908979487a9fc0e6e38295747e49ac0
FROM ${PYTHON_BASE} AS runtime
WORKDIR /app
ENV PYTHONPATH=/app/src PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 \
    CUDA_VISIBLE_DEVICES="" PYTHON_DOTENV_DISABLED=1
ENTRYPOINT ["python", "-B", "/app/deploy/container_entry.py"]

FROM runtime AS control
COPY src ./src
COPY tests ./tests
COPY tools/*.py ./tools/
COPY docs/requirements-sdk-mmct.txt ./docs/requirements-sdk-mmct.txt
COPY docs/requirements-sdk-crag.txt ./docs/requirements-sdk-crag.txt
COPY deploy/*.py ./deploy/
COPY README.md pyproject.toml ./
COPY sources/ /input/sources/
COPY sources.json release-manifest.json SOURCE-NOTICES.json /input/
# Uploaded releases may have 0600 files under the user's private 0700 folder.
# Make only copied public image artifacts readable to the Compose-selected UID.
RUN chmod -R a+rX /app /input
LABEL org.opencontainers.image.version="0.5.0"
USER 10001:10001
EXPOSE 8874
CMD ["serve"]

# Optional disposable CPU test image. These packages live inside this image,
# never in the shared server's system Python or the control image.
# Dependency stages depend only on the base digest and their requirement file;
# editing application code does not invalidate the package installation cache.
FROM runtime AS mmct-dependencies
USER 0:0
COPY docs/requirements-sdk-mmct.txt /tmp/requirements-sdk.txt
RUN PIP_CONFIG_FILE=/dev/null PIP_DISABLE_PIP_VERSION_CHECK=1 \
    python -m pip install --no-cache-dir -r /tmp/requirements-sdk.txt

FROM mmct-dependencies AS sdk-probe
COPY --from=control /app /app
COPY --from=control /input /input
LABEL org.opencontainers.image.version="0.5.0"
USER 10001:10001
CMD ["probe-mmct"]

FROM runtime AS crag-dependencies
USER 0:0
COPY docs/requirements-sdk-crag.txt /tmp/requirements-sdk.txt
RUN PIP_CONFIG_FILE=/dev/null PIP_DISABLE_PIP_VERSION_CHECK=1 \
    python -m pip install --no-cache-dir -r /tmp/requirements-sdk.txt

FROM crag-dependencies AS crag-sdk-probe
COPY --from=control /app /app
COPY --from=control /input /input
LABEL org.opencontainers.image.version="0.5.0"
USER 10001:10001
CMD ["probe-crag-sdk"]

# Default application image: the control workbench can execute CRAG tasks.
FROM crag-sdk-probe AS demo
CMD ["serve"]
