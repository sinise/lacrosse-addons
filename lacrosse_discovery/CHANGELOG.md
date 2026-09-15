# Changelog

## 1.1.1

- Fix the MQTT bridge finding no JeeLink at startup: serial device nodes
  can briefly report busy right after the container boots (observed: every
  port came back busy on the very first probe, succeeding moments later on
  a manual retry). The bridge now waits ~10s before its first probe and
  retries up to 4 times, 20s apart, before giving up. Probing is also now
  shared code between the manual scan and the bridge, with clearer
  per-port log lines either way.

## 1.1.0

- Add an optional persistent MQTT bridge mode (`mqtt_enabled`): publishes
  discovered sensors to Home Assistant via MQTT Discovery so they get
  real, UI-manageable entities instead of the YAML `lacrosse` platform's
  unique-ID-less ones. Auto-discovers the Mosquitto broker add-on via
  Supervisor's `mqtt` service, or accepts a manual broker (`mqtt_host`,
  `mqtt_port`, `mqtt_username`, `mqtt_password`). New `discovery_prefix`
  and `expire_after` options. Web UI now shows bridge/connection status
  and which ports are bridged.

## 1.0.1

- Replace `full_access: true` with `uart: true`. `full_access` is silently
  ignored for "protected" add-ons (the default), which caused
  `Operation not permitted` errors when opening serial ports; `uart: true`
  maps in all serial devices without requiring Protection mode to be
  disabled.

## 1.0.0

- Initial release: probes serial ports for a JeeLink running
  `LaCrosseITPlusReader`, listens for sensor packets, and generates a
  `sensor: - platform: lacrosse` YAML block via an Ingress web UI.
