#!/usr/bin/with-contenv bashio
set -e

export DISCOVERY_PREFIX=$(bashio::config 'discovery_prefix')
export EXPIRE_AFTER=$(bashio::config 'expire_after')

MQTT_ENABLED=$(bashio::config 'mqtt_enabled')
export MQTT_ENABLED

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

# --- JeeLink port discovery -------------------------------------------------
# Same pattern as the autoterm-5d-control add-on: verify the configured port
# first, and only scan every /dev/ttyUSB*//dev/ttyACM* candidate if that
# fails. Serial device nodes can briefly report busy right after the
# container boots, so retry a few times with a settle delay before giving up.
CONFIGURED_PORT=$(bashio::config 'com_port' '')
COM_PORT="${CONFIGURED_PORT}"

if bashio::config.true 'autodiscover_ports'; then
    ATTEMPTS=4
    SETTLE=10
    RETRY_DELAY=20

    bashio::log.info "discovery: waiting ${SETTLE}s for serial devices to settle..."
    sleep "${SETTLE}"

    for i in $(seq 1 "${ATTEMPTS}"); do
        bashio::log.info "discovery: looking for a JeeLink (attempt ${i}/${ATTEMPTS})..."
        if DISCOVERY_OUT=$(python3 /app/discover.py --discover-port "${CONFIGURED_PORT}"); then
            FOUND=$(echo "${DISCOVERY_OUT}" | grep '^COM_PORT=' | cut -d= -f2-)
            if [ -n "${FOUND}" ]; then
                COM_PORT="${FOUND}"
                bashio::log.info "discovery: using ${COM_PORT}"
                if [ "${FOUND}" != "${CONFIGURED_PORT}" ]; then
                    bashio::app.option 'com_port' "${COM_PORT}" \
                        || bashio::log.warning "discovery: could not save discovered com_port to app options"
                fi
                break
            fi
        fi
        bashio::log.warning "discovery: no JeeLink found on attempt ${i}/${ATTEMPTS}"
        if [ "${i}" -lt "${ATTEMPTS}" ]; then
            sleep "${RETRY_DELAY}"
        fi
    done
else
    bashio::log.info "discovery: autodiscover_ports is false, using the configured com_port as-is"
fi

if [ -z "${COM_PORT}" ]; then
    bashio::log.warning "discovery: no JeeLink port known - restart the add-on once one is connected."
fi
export COM_PORT

bashio::log.info "Starting LaCrosse MQTT bridge..."
exec python3 /app/discover.py
