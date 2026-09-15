# LaCrosse Discovery

Home Assistant's built-in [`lacrosse` integration](https://www.home-assistant.io/integrations/lacrosse/)
talks to a JeeLink USB dongle running the `LaCrosseITPlusReader` sketch, but
it has no discovery: you have to already know which `/dev/ttyUSBx` the
dongle is on and the numeric `id` of every sensor, and those change
whenever you replug the dongle or swap a sensor's battery.

This add-on scans your host for a JeeLink, listens for real sensor packets,
and writes out the exact YAML block you can paste into `configuration.yaml`.

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

## Notes and caveats

- **The add-on requests full device access** (`full_access: true`) so it
  can find the JeeLink without you having to tell it which port to use
  first. During a scan it briefly opens and writes a single byte to
  *every* serial port it finds - harmless for virtually all USB-serial
  devices, but if you have another sensitive serial device attached and
  want to be extra cautious, unplug it before scanning.
- **JeeLinks reset when the serial port is opened**, so each probe/listen
  waits ~2 seconds for the sketch to reboot before talking to it.
- If a sensor never reported a plausible humidity value (temperature-only
  sensors like the Technoline TX29 send a placeholder), the generated YAML
  omits its `humidity` entity and adds a comment instead of a bad reading.
- Sensor IDs are only stable until a battery change. If entities go
  `unavailable`, re-run a scan.
- Prefer the `/dev/serial/by-id/...` path when one is found - it survives
  reboots and USB replugs, unlike `/dev/ttyUSB0`.
