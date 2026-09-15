# LaCrosse MQTT Bridge

Home Assistant's built-in [`lacrosse` integration](https://www.home-assistant.io/integrations/lacrosse/)
talks to a JeeLink USB dongle running the `LaCrosseITPlusReader` sketch, but
its YAML-configured entities never get a `unique_id`, so they can't be
managed from the UI (renamed, moved to an area, disabled) - see
[that limitation](https://www.home-assistant.io/integrations/lacrosse/).

This add-on is a headless bridge instead: it finds the JeeLink's serial
port on its own, listens for LaCrosse/Technoline sensor packets, and
publishes them to Home Assistant via [MQTT Discovery](https://www.home-assistant.io/integrations/mqtt/#mqtt-discovery)
- giving every sensor a real, fully UI-manageable entity that keeps
updating live for as long as the add-on runs. There is no web UI; it's
pure background service, configured entirely through the add-on's
**Configuration** tab.

## Port discovery

On every start, `run.sh` resolves which serial device is the JeeLink,
mirroring the same verify-then-scan approach used by this author's
autoterm-5d-control add-on:

1. **Verify the configured port first.** If `com_port` is already set (from
   a previous run, or typed in manually), it's checked first - fast path,
   no need to touch any other port.
2. **Scan everything if that fails.** Every `/dev/ttyUSB*` and
   `/dev/ttyACM*` is opened and asked for its version banner (`v`); a
   JeeLink running `LaCrosseITPlusReader` answers with something like
   `[LaCrosseITPlusReader.10.1s (RFM12B f:0 r:17241)]`.
3. **Resolve to a stable path.** Whichever port answers is resolved to its
   `/dev/serial/by-id/...` symlink when one exists (survives reboots and
   replugs, unlike `/dev/ttyUSB0`), falling back to the raw path otherwise.
4. **Save it back.** If the discovered port differs from what was
   configured, it's written back into this add-on's own options
   (`com_port`) via the Supervisor API - so the next start goes straight to
   step 1 and skips scanning entirely.
5. **Retry around the boot window.** Serial device nodes can briefly report
   busy right after the container boots. The add-on waits ~10s before its
   first attempt and retries up to 4 times, 20s apart, before giving up for
   this run (it'll try again on the next restart).

Set `autodiscover_ports` to `false` to skip all of this and use `com_port`
exactly as configured, with no verification or scanning.

## MQTT bridging

1. Install and start the official **Mosquitto broker** add-on (or point at
   an external broker - see below).
2. `mqtt_enabled` is on by default. Leave `mqtt_host` etc. blank to
   auto-discover the Mosquitto broker add-on via Supervisor; fill them in
   only if you're using a different broker.
3. Optionally adjust `discovery_prefix` (default `homeassistant`, only
   change this if you've customized your MQTT integration's discovery
   prefix) and `expire_after` (seconds after which an entity goes
   `unavailable` if no new reading arrives - default 1800s/30min, generous
   given sensors typically transmit every 30-60s).
4. Once the JeeLink is found, the add-on listens on it **indefinitely** and
   publishes each reading to MQTT with retained discovery messages. If the
   connection drops (dongle unplugged, etc.) it retries every 15s.
5. New entities (`sensor.lacrosse_<port>_<id>_temperature/_humidity/_battery`)
   appear automatically under **Settings -> Devices & services -> MQTT** as
   sensors are heard - each physical sensor becomes one HA "device" grouping
   its temperature/humidity/battery entities.

## Notes and caveats

- **The add-on maps in every serial device** (`uart: true`) so discovery
  can find the JeeLink without you having to tell it which port to use.
  Probing briefly opens and writes a single byte to *every* serial port on
  the host - harmless for virtually all USB-serial devices, but if you have
  another sensitive serial device attached and want to be extra cautious,
  unplug it, or set `com_port` manually and `autodiscover_ports: false`.
- If a sensor never reported a plausible humidity value (temperature-only
  sensors like the Technoline TX29 send a placeholder), its `humidity`
  entity is simply never created.
- Sensor IDs are only stable until a battery change; a sensor that gets a
  new battery shows up as a new set of entities.
- Check the add-on's **Log** tab for anything prefixed `discovery:` (port
  resolution) or `MQTT` (broker connection) when troubleshooting.
