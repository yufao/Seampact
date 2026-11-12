# syntax=docker/dockerfile:1
# build_platform.py prepares and validates the source-free environment image.
ARG DEPENDENCY_IMAGE
FROM node:24.17.0-bookworm-slim AS pi_dependencies
WORKDIR /opt/seampact-pi
COPY src/seampact/pi_host/package.json src/seampact/pi_host/package-lock.json ./
RUN npm ci --ignore-scripts --no-audit --no-fund

FROM ${DEPENDENCY_IMAGE}
WORKDIR /app
COPY --from=pi_dependencies /usr/local/bin/node /usr/local/bin/node
COPY --from=pi_dependencies /opt/seampact-pi/node_modules /opt/seampact-pi/node_modules
ARG SEAMPACT_EXTRAS=
# An optional named context contains agentdojo.bundle for offline builds.
# Keep the Git checkout: the native evaluator verifies its exact identity.
RUN --mount=type=bind,from=agentdojo_input,target=/agentdojo-input \
    case ",${SEAMPACT_EXTRAS}," in *,office,*) \
      if [ -f /agentdojo-input/agentdojo.bundle ]; then \
        git clone /agentdojo-input/agentdojo.bundle /opt/agentdojo; \
      else \
        git clone https://github.com/ethz-spylab/agentdojo.git /opt/agentdojo; \
      fi && \
      git -C /opt/agentdojo checkout --detach 089ed468cf3ed0322acc66b0211f26d9d90dbf60 && \
      test "$(git -C /opt/agentdojo rev-parse HEAD)" = 089ed468cf3ed0322acc66b0211f26d9d90dbf60 && \
      python -m pip install --no-deps --no-build-isolation -e /opt/agentdojo && \
      test -z "$(git -C /opt/agentdojo status --porcelain --untracked-files=no)" ;; \
    esac
COPY pyproject.toml ./
COPY src ./src
# Runtime and build dependencies have already been installed and frozen.
RUN --network=none python -m pip install --no-deps --no-build-isolation ".${SEAMPACT_EXTRAS:+[$SEAMPACT_EXTRAS]}" \
    && python -m pip check \
    && python -c "from pathlib import Path; import seampact, subprocess; host = Path(seampact.__file__).parent / 'pi_host'; (host / 'node_modules').symlink_to('/opt/seampact-pi/node_modules', target_is_directory=True); subprocess.run(['node', '--input-type=module', '-e', 'await import(' + repr((host / 'bridge.mjs').as_uri()) + ')'], check=True)" \
    && python -m pip freeze --all > /app/runtime-lock.txt \
    && chmod -R a+rX /app /opt/seampact-pi \
    && if [ -d /opt/agentdojo ]; then chmod -R a+rX /opt/agentdojo; fi
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
