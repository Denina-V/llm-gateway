# Multi-stage: the wheel-building layer carries a compiler and headers that have
# no business being in a running container.
FROM python:3.12-slim AS builder

WORKDIR /build
ENV PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_NO_CACHE_DIR=1

COPY pyproject.toml README.md ./
COPY src ./src
RUN python -m venv /opt/venv \
 && /opt/venv/bin/pip install --upgrade pip \
 && /opt/venv/bin/pip install .

# ---------------------------------------------------------------------------

FROM python:3.12-slim AS runtime

# Non-root. A gateway is an internet-facing process; if it is ever compromised,
# the blast radius should not include the container's filesystem.
RUN useradd --create-home --uid 10001 gateway
COPY --from=builder /opt/venv /opt/venv

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    GATEWAY_PROVIDER=mock

WORKDIR /app
COPY --chown=gateway:gateway config.example.yaml /app/config.yaml
USER gateway
EXPOSE 8080

# Liveness only -- readiness is /readyz, which the orchestrator should poll
# separately so an open circuit pulls the replica from the load balancer
# without restarting a process that is working perfectly well.
HEALTHCHECK --interval=15s --timeout=3s --start-period=5s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8080/healthz', timeout=2).status==200 else 1)"

CMD ["uvicorn", "gateway.main:app", "--host", "0.0.0.0", "--port", "8080"]
