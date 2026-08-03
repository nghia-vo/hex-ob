"""
HEX beamline motor device definitions (ophyd-async).

Devices defined here:
    - rot_stage  : tomography rotation stage (XF:27IDF-OP:1{MC:5-Ax:4}Mtr)
    - sample_x   : sample tower X-axis        (XF:27IDF-OP:1{SMPL:1-Ax:X1}Mtr)
"""

from ophyd_async.epics.motor import Motor


# ---------------------------------------------------------------------------
# Tomography rotation stage
# ---------------------------------------------------------------------------
rot_stage = Motor(
    "XF:27IDF-OP:1{MC:5-Ax:4}Mtr",
    name="rot_stage",
)

# ---------------------------------------------------------------------------
# Sample tower X-axis  (used to move sample in/out of beam for flat-field)
# ---------------------------------------------------------------------------
sample_x = Motor(
    "XF:27IDF-OP:1{SMPL:1-Ax:X1}Mtr",
    name="sample_x",
)
