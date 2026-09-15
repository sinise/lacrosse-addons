# lacrosse

A headless Home Assistant add-on that auto-discovers a JeeLink USB dongle's
serial port, remembers it, and bridges LaCrosse/Technoline sensor readings
to Home Assistant via MQTT Discovery - so they show up as normal,
fully UI-manageable entities. See
[`lacrosse_discovery/DOCS.md`](lacrosse_discovery/DOCS.md) for how it works.

## Installing

**Local add-on (quickest, no GitHub repo needed):**

1. Copy the `lacrosse_discovery/` folder onto your Home Assistant host, into
   `/addons/local/lacrosse_discovery` (e.g. via the Samba or SSH/Terminal
   add-on).
2. In Home Assistant: **Settings -> Add-ons -> Add-on Store -> ⋮ -> Check for
   updates** (or reload the page) - "LaCrosse MQTT Bridge" appears under
   **Local add-ons**.
3. Install, and start it - no further setup needed if the official
   Mosquitto broker add-on is already running.

**As a custom repository:**

1. Push this folder (including `repository.yaml`) to a GitHub repo, and
   update the `url` fields in `repository.yaml` and
   `lacrosse_discovery/config.yaml` to point at it.
2. In Home Assistant: **Settings -> Add-ons -> Add-on Store -> ⋮ -> Repositories**,
   add the repo URL.
3. Install "LaCrosse MQTT Bridge" from the store.
