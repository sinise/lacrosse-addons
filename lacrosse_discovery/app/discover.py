"""
LaCrosse Discovery
-------------------
Scans every USB-serial port on the host for a JeeLink running the
LaCrosseITPlusReader sketch, listens for incoming sensor packets, and
renders a ready-to-paste `sensor: - platform: lacrosse` YAML block for
Home Assistant's built-in `lacrosse` integration:
https://www.home-assistant.io/integrations/lacrosse/

Protocol notes (taken from the `pylacrosse` library that the HA
integration itself uses, https://github.com/hthiery/python-lacrosse):

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

import glob
import json
import logging
import os
import re
import threading
import time
from datetime import datetime, timezone

import paho.mqtt.client as mqtt
import serial
from flask import Flask, Response, jsonify, request
from serial.tools import list_ports

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("lacrosse_discovery")

BAUD = 57600
PROBE_TIMEOUT = 3.0
BOOT_SETTLE = 2.0  # JeeLinks reset when the serial port is opened; give the sketch time to boot

INFO_RE = re.compile(r"^\[(?P<info>.+)\]$")
READING_RE = re.compile(r"^OK (\d+) (\d+) (\d+) (\d+) (\d+) (\d+)$")

RESULT_DIR = os.environ.get("RESULT_DIR", ".")
RESULT_FILE = os.path.join(RESULT_DIR, "lacrosse.yaml")

MQTT_ENABLED = os.environ.get("MQTT_ENABLED", "false").strip().lower() == "true"
MQTT_HOST = os.environ.get("MQTT_HOST", "").strip()
MQTT_PORT = int(os.environ.get("MQTT_PORT") or 1883)
MQTT_USERNAME = os.environ.get("MQTT_USERNAME", "").strip()
MQTT_PASSWORD = os.environ.get("MQTT_PASSWORD", "")
MQTT_SSL = os.environ.get("MQTT_SSL", "false").strip().lower() == "true"
DISCOVERY_PREFIX = os.environ.get("DISCOVERY_PREFIX", "homeassistant").strip() or "homeassistant"
EXPIRE_AFTER = int(os.environ.get("EXPIRE_AFTER") or 1800)

app = Flask(__name__)

STATE_LOCK = threading.RLock()
SCAN_STOP_EVENT = threading.Event()   # stops a manual, finite-duration scan only
BRIDGED_DEVICES: set[str] = set()      # devices already owned by the persistent MQTT bridge
STATE = {
    "scanning": False,
    "phase": None,          # "probing" | "listening" | None
    "message": None,
    "duration": None,
    "started_at": None,
    "finished_at": None,
    "ports": {},             # device -> port record (see new_port_record)
}

_mqtt_client: "mqtt.Client | None" = None
_mqtt_connected = False
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
            global _mqtt_connected
            _mqtt_connected = rc == 0
            if rc == 0:
                log.info("Connected to MQTT broker %s:%s", MQTT_HOST, MQTT_PORT)
            else:
                log.warning("MQTT connect failed: %s", mqtt.connack_string(rc))

        def on_disconnect(client, userdata, rc):
            global _mqtt_connected
            _mqtt_connected = False
            log.warning("Disconnected from MQTT broker (rc=%s)", rc)

        client = mqtt.Client(client_id="lacrosse_discovery", protocol=mqtt.MQTTv311)
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


def new_port_record(port) -> dict:
    return {
        "device": port.device,
        "description": port.description or "",
        "hwid": port.hwid or "",
        "stable_path": find_stable_path(port.device),
        "is_usb": is_usb_port(port),
        "probe_status": "pending",   # pending|probing|jeelink|not_jeelink|busy|error
        "sketch_info": None,
        "error": None,
        "sensors": {},               # sensor_id -> sensor record
        "packet_count": 0,
    }


def is_usb_port(port) -> bool:
    hwid = port.hwid or ""
    device = port.device or ""
    return "USB VID:PID" in hwid or "ACM" in device or "USB" in device


def find_stable_path(device: str) -> str | None:
    """Resolve a /dev/serial/by-id/* symlink pointing at this device, if any.

    /dev/ttyUSB0 style names can be reassigned across reboots or replugs;
    the by-id path is stable and is what we prefer to put in the generated
    config.
    """
    try:
        target = os.path.realpath(device)
        for candidate in glob.glob("/dev/serial/by-id/*"):
            if os.path.realpath(candidate) == target:
                return candidate
    except OSError:
        pass
    return None


def slugify(text: str) -> str:
    text = re.sub(r"[^a-zA-Z0-9]+", "_", text).strip("_").lower()
    return text or "port"


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


def record_reading(port_record: dict, reading: dict) -> None:
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    sensors = port_record["sensors"]
    sid = reading["sensor_id"]
    entry = sensors.setdefault(
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
    port_record["packet_count"] += 1


def mqtt_device_id(device: str, sensor_id: int) -> str:
    return f"lacrosse_{slugify(os.path.basename(device))}_{sensor_id}"


def mqtt_state_topic(device: str, sensor_id: int) -> str:
    return f"lacrosse_discovery/{mqtt_device_id(device, sensor_id)}/state"


def publish_discovery_if_needed(client: "mqtt.Client", device: str, sensor_id: int, sensor: dict) -> None:
    device_id = mqtt_device_id(device, sensor_id)
    state_topic = mqtt_state_topic(device, sensor_id)
    ha_device = {
        "identifiers": [device_id],
        "name": f"LaCrosse sensor {sensor_id}",
        "manufacturer": "LaCrosse/Technoline",
        "model": f"JeeLink sensor type {sensor['sensor_type']}",
        "via_device": f"lacrosse_discovery_{slugify(os.path.basename(device))}",
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


def listen_on_port(device: str, duration: float | None, stop_event: threading.Event) -> None:
    """Listen for sensor packets on `device`.

    duration=None means listen forever (used by the persistent MQTT bridge);
    a finite duration is used by manual, ad-hoc scans from the web UI.
    """
    try:
        ser = serial.Serial(device, BAUD, timeout=0.5)
    except (serial.SerialException, OSError) as exc:
        with STATE_LOCK:
            STATE["ports"][device]["error"] = f"Failed to reopen for listening: {exc}"
        return

    try:
        time.sleep(BOOT_SETTLE)
        ser.reset_input_buffer()
        deadline = None if duration is None else time.time() + duration
        while (deadline is None or time.time() < deadline) and not stop_event.is_set():
            raw = ser.readline()
            if not raw:
                continue
            line = raw.decode("utf-8", errors="ignore").strip()
            if not line.startswith("OK "):
                continue
            reading = decode_reading(line)
            if reading is None:
                continue
            with STATE_LOCK:
                record_reading(STATE["ports"][device], reading)
                sensor_snapshot = dict(STATE["ports"][device]["sensors"][reading["sensor_id"]])
            if mqtt_available():
                mqtt_publish_reading(device, reading["sensor_id"], sensor_snapshot)
    except (serial.SerialException, OSError) as exc:
        with STATE_LOCK:
            STATE["ports"][device]["error"] = f"Lost connection while listening: {exc}"
    finally:
        try:
            ser.close()
        except Exception:
            pass


def probe_all_ports(skip_devices: set[str]) -> None:
    """Probe every USB-serial port not in `skip_devices`, updating STATE as it goes."""
    for port in list_ports.comports():
        if port.device in skip_devices:
            continue
        record = new_port_record(port)
        with STATE_LOCK:
            STATE["ports"][port.device] = record
        if not record["is_usb"]:
            with STATE_LOCK:
                STATE["ports"][port.device]["probe_status"] = "not_jeelink"
                STATE["ports"][port.device]["sketch_info"] = "skipped (not a USB serial device)"
            continue
        with STATE_LOCK:
            STATE["ports"][port.device]["probe_status"] = "probing"
        log.info("Probing %s ...", port.device)
        result = probe_port(port.device)
        with STATE_LOCK:
            rec = STATE["ports"][port.device]
            rec["probe_status"] = result["status"]
            rec["sketch_info"] = result["sketch_info"]
            rec["error"] = result["error"]
        log.info(
            "Probe result for %s: %s (%s)",
            port.device,
            result["status"],
            result["sketch_info"] or result["error"] or "no info",
        )


def run_full_scan(duration: float) -> None:
    SCAN_STOP_EVENT.clear()
    with STATE_LOCK:
        STATE["scanning"] = True
        STATE["phase"] = "probing"
        STATE["message"] = None
        STATE["duration"] = duration
        STATE["started_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        STATE["finished_at"] = None
        # Ports already owned by the persistent MQTT bridge keep their live record;
        # everything else gets re-probed from scratch.
        STATE["ports"] = {d: r for d, r in STATE["ports"].items() if d in BRIDGED_DEVICES}

    probe_all_ports(skip_devices=BRIDGED_DEVICES)

    with STATE_LOCK:
        jeelink_devices = [
            d for d, r in STATE["ports"].items() if r["probe_status"] == "jeelink" and d not in BRIDGED_DEVICES
        ]
        any_jeelink = any(r["probe_status"] == "jeelink" for r in STATE["ports"].values())

    if not any_jeelink:
        with STATE_LOCK:
            STATE["scanning"] = False
            STATE["phase"] = None
            STATE["message"] = "No JeeLink running the LaCrosseITPlusReader sketch was found."
            STATE["finished_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        return

    if jeelink_devices:
        with STATE_LOCK:
            STATE["phase"] = "listening"
        threads = [
            threading.Thread(target=listen_on_port, args=(d, duration, SCAN_STOP_EVENT), daemon=True)
            for d in jeelink_devices
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

    with STATE_LOCK:
        STATE["scanning"] = False
        STATE["phase"] = None
        STATE["finished_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        combined = build_combined_yaml(STATE["ports"])

    try:
        os.makedirs(RESULT_DIR, exist_ok=True)
        with open(RESULT_FILE, "w") as f:
            f.write(combined)
        log.info("Wrote generated config to %s", RESULT_FILE)
    except OSError as exc:
        log.warning("Could not write %s: %s", RESULT_FILE, exc)


BRIDGE_STARTUP_SETTLE = 10.0  # let /dev nodes finish settling right after container boot
BRIDGE_PROBE_ATTEMPTS = 4
BRIDGE_RETRY_DELAY = 20.0


def run_mqtt_bridge() -> None:
    """Continuously listen on every JeeLink found at startup and publish
    readings to MQTT via HA MQTT Discovery, for the lifetime of the add-on."""
    if not mqtt_available():
        log.info("MQTT bridge not starting: mqtt_enabled is false or no broker host is configured.")
        return

    never_stop = threading.Event()  # the bridge only stops when the add-on process exits

    # Serial device nodes can briefly report busy/not-ready right after the
    # container boots (seen in practice: every port came back "busy" on the
    # very first probe pass, but succeeded moments later). Give it a moment,
    # and retry a few times before giving up.
    log.info("MQTT bridge: waiting %.0fs for serial devices to settle...", BRIDGE_STARTUP_SETTLE)
    time.sleep(BRIDGE_STARTUP_SETTLE)

    for attempt in range(1, BRIDGE_PROBE_ATTEMPTS + 1):
        log.info("MQTT bridge: probing serial ports for a JeeLink to bridge (attempt %d/%d)...",
                  attempt, BRIDGE_PROBE_ATTEMPTS)
        probe_all_ports(skip_devices=BRIDGED_DEVICES)

        with STATE_LOCK:
            newly_found = [
                d for d, r in STATE["ports"].items() if r["probe_status"] == "jeelink" and d not in BRIDGED_DEVICES
            ]
            BRIDGED_DEVICES.update(newly_found)

        for device in newly_found:
            log.info("MQTT bridge: listening on %s indefinitely", device)
            threading.Thread(target=listen_on_port, args=(device, None, never_stop), daemon=True).start()

        if BRIDGED_DEVICES:
            break
        if attempt < BRIDGE_PROBE_ATTEMPTS:
            log.warning("MQTT bridge: no JeeLink found on attempt %d/%d, retrying in %.0fs...",
                        attempt, BRIDGE_PROBE_ATTEMPTS, BRIDGE_RETRY_DELAY)
            time.sleep(BRIDGE_RETRY_DELAY)

    if not BRIDGED_DEVICES:
        log.warning(
            "MQTT bridge: no JeeLink found after %d attempts. Once one is available/free, restart the add-on to bridge it.",
            BRIDGE_PROBE_ATTEMPTS,
        )


def build_port_yaml(device: str, record: dict, multi_port: bool) -> str:
    if not record["sensors"]:
        return ""

    path = record["stable_path"] or device
    lines = []
    lines.append(f"# LaCrosse sensors seen on {device}" + (f" ({record['stable_path']})" if record["stable_path"] else ""))
    lines.append(f"# Generated by LaCrosse Discovery on {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    if not record["stable_path"]:
        lines.append("# NOTE: no stable /dev/serial/by-id path was found for this port - the")
        lines.append("#       /dev/ttyUSBx name below can change after a reboot or replug.")
    lines.append("# NOTE: sensor IDs can change when a sensor's battery is replaced - rerun")
    lines.append("#       the scan if an entity goes unavailable.")
    lines.append("sensor:")
    lines.append("  - platform: lacrosse")
    lines.append(f"    device: {path}")
    lines.append(f"    baud: {BAUD}")
    lines.append("    sensors:")

    port_key = slugify(os.path.basename(device)) if multi_port else None

    for sid in sorted(record["sensors"]):
        sensor = record["sensors"][sid]
        key_prefix = f"lacrosse_{port_key}_{sid}" if multi_port else f"lacrosse_{sid}"

        lines.append(f"      {key_prefix}_temperature:")
        lines.append(f"        id: {sid}")
        lines.append("        type: temperature")
        lines.append(f'        name: "LaCrosse {sid} Temperature"')

        if sensor["humidity_ever_valid"]:
            lines.append(f"      {key_prefix}_humidity:")
            lines.append(f"        id: {sid}")
            lines.append("        type: humidity")
            lines.append(f'        name: "LaCrosse {sid} Humidity"')
        else:
            lines.append(f"      # {key_prefix}_humidity omitted - sensor {sid} never reported a plausible humidity value")

        lines.append(f"      {key_prefix}_battery:")
        lines.append(f"        id: {sid}")
        lines.append("        type: battery")
        lines.append(f'        name: "LaCrosse {sid} Battery"')

    return "\n".join(lines) + "\n"


def build_combined_yaml(ports: dict) -> str:
    jeelink_ports = {d: r for d, r in ports.items() if r["probe_status"] == "jeelink" and r["sensors"]}
    if not jeelink_ports:
        return "# No sensors discovered yet.\n"
    multi_port = len(jeelink_ports) > 1
    blocks = [build_port_yaml(d, r, multi_port) for d, r in jeelink_ports.items()]
    return "\n".join(b for b in blocks if b)


@app.route("/")
def index():
    return Response(INDEX_HTML, mimetype="text/html")


@app.route("/api/ports")
def api_ports():
    ports = []
    for port in list_ports.comports():
        ports.append(
            {
                "device": port.device,
                "description": port.description or "",
                "hwid": port.hwid or "",
                "is_usb": is_usb_port(port),
            }
        )
    return jsonify({"ports": ports})


@app.route("/api/scan", methods=["POST"])
def api_scan():
    with STATE_LOCK:
        if STATE["scanning"]:
            return jsonify({"error": "A scan is already running."}), 409

    body = request.get_json(silent=True) or {}
    try:
        duration = float(body.get("duration", 60))
    except (TypeError, ValueError):
        duration = 60.0
    duration = max(5.0, min(duration, 600.0))

    thread = threading.Thread(target=run_full_scan, args=(duration,), daemon=True)
    thread.start()
    return jsonify({"status": "started", "duration": duration})


@app.route("/api/stop", methods=["POST"])
def api_stop():
    SCAN_STOP_EVENT.set()
    return jsonify({"status": "stopping"})


@app.route("/api/status")
def api_status():
    with STATE_LOCK:
        ports = STATE["ports"]
        multi_port = len([r for r in ports.values() if r["probe_status"] == "jeelink"]) > 1
        snapshot = {
            "scanning": STATE["scanning"],
            "phase": STATE["phase"],
            "message": STATE["message"],
            "duration": STATE["duration"],
            "started_at": STATE["started_at"],
            "finished_at": STATE["finished_at"],
            "ports": [
                {
                    **{k: v for k, v in rec.items() if k != "sensors"},
                    "sensors": [rec["sensors"][sid] for sid in sorted(rec["sensors"])],
                    "yaml": build_port_yaml(device, rec, multi_port),
                    "bridged": device in BRIDGED_DEVICES,
                }
                for device, rec in ports.items()
            ],
            "combined_yaml": build_combined_yaml(ports),
            "mqtt": {
                "enabled": MQTT_ENABLED,
                "configured": mqtt_available(),
                "connected": _mqtt_connected,
                "host": MQTT_HOST or None,
                "port": MQTT_PORT,
                "discovery_prefix": DISCOVERY_PREFIX,
                "bridged_ports": sorted(BRIDGED_DEVICES),
            },
        }
    return jsonify(snapshot)


INDEX_HTML = """<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>LaCrosse Discovery</title>
<style>
  body { font-family: -apple-system, Roboto, Helvetica, Arial, sans-serif; margin: 0; padding: 16px; background: #fafafa; color: #212121; }
  h1 { font-size: 1.3em; }
  h2 { font-size: 1.05em; margin-top: 1.6em; }
  .card { background: #fff; border: 1px solid #e0e0e0; border-radius: 8px; padding: 12px 16px; margin-bottom: 12px; }
  button { background: #03a9f4; color: #fff; border: none; border-radius: 4px; padding: 8px 14px; font-size: 0.95em; cursor: pointer; }
  button:disabled { background: #bdbdbd; cursor: default; }
  button.secondary { background: #757575; }
  input[type=number] { width: 70px; padding: 6px; border: 1px solid #ccc; border-radius: 4px; }
  table { border-collapse: collapse; width: 100%; margin-top: 8px; font-size: 0.9em; }
  th, td { text-align: left; padding: 4px 8px; border-bottom: 1px solid #eee; }
  .status-jeelink { color: #2e7d32; font-weight: bold; }
  .status-not_jeelink { color: #9e9e9e; }
  .status-busy, .status-error { color: #c62828; }
  .status-probing, .status-pending { color: #f57f17; }
  textarea { width: 100%; box-sizing: border-box; min-height: 160px; font-family: monospace; font-size: 0.85em; }
  .row { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
  .muted { color: #757575; font-size: 0.85em; }
</style>
</head>
<body>

<h1>LaCrosse Discovery</h1>
<p class="muted">Finds JeeLink USB dongles running the LaCrosseITPlusReader sketch, listens for
sensor packets, and generates a <code>sensor: - platform: lacrosse</code> block for
<code>configuration.yaml</code>.</p>

<div class="card">
  <div class="row">
    <label>Scan duration (seconds): <input type="number" id="duration" value="60" min="10" max="600"></label>
    <button id="scanBtn" onclick="startScan()">Start scan</button>
    <button id="stopBtn" class="secondary" onclick="stopScan()" disabled>Stop</button>
    <span id="statusLine" class="muted"></span>
  </div>
</div>

<h2>MQTT bridge</h2>
<div class="card" id="mqttCard"><span class="muted">Loading...</span></div>

<h2>Ports</h2>
<div class="card"><table id="portsTable">
  <thead><tr><th>Device</th><th>Description</th><th>Status</th><th>Sketch / info</th><th>Sensors seen</th><th>MQTT</th></tr></thead>
  <tbody></tbody>
</table></div>

<h2>Generated configuration</h2>
<div class="card">
  <p class="muted">Copy the block(s) below into <code>configuration.yaml</code> under the <code>sensor:</code> section
  (or into a file you <code>!include</code>). A copy is also saved to
  <code>/addon_configs/&lt;slug&gt;/lacrosse.yaml</code> on the host after each scan.</p>
  <div id="yamlBlocks"></div>
</div>

<script>
let polling = null;

function startScan() {
  const duration = parseInt(document.getElementById('duration').value, 10) || 60;
  fetch('api/scan', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({duration})
  }).then(r => r.json()).then(() => {
    document.getElementById('scanBtn').disabled = true;
    document.getElementById('stopBtn').disabled = false;
    if (!polling) polling = setInterval(refresh, 1500);
    refresh();
  });
}

function stopScan() {
  fetch('api/stop', {method: 'POST'});
}

function copyBlock(id) {
  const el = document.getElementById(id);
  el.select();
  navigator.clipboard && navigator.clipboard.writeText(el.value).catch(() => document.execCommand('copy'));
}

function refresh() {
  fetch('api/status').then(r => r.json()).then(data => {
    const scanBtn = document.getElementById('scanBtn');
    const stopBtn = document.getElementById('stopBtn');
    scanBtn.disabled = data.scanning;
    stopBtn.disabled = !data.scanning;

    let statusText = '';
    if (data.scanning) {
      statusText = data.phase === 'probing' ? 'Probing serial ports for a JeeLink...' : 'Listening for sensor packets...';
    } else if (data.message) {
      statusText = data.message;
    } else if (data.finished_at) {
      statusText = 'Scan finished at ' + data.finished_at;
    }
    document.getElementById('statusLine').textContent = statusText;
    if (!data.scanning && polling) { clearInterval(polling); polling = null; }

    const mqttCard = document.getElementById('mqttCard');
    const m = data.mqtt || {};
    if (!m.enabled) {
      mqttCard.innerHTML = '<span class="muted">Disabled. Turn on <code>mqtt_enabled</code> in this add-on\'s Configuration tab to bridge sensors as full, UI-managed entities via MQTT Discovery.</span>';
    } else if (!m.configured) {
      mqttCard.innerHTML = '<span class="status-busy">Enabled, but no broker host is known. Install/start the Mosquitto broker add-on, or set mqtt_host manually in Configuration.</span>';
    } else {
      const cls = m.connected ? 'status-jeelink' : 'status-busy';
      mqttCard.innerHTML = `<span class="${cls}">${m.connected ? 'Connected' : 'Not connected'}</span> to ${m.host}:${m.port}
        &middot; discovery prefix <code>${m.discovery_prefix}</code>
        &middot; bridging ${m.bridged_ports.length} port(s)`;
    }

    const tbody = document.querySelector('#portsTable tbody');
    tbody.innerHTML = '';
    (data.ports || []).forEach(p => {
      const tr = document.createElement('tr');
      tr.innerHTML = `<td>${p.device}${p.stable_path ? '<br><span class="muted">' + p.stable_path + '</span>' : ''}</td>
        <td>${p.description || ''}</td>
        <td class="status-${p.probe_status}">${p.probe_status}</td>
        <td>${p.sketch_info || p.error || ''}</td>
        <td>${p.sensors.length}</td>
        <td>${p.bridged ? '<span class="status-jeelink">bridged</span>' : ''}</td>`;
      tbody.appendChild(tr);
    });

    const yamlDiv = document.getElementById('yamlBlocks');
    yamlDiv.innerHTML = '';
    const jeelinkPorts = (data.ports || []).filter(p => p.probe_status === 'jeelink' && p.sensors.length);
    if (!jeelinkPorts.length) {
      yamlDiv.innerHTML = '<p class="muted">No sensors discovered yet.</p>';
      return;
    }
    jeelinkPorts.forEach((p, i) => {
      const id = 'yaml_' + i;
      const wrap = document.createElement('div');
      wrap.innerHTML = `<div class="row"><strong>${p.device}</strong>
        <button class="secondary" onclick="copyBlock('${id}')">Copy</button></div>
        <textarea id="${id}" readonly>${p.yaml}</textarea>`;
      yamlDiv.appendChild(wrap);
    });
  });
}

refresh();
</script>
</body>
</html>
"""


if __name__ == "__main__":
    if mqtt_available():
        threading.Thread(target=run_mqtt_bridge, daemon=True).start()
    app.run(host="0.0.0.0", port=8099, threaded=True)
