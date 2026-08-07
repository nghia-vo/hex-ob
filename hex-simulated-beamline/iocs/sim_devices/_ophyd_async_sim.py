"""Generate a caproto simulation IOC from an ophyd-async device.

Introspects a *mock-connected* ophyd-async `Device`, then serves **exactly that
device's PV set** over Channel Access with channel types matching each signal's
datatype (bool → 2-state enum, `StrictEnum`/`SubsetEnum` → enum with the member
values, int/float/str → scalar). This lets the real device connect and
read/write as it would against hardware — without fabricating unrelated PVs
(unlike the generic blackhole).

Reusable for any hextools device (Kinetix now; Phantom / Perkin-Elmer later):
just pass a constructed device to `run_device_sim`.
"""

import asyncio
from enum import Enum

from caproto import (
    ChannelChar,
    ChannelData,
    ChannelDouble,
    ChannelEnum,
    ChannelInteger,
    ChannelString,
)
from caproto.server import run
from ophyd_async.core import Device, Signal


def _walk_signals(device: Device, acc: list, seen: set) -> None:
    for _name, child in device.children():
        if id(child) in seen:
            continue
        seen.add(id(child))
        if isinstance(child, Signal):
            acc.append(child)
        elif isinstance(child, Device):
            _walk_signals(child, acc, seen)


def _signal_datatype(sig: Signal):
    conn = getattr(sig, "_connector", None)
    backend = getattr(conn, "backend", None) if conn is not None else None
    if backend is None:
        backend = getattr(sig, "_backend", None)
    return getattr(backend, "datatype", None)


def _channel_for(datatype) -> ChannelData:
    if datatype is bool:
        # ophyd-async bool reads a 2-state EPICS enum (bi/bo) fine.
        return ChannelEnum(value=0, enum_strings=["Off", "On"])
    if isinstance(datatype, type) and issubclass(datatype, Enum):
        # StrictEnum/SubsetEnum: CA enum states must equal the member *values*.
        return ChannelEnum(value=0, enum_strings=[str(e.value) for e in datatype])
    if datatype is int:
        return ChannelInteger(value=0)
    if datatype is float:
        return ChannelDouble(value=0.0)
    if datatype is str:
        # char waveform so long strings (file paths) aren't truncated at 40.
        return ChannelChar(value=b"\x00" * 256)
    # arrays / unknown -> harmless scalar; extend if a device needs real arrays.
    return ChannelDouble(value=0.0)


def build_pvdb(device: Device) -> dict:
    """Map the device's signals to caproto channels (shared base/_RBV pair)."""
    signals: list = []
    _walk_signals(device, signals, set())
    pvdb: dict = {}
    for sig in signals:
        pv = sig.source.split("://", 1)[-1]  # strip 'mock+ca://'
        base = pv[:-4] if pv.endswith("_RBV") else pv
        channel = _channel_for(_signal_datatype(sig))
        # Serve both write (base) and readback (_RBV) from one channel so a put
        # reflects in the RBV that ophyd-async monitors during set().
        pvdb.setdefault(base, channel)
        pvdb.setdefault(base + "_RBV", pvdb[base])
    return pvdb


def run_devices_sim(devices, label: str = "") -> None:
    """Mock-connect each device and serve the union of their typed PV sets."""

    async def _connect_all():
        for dev in devices:
            await dev.connect(mock=True)

    asyncio.run(_connect_all())
    pvdb: dict = {}
    for dev in devices:
        pvdb.update(build_pvdb(dev))
    print(f"[sim-device] {label}: serving {len(pvdb)} PVs on 127.0.0.1")
    # Bind only to loopback; call run() directly so we don't parse sys.argv
    # (the entry point has its own args like --det-ids).
    run(pvdb, interfaces=["127.0.0.1"])


def run_device_sim(device: Device, label: str = "") -> None:
    """Mock-connect `device`, then serve its typed PV set on 127.0.0.1."""
    run_devices_sim([device], label=label)
