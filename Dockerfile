# Combined image for Render (or any single-container host): runs the
# Mosquitto MQTT broker, the official SAIC/MG iSMART MQTT gateway, and the
# Yemot Hamashiach bridge together in one container, managed by supervisord.
#
# The "builder" stage clones and builds the upstream gateway project
# (github.com/SAIC-iSmart-API/saic-python-mqtt-gateway) with Poetry, the same
# tool it uses itself, so we inherit its real dependency versions instead of
# guessing them.

ARG PYTHON_VERSION=3.14
ARG GATEWAY_REPO=https://github.com/SAIC-iSmart-API/saic-python-mqtt-gateway.git
# Pin to a specific commit/tag here if a future upstream change ever breaks
# the build - "main" always tracks the latest.
ARG GATEWAY_REF=main

FROM python:${PYTHON_VERSION}-slim AS gw-builder
ARG GATEWAY_REPO
ARG GATEWAY_REF
WORKDIR /usr/src/app
RUN apt-get update \
    && apt-get install -y --no-install-recommends git build-essential \
    && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir poetry
RUN git clone --depth 1 --branch ${GATEWAY_REF} ${GATEWAY_REPO} . \
    || (git clone ${GATEWAY_REPO} . && git checkout ${GATEWAY_REF})
RUN poetry config virtualenvs.in-project true \
    && poetry install --no-root --without dev

FROM python:${PYTHON_VERSION}-slim AS runtime

# System packages: Mosquitto (MQTT broker) + supervisord (process manager)
RUN apt-get update \
    && apt-get install -y --no-install-recommends mosquitto supervisor \
    && rm -rf /var/lib/apt/lists/*

# ---- MG iSMART gateway (from the builder stage above) ----
ENV GATEWAY_VENV=/usr/src/gateway/.venv
COPY --from=gw-builder /usr/src/app/.venv ${GATEWAY_VENV}
COPY --from=gw-builder /usr/src/app/src/ /usr/src/gateway/
COPY --from=gw-builder /usr/src/app/examples/ /usr/src/gateway/
# Our own bridge needs to talk MQTT too - add it to the same venv so both
# processes share one consistent, known-good Python environment.
RUN ${GATEWAY_VENV}/bin/pip install --no-cache-dir flask paho-mqtt

# ---- Yemot bridge ----
COPY bridge/yemot_bridge.py /usr/src/bridge/yemot_bridge.py

# ---- Mosquitto config ----
COPY mosquitto/config/mosquitto.conf /etc/mosquitto/mosquitto.conf

# ---- supervisord: runs all three processes in this one container ----
COPY supervisord.conf /etc/supervisor/conf.d/supervisord.conf

EXPOSE 10000
CMD ["/usr/bin/supervisord", "-n", "-c", "/etc/supervisor/conf.d/supervisord.conf"]
