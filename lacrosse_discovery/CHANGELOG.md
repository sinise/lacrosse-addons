# Changelog

## 2.0.3

- Fix duplicate MQTT devices appearing after every restart:
  `resolve_by_id()` globbed `/dev/serial/by-id/*` exactly once, with no
  retry. `udev` creates that symlink *after* the raw `/dev/ttyUSBx` node
  is already openable, and since 2.0.2 made re-verifying an
  already-known-good port much faster than a full scan, discovery could
  now finish before the symlink existed - silently falling back to the
  raw path, which hashes into a *different* `unique_id` than the by-id
  path used before. Each such restart both created a new device in HA and
  overwrote the good saved `com_port` with the worse one, compounding on
  the next restart. `resolve_by_id()` now retries for up to ~12s before
  falling back.

  This does not clean up devices duplicated by earlier versions - delete
  the stale one(s) manually from Settings -> Devices & services -> MQTT
  (the one that stopped updating, keeping whichever still receives live
  readings).

## 2.0.2

Two bugs from real-world logs, both in `run.sh`:

- `bashio::config 'com_port' ''` didn't actually default to empty: bash's
  `${2:-default}` treats an explicitly-passed empty string the same as
  "not passed", so bashio's own hardcoded `"null"` default won instead of
  the empty string I tried to pass. `com_port` is now read with an
  `bashio::config.has_value` guard instead (matching the pattern already
  used for the `mqtt_*` options), so it's a real empty string when unset.
  In practice this was mostly cosmetic - `discover_port()` already falls
  back to a full scan when the configured port doesn't respond, so
  discovery still succeeded, just with a confusing "verifying configured
  port null" log line.
- `bashio::app.option` doesn't exist in the bashio version bundled in the
  pinned base image - only the older `bashio::addon.option` name does
  (also what autoterm-5d-control uses). This is why a discovered port was
  never actually saved, forcing a full ~35s port scan on every restart.
  Switched to `bashio::addon.option`.

## 2.0.1

- Fix a misleading-freshness bug: sensor state was published with
  `retain=True`, so the broker kept redelivering the last known reading to
  every new subscriber (e.g. on a Home Assistant restart) regardless of
  how old it actually was - and HA treated each redelivery as a fresh
  update, resetting `last_changed` and the `expire_after` countdown. A
  reading from hours ago could keep looking "9 minutes old" indefinitely.
  State is now published with `retain=False`; discovery config topics
  (which must stay retained) are unaffected. Also added a one-time cleanup
  that explicitly clears any stale retained state message already sitting
  on the broker (both the current and the pre-2.0.0 topic name) the next
  time each sensor is heard from.

## 2.0.0

Breaking rewrite: this add-on is now a headless MQTT bridge only - the
generated YAML config block was never used and is gone.

- **Removed**: the web UI, Ingress, manual "Start scan", and
  `sensor: - platform: lacrosse` YAML generation (`scan_duration` option
  and the `/config/lacrosse.yaml` file are gone too).
- **Added `com_port` option and port auto-discovery/persistence**, matching
  the same verify-then-scan pattern as this author's autoterm-5d-control
  add-on: on every start, the configured `com_port` is checked first; if
  that's not a JeeLink (or nothing is configured yet), every
  `/dev/ttyUSB*`/`/dev/ttyACM*` is scanned. Whichever port answers is
  resolved to its stable `/dev/serial/by-id/...` path and saved back into
  `com_port` via the Supervisor API, so subsequent restarts skip scanning
  entirely. New `autodiscover_ports` option (default true) to disable this
  and use `com_port` as configured. Keeps the ~10s settle delay + up to 4
  retries for the busy-right-after-boot race fixed in 1.1.1.
- `mqtt_enabled` now defaults to **true** and `boot` defaults to **auto**,
  since bridging is the add-on's only function now.
- If reading from the port fails after it's already bridging (dongle
  unplugged, etc.), it now retries reopening the same port every 15s
  instead of giving up until a restart.
- MQTT state topic prefix changed from `lacrosse_discovery/` to
  `lacrosse_bridge/` (internal detail only - `unique_id`s are unchanged, so
  existing entities are not duplicated; HA just needs the next retained
  discovery republish to switch over, which happens automatically).

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
