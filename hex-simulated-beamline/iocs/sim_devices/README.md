# Per-detector caproto sims (typed PV sets)

Phase-2 **(B) "quick connect"** device-development tier: present each ophyd-async
detector's **exact, correctly-typed PV set** so the real device connects and
reads/writes — without the type errors the generic blackhole produces (it
returned `float` for bool AD PVs and couldn't match Kinetix `StrictEnum` states).

## How it works — introspection, not hand-listing

[`_ophyd_async_sim.py`](_ophyd_async_sim.py) takes a **mock-connected** ophyd-async
device, walks its signals, and builds a caproto channel per PV with a type
matching the signal's datatype:

| ophyd-async datatype | caproto channel |
|---|---|
| `bool` | 2-state `ChannelEnum` (`Off`/`On`) |
| `StrictEnum` / `SubsetEnum` | `ChannelEnum` with the enum **member values** |
| `int` / `float` | `ChannelInteger` / `ChannelDouble` |
| `str` | `ChannelChar` (char waveform) |

So the PV set is derived from the device itself — add a new detector by pointing
it at that device (no hand-maintained PV lists, no per-device enum tables in the
blackhole).

- [`kinetix_sim.py`](kinetix_sim.py) — builds `KinetixDetector` (both HEX cameras
  `Det:1`/`Det:3`) the way `hex-profile-collection` does. `KinetixDetector`
  connects cleanly against it, incl. `TriggerMode` = `Internal`/`Rising Edge`/
  `Exp. Gate` and `ReadoutPortIdx` = `1`..`4`.

## One server (why the unified IOC)

Two caproto servers on one host can't both receive CA searches (UDP 5064), so a
standalone per-detector sim goes deaf next to the blackhole. Instead,
[`../sim_ioc.py`](../sim_ioc.py) runs **one** CA server that **seeds** these typed
detector PV sets and **fabricates everything else** (motors/shutters/plugins) via
the vendored blackhole. That's what the boot uses; the per-detector modules here
stay clean, reusable providers of a device's typed PV dict.

## Scope

This is the **device-connect** tier (no frames / no motion). Full fidelity
(real frames via ADSimDetector + the `ADKinetix.template` overlay, real rotation
via motorsim) is the later **(A)** upgrade — add it when plan/data-path testing
needs it.
