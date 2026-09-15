# LaCrosse MQTT Bridge add-on

Headless add-on that finds a JeeLink running the `LaCrosseITPlusReader`
sketch, remembers its serial port, and bridges LaCrosse/Technoline sensor
packets to Home Assistant via MQTT Discovery - giving every sensor a real,
fully UI-manageable entity instead of the built-in
[`lacrosse` integration](https://www.home-assistant.io/integrations/lacrosse/)'s
unique-ID-less ones.

See [DOCS.md](DOCS.md) for full usage instructions (also shown in the
add-on's "Documentation" tab in Home Assistant).
