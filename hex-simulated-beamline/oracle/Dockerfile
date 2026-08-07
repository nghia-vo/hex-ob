# Oracle runner: executes the hex-acq-pyepics scripts VERBATIM against the sim.
#
# Why a container: the scripts hardcode /nsls2/... paths that the host
# deliberately does NOT have (dec: never a real-looking /nsls2 on a host) —
# in here, /nsls2 is a bind of the SIM storage tree, mirroring how beamline
# hosts mount the central store. Build:
#   docker build -t hexsim-oracle:local oracle/
# Run (see PROGRESS.md bring-up):
#   docker run --rm --network host \
#     -v ~/git_projects/hex-acq-pyepics:/hex-acq-pyepics:ro \
#     -v /tmp/hex-sim-data/nsls2:/nsls2 \
#     -e EPICS_CA_AUTO_ADDR_LIST=NO \
#     -e "EPICS_CA_ADDR_LIST=127.0.0.1:5064 127.0.0.1:5075 127.0.0.1:5085 127.0.0.1:5095" \
#     hexsim-oracle:local \
#     python /hex-acq-pyepics/techniques/tomography/kinetix/tomo_flyscan.py -n 50 -e 0.02
# Pinned to the beamline's 2023-era conda env vintage (the scripts' shebang is
# /nsls2/conda/envs/2023-2.0-py310-tiled): modern h5py (>=3.9) rejects the
# str-for-bytes dtype pattern lib_loader_saver.save_nxs_metadata uses.
FROM python:3.10-slim
RUN pip install --no-cache-dir pyepics "numpy==1.24.*" "h5py==3.8.0" \
        "pillow==9.5.0" "matplotlib==3.7.*"
ENV MPLBACKEND=Agg
