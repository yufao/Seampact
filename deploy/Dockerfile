FROM python:3.11-slim
WORKDIR /app

# Git is needed when the user supplies a remote Harness repository URL.
RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY src ./src
RUN chmod -R a+rX /app/src
ENV PYTHONPATH=/app/src PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
LABEL org.opencontainers.image.version="0.7.1"
EXPOSE 8880
ENTRYPOINT ["python", "-B", "-m", "seampact"]
CMD ["serve", "--home", "/data", "--host", "0.0.0.0", "--port", "8880"]
