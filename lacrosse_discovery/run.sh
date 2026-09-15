#!/usr/bin/with-contenv bashio
set -e

SCAN_DURATION=$(bashio::config 'scan_duration')
export SCAN_DURATION

RESULT_DIR="/config"
mkdir -p "${RESULT_DIR}"
export RESULT_DIR

bashio::log.info "Default scan duration: ${SCAN_DURATION}s"
bashio::log.info "Generated YAML will also be saved under ${RESULT_DIR}/lacrosse.yaml"
bashio::log.info "Starting LaCrosse Discovery web UI..."

exec python3 /app/discover.py
