#!/usr/bin/with-contenv bashio
set -e

SCAN_DURATION=$(bashio::config 'scan_duration')
export SCAN_DURATION

RESULT_DIR="/config"
mkdir -p "${RESULT_DIR}"
export RESULT_DIR

MQTT_ENABLED=$(bashio::config 'mqtt_enabled')
export MQTT_ENABLED
export DISCOVERY_PREFIX=$(bashio::config 'discovery_prefix')
export EXPIRE_AFTER=$(bashio::config 'expire_after')

MQTT_HOST=""
MQTT_PORT="1883"
MQTT_USERNAME=""
MQTT_PASSWORD=""
MQTT_SSL="false"

if bashio::config.true 'mqtt_enabled'; then
    if bashio::services.available 'mqtt'; then
        bashio::log.info "Auto-discovered MQTT broker via Supervisor services."
        MQTT_HOST=$(bashio::services 'mqtt' 'host')
        MQTT_PORT=$(bashio::services 'mqtt' 'port')
        MQTT_USERNAME=$(bashio::services 'mqtt' 'username')
        MQTT_PASSWORD=$(bashio::services 'mqtt' 'password')
        MQTT_SSL=$(bashio::services 'mqtt' 'ssl')
    else
        bashio::log.warning "mqtt_enabled is true but no MQTT service was found (install/start the Mosquitto broker add-on, or set mqtt_host manually)."
    fi

    # Manually configured options always win over auto-discovery.
    if bashio::config.has_value 'mqtt_host'; then
        MQTT_HOST=$(bashio::config 'mqtt_host')
    fi
    if bashio::config.has_value 'mqtt_port'; then
        MQTT_PORT=$(bashio::config 'mqtt_port')
    fi
    if bashio::config.has_value 'mqtt_username'; then
        MQTT_USERNAME=$(bashio::config 'mqtt_username')
    fi
    if bashio::config.has_value 'mqtt_password'; then
        MQTT_PASSWORD=$(bashio::config 'mqtt_password')
    fi

    if [ -n "${MQTT_HOST}" ]; then
        bashio::log.info "MQTT bridge enabled: ${MQTT_HOST}:${MQTT_PORT} (discovery prefix: ${DISCOVERY_PREFIX})"
    else
        bashio::log.warning "MQTT bridge enabled but no broker host is known - it will stay idle until one is available."
    fi
fi

export MQTT_HOST MQTT_PORT MQTT_USERNAME MQTT_PASSWORD MQTT_SSL

bashio::log.info "Default scan duration: ${SCAN_DURATION}s"
bashio::log.info "Generated YAML will also be saved under ${RESULT_DIR}/lacrosse.yaml"
bashio::log.info "Starting LaCrosse Discovery web UI..."

exec python3 /app/discover.py
