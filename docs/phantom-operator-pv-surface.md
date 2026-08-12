# Phantom operator PV surface — from the HEX Phoebus screens

Recon for the simulated beamline's Phantom tier: the PVs the **operator**
actually drives, extracted from the ADPhantom Phoebus screen set that HEX's
CS-Studio launcher opens for `XF:27ID1-ES{Phantom-Det:1}cam1:`.

- Source: `cs-studio-xf/ADet/phoebus/ADPhantom/*.bob` (macros from
  `cs-studio-xf/27id/main.bob`, read 2026-08-11).
- Extraction: `grep -h -o "<pv_name>[^<]*</pv_name>" *.bob` per file,
  deduplicated, cine indices collapsed to `C<n>:`.
- `$(P)$(R)` = `XF:27ID1-ES{Phantom-Det:1}` + `cam1:` at HEX.

This is the *operator* surface. The *plan* surface (what
`plans/phantom/` + `lib/phantom.py` drive) is a subset; a digital-twin sim
must serve both — which is the strongest argument for running the real
ADPhantom IOC against a fake camera rather than spoofing PVs: the screens
then work against the sim unmodified.

## Findings that were NOT in the ophyd device or the pyepics scripts

- **Per-cine state words**: every cine partition has its own
  `C<n>:State_RBV`; the overview screen polls bits B0/B1/B4/B8/B9 for up
  to **63 cines**, the details screen all of B0–B9. (The ophyd device only
  reads the camera-level `State_RBV` bits B0–B3, B9.)
- **`TotalFrameCount_RBV` ≠ `ArrayCounter_RBV`** — both exist and both are
  displayed. The legacy scripts wait on TotalFrameCount for post-trigger
  frames; the ophyd device waits on ArrayCounter. Which counts what is a
  driver-semantics question for the ADPhantom source.
- **Auto-trigger block**: `AutoTriggerMode/X/Y/W/H/Area/Thresh/Interval` —
  image-based (motion-detect) event triggering, operator-configurable.
- **Connection management**: `CONNECT` / `CONNECTED_RBV` — operators can
  drop and re-establish the camera link; a fake camera must survive it.
- **Cine-range download** (`DownloadStartCine`/`EndCine`, frame mode,
  speed, `AbortDownload`, `MarkCineSaved`) and a **guarded delete /
  repartition workflow** (dedicated confirm dialog — repartitioning is
  destructive).

## PVs by screen

### phantomTop.bob (main control)
AutoAdvance(+RBV) · AutoBref(+RBV) · AutoRestart(+RBV) · CineName(+RBV) ·
CONNECT · CONNECTED_RBV · CSRCount_RBV · MaxFrameCount_RBV · PerformCSR ·
PostTrigFrames(+RBV) · Preview(+RBV) · SelectedCine(+RBV) ·
SendSoftwareTrigger

### phantomBase.bob (ADBase area)
Acquire · AcqNotify · AcquireTime/AcquirePeriod(+RBV, .DISA gating) ·
ArrayCounter_RBV · ArraySize[_X_Y]_RBV · Bin/Min/Size/Reverse X/Y (+RBV,
.DISA) · ColorMode · DataType · DetectorState_RBV · Gain(+RBV) ·
Manufacturer/Model_RBV · MaxSizeX/Y_RBV · PortName_RBV ·
State_RBV.B1/.B2/.B3 · StatusMessage_RBV · TotalFrameCount_RBV

### phantomDetails.bob (advanced)
AutoRestart/AutoSave(+RBV) · AutoTrigger{Mode,X,Y,W,H,Area,Thresh,
Interval}(+RBV) · Aux1/2/4PinMode(+RBV) · CameraTemp/SensorTemp/FanPower/
ThermoPower_RBV · EDR(+RBV) · ExtSyncType(+RBV) · FrameDelay(+RBV) ·
QuietFan(+RBV) · ReadySignal(+RBV) · SettingsSave/Load/Slot(+RBV) ·
SyncClock · TriggerEdge(+RBV) · TriggerFilter(+RBV)

### phantomCine.bob (partition overview)
CineCount_RBV · C\<n\>:State_RBV.B0/.B1/.B4/.B8/.B9 (n = 1..63)

### phantomCineDetails.bob (one partition)
C\<n\>:{FirstFrame,LastFrame,FrameCount,Width,Height,Name}_RBV ·
C\<n\>:State_RBV.B0–B9 · Download · AbortDownload · DownloadCount_RBV ·
DownloadStart/EndFrame(+RBV) · DownloadSpeed · DroppedPackets_RBV ·
SelectPixelDataFormat

### phantomDownload.bob
Download · AbortDownload · DownloadCount_RBV ·
DownloadStart/EndFrame(+RBV) · DownloadStart/EndCine(+RBV) ·
DownloadFrameMode · DownloadSpeed · DroppedPackets_RBV ·
FrameReadSpeed_RBV · MarkCineSaved · SelectPixelDataFormat ·
StatusMessage_RBV

### phantomDelete.bob
Delete · DeleteStart/EndCine(+RBV) · StatusMessage_RBV

### phantomPartitionConfirm.bob
CineCount_RBV · PartitionCines

## Driver lineage (recon 2026-08-11)

The deployed ADPhantom is a fork of Diamond's **miroCamera** driver
(github: dls-controls miroCamera; local clone `~/git_projects/miroCamera`;
jwlodek has merges there). Key deltas found so far:

- Config renamed and extended: `miroCameraConfig(port, ctrl, data, ...)` →
  `ADPhantomConfig(port, ctrl, data, MAC, interface, ...)`.
- The fork ADDED `DownloadStartFrame/EndFrame` (the frame-window download
  the ophyd device and plans drive) and the `AutoTrigger*` block — neither
  exists in the Diamond ancestor.
- **A camera protocol simulator already exists**: `miroCamera/sim/SimServer.py`
  — a TCP server speaking the PH16 wire protocol (Python 2; predates the
  fork's added features). Prime starting point for the sim tier's fake
  camera.
- Counter semantics in the ancestor: `TotalFrameCount_RBV` = the camera's
  own cine frame count (`c<n>.frcount` over the wire — meaningful during
  recording); `ArrayCounter_RBV` = areaDetector NDArray counter (advances
  when frames flow through the pipeline, i.e. preview/download). Whether
  the fork changed this decides which PV the ophyd device should watch for
  post-trigger frames — settle from the deployed source (rsync from the
  IOC host).

## Deployed-source verdict (rsync from xf27id1-det1, 2026-08-11)

Snapshots on this machine: `~/git_projects/phantom-det1-ioc-snapshot`
(IOC dir incl. `records.dbl`, 8328 live records) and
`~/git_projects/ADPhantom-deployed` (`/epics/modules/adphantom_329598e`,
"ADPhantom" for the Phantom T2410, README credits miroCamera as base).

- **Counter question SETTLED — the ophyd device is correct.** During
  recording the fork's status poll sets `ArrayCounter_RBV = c<n>.lastfr + 1`,
  which (trigger-relative frame numbering, `lastFrame > 0` guard) IS the
  post-trigger frame count: 0 until the event, then counting up. So
  `PhantomAcquireLogic` waiting on `array_counter == post_trig_frames` is
  right. `TotalFrameCount_RBV = c<n>.frcount` (pre+post total).
- **Cine state tokens** the driver parses from `c<n>.state`: `WTR`
  (waiting for trigger), `TRG` (triggered), `ACT` (active), `STR`
  (stored) — the state machine the sim server must walk.
- **Template delta vs ancestor**: fork adds 52 records — the whole
  RAM-download block (frame + cine windows, mode, speed, count, abort,
  MarkCineSaved), the AutoTrigger block, Aux1/2/4 per-pin modes, Delete
  block, SelectPixelDataFormat, DroppedPackets, FrameReadSpeed, QuietFan;
  drops the ancestor's `Record*`/`SaveToCF` (CompactFlash) workflow.
- **Sim server NOT modernized in the fork**: `sim/SimServer.py` is
  byte-identical to the ancestor's (Python 2, 411 lines, PH16 parameter
  dictionary inline; predates the fork's download-window/auto-trigger
  features). Porting + extending it is the sim-tier work.
- **HEX deployment values** (`phantom-det1.yml`): prefix
  `XF:27ID1-ES{Phantom-Det:1}`, `NUM_CINES: 63` (matches the screens),
  camera at `100.100.214.107` on `ens10f0`.
- Live plugin chain (records.dbl): Over1, Stats1–5, Proc1, ROIStat1,
  HDF1, Attr1, … — the sim's IOC must load the same chain.
