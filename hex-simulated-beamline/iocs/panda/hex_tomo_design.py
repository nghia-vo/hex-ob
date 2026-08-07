#!/usr/bin/env python3
"""Apply the REAL HEX tomography block design to the simulated PandA.

This reproduces ``Tomo_radio_1_config`` as captured live from the production
PandA (xf27id1-panda1 web GUI, 2026-07-30 — see ``designs/tomo_radio_1_config.md``
for the full recovered values). The pyepics scripts (`lib.lib_device_control.Panda`)
assume this design is loaded and only set run-time parameters.

Real data/trigger flow::

    INENC1.VAL (raw encoder = 39660 - 200*deg; injected by the motor bridge)
        -> CALC1 "Position calc"  (TYPEA=-Value: OUT = -INENC1.VAL = 200*deg - 39660)
             |-> CALC2 "angles"   (identity; captured w/ scale 0.005 offset 198.3 -> degrees)
             |-> PCOMP1.INP       (ENABLE = PCAP.ACTIVE -- ARMING IS THE GATE)
                    PCOMP1 (PULSES=1) fires ONCE at the start angle
                        -> PULSE1.TRIG (ENABLE=ONE): N-pulse TIME train (STEP=acq_period)
                              |-> PCAP.GATE + PCAP.TRIG   (one angle sample per pulse)
                              |-> LUT1.INPA --\\
    BITS.A (radiography path)                  LUT1 (A|B) -> TTLOUT1 -> camera trigger
        -> PULSE3 (one-shot) -> PULSE2.TRIG --/
           (PULSE2.ENABLE = BITS.OUTA; Panda.start_trigger drives radiography, NOT tomo)

Notes vs the real box: COUNTER1 (camera-return tally via TTLIN1, captured as
Diff) is configured for capture but its wiring was not in the capture set —
left unwired here. PCAP TS_* capture options omitted (not consumed by deco).

Applied over the PandA control protocol (config port, default 127.0.0.1:8888) so
it does not depend on the EPICS IOC. Writes go to the LOCAL sim only.

``--yaml PATH`` applies the design from a declarative xpdtools-shape YAML
(``block.n.field: value`` — see hextools ``panda_configurations/``, per
``dec:panda-configurations``) instead of the built-in DESIGN list, making the
YAML the single source of truth shared with the hextools scan code.
``*_dataset`` keys are IOC-level (pandablocks-ioc ``:DATASET`` PVs, not control
fields) and are applied over CA when pyepics is available — the same setting
``init_panda_ioc.py`` restores after a container recreate.
"""

import argparse
import socket
import sys

PANDA_PV_PREFIX = "XF:27ID1-ES{PANDA:1}"

# YAML field suffixes that are ATTRIBUTES of a field (FIELD.SUFFIX in the
# control protocol), e.g. out_capture -> OUT.CAPTURE, trig_delay -> TRIG.DELAY.
# Everything else maps to a plain field: trig_edge -> TRIG_EDGE.
_ATTR_SUFFIXES = ("capture", "dataset", "scale", "offset", "units", "delay")


def yaml_to_commands(config):
    """Map xpdtools-shape YAML entries to (control_cmds, ioc_datasets, skipped)."""
    ctrl, datasets, skipped = [], [], []
    for key, value in config.items():
        parts = key.split(".")
        if parts[-1] == "label" or parts[0] == "data":
            skipped.append((key, "GUI/session-level, not part of the design"))
            continue
        if len(parts) == 3:
            block, field = parts[0].upper() + parts[1], parts[2]
        elif len(parts) == 2:
            block, field = parts[0].upper(), parts[1]
        else:
            skipped.append((key, "unrecognized key shape"))
            continue
        base, _, suffix = field.rpartition("_")
        if base and suffix in _ATTR_SUFFIXES:
            field = "%s.%s" % (base.upper(), suffix.upper())
        else:
            field = field.rstrip("_").upper()  # set_ -> SET
        if field.endswith(".DATASET"):
            datasets.append((block, field, str(value)))
        else:
            ctrl.append("%s.%s=%s" % (block, field, value))
    return ctrl, datasets, skipped


def apply_datasets(datasets):
    """Apply IOC-level :DATASET settings over CA (best-effort)."""
    if not datasets:
        return
    try:
        from epics import caput
    except ImportError:
        print("WARN: pyepics unavailable — skipping %d DATASET setting(s); "
              "init_panda_ioc.py covers them" % len(datasets), file=sys.stderr)
        return
    for block, field, value in datasets:
        pv = "%s:%s:%s" % (PANDA_PV_PREFIX, block, field.replace(".", ":"))
        ok = caput(pv, value, wait=True, timeout=5)
        print("%-40s -> %s" % ("%s=%s" % (pv, value),
                               "OK" if ok else "FAILED"))

# Wiring + calibration recovered from the live Tomo_radio_1_config design.
DESIGN = [
    # CALC1 "Position calc 1": NEGATE the raw encoder (real TYPEA = -Value).
    "CALC1.INPA=INENC1.VAL",
    "CALC1.TYPEA=-Value",
    "CALC1.INPB=ZERO",
    "CALC1.INPC=ZERO",
    "CALC1.INPD=ZERO",
    "CALC1.SHIFT=0",
    # CALC2 "angles": identity of CALC1.OUT (what get_enc_value reads).
    "CALC2.INPA=CALC1.OUT",
    "CALC2.TYPEA=Value",
    "CALC2.INPB=ZERO",
    "CALC2.INPC=ZERO",
    "CALC2.INPD=ZERO",
    "CALC2.SHIFT=0",
    # PCOMP1: enabled by the ARM itself; watches the negated position.
    "PCOMP1.ENABLE=PCAP.ACTIVE",
    "PCOMP1.INP=CALC1.OUT",
    "PCOMP1.DIR=Positive",
    "PCOMP1.RELATIVE=Absolute",
    "PCOMP1.PRE_START=0",
    # PULSE1: always-armed one-shot -> N-pulse time train (tomo).
    "PULSE1.ENABLE=ONE",
    "PULSE1.TRIG=PCOMP1.OUT",
    "PULSE1.TRIG_EDGE=Rising",
    # Radiography path: BITS.A one-shots PULSE3, which kicks PULSE2's train.
    "PULSE3.ENABLE=ONE",
    "PULSE3.TRIG=BITS.OUTA",
    "PULSE3.TRIG_EDGE=Rising",
    "PULSE3.DELAY=0.1",
    "PULSE3.WIDTH=0.1",
    "PULSE2.ENABLE=BITS.OUTA",
    "PULSE2.TRIG=PULSE3.OUT",
    "PULSE2.TRIG_EDGE=Rising",
    # COUNTER3 tallies the tomo train, as on the real box (its readback showed
    # 1801 = a full scan's pulses; exact wiring inferred from that). Also load-
    # bearing in the sim: with PCAP as PULSE1.OUT's only listener the engine
    # loses the train after one edge (single-listener wakeup quirk — see
    # designs/tomo_radio_1_config.md).
    "COUNTER3.TRIG=PULSE1.OUT",
    "COUNTER3.ENABLE=PCAP.ACTIVE",
    "COUNTER3.START=0",
    "COUNTER3.STEP=1",
    # Camera trigger = tomo train OR radio train.
    "LUT1.INPA=PULSE1.OUT",
    "LUT1.INPB=PULSE2.OUT",
    "LUT1.INPC=ZERO",
    "LUT1.INPD=ZERO",
    "LUT1.INPE=ZERO",
    "LUT1.TYPEA=Input-Level",
    "LUT1.TYPEB=Input-Level",
    "LUT1.FUNC=A|B",
    "TTLOUT1.VAL=LUT1.OUT",
    # PCAP: self-enabled on arm; gated + triggered by the tomo train.
    "PCAP.ENABLE=PCAP.ACTIVE",
    "PCAP.GATE=PULSE1.OUT",
    "PCAP.TRIG=PULSE1.OUT",
    "PCAP.TRIG_EDGE=Rising",
    # Captures per the real Positions table. CALC2 is scaled to DEGREES:
    # deg = raw*0.005 + 198.3  (raw = 200*deg - 39660).
    "CALC1.OUT.CAPTURE=Value",
    "CALC2.OUT.CAPTURE=Value",
    "CALC2.OUT.SCALE=0.005",
    "CALC2.OUT.OFFSET=198.3",
    "INENC1.VAL.CAPTURE=Value",
    "COUNTER1.OUT.CAPTURE=Diff",
]


def send(sock, command, timeout=2.0):
    sock.sendall((command + "\n").encode())
    sock.settimeout(timeout)
    return sock.recv(4096).decode().strip()


def apply_design(host, port, commands=DESIGN, verbose=True):
    sock = socket.create_connection((host, port), timeout=5)
    failures = []
    try:
        for command in commands:
            reply = send(sock, command)
            ok = reply.startswith("OK")
            if verbose or not ok:
                print("%-32s -> %s" % (command, reply))
            if not ok:
                failures.append((command, reply))
    finally:
        sock.close()
    return failures


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8888)
    parser.add_argument("--yaml", default=None, metavar="PATH",
                        help="apply the design from an xpdtools-shape YAML "
                             "(hextools panda_configurations/) instead of the "
                             "built-in DESIGN list")
    args = parser.parse_args(argv)
    commands, datasets = DESIGN, []
    if args.yaml:
        import yaml as _yaml
        with open(args.yaml) as f:
            config = _yaml.safe_load(f)
        commands, datasets, skipped = yaml_to_commands(config)
        print("YAML %s: %d control command(s), %d dataset setting(s)"
              % (args.yaml, len(commands), len(datasets)))
        for key, why in skipped:
            print("  skipped %s (%s)" % (key, why))
    failures = apply_design(args.host, args.port, commands=commands)
    apply_datasets(datasets)
    if failures:
        print("\n%d command(s) failed" % len(failures), file=sys.stderr)
        return 1
    print("\nHEX tomo design applied%s."
          % (" from " + args.yaml if args.yaml else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
