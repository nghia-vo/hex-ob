# Bluesky + Ophyd-async Tutorial for beamline scientists

This guide shows how to do common tasks in Bluesky + Ophyd-async.
It is written from a **scientist's point of view** — no deep Python knowledge needed.

Think of it this way:
- **Ophyd-async** = the objects that represent your hardware (motors, detectors)
- **Bluesky plan** = the recipe that describes what to do
- **RunEngine (RE)** = the engine that actually executes the recipe

---

## Table of Contents

1. [Motor — single axis](#1-motor--single-axis)
2. [Device — multiple motors (Monochromator)](#2-device--multiple-motors-monochromator)
3. [Area Detector (Kinetix)](#3-area-detector-kinetix)
4. [Local database setup](#4-local-database-setup)
5. [Simple 1D scan — single image per point](#5-simple-1d-scan--single-image-per-point)
6. [Simple 1D scan — multiple images per point](#6-simple-1d-scan--multiple-images-per-point)
7. [2D scan — energy + translation](#7-2d-scan--energy--translation)
8. [Multiple scans in a loop](#8-multiple-scans-in-a-loop)
9. [Retrieve data and metadata after a scan](#9-retrieve-data-and-metadata-after-a-scan)

---

## 1. Motor — single axis

### Initialize

```python
from ophyd_async.epics.motor import Motor
from ophyd_async.core import init_devices

# Replace the PV prefix with the real motor PV (without the field suffix)
with init_devices():
    sample_x = Motor("XF:27IDF-OP:1{SMPL:1-Ax:X1}Mtr", name="sample_x")
```

> `init_devices()` connects to the hardware and waits for all signals to be ready.
> Without it you would have to `await` the connection manually.

### Get and set position

```python
import bluesky.plan_stubs as bps

# Read current position
pos = yield from bps.rd(sample_x)
print(f"Current position: {pos} mm")

# Move to an absolute position (blocking — waits until motor stops)
yield from bps.mv(sample_x, 10.5)

# Move by a relative offset
yield from bps.mvr(sample_x, 0.5)   # move +0.5 mm from current position
```

> **Outside a plan** (e.g. in a Jupyter notebook or IPython) wrap with `RE()`:
> ```python
> RE(bps.mv(sample_x, 10.5))
> ```

### Get and set velocity

```python
# Read velocity
vel = yield from bps.rd(sample_x.velocity)
print(f"Velocity: {vel} mm/s")

# Set velocity
yield from bps.mv(sample_x.velocity, 5.0)
```

### Get and set acceleration

```python
# Read acceleration time (seconds to reach full speed)
acc = yield from bps.rd(sample_x.acceleration)
print(f"Acceleration time: {acc} s")

# Set acceleration time
yield from bps.mv(sample_x.acceleration, 0.5)
```

### Get and set software limits

```python
# Read limits
low  = yield from bps.rd(sample_x.low_limit_travel)
high = yield from bps.rd(sample_x.high_limit_travel)
print(f"Limits: [{low}, {high}] mm")

# Set limits
yield from bps.mv(sample_x.low_limit_travel,  -20.0)
yield from bps.mv(sample_x.high_limit_travel,  20.0)
```

---

## 2. Device — multiple motors (Monochromator)

A **Device** groups related motors under one name so you can treat them as a unit.

### Define the device

```python
from ophyd_async.core import Device
from ophyd_async.epics.motor import Motor
from ophyd_async.core import init_devices

class Monochromator(Device):
    """
    HEX double-crystal monochromator.
    Two crystals, each with a Z (translation) and a pitch motor.
    """
    crystal1_z     = Motor("XF:27IDA-OP:1{Mono:DCLM-Ax:C1Y}Mtr",  name="")
    crystal1_pitch = Motor("XF:27IDA-OP:1{Mono:DCLM-Ax:C1P}Mtr",  name="")
    crystal2_z     = Motor("XF:27IDA-OP:1{Mono:DCLM-Ax:Z2}Mtr",   name="")
    crystal2_pitch = Motor("XF:27IDA-OP:1{Mono:DCLM-Ax:C2P}Mtr",  name="")

with init_devices():
    mono = Monochromator(name="mono")
```

### Coordinate motion — change energy

The plan below shows the pattern:
**given energy → calculate motor positions → move all motors simultaneously**.

```python
import bluesky.plan_stubs as bps

def energy_to_mono_positions(energy_keV: float) -> dict:
    """
    Convert photon energy (keV) to monochromator motor positions.
    Replace this function with the real HEX calculation.
    """
    import math
    d_spacing = 3.1355e-10      # Si(111) d-spacing in metres
    hc = 1.23984193e-9          # h*c in keV·m
    theta = math.asin(hc / (2 * d_spacing * energy_keV))
    theta_deg = math.degrees(theta)
    z_offset = 20.0 * math.cos(theta)   # crystal gap in mm (example)
    return {
        "crystal1_pitch": theta_deg,
        "crystal2_pitch": theta_deg,
        "crystal2_z":     z_offset,
    }


def set_energy(energy_keV: float):
    """Move the monochromator to the given photon energy."""
    positions = energy_to_mono_positions(energy_keV)

    print(f"Setting energy to {energy_keV} keV")
    print(f"  crystal1_pitch -> {positions['crystal1_pitch']:.4f} deg")
    print(f"  crystal2_pitch -> {positions['crystal2_pitch']:.4f} deg")
    print(f"  crystal2_z     -> {positions['crystal2_z']:.4f} mm")

    # Move all three motors simultaneously, wait for all to complete
    yield from bps.mv(
        mono.crystal1_pitch, positions["crystal1_pitch"],
        mono.crystal2_pitch, positions["crystal2_pitch"],
        mono.crystal2_z,     positions["crystal2_z"],
    )
    print("Done — energy set.")
```

Usage:
```python
RE(set_energy(12.0))    # set energy to 12 keV
```

---

## 3. Area Detector (Kinetix)

### Initialize

```python
from ophyd_async.core import init_devices, StaticFilenameProvider, StaticPathProvider
from lib.detectors import make_kinetix   # HEX helper from lib/detectors.py

output_dir = "/nsls2/data/hex/proposals/2026-2/pass-319162/tomography/alignment/scan_00001"

path_provider = StaticPathProvider(
    filename_provider=StaticFilenameProvider("proj"),
    path=output_dir,
)

with init_devices():
    kinetix1 = make_kinetix(detector_id=1, path_provider=path_provider)
```

> `make_kinetix(1, ...)` creates a `HEXKinetixDetector` that resets to live-view mode
> automatically after every scan.

### Configure basic parameters

```python
import bluesky.plan_stubs as bps

def configure_kinetix(detector, exposure_time, num_images=1,
                      image_mode="Single", trigger_mode="Internal"):
    """
    Configure the Kinetix camera before a scan.

    image_mode    : "Single" | "Multiple" | "Continuous"
    trigger_mode  : "Internal" (free-run) | "External" (hardware trigger)
    """
    yield from bps.mv(
        detector.driver.acquire_time,   exposure_time,
        detector.driver.acquire_period, exposure_time + 0.002,  # small overhead
        detector.driver.image_mode,     image_mode,
        detector.driver.trigger_mode,   trigger_mode,
        detector.driver.num_images,     num_images,
    )
```

### Set output path — HDF format

```python
from pathlib import Path
from ophyd_async.core import StaticFilenameProvider, StaticPathProvider

def set_output_hdf(detector, output_dir: str, file_prefix: str = "proj"):
    """
    Tell the detector to write HDF files to *output_dir*.
    Equivalent to old:  camera.set_hdf_file_path(output_folder, "proj")
    """
    provider = StaticPathProvider(
        filename_provider=StaticFilenameProvider(file_prefix),
        path=Path(output_dir),
    )
    detector.set_path_provider(provider)
    print(f"Output HDF -> {output_dir}/{file_prefix}_XXXXX.hdf")
```

### Set output path — TIF format

> TIF output in ophyd-async is handled through the TIFF1 plugin, which is part of
> the same `ADKinetix` device but accessed via `detector.fileio` (or a dedicated
> TIF plugin signal group). The simplest approach for scientists is to use HDF
> (which is recommended) and convert later if needed.

```python
# Example: switch path provider to write TIF-named files
# (The actual plugin configuration depends on your AD IOC version)
from ophyd_async.core import StaticFilenameProvider, StaticPathProvider

def set_output_tif(detector, output_dir: str, file_prefix: str = "img"):
    provider = StaticPathProvider(
        filename_provider=StaticFilenameProvider(file_prefix),
        path=Path(output_dir),
    )
    detector.set_path_provider(provider)
    print(f"Output TIF -> {output_dir}/{file_prefix}_XXXXX.tif")
```

---

## 4. Local database setup

If you are at a facility without a centralised database, or just want to try things
locally, you can set up a minimal Bluesky data stack using **Tiled** and a local
file catalogue.

### Install (once)

```bash
pip install bluesky tiled ophyd-async databroker
```

### Start a local Tiled server (in a terminal)

```bash
tiled serve directory /tmp/my_bluesky_data --api-key=secret
```

### Connect from Python

```python
from bluesky import RunEngine
from bluesky.callbacks.tiled_writer import TiledWriter
from tiled.client import from_uri

RE = RunEngine()

# Connect the RunEngine to the local Tiled server
tiled_client = from_uri("http://localhost:8000", api_key="secret")
tiled_writer  = TiledWriter(tiled_client)
RE.subscribe(tiled_writer)

print("RunEngine connected to local Tiled database.")
```

### Alternatively — save to files directly (no server needed)

```python
from bluesky import RunEngine
from bluesky.callbacks.best_effort import BestEffortCallback
import databroker

RE  = RunEngine()
db  = databroker.Broker.named("temp")    # in-memory, lost on restart
RE.subscribe(db.insert)
RE.subscribe(BestEffortCallback())       # live table printed to terminal
```

---

## 5. Simple 1D scan — single image per point

**Scenario:** scan `sample_x` from 0 to 5 mm in 6 steps, take one image per point.

```python
import bluesky.plans as bp
import bluesky.plan_stubs as bps
import bluesky.preprocessors as bpp

def scan_1d_single(detector, motor, start, stop, num_points,
                   exposure_time, output_dir, md=None):
    """
    Scan a motor and take one image at each position.

    detector     : HEXKinetixDetector (or PerkinElmer, etc.)
    motor        : Motor object (e.g. sample_x)
    start, stop  : motor range
    num_points   : number of positions
    exposure_time: camera exposure time in seconds
    output_dir   : where to save the HDF file
    """
    # 1. Point detector output to the right folder
    set_output_hdf(detector, output_dir, file_prefix="img")

    # 2. Configure camera for single-image / internal trigger
    yield from configure_kinetix(detector, exposure_time,
                                  num_images=1,
                                  image_mode="Single",
                                  trigger_mode="Internal")

    # 3. Build metadata for this run
    _md = {
        "plan_name":     "scan_1d_single",
        "exposure_time": exposure_time,
        "output_dir":    output_dir,
    }
    _md.update(md or {})

    # 4. Run the scan
    #    bp.scan moves the motor, triggers the detector, records — all automatically
    yield from bp.scan([detector], motor, start, stop, num_points, md=_md)
```

Usage:
```python
RE(scan_1d_single(
    kinetix1, sample_x,
    start=0.0, stop=5.0, num_points=6,
    exposure_time=0.05,
    output_dir="/nsls2/data/hex/proposals/2026-2/pass-319162/xrd/raw_data/scan_00001",
    md={"sample": "my_sample"},
))
```

---

## 6. Simple 1D scan — multiple images per point

**Scenario:** same as above, but take 10 images at each motor position (e.g. for averaging).

```python
def scan_1d_multi(detector, motor, start, stop, num_points,
                  exposure_time, num_images_per_point, output_dir, md=None):
    """
    Scan a motor and take multiple images at each position.
    """
    set_output_hdf(detector, output_dir, file_prefix="img")
    yield from configure_kinetix(detector, exposure_time,
                                  num_images=num_images_per_point,
                                  image_mode="Multiple",
                                  trigger_mode="Internal")

    _md = {
        "plan_name":            "scan_1d_multi",
        "exposure_time":        exposure_time,
        "num_images_per_point": num_images_per_point,
        "output_dir":           output_dir,
    }
    _md.update(md or {})

    # per_step lets us customise what happens at each motor position
    def per_step(detectors, step, pos_cache):
        yield from bps.mv(motor, step[motor])
        for _ in range(num_images_per_point):
            yield from bps.trigger_and_read(detectors)

    yield from bp.scan([detector], motor, start, stop, num_points,
                        per_step=per_step, md=_md)
```

Usage:
```python
RE(scan_1d_multi(
    kinetix1, sample_x,
    start=0.0, stop=5.0, num_points=6,
    exposure_time=0.05,
    num_images_per_point=10,
    output_dir="/nsls2/data/hex/proposals/2026-2/pass-319162/xrd/raw_data/scan_00001",
))
```

---

## 7. 2D scan — energy + translation

**Scenario:** for each energy step, scan `sample_x` and take one image per position.
This is an outer (energy) × inner (x) nested scan.

```python
import bluesky.plans as bp

def scan_2d_energy_x(detector, motor_x, x_start, x_stop, x_num,
                     energy_list_keV, exposure_time, output_dir, md=None):
    """
    2D scan: outer loop = energy, inner loop = x translation.

    energy_list_keV : list of energies to visit, e.g. [10.0, 11.0, 12.0]
    """
    _md = {
        "plan_name":     "scan_2d_energy_x",
        "exposure_time": exposure_time,
        "energies_keV":  energy_list_keV,
        "output_dir":    output_dir,
    }
    _md.update(md or {})

    set_output_hdf(detector, output_dir, file_prefix="img")
    yield from configure_kinetix(detector, exposure_time,
                                  image_mode="Single", trigger_mode="Internal")

    for energy in energy_list_keV:
        print(f"\n--- Energy: {energy} keV ---")
        yield from set_energy(energy)          # move monochromator
        yield from bp.scan([detector], motor_x, x_start, x_stop, x_num, md=_md)
```

Usage:
```python
RE(scan_2d_energy_x(
    kinetix1, sample_x,
    x_start=-2.0, x_stop=2.0, x_num=5,
    energy_list_keV=[10.0, 11.0, 12.0],
    exposure_time=0.1,
    output_dir="/nsls2/data/hex/proposals/2026-2/pass-319162/xrd/raw_data/scan_00001",
))
```

---

## 8. Multiple scans in a loop

### Time-based: repeat a scan every N seconds

```python
import bluesky.plan_stubs as bps

def repeat_scan_timed(detector, motor, start, stop, num_points,
                      exposure_time, output_dir,
                      num_repeats=5, delay_seconds=60):
    """
    Run the same 1D scan *num_repeats* times with a *delay_seconds* pause between.
    Useful for time-resolved experiments.
    """
    for i in range(num_repeats):
        print(f"\n=== Repeat {i+1}/{num_repeats} ===")
        # Give each repeat its own sub-folder
        scan_dir = output_dir + f"/repeat_{i+1:03d}"
        yield from scan_1d_single(detector, motor, start, stop, num_points,
                                   exposure_time, scan_dir,
                                   md={"repeat_index": i})
        if i < num_repeats - 1:
            print(f"Waiting {delay_seconds} s before next scan...")
            yield from bps.sleep(delay_seconds)
```

### Motor-based: scan over a list of sample positions

```python
def scan_sample_list(detector, sample_stage, sample_positions,
                     scan_motor, scan_start, scan_stop, scan_num,
                     exposure_time, output_dir):
    """
    For each sample position (e.g. a carousel), run a 1D scan.
    """
    for idx, sample_pos in enumerate(sample_positions):
        print(f"\n=== Sample {idx+1}: stage position = {sample_pos} mm ===")
        yield from bps.mv(sample_stage, sample_pos)   # move to sample
        scan_dir = output_dir + f"/sample_{idx+1:03d}"
        yield from scan_1d_single(detector, scan_motor,
                                   scan_start, scan_stop, scan_num,
                                   exposure_time, scan_dir,
                                   md={"sample_index": idx,
                                       "sample_position": sample_pos})
```

### Energy-based: loop over energies

```python
def scan_energy_list(detector, motor, start, stop, num_points,
                     exposure_time, energy_list_keV, output_dir):
    """
    Run a 1D scan at each energy in *energy_list_keV*.
    """
    for energy in energy_list_keV:
        print(f"\n=== Energy: {energy} keV ===")
        yield from set_energy(energy)
        scan_dir = output_dir + f"/energy_{energy:.2f}keV"
        yield from scan_1d_single(detector, motor, start, stop, num_points,
                                   exposure_time, scan_dir,
                                   md={"energy_keV": energy})
```

---

## 9. Retrieve data and metadata after a scan

After `RE(...)` finishes, a **run UID** is returned. Use it to get back your data.

```python
# RE returns the UID of every completed run
uid = RE(scan_1d_single(...))
```

### With a local Tiled database

```python
run   = tiled_client[uid]          # look up the run
df    = run["primary"].read()      # load the table as a pandas DataFrame
print(df)                          # shows motor positions and detector readings
```

### With DataBroker (in-memory or file-based)

```python
run    = db[uid]                   # look up the run
df     = run.primary.read()        # pandas DataFrame
meta   = run.metadata              # dict with start, stop, plan_name, etc.
print(meta["start"])               # all run-start metadata
```

### Extract specific metadata

```python
start_doc = run.metadata["start"]

print("Plan name :",  start_doc["plan_name"])
print("Exposure  :",  start_doc["exposure_time"])
print("Output dir:",  start_doc["output_dir"])
print("Sample    :",  start_doc.get("sample", "not recorded"))
```

### Save metadata alongside the raw HDF file

```python
import json, datetime

def save_run_metadata(run, output_dir: str):
    """
    Export run metadata to a JSON file next to the HDF data.
    Equivalent to old:  losa.save_metadata(path, metadata_dict)
    """
    start_doc = run.metadata["start"]
    stop_doc  = run.metadata["stop"]

    metadata = {
        "date_recorded": datetime.datetime.now().strftime("%H:%M:%S; %d %B %Y"),
        "uid":           start_doc["uid"],
        "plan_name":     start_doc.get("plan_name"),
        "scan_start":    start_doc.get("time"),
        "scan_stop":     stop_doc.get("time"),
        "parameters":    {k: v for k, v in start_doc.items()
                          if k not in ("uid", "time", "hints")},
    }

    out_path = f"{output_dir}/metadata.json"
    with open(out_path, "w") as f:
        json.dump(metadata, f, indent=4, default=str)
    print(f"Metadata saved to: {out_path}")
```

Usage:
```python
uid = RE(scan_1d_single(..., output_dir="/my/scan/folder"))
save_run_metadata(db[uid], output_dir="/my/scan/folder")
```

---

## Quick Reference Cheat Sheet

| Task | Old pyepics | Bluesky / Ophyd-async |
|---|---|---|
| Move motor | `move_motor(pv, pos)` | `RE(bps.mv(motor, pos))` |
| Relative move | `move_motor_relative(pv, delta)` | `RE(bps.mvr(motor, delta))` |
| Read position | `caget(pv + ".RBV")` | `RE(bps.rd(motor))` |
| Set velocity | `caput(pv + ".VELO", v)` | `RE(bps.mv(motor.velocity, v))` |
| Take one image | `camera.start_acquire()` | `RE(bps.trigger_and_read([det]))` |
| 1D motor scan | manual `for` loop | `RE(bp.scan([det], motor, s, e, n))` |
| Set output path | `camera.set_hdf_file_path(dir, name)` | `detector.set_path_provider(...)` |
| Open shutter | `open_photon_shutter()` | `RE(bps.abs_set(ph_open_cmd, 1))` |
| Wait N seconds | `time.sleep(N)` | `yield from bps.sleep(N)` |
| Save metadata | `losa.save_metadata(path, dict)` | Automatic in run start document; or `save_run_metadata(run, dir)` |
