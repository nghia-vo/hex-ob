"""
Functional test: batch-runner orchestration against the simulated HEX beamline.

Runs run_multiple_scans in motor mode with REAL devices — sample_x stepped
per scan, a real take_dark_flat (initial, dark_flat_every=-1), and
take_radiograph injected as the per-scan plan (the tomo fly path waits on
the sim's armed-external gating; the runner contract only needs a
plan(output_dir, index) callable, which is also how the _average variants
ride the same runner).

Asserts: run count and order (1 dark/flat run + 2 scan runs), per-stream
event counts, and the scan motor restored to its initial position.

Prerequisites/safety: as the other *_sim_test.py files.
"""

import os
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

addr_list = os.environ.get("EPICS_CA_ADDR_LIST", "")
addrs = addr_list.split()
if not addrs or any(not a.startswith("127.0.0.1") for a in addrs):
    sys.exit(
        f"REFUSING to run: EPICS_CA_ADDR_LIST={addr_list!r} is not loopback-only.\n"
        "Source hex-simulated-beamline/scripts/env.sh first."
    )
os.environ.setdefault("EPICS_CA_AUTO_ADDR_LIST", "NO")

import epics.ca

epics.ca.initialize_libca()

from bluesky import RunEngine
from ophyd_async.epics.core import epics_signal_rw
from ophyd_async.plan_stubs import ensure_connected

from lib.detectors import make_kinetix
from lib.motors import rot_stage, sample_x
from lib.panda import make_panda
from plans.tomography import run_multiple_scans, take_radiograph

BASE = "/nsls2/data/hex/proposals/2026-2/pass-000000/tomography/raw_data/runner_sim_test"


def main() -> None:
    RE = RunEngine({})
    ph_open_cmd = epics_signal_rw(int, "XF:27IDA-PPS{L1-S1}Cmd:Opn-Cmd", name="ph_open_cmd")
    ph_close_cmd = epics_signal_rw(int, "XF:27IDA-PPS{L1-S1}Cmd:Cls-Cmd", name="ph_close_cmd")
    kinetix1 = make_kinetix(1)
    panda1 = make_panda(1)
    RE(ensure_connected(kinetix1, panda1, rot_stage, sample_x, ph_open_cmd, ph_close_cmd))
    print("All devices connected.")

    docs: list[tuple[str, dict]] = []
    RE.subscribe(lambda name, doc: docs.append((name, doc)))

    def scan_plan(output_dir, index):
        return take_radiograph(
            kinetix1, ph_open_cmd, ph_close_cmd,
            output_dir=output_dir, exposure_time=0.05, num_images=2,
            md={"batch_index": index},
        )

    import asyncio
    x0 = asyncio.run(sample_x.user_readback.get_value())

    RE(run_multiple_scans(
        kinetix1, panda1, rot_stage, sample_x,
        output_base_dir=BASE,
        exposure_time=0.05, num_projections=61, flat_x_offset=2.0,
        scan_motor=sample_x, start=x0 - 1.0, stop=x0 + 1.0, num_points=2,
        dark_flat_every=-1, num_dark=1, num_flat=2,
        ph_open_cmd=ph_open_cmd, ph_close_cmd=ph_close_cmd,
        scan_plan=scan_plan,
    ))

    starts = [d for n, d in docs if n == "start"]
    stops = [d for n, d in docs if n == "stop"]
    assert len(starts) == 3, f"expected 3 runs (1 dark/flat + 2 scans), got {len(starts)}"
    assert all(s["exit_status"] == "success" for s in stops), stops
    plan_names = [s.get("plan_name") for s in starts]
    assert plan_names == ["take_dark_flat", "take_radiograph", "take_radiograph"], plan_names
    print(f"PASS  3 runs in order: {plan_names}")

    desc = {d["uid"]: (d["name"], d["run_start"]) for n, d in docs if n == "descriptor"}
    events = Counter(desc[d["descriptor"]][0] for n, d in docs if n == "event")
    assert events == {"dark": 1, "flat": 2, "primary": 4}, events
    print(f"PASS  event streams: {dict(events)}")

    x_final = asyncio.run(sample_x.user_readback.get_value())
    assert abs(x_final - x0) < 1e-6, f"sample_x should be restored to {x0}, got {x_final}"
    print(f"PASS  scan motor restored to {x0:g}")

    print("\nALL PASS")


if __name__ == "__main__":
    main()
