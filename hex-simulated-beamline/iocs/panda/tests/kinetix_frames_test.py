#!/usr/bin/env python3
"""Kinetix frame-tier check: the containerized REAL AD IOC writes real frames.

Drives the ADSimDetector-backed Kinetix IOC (:5085, real HEX prefix) through
the exact PV sequence ``deco.Kinetix`` uses in ``tomo_flyscan.py``: configure
exposure/period, ImageMode=Multiple, NumImages=N, HDF plugin in Stream mode
with the deco file template, Capture, Acquire — then expects
``HDF1:NumCaptured_RBV`` to reach N and a REAL, readable HDF file on disk.

This is the production plugin machinery (controls-level mock), not a faked
counter — the same code path whose file-writing/close behaviour the beamline
file-lock saga exercises.

Prereqs: hexsim-kinetix-ioc container running
(``docker compose -f compose/docker-compose.kinetix.yml up -d``).

Run (env with `pyepics` + `h5py`): python tests/kinetix_frames_test.py
Exit code 0 = PASS.
"""

import argparse
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from localguard import assert_local_epics  # noqa: E402

assert_local_epics(default_ca="127.0.0.1:5085")

import h5py  # noqa: E402
from epics import caget, caput  # noqa: E402

P = "XF:27ID1-BI{Kinetix-Det:1}"
HDF_FILE_TEMPLATE = "%s%s_%05d.hdf"  # deco.lib_device_control convention


def wait_for(fn, timeout, why):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if fn():
            return True
        time.sleep(0.2)
    print("TIMEOUT waiting for", why)
    return False


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--data-dir", default="/tmp/hex-sim-data/")
    p.add_argument("--frames", type=int, default=5)
    p.add_argument("--exp", type=float, default=0.05)
    p.add_argument("--period", type=float, default=0.1)
    args = p.parse_args(argv)

    # Camera config, deco-style.
    caput(P + "cam1:Acquire", 0, wait=True)
    caput(P + "cam1:AcquireTime", args.exp, wait=True)
    caput(P + "cam1:AcquirePeriod", args.period, wait=True)
    caput(P + "cam1:ImageMode", 1, wait=True)         # Multiple
    caput(P + "cam1:TriggerMode", 0, wait=True)       # Internal (sim free-runs)
    caput(P + "cam1:NumImages", args.frames, wait=True)
    caput(P + "cam1:ArrayCounter", 0, wait=True)
    # Fresh IOC quirk: the real beamline IOC has driver ArrayCallbacks
    # autosaved ON (deco never touches it); without it no frames reach the
    # plugins at all.
    caput(P + "cam1:ArrayCallbacks", 1, wait=True)

    # HDF plugin, exactly deco.set_hdf_file_path + open_hdf_stream.
    caput(P + "HDF1:EnableCallbacks", 1, wait=True)
    caput(P + "HDF1:Capture", 0, wait=True)
    caput(P + "HDF1:FileNumber", 0, wait=True)
    caput(P + "HDF1:AutoIncrement", 1, wait=True)
    caput(P + "HDF1:CreateDirectory", -5, wait=True)
    caput(P + "HDF1:FileTemplate", HDF_FILE_TEMPLATE, wait=True)
    caput(P + "HDF1:FileName", "kinetix_frames_test", wait=True)
    caput(P + "HDF1:FilePath", args.data_dir, wait=True)
    # Freshly (re)started IOCs need a beat before plugin readbacks settle —
    # poll rather than insta-fail (seen as an empty FullFileName race).
    if not wait_for(lambda: caget(P + "HDF1:FilePathExists_RBV") == 1,
                    15, "HDF1:FilePathExists_RBV"):
        print("FAIL: IOC says file path %r does not exist" % args.data_dir)
        return 1
    caput(P + "HDF1:NumCapture", args.frames, wait=True)
    caput(P + "HDF1:FileWriteMode", 2, wait=True)     # Stream
    caput(P + "HDF1:Capture", 1, wait=False)
    time.sleep(0.5)

    caput(P + "cam1:Acquire", 1, wait=False)
    ok = wait_for(
        lambda: caget(P + "HDF1:NumCaptured_RBV") >= args.frames,
        30, "HDF1:NumCaptured_RBV >= %d" % args.frames)
    n = int(caget(P + "HDF1:NumCaptured_RBV"))
    print("HDF1:NumCaptured_RBV =", n)
    caput(P + "cam1:Acquire", 0, wait=True)
    # Stream mode with NumCapture reached closes the file itself; make sure.
    wait_for(lambda: caget(P + "HDF1:Capture_RBV") == 0, 10, "file close")
    fullname = caget(P + "HDF1:FullFileName_RBV", as_string=True)
    print("FullFileName_RBV =", fullname)

    path = fullname if fullname and os.path.exists(fullname) else None
    if not path:
        print("FAIL: HDF file not found on host at %r" % fullname)
        return 1
    with h5py.File(path, "r") as f:
        data = f["/entry/data/data"]
        shape = data.shape
        print("dataset /entry/data/data shape:", shape)
    if ok and n >= args.frames and shape[0] == args.frames:
        print("PASS")
        return 0
    print("FAIL")
    return 1


if __name__ == "__main__":
    sys.exit(main())
