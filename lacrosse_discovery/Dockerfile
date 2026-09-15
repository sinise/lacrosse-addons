# https://developers.home-assistant.io/docs/add-ons/configuration#add-on-dockerfile
ARG BUILD_FROM=ghcr.io/home-assistant/base:3.23
FROM ${BUILD_FROM}

RUN apk add --no-cache python3 py3-pip

WORKDIR /app
COPY requirements.txt /app/requirements.txt
RUN pip3 install --no-cache-dir --break-system-packages -r /app/requirements.txt

COPY app/ /app/
COPY run.sh /run.sh
RUN chmod a+x /run.sh

LABEL \
    org.opencontainers.image.title="LaCrosse MQTT Bridge" \
    org.opencontainers.image.description="Finds a JeeLink and bridges LaCrosse sensor readings to Home Assistant via MQTT" \
    org.opencontainers.image.licenses="MIT"

CMD [ "/run.sh" ]
