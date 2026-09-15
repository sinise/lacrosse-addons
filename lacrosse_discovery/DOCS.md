# LaCrosse Discovery

Home Assistant's built-in [`lacrosse` integration](https://www.home-assistant.io/integrations/lacrosse/)
talks to a JeeLink USB dongle running the `LaCrosseITPlusReader` sketch, but
it has no discovery: you have to already know which `/dev/ttyUSBx` the
dongle is on and the numeric `id` of every sensor, and those change
whenever you replug the dongle or swap a sensor's battery.

This add-on scans your host for a JeeLink, listens for real sensor packets,
and writes out the exact YAML block you can paste into `configuration.yaml`.

It can also run as a persistent **MQTT bridge**: it publishes sensors to Home
Assistant via [MQTT Discovery](https://www.home-assistant.io/integrations/mqtt/#mqtt-discovery),
so instead of the `lacrosse` platform's unique-ID-less entities, you get
normal, fully UI-manageable entities (rename, move to an area, disable, etc.)
that keep updating live for as long as the add-on runs.

## How it works

1. **Probe.** The add-on opens every USB-serial port it can see
   (`/dev/ttyUSB*`, `/dev/ttyACM*`, or anything else pyserial reports with a
   USB VID:PID) and asks it for its version banner. A JeeLink running
   `LaCrosseITPlusReader` answers with something like:
   `[LaCrosseITPlusReader.10.1s (RFM12B f:0 r:17241)]`.
2. **Listen.** For every port that answers as a JeeLink, the add-on listens
   for incoming `OK ...` sensor packets for the configured duration (60s by
   default) and decodes each one: sensor ID, temperature, humidity, and
   battery state - using the same decoding as the `pylacrosse` library that
   the HA integration itself uses.
3. **Generate.** Once the scan finishes, the web UI shows every sensor ID
   seen per port and renders a `sensor: - platform: lacrosse` block you can
   copy straight into `configuration.yaml`. A copy is also written to
   `/addon_configs/local_lacrosse_discovery/lacrosse.yaml` on the host (visible
   via the File Editor or Samba add-ons) so you don't have to keep the tab
   open.

## Using it

1. Install and start the add-on, then open its **Web UI** (or the sidebar
   panel, since it uses Ingress).
2. Set how long to listen (sensors typically transmit every 30-60s, so
   give it at least 60-120s; a longer scan just means more confidence that
   you've seen every sensor and its true low-battery state) and click
   **Start scan**.
3. Watch the **Ports** table - it lists every serial port and whether a
   JeeLink was found on it.
4. Once sensors start showing up, copy the generated YAML block into
   `configuration.yaml` under `sensor:`, restart Home Assistant, and check
   that the new entities appear with the values you expect.
5. Rename the `name:` fields to something meaningful once you know which
   physical sensor each ID belongs to (e.g. "Outside", "Garage").

## MQTT bridge mode (recommended over the YAML block)

The `lacrosse` YAML platform's entities never get a `unique_id`, so Home
Assistant can't manage them from the UI (rename, area, disable) - see
[the integration's own limitation](https://www.home-assistant.io/integrations/lacrosse/).
Bridging over MQTT instead gives every sensor a real, UI-manageable entity.

1. Install and start the official **Mosquitto broker** add-on (or point at
   any external broker - see below).
2. Open this add-on's **Configuration** tab and turn on **mqtt_enabled**.
   Leave `mqtt_host` etc. blank to auto-discover the Mosquitto broker add-on;
   fill them in only if you're using a different/external broker.
3. Optionally adjust `discovery_prefix` (default `homeassistant`, only
   change this if you've customized your MQTT integration's discovery
   prefix) and `expire_after` (seconds after which an entity goes
   `unavailable` if no new reading arrives - default 1800s/30min, generous
   given sensors typically transmit every 30-60s).
4. Restart the add-on. On startup it probes every serial port once, and for
   every JeeLink it finds, listens **indefinitely** and publishes each
   reading to MQTT with retained discovery messages. The web UI's **MQTT
   bridge** card shows connection status and which ports are bridged.
5. New entities (`sensor.lacrosse_<port>_<id>_temperature/_humidity/_battery`)
   appear automatically under **Settings -> Devices & services -> MQTT** as
   sensors are heard - each physical sensor becomes one HA "device" grouping
   its temperature/humidity/battery entities.
6. At startup the bridge waits ~10s for serial devices to settle, then
   probes for a JeeLink up to 4 times (20s apart) before giving up - this
   covers the brief window right after boot where ports can report busy. A
   newly plugged-in JeeLink (or one connected after that startup window) is
   only picked up on the next add-on restart.
7. The manual **Start scan** button still works independently for discovery
   purposes; it skips ports already owned by the bridge (shown as
   `bridged` in the Ports table) and just displays their live data.

## Notes and caveats

- **The add-on maps in every serial device** (`uart: true`) so it can find
  the JeeLink without you having to tell it which port to use first.
  During a scan it briefly opens and writes a single byte to *every*
  serial port it finds - harmless for virtually all USB-serial devices,
  but if you have another sensitive serial device attached and want to be
  extra cautious, unplug it before scanning.
- **If a port shows `busy` with `Operation not permitted`**, the container
  couldn't get access to that device node. This add-on intentionally
  avoids `full_access`/privileged mode (which Supervisor only grants to
  add-ons with *Protection mode* turned off) in favor of the more scoped
  `uart: true`. If your JeeLink still isn't picked up as a UART device
  (rare, but possible with some CH340-based clones or unusual host
  setups), the fallback is: enable **Advanced Mode** on your HA user
  profile, open this add-on's *Info* tab, turn **Protection mode** off,
  and change `uart: true` to `full_access: true` in `config.yaml` before
  reinstalling.
- **JeeLinks reset when the serial port is opened**, so each probe/listen
  waits ~2 seconds for the sketch to reboot before talking to it.
- If a sensor never reported a plausible humidity value (temperature-only
  sensors like the Technoline TX29 send a placeholder), the generated YAML
  omits its `humidity` entity and adds a comment instead of a bad reading.
- Sensor IDs are only stable until a battery change. If entities go
  `unavailable`, re-run a scan.
- Prefer the `/dev/serial/by-id/...` path when one is found - it survives
  reboots and USB replugs, unlike `/dev/ttyUSB0`.
