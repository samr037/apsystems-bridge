ARG BUILD_FROM
FROM ${BUILD_FROM}

# BUILD_FROM is ghcr.io/samr037/apsystems-bridge — it already carries the
# daemon, web UI and Python deps. This layer only adds the Home Assistant
# add-on wiring: option parsing + MQTT service auto-config (run.sh).
# tini is inherited from the base image's ENTRYPOINT for clean signals.
COPY run.sh /run.sh
RUN chmod +x /run.sh

CMD ["/run.sh"]
