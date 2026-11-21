# Combustion Inc for Home Assistant

A Home Assistant integration for every [Combustion Inc](https://combustion.inc) device: the
Predictive Thermometer, the Giant Grill Gauge, the Display (Timer), the Booster and the Engine.
It is written from Combustion's published
[BLE specifications](https://github.com/combustion-inc/combustion-documentation) and checked
against the byte vectors in their official SDK tests. It has no Python dependencies beyond
what Home Assistant ships.

## What you get

Temperatures come from Bluetooth advertisements, which every adapter and proxy can receive.
Everything else needs a Bluetooth connection, which the integration opens on its own unless you
turn that off.

| Device | From advertisements | With a connection |
|---|---|---|
| Probe | Core, surface and ambient temperature, all 8 sensors (disabled by default), Instant Read, battery, overheating, probe ID and color, MeatNet hops | Prediction state, time remaining, progress, estimated core temperature, food safety, alarm state; set the removal temperature and prediction mode; silence alarms |
| Giant Grill Gauge | Temperature, sensor connected, overheating, battery, high and low alarm state and limits, gauge ID | Set the alarm limits and turn the alarms on or off; silence alarms |
| Engine | Set point, app mode, control device, lid, fixed speed | Control temperature, fan, controller, battery; change the set point (in app mode) |
| Display, Booster | Discovered as devices, signal strength | Connected indicator |

Probes are tracked by serial number, not by Bluetooth address. A probe heard directly and
through a Display or Gauge is one device; direct readings win while they are fresh.

### How the connection works

One connection to any MeatNet device (Gauge, Display, Booster or Engine) is enough: it pushes
the status of every probe, gauge and engine on the network and relays commands to them. A probe
out of Home Assistant's range is still covered if a MeatNet device near it is in range. With no
MeatNet device around, the integration connects to the probes directly (up to three).

Connections need a local Bluetooth adapter or an
[ESPHome Bluetooth proxy](https://esphome.io/components/bluetooth_proxy.html) with active
connections. Passive proxies (Shelly, for example) still give you every advertisement-based
entity. Turn connections off under **Settings → Devices & services → Combustion Inc →
Configure** if you only want advertisements.

## Installation

### Marketplace (Home Assistant 2026.11 and later)

[![Open your Home Assistant instance and open this repository in the Marketplace.](https://my.home-assistant.io/badges/marketplace_repository.svg)](https://my.home-assistant.io/redirect/marketplace_repository/?owner=benyaming&repository=ha-combustion&category=integration)

Or by hand: **Settings → Marketplace → Add from link**, enter `benyaming/ha-combustion`. Install,
then restart Home Assistant. Custom repositories need the Marketplace connected to GitHub; it
asks you to connect the first time if it is not.

### HACS (Home Assistant 2026.10)

[![Open your Home Assistant instance and open this repository in HACS.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=benyaming&repository=ha-combustion&category=integration)

Or by hand: **HACS → ⋮ → Custom repositories**, add `https://github.com/benyaming/ha-combustion`
as an *Integration*. Install **Combustion Inc**, then restart. On upgrading to 2026.11 the
Marketplace takes this over automatically.

### Manual

Copy `custom_components/combustion_inc` into your configuration's `custom_components` folder
and restart.

### Setup

Combustion devices in range are discovered automatically: accept the discovery under
**Settings → Devices & services**. One entry covers all of them; new devices are added as they
appear. To add it before any device is in range:

[![Open your Home Assistant instance and start setting up Combustion Inc.](https://my.home-assistant.io/badges/config_flow_start.svg)](https://my.home-assistant.io/redirect/config_flow_start/?domain=combustion_inc)

If you used `legrego/homeassistant-combustion` or a fork of it, remove it first. This
integration uses its own domain (`combustion_inc`), so the two do not clash, but there is no
point running both.

## Notes and limits

- **Range.** BLE from a grill on a balcony rarely reaches a Home Assistant box indoors. An ESPHome
  Bluetooth proxy near the grill fixes that.
- **Gauge battery.** The gauge always reports 100% (its coin cells cannot be measured), so only
  the low-battery flag is exposed.
- **Overheating** counts every sensor over its limit, plus the sensors the probe itself flags
  while any sensor really is over its limit. The probe's flags alone are not trusted: older
  repeater firmware sets them wrongly. The raw flags are kept as an attribute.
- **Not yet supported:** per-sensor probe alarms (the probe's alarm state is shown, but alarm
  limits are set in the Combustion app), the Display's timer, food safety configuration, logs.
- **Diagnostics** (device page → Download diagnostics) include the raw advertisement bytes of
  each device. Attach them to bug reports.

## Development

```bash
uv sync --group test --group lint
uv run pytest
uv run ruff check custom_components tests
```

The protocol code in `custom_components/combustion_inc/protocol/` has no Home Assistant imports.
Its tests use the firmware's golden frames and the official SDK's test vectors.

Not affiliated with or endorsed by Combustion Inc.
