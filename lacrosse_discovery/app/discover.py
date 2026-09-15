"""
LaCrosse MQTT Bridge
---------------------
Headless service: finds a JeeLink running the LaCrosseITPlusReader sketch,
listens for LaCrosse/Technoline sensor packets indefinitely, and publishes
them to Home Assistant via MQTT Discovery so they show up as normal,
fully UI-manageable entities.

Port resolution follows the same verify-then-scan pattern used by the
autoterm-5d-control add-on: `--discover-port <configured>` checks the
configured port first (fast path) and only scans every /dev/ttyUSB*
/dev/ttyACM* candidate if that fails, then resolves the winner to its
stable /dev/serial/by-id/... path and prints `COM_PORT=<path>` on stdout
(logging goes to stderr) for run.sh to persist via `bashio::app.option`.

Protocol notes (from the `pylacrosse` library that HA's own `lacrosse`
integration uses, https://github.com/hthiery/python-lacrosse):

  - The dongle talks at 57600 baud.
  - Sending the single byte 'v' makes the sketch print a banner like
    "[LaCrosseITPlusReader.10.1s (RFM12B f:0 r:17241)]". Most builds also
    print this once on their own right after boot / USB reset.
  - Sensor readings arrive as lines shaped like:
        OK 9 248 1 4 150 106
    which decode as:
        _, sensor_id, type_and_newbattery, temp_hi, temp_lo, hum_and_lowbatt
        sensor_id     = fields[1]
        sensor_type   = fields[2] & 0x7f
        new_battery   = bool(fields[2] & 0x80)
        temperature_c = (fields[3] * 256 + fields[4] - 1000) / 10
        humidity      = fields[5] & 0x7f   (>99 usually means "no humidity")
        low_battery   = bool(fields[5] & 0x80)
"""

from __future__ import annotations

import argparse
import glob
import json
import logging
import os
import re
import sys
import threading
import time
from datetime import datetime, timezone

import paho.mqtt.client as mqtt
import serial

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", stream=sys.stderr)
log = logging.getLogger("lacrosse_bridge")

BAUD = 57600
PROBE_TIMEOUT = 3.0
BOOT_SETTLE = 2.0  # JeeLinks reset when the serial port is opened; give the sketch time to boot
RECONNECT_DELAY = 15.0  # if the bridged port drops, how long to wait before trying to reopen it

INFO_RE = re.compile(r"^\[(?P<info>.+)\]$")
READING_RE = re.compile(r"^OK (\d+) (\d+) (\d+) (\d+) (\d+) (\d+)$")

MQTT_ENABLED = os.environ.get("MQTT_ENABLED", "false").strip().lower() == "true"
MQTT_HOST = os.environ.get("MQTT_HOST", "").strip()
MQTT_PORT = int(os.environ.get("MQTT_PORT") or 1883)
MQTT_USERNAME = os.environ.get("MQTT_USERNAME", "").strip()
MQTT_PASSWORD = os.environ.get("MQTT_PASSWORD", "")
MQTT_SSL = os.environ.get("MQTT_SSL", "false").strip().lower() == "true"
DISCOVERY_PREFIX = os.environ.get("DISCOVERY_PREFIX", "homeassistant").strip() or "homeassistant"
EXPIRE_AFTER = int(os.environ.get("EXPIRE_AFTER") or 1800)
COM_PORT = os.environ.get("COM_PORT", "").strip()

SENSORS_LOCK = threading.Lock()
SENSORS: dict[int, dict] = {}  # sensor_id -> sensor record, see record_reading()

_mqtt_client: "mqtt.Client | None" = None
_mqtt_client_lock = threading.Lock()
_mqtt_discovery_published: set[tuple[str, str]] = set()
_mqtt_discovery_lock = threading.Lock()


def mqtt_available() -> bool:
    return MQTT_ENABLED and bool(MQTT_HOST)


def get_mqtt_client() -> "mqtt.Client | None":
    """Lazily create (and keep reusing) a connected MQTT client, if configured."""
    global _mqtt_client
    if not mqtt_available():
        return None
    with _mqtt_client_lock:
        if _mqtt_client is not None:
            return _mqtt_client

        def on_connect(client, userdata, flags, rc):
            if rc == 0:
                log.info("Connected to MQTT broker %s:%s", MQTT_HOST, MQTT_PORT)
            else:
                log.warning("MQTT connect failed: %s", mqtt.connack_string(rc))

        def on_disconnect(client, userdata, rc):
            log.warning("Disconnected from MQTT broker (rc=%s)", rc)

        client = mqtt.Client(client_id="lacrosse_bridge", protocol=mqtt.MQTTv311)
        if MQTT_USERNAME:
            client.username_pw_set(MQTT_USERNAME, MQTT_PASSWORD or None)
        if MQTT_SSL:
            client.tls_set()
        client.on_connect = on_connect
        client.on_disconnect = on_disconnect
        client.reconnect_delay_set(min_delay=1, max_delay=30)
        try:
            client.connect(MQTT_HOST, MQTT_PORT, keepalive=60)
        except (OSError, ConnectionError) as exc:
            log.warning("Could not connect to MQTT broker %s:%s - %s", MQTT_HOST, MQTT_PORT, exc)
            return None
        client.loop_start()
        _mqtt_client = client
        return client


# ---------------------------------------------------------------------------
# Port discovery - verify the configured port first, only scan everything
# if that fails (same pattern as autoterm-5d-control's port discovery).
# ---------------------------------------------------------------------------

def list_candidate_ports() -> list[str]:
    return sorted(set(glob.glob("/dev/ttyUSB*") + glob.glob("/dev/ttyACM*")))


def resolve_by_id(devpath: str) -> str:
    """Resolve devpath to its stable /dev/serial/by-id/... symlink, if one
    exists. /dev/ttyUSBx names can be reassigned across reboots/replugs;
    the by-id path survives that. Falls back to the raw path (with a
    warning) for cheap clones that don't expose a USB serial number."""
    try:
        target = os.path.realpath(devpath)
        for candidate in glob.glob("/dev/serial/by-id/*"):
            if os.path.realpath(candidate) == target:
                return candidate
    except OSError:
        pass
    log.warning("discovery: no /dev/serial/by-id link found for %s, using the raw path", devpath)
    return devpath


def probe_port(device: str) -> dict:
    """Open a port, ask for its version banner, and check whether it looks
    like a JeeLink running the LaCrosseITPlusReader sketch."""
    result = {"status": "not_jeelink", "sketch_info": None, "error": None}
    ser = None
    try:
        ser = serial.Serial(device, BAUD, timeout=0.5)
    except (serial.SerialException, OSError) as exc:
        result["status"] = "busy"
        result["error"] = str(exc)
        return result

    try:
        time.sleep(BOOT_SETTLE)  # let the sketch finish booting after the DTR reset
        ser.reset_input_buffer()
        ser.write(b"v")
        ser.flush()

        deadline = time.time() + PROBE_TIMEOUT
        while time.time() < deadline:
            raw = ser.readline()
            if not raw:
                continue
            line = raw.decode("utf-8", errors="ignore").strip()
            if not line:
                continue
            match = INFO_RE.match(line)
            if match:
                info = match.group("info")
                result["sketch_info"] = info
                result["status"] = "jeelink" if "lacrosse" in info.lower() else "not_jeelink"
                return result
    except (serial.SerialException, OSError) as exc:
        result["status"] = "error"
        result["error"] = str(exc)
    finally:
        try:
            ser.close()
        except Exception:
            pass

    return result


def verify_port(device: str) -> bool:
    """Fast check: does `device` currently answer as a JeeLink?"""
    if not device:
        return False
    log.info("discovery: verifying configured port %s ...", device)
    result = probe_port(device)
    log.info("discovery: %s -> %s (%s)", device, result["status"], result["sketch_info"] or result["error"] or "")
    return result["status"] == "jeelink"


def discover_port(configured: str) -> str | None:
    """Verify the configured port first; only scan every candidate if that fails."""
    if configured and verify_port(configured):
        return resolve_by_id(configured)

    if configured:
        log.warning("discovery: configured port %s did not respond as a JeeLink, scanning all ports", configured)
    else:
        log.info("discovery: no port configured yet, scanning all ports")

    for device in list_candidate_ports():
        if device == configured:
            continue  # already tried above
        log.info("discovery: probing %s ...", device)
        result = probe_port(device)
        log.info("discovery: %s -> %s (%s)", device, result["status"], result["sketch_info"] or result["error"] or "")
        if result["status"] == "jeelink":
            return resolve_by_id(device)

    return None


# ---------------------------------------------------------------------------
# Sensor packet decoding and MQTT publishing
# ---------------------------------------------------------------------------

def decode_reading(line: str) -> dict | None:
    match = READING_RE.match(line)
    if not match:
        return None
    try:
        fields = [int(g) for g in match.groups()]
    except ValueError:
        return None
    sensor_id = fields[1]
    sensor_type = fields[2] & 0x7F
    new_battery = bool(fields[2] & 0x80)
    temperature = (fields[3] * 256 + fields[4] - 1000) / 10.0
    humidity_raw = fields[5] & 0x7F
    low_battery = bool(fields[5] & 0x80)
    return {
        "sensor_id": sensor_id,
        "sensor_type": sensor_type,
        "new_battery": new_battery,
        "temperature": temperature,
        "humidity_raw": humidity_raw,
        "humidity_valid": 1 <= humidity_raw <= 99,
        "low_battery": low_battery,
    }


def record_reading(reading: dict) -> dict:
    """Update SENSORS with a new reading and return a snapshot of that sensor's record."""
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    sid = reading["sensor_id"]
    with SENSORS_LOCK:
        entry = SENSORS.setdefault(
            sid,
            {
                "sensor_id": sid,
                "sensor_type": reading["sensor_type"],
                "count": 0,
                "first_seen": now,
                "last_seen": now,
                "last_temperature": None,
                "last_humidity": None,
                "humidity_ever_valid": False,
                "low_battery": False,
                "new_battery": False,
            },
        )
        entry["count"] += 1
        entry["last_seen"] = now
        entry["sensor_type"] = reading["sensor_type"]
        entry["last_temperature"] = round(reading["temperature"], 1)
        entry["low_battery"] = reading["low_battery"]
        entry["new_battery"] = reading["new_battery"]
        if reading["humidity_valid"]:
            entry["last_humidity"] = reading["humidity_raw"]
            entry["humidity_ever_valid"] = True
        return dict(entry)


def mqtt_device_id(device: str, sensor_id: int) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "_", os.path.basename(device)).strip("_").lower() or "port"
    return f"lacrosse_{slug}_{sensor_id}"


def mqtt_state_topic(device: str, sensor_id: int) -> str:
    return f"lacrosse_bridge/{mqtt_device_id(device, sensor_id)}/state"


def publish_discovery_if_needed(client: "mqtt.Client", device: str, sensor_id: int, sensor: dict) -> None:
    device_id = mqtt_device_id(device, sensor_id)
    state_topic = mqtt_state_topic(device, sensor_id)
    ha_device = {
        "identifiers": [device_id],
        "name": f"LaCrosse sensor {sensor_id}",
        "manufacturer": "LaCrosse/Technoline",
        "model": f"JeeLink sensor type {sensor['sensor_type']}",
    }

    keys = ["temperature", "battery"]
    if sensor["humidity_ever_valid"]:
        keys.append("humidity")

    with _mqtt_discovery_lock:
        for key in keys:
            marker = (device_id, key)
            if marker in _mqtt_discovery_published:
                continue
            payload = {
                "name": key.capitalize(),
                "unique_id": f"{device_id}_{key}",
                "state_topic": state_topic,
                "value_template": f"{{{{ value_json.{key} }}}}",
                "json_attributes_topic": state_topic,
                "device": ha_device,
                "expire_after": EXPIRE_AFTER,
            }
            if key == "temperature":
                payload.update(device_class="temperature", unit_of_measurement="°C", state_class="measurement")
            elif key == "humidity":
                payload.update(device_class="humidity", unit_of_measurement="%", state_class="measurement")
            elif key == "battery":
                payload["icon"] = "mdi:battery"

            config_topic = f"{DISCOVERY_PREFIX}/sensor/{device_id}/{key}/config"
            client.publish(config_topic, json.dumps(payload), qos=1, retain=True)
            _mqtt_discovery_published.add(marker)


def publish_state(client: "mqtt.Client", device: str, sensor_id: int, sensor: dict) -> None:
    payload = {
        "temperature": sensor["last_temperature"],
        "battery": "low" if sensor["low_battery"] else "ok",
        "low_battery": sensor["low_battery"],
        "new_battery": sensor["new_battery"],
        "sensor_type": sensor["sensor_type"],
        "last_seen": sensor["last_seen"],
    }
    if sensor["humidity_ever_valid"]:
        payload["humidity"] = sensor["last_humidity"]
    client.publish(mqtt_state_topic(device, sensor_id), json.dumps(payload), qos=0, retain=True)


def mqtt_publish_reading(device: str, sensor_id: int, sensor: dict) -> None:
    client = get_mqtt_client()
    if client is None:
        return
    try:
        publish_discovery_if_needed(client, device, sensor_id, sensor)
        publish_state(client, device, sensor_id, sensor)
    except Exception:
        log.exception("Failed to publish MQTT update for %s sensor %s", device, sensor_id)


# ---------------------------------------------------------------------------
# Listening loop
# ---------------------------------------------------------------------------

def listen_on_port_once(device: str) -> None:
    """Listen for sensor packets on `device` until the connection drops."""
    ser = serial.Serial(device, BAUD, timeout=0.5)
    try:
        time.sleep(BOOT_SETTLE)
        ser.reset_input_buffer()
        while True:
            raw = ser.readline()
            if not raw:
                continue
            line = raw.decode("utf-8", errors="ignore").strip()
            if not line.startswith("OK "):
                continue
            reading = decode_reading(line)
            if reading is None:
                continue
            sensor_snapshot = record_reading(reading)
            if mqtt_available():
                mqtt_publish_reading(device, reading["sensor_id"], sensor_snapshot)
    finally:
        try:
            ser.close()
        except Exception:
            pass


def run_bridge() -> None:
    if not mqtt_available():
        log.error("MQTT is not enabled/configured - nothing to publish to. "
                   "Turn on mqtt_enabled and make sure a broker is reachable, then restart.")
        return

    if not COM_PORT:
        log.error("No JeeLink port known. Connect one and restart the add-on "
                   "(autodiscover_ports will find and save it on the next boot).")
        return

    log.info("Bridging %s to MQTT indefinitely (discovery prefix: %s)", COM_PORT, DISCOVERY_PREFIX)
    while True:
        try:
            listen_on_port_once(COM_PORT)
        except (serial.SerialException, OSError) as exc:
            log.warning("Lost connection to %s (%s) - retrying in %.0fs", COM_PORT, exc, RECONNECT_DELAY)
            time.sleep(RECONNECT_DELAY)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--discover-port",
        metavar="CONFIGURED_PORT",
        help="Resolve the JeeLink's serial port (verifying CONFIGURED_PORT first, "
             "then scanning if needed), print COM_PORT=<path> on success, and exit.",
    )
    args = parser.parse_args()

    if args.discover_port is not None:
        found = discover_port(args.discover_port)
        if found:
            print(f"COM_PORT={found}")
            return 0
        return 1

    run_bridge()
    return 0


if __name__ == "__main__":
    sys.exit(main())
