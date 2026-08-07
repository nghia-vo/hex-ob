# Real HEX PandA design: `Tomo_radio_1_config` (recovered 2026-07-30)

Source: live block panels on `xf27id1-panda1.nsls2.bnl.local:8008` (PandA Web Control 3.0),
captured by AJ as saved HTML (`~/Downloads/hex_panda_config/`) with the design loaded,
Health OK / State Ready. This is the design `tomo_flyscan.py` (time mode) runs against —
it supersedes the `hex_tomo_design.py` reconstruction, which must be rewired to match
(tracked in PROGRESS.md).

## Recovered wiring (Priority 1 — trigger topology, complete)

| Block.Field | Value | Meaning |
|---|---|---|
| PCAP.ENABLE | **PCAP.ACTIVE** | arming *is* the gate — no soft bit involved |
| PCAP.GATE | PULSE1.OUT (delay 1) | |
| PCAP.TRIG | PULSE1.OUT (delay 1), edge Rising | one angle capture per pulse |
| PCAP capture opts | TS_START/TS_END/TS_TRIG/GATE_DURATION = Value | |
| PCOMP1.ENABLE | **PCAP.ACTIVE** | enabled by the arm |
| PCOMP1.INP | **CALC1.OUT** (not raw INENC1.VAL!) | CALC1 config still needed (P2) |
| PCOMP1 params | PRE_START=0 START=**-39664** WIDTH=3 STEP=20 **PULSES=1**, Absolute, Positive | fires **once** at the start angle (−39660-offset counts convention confirmed) |
| PULSE1.ENABLE | **ONE** | always armed |
| PULSE1.TRIG | **PCOMP1.OUT**, edge Rising | the single PCOMP edge launches the train |
| PULSE1 params | DELAY=0, WIDTH=**0.002 s**, STEP=**0.01333334 s** (75 Hz), PULSES=**1801** | a real 1801-projection scan's numbers (script rewrites these per scan) |
| LUT1.INPA / INPB | PULSE1.OUT / PULSE2.OUT (C–E=ZERO, all Input-Level) | |
| LUT1.FUNC | **A\|B** | camera trigger = tomo train OR radio train |
| TTLOUT1.VAL | **LUT1.OUT** | → Kinetix trigger (cabling to confirm) |
| TTLIN1 | TERM=High-Z; NOT wired into PCAP | camera return likely only counted (COUNTER1, P2) |

## Mechanism ("time mode" = position-armed time train)

```
arm PCAP ──▶ PCAP.ACTIVE=1 ──▶ enables PCOMP1 (and PCAP itself)
motor sweeps ──▶ CALC1.OUT crosses START ──▶ PCOMP1.OUT fires ONCE
   └─▶ PULSE1: N pulses @ STEP seconds
         ├─▶ PCAP.GATE/TRIG: capture angle (CALC2, P2) per pulse
         └─▶ LUT1 (A|B) ─▶ TTLOUT1 ─▶ camera frame per pulse
```

This maps 1:1 onto `deco.Panda` (hex-acq-pyepics `lib_device_control.py`):
`arm()` starts everything; `set_start_encoder_angle()` → PCOMP1.START;
`set_num_pulses("time")` → PULSE1.PULSES; `set_acquire_period()`/`set_angle_step("time")`
→ PULSE1.STEP/WIDTH; `start_trigger()` (BITS.A) is NOT part of this chain.
Position mode presumably flips PCOMP1.PULSES=N / PULSE1.PULSES=1 — same wiring.

## Priority 2/3 values (captured 2026-07-30, second panel batch)

| Block.Field | Value | Meaning |
|---|---|---|
| CALC1.INPA | INENC1.VAL, **TYPEA = -Value** (B–D ZERO, SHIFT 0) | **negates the raw encoder**: CALC1.OUT = −INENC1.VAL = 200·deg − 39660 |
| CALC2.INPA | CALC1.OUT, TYPEA = Value | "angles" = identity of CALC1 (what `get_enc_value` reads, raw) |
| INENC1 | Quadrature, Unsigned Binary, dcard Encoder Monitor | raw VAL runs backwards: 39660 − 200·deg (checked: 3790 ⇒ 179.35°) |
| PULSE3 | ENABLE=ONE, TRIG=**BITS.OUTA**, DELAY 0.1 s, WIDTH 0.1 s | `Panda.start_trigger()` one-shot |
| PULSE2 | ENABLE=**BITS.OUTA**, TRIG=PULSE3.OUT, WIDTH 0.25 s, STEP 0.6 s, PULSES 50 | the radiography train (`set_scan_period`/`set_num_scans`) |
| Positions table | CALC1.OUT (Value, 1/0) · **CALC2.OUT (Value, scale 0.005, offset 198.3)** · INENC1.VAL (Value) · COUNTER1.OUT (**Diff**) | **HDF "Angle" is in DEGREES**: deg = raw×0.005 + 198.3 |
| COUNTER3.OUT | reads 1801 | tallied the last scan's PULSE1 train (config not captured) |
| Sidebar | Save-name box shows "Tingkun_318744"; design Modified flag set | live state is authoritative |

Not captured (diagnostics only, non-blocking): COUNTER1/2/3 configs, TTLOUT2, physical
TTL cabling (TTLOUT1 → which Kinetix input; TTLIN1 source).

## Sim status: REWIRED AND VERIFIED (2026-07-30)

`hex_tomo_design.py` now applies exactly this design (incl. the CALC2 scale/offset →
degrees), the bridge injects the raw backwards convention (deg×−200 + 39660), and all
three `tests/` pass against it — including `DATA:NumCaptured=5` with an HDF `Angle`
dataset in real degrees. Two engine patches were needed: the PCAP.ACTIVE self-enable
and (new) a PULSE-train wakeup fix — `pulse_sim` never returns `edge_ts` as its wakeup,
so trains died after the first edge in the event-driven server (`fpga_sim_server.py`
duck-types the fix in `process_blocks`).
