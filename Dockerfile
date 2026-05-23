# APsystems Open Bridge — single image, two roles selected by `command:` in
# docker-compose (daemon / webui). The image stays small (~85 MB) because
# the runtime needs almost nothing — stdlib http.server for the UI,
# pyserial for the dongle, paho-mqtt for optional HA integration.
#
# Multi-arch: builds cleanly on arm64 (Raspberry Pi) and amd64. No native
# compilation needed at install time — wheels for all deps exist for both.

FROM python:3.12-slim-bookworm

LABEL org.opencontainers.image.title="APsystems Open Bridge" \
      org.opencontainers.image.description="Self-host APsystems YC600/QS1/DS3 inverter telemetry via a Sonoff CC2652P dongle." \
      org.opencontainers.image.licenses="MIT"

# Python hygiene: no .pyc clutter on the writable layer, stdout flushes
# straight through to `docker logs` instead of buffering for ages.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    APS_WEB_PORT=8088

# tini: PID 1 signal handling, so `docker stop` cleanly delivers SIGTERM to
# the Python process (no 10 s wait while Docker forces a SIGKILL).
RUN apt-get update \
 && apt-get install -y --no-install-recommends tini \
 && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir pyserial==3.5 paho-mqtt==2.1.0

# Code lives at /opt/aps/host/ — same path as the bare-metal install, so
# every hardcoded `/opt/aps/...` reference in the source works unchanged.
# Persistent state (/opt/aps/etc + /opt/aps/logs) is bind-mounted from the
# host via docker-compose; nothing stateful lives inside the image.
WORKDIR /opt/aps/host
COPY host/aps_bridge            ./aps_bridge
COPY host/aps_unified_daemon.py ./aps_unified_daemon.py
COPY host/webui                 ./webui

# Make sure the mount points exist even before the compose volumes mount,
# so the daemon doesn't refuse to write to a missing parent dir.
RUN mkdir -p /opt/aps/etc /opt/aps/logs

EXPOSE 8088

ENTRYPOINT ["/usr/bin/tini", "--"]
# Default to the daemon; the webui service overrides this in compose.
CMD ["python3", "/opt/aps/host/aps_unified_daemon.py"]
