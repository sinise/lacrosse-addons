# lacrosse

A Home Assistant add-on that discovers JeeLink USB dongles and the LaCrosse
sensor IDs they're receiving, and generates the `sensor: - platform: lacrosse`
config block to paste into `configuration.yaml`. See
[`lacrosse_discovery/DOCS.md`](lacrosse_discovery/DOCS.md) for how it works.

## Installing

**Local add-on (quickest, no GitHub repo needed):**

1. Copy the `lacrosse_discovery/` folder onto your Home Assistant host, into
   `/addons/local/lacrosse_discovery` (e.g. via the Samba or SSH/Terminal
   add-on).
2. In Home Assistant: **Settings -> Add-ons -> Add-on Store -> ⋮ -> Check for
   updates** (or reload the page) - "LaCrosse Discovery" appears under
   **Local add-ons**.
3. Install, start it, and open its Web UI.

**As a custom repository:**

1. Push this folder (including `repository.yaml`) to a GitHub repo, and
   update the `url` fields in `repository.yaml` and
   `lacrosse_discovery/config.yaml` to point at it.
2. In Home Assistant: **Settings -> Add-ons -> Add-on Store -> ⋮ -> Repositories**,
   add the repo URL.
3. Install "LaCrosse Discovery" from the store.
