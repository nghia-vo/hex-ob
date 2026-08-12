"""
HEX beamline Phantom (Vision Research VEO) detector device (ophyd-async).

This is Jakub Wlodek's ``PhantomDetector`` from
``hextools/src/hextools/detectors/phantom.py`` (written against the
ophyd-async **0.17a4** lock), migrated to the **0.19.x** API this repo runs
on (``hex-profile-collection`` pins ``>=0.19.3,<0.20``).  The device logic —
PV surface, trigger/download semantics, error handling — is Jakub's,
unchanged.  What the migration touches is only the framework glue:

    0.17a4                              0.19.x (this file)
    ----------------------------------  ----------------------------------
    ADArmLogic                          ADAcquireLogic
    PhantomArmLogic.arm()               PhantomAcquireLogic.start_acquiring()
    AreaDetector(arm_logic=,            AreaDetector(*writer_factories,
        path_provider=, writer_type=)       acquire_logic=)
    post-hoc loop over _data_logics     ADWriterFactory(array_description=
        setting description                 <callable>) at construction
        .data_type_signal

The HDF data logic is the shared ``HEXADHDFDataLogic`` (no SWMR, no
flush-now, 60 s close, num_captured baseline wait) — the same HEX storage
workarounds the Kinetix uses, which Jakub's original predates.

Staged in hex-ob per the HXM-1288 workflow; promotes back to
``hextools.detectors.phantom`` together with hextools' own 0.17→0.20
ophyd-async migration.
"""

import asyncio
from collections.abc import Sequence
from typing import Annotated as A

from ophyd_async.core import (
    DEFAULT_TIMEOUT,
    AsyncStatus,
    DetectorTriggerLogic,
    DeviceVector,
    OnOff,
    PathProvider,
    SignalR,
    SignalRW,
    StrictEnum,
    SubsetEnum,
    TriggerInfo,
    YesNo,
    derived_signal_r,
    observe_value,
    set_and_wait_for_other_value,
)
from ophyd_async.epics.adcore import (
    ADAcquireLogic,
    ADBaseDataType,
    ADBaseIO,
    ADWriterFactory,
    AreaDetector,
    NDArrayDescription,
    NDFileHDF5IO,
    NDPluginBaseIO,
)
from ophyd_async.epics.core import (
    PvSuffix,
    epics_signal_r,
    epics_signal_rw,
)

from .detectors import HEXADHDFDataLogic, SettablePathProvider


class PhantomDownloadFrameMode(StrictEnum):
    """Mode for selecting frames to download from the camera."""

    FIRST_AND_LAST = "First and Last"
    UNIVERSAL = "Universal"


class PhantomDownloadSpeed(StrictEnum):
    """Speed for downloading frames from the camera."""

    ONE_GIGABIT = "1 Gb/s"
    TEN_GIGABIT = "10 Gb/s"


class PhantomSettingsSlot(StrictEnum):
    """Settings slots available on the camera for saving and loading configurations."""

    SLOT_1 = "1"
    SLOT_2 = "2"
    SLOT_3 = "3"
    SLOT_4 = "4"
    SLOT_5 = "5"


class PhantomExtSyncType(StrictEnum):
    """Types of external synchronization available for triggering the camera."""

    FREE_RUN = "FREE-RUN"  # Software internal trigger
    FSYNC = "FSYNC"  # External frame sync signal, typically used for edge triggering
    IRIG = "IRIG"
    VIDEO = "VIDEO"


class PhantomTrigEdge(StrictEnum):
    """Trigger edge for external triggering."""

    FALLING = "FALLING"
    RISING = "RISING"


class PhantomReadySignal(StrictEnum):
    """Defines behavior of the camera's ready signal output."""

    RECORDING = "RECORDING"
    TRIGGER = "TRIGGER"


class PhantomAuxPinMode(SubsetEnum):
    """Defines possible modes for camera's auxiliary pins."""

    FSYNCIO = "FSYNCIO"
    STROBE = "STROBE"
    EVENT = "EVENT"
    MEMGATE = "MEMGATE"
    READY = "READY"
    TCOUT = "TCOUT"
    PRETRIG = "PRETRIG"
    AUXTRIG = "AUXTRIG"
    MSTROBE = "MSTROBE"
    AUTOTRIG = "AUTOTRIG"
    SWTRIG = "SWTRIG"
    RECORDING = "RECORDING"


class PhantomFanState(StrictEnum):
    """Defines possible states for the camera's fan."""

    ON = "Fan On"
    QUIET = "Fan Quiet"


class PhantomPixelDataFormat(StrictEnum):
    """Defines the pixel data format for the camera."""

    EIGHT = "8"
    EIGHT_R = "8R"
    P_SIXTEEN = "P16"
    P_SIXTEEN_R = "P16R"
    P_TEN = "P10"
    P_TWELVE_L = "P12L"


class PhantomIO(ADBaseIO):
    """IO class for ADPhantom driver.

    Mirrors API found in ADPhantomApp/Db/phantomCamera.template
    """

    acq_notify: A[SignalR[float], PvSuffix("AcqNotify")]
    acquire_time_ms: A[SignalRW[float], PvSuffix.rbv("AcquireTimeMs")]
    partition_cines: A[SignalRW[int], PvSuffix("PartitionCines")]
    cine_count: A[SignalR[int], PvSuffix("CineCount_RBV")]
    max_frame_count: A[SignalR[int], PvSuffix("MaxFrameCount_RBV")]
    post_trig_frames: A[SignalRW[int], PvSuffix.rbv("PostTrigFrames")]
    auto_advance: A[SignalRW[OnOff], PvSuffix.rbv("AutoAdvance")]
    auto_save: A[SignalRW[OnOff], PvSuffix.rbv("AutoSave")]
    auto_restart: A[SignalRW[OnOff], PvSuffix.rbv("AutoRestart")]
    auto_bref: A[SignalRW[OnOff], PvSuffix.rbv("AutoBref")]
    cine_name: A[SignalRW[str], PvSuffix.rbv("CineName")]
    selected_cine: A[SignalRW[int], PvSuffix.rbv("SelectedCine")]
    cine_frame_count: A[SignalR[int], PvSuffix("CineFrameCount_RBV")]
    cine_first_frame: A[SignalR[int], PvSuffix("CineFirstFrame_RBV")]
    cine_last_frame: A[SignalR[int], PvSuffix("CineLastFrame_RBV")]
    download_start_frame: A[SignalRW[int], PvSuffix.rbv("DownloadStartFrame")]
    download_end_frame: A[SignalRW[int], PvSuffix.rbv("DownloadEndFrame")]
    download_start_cine: A[SignalRW[int], PvSuffix.rbv("DownloadStartCine")]
    download_end_cine: A[SignalRW[int], PvSuffix.rbv("DownloadEndCine")]
    download_frame_mode: A[
        SignalRW[PhantomDownloadFrameMode], PvSuffix.rbv("DownloadFrameMode")
    ]
    download_speed: A[SignalRW[PhantomDownloadSpeed], PvSuffix.rbv("DownloadSpeed")]
    dropped_packets: A[SignalR[int], PvSuffix("DroppedPackets_RBV")]
    mark_cine_saved: A[SignalRW[YesNo], PvSuffix.rbv("MarkCineSaved")]
    download: A[SignalRW[int], PvSuffix("Download")]
    abort_download: A[SignalRW[int], PvSuffix("AbortDownload")]
    delete_start_cine: A[SignalRW[int], PvSuffix.rbv("DeleteStartCine")]
    delete_end_cine: A[SignalRW[int], PvSuffix.rbv("DeleteEndCine")]
    download_count: A[SignalR[int], PvSuffix("DownloadCount_RBV")]
    delete: A[SignalRW[int], PvSuffix("Delete")]
    send_software_trigger: A[SignalRW[int], PvSuffix("SendSoftwareTrigger")]
    perform_csr: A[SignalRW[int], PvSuffix("PerformCSR")]
    csr_count: A[SignalR[int], PvSuffix("CSRCount_RBV")]
    settings_save: A[SignalRW[int], PvSuffix("SettingsSave")]
    settings_load: A[SignalRW[int], PvSuffix("SettingsLoad")]
    settings_slot: A[SignalRW[PhantomSettingsSlot], PvSuffix.rbv("SettingsSlot")]
    preview: A[SignalRW[int], PvSuffix.rbv("Preview")]
    sensor_temp: A[SignalR[int], PvSuffix("SensorTemp_RBV")]
    thermo_power: A[SignalR[int], PvSuffix("ThermoPower_RBV")]
    camera_temp: A[SignalR[int], PvSuffix("CameraTemp_RBV")]
    fan_power: A[SignalR[int], PvSuffix("FanPower_RBV")]
    ext_sync_type: A[SignalRW[PhantomExtSyncType], PvSuffix.rbv("ExtSyncType")]
    edr: A[SignalRW[int], PvSuffix.rbv("EDR")]
    frame_delay: A[SignalRW[int], PvSuffix.rbv("FrameDelay")]
    trigger_edge: A[SignalRW[PhantomTrigEdge], PvSuffix.rbv("TriggerEdge")]
    trigger_filter: A[SignalRW[int], PvSuffix.rbv("TriggerFilter")]
    ready_signal: A[SignalRW[PhantomReadySignal], PvSuffix.rbv("ReadySignal")]
    quiet_fan: A[SignalRW[PhantomFanState], PvSuffix.rbv("QuietFan")]
    sync_clock: A[SignalRW[int], PvSuffix("SyncClock")]
    select_pixel_data_format: A[
        SignalRW[PhantomPixelDataFormat], PvSuffix.rbv("SelectPixelDataFormat")
    ]
    frame_read_speed: A[SignalR[int], PvSuffix("FrameReadSpeed_RBV")]
    invalid: A[SignalR[int], PvSuffix("State_RBV.B0")]
    complete_and_valid: A[SignalR[int], PvSuffix("State_RBV.B1")]
    waiting_for_trigger: A[SignalR[int], PvSuffix("State_RBV.B2")]
    trigger_received: A[SignalR[int], PvSuffix("State_RBV.B3")]
    cine_content_saved: A[SignalR[int], PvSuffix("State_RBV.B9")]

    def __init__(self, prefix: str, name: str = ""):
        super().__init__(prefix, name=name)
        # The real IOC (and the sim, byte-identical db) serves pins 1/2/4 only —
        # there is no Aux3PinMode record — and the setpoint mbbo carries 12
        # choices while the _RBV mbbi carries 16 (adds CEVENT/CMEMGATE/CFSYNC/
        # CPRETRIG), so a paired rw_rbv signal can never connect. Setpoint and
        # readback are therefore separate signals, the readback as str so the
        # C-states remain readable.
        self.aux_pins = DeviceVector(
            {
                i: epics_signal_rw(
                    PhantomAuxPinMode, prefix + f"Aux{i}PinMode", name=f"aux_pin{i}"
                )
                for i in (1, 2, 4)
            },
            name="aux_pins",
        )
        self.aux_pins_rbv = DeviceVector(
            {
                i: epics_signal_r(
                    str, prefix + f"Aux{i}PinMode_RBV", name=f"aux_pin{i}_rbv"
                )
                for i in (1, 2, 4)
            },
            name="aux_pins_rbv",
        )

        # IOC does not provide these signals, so make them derived here
        self.total_download_frames = derived_signal_r(
            self.get_total_downloaded_frames,
            start=self.download_start_frame,
            end=self.download_end_frame,
        )
        self.download_data_type = derived_signal_r(
            self.get_downloaded_dtype, pixel_token=self.select_pixel_data_format
        )

    def get_downloaded_dtype(
        self, pixel_token: PhantomPixelDataFormat
    ) -> ADBaseDataType:
        """Convert the pixel data format token to the corresponding ADBaseDataType.

        Parameters
        ----------
        pixel_token : PhantomPixelDataFormat
            The pixel data format token read from the camera.

        Returns
        -------
        ADBaseDataType
            The corresponding ADBaseDataType to use for the downloaded frames.
        """
        if pixel_token in (
            PhantomPixelDataFormat.EIGHT,
            PhantomPixelDataFormat.EIGHT_R,
        ):
            return ADBaseDataType.UINT8
        return ADBaseDataType.UINT16

    def get_total_downloaded_frames(self, start: int, end: int) -> int:
        """Get the number of frames to download based on start/end frame indices.

        Parameters
        ----------
        start : int
            The starting frame index for the download.
        end : int
            The ending frame index for the download.

        Returns
        -------
        int
            The total number of frames to download, calculated as end - start + 1.
            If end is less than start, returns 0.
        """
        if end < start:
            return 0

        return end - start + 1


class PhantomTriggerLogic(DetectorTriggerLogic):
    """Trigger logic for the Phantom camera."""

    def __init__(self, driver: PhantomIO):
        self.driver = driver

    def config_sigs(self) -> set[SignalR]:
        """Return the signals that should appear in read_configuration.

        Returns
        -------
        set[SignalR]
            The set of signals to include in the configuration.
        """
        return {
            self.driver.acquire_time_ms,
            self.driver.ext_sync_type,
            self.driver.model,
            self.driver.manufacturer,
            self.driver.post_trig_frames,
            self.driver.cine_name,
            self.driver.selected_cine,
            self.driver.download_start_frame,
            self.driver.download_end_frame,
            self.driver.select_pixel_data_format,
        }

    async def setup_download(self, num: int):
        """Given a total number of frames to download, set up the start/end indices.

        If post trigger frames are greater than or equal to the total number of frames
        to download, then the download will be set up to download all frames from the
        event trigger until num. If post trigger frames are less than the total number
        of frames to download, then the download will be set up to download the
        specified number of post trigger frames, and the remaining pre trigger frames.

        Parameters
        ----------
        num : int
            The total num of frames to download, incl both pre and post trigger frames.

        Raises
        ------
        ValueError
            If num is less than or equal to 0.
        """
        if num <= 0:
            raise ValueError("Number of frames to download must be greater than 0!")

        post_trig_frames = await self.driver.post_trig_frames.get_value()

        if num < post_trig_frames:
            await asyncio.gather(
                self.driver.download_start_frame.set(0),
                self.driver.download_end_frame.set(num - 1),
            )
        else:
            await asyncio.gather(
                self.driver.download_start_frame.set((num - post_trig_frames) * -1),
                self.driver.download_end_frame.set(post_trig_frames - 1),
            )

    async def prepare_internal(self, num: int, livetime: float, deadtime: float):
        """Prepare the detector to take internally triggered exposures.

        Parameters
        ----------
        num : int
            The total num of frames to download, incl both pre and post trigger frames.
        livetime : float
            How long the exposure should be, in s, 0 means what is currently set.
        deadtime : float
            How long between exposures, in s, 0 means the shortest possible.
        """
        coros = [self.driver.ext_sync_type.set(PhantomExtSyncType.FREE_RUN)]
        if livetime != 0:
            coros.append(self.driver.acquire_time_ms.set(livetime * 1000))
        await asyncio.gather(*coros)

    async def prepare_edge(self, num: int, livetime: float):
        """Prepare the detector to take external edge triggered exposures.

        Parameters
        ----------
        num : int
            The total num of frames to download, incl both pre and post trigger frames.
        livetime : float
            How long the exposure should be, in s, 0 means what is currently set.
        """
        coros = [self.driver.ext_sync_type.set(PhantomExtSyncType.FSYNC)]
        if livetime != 0:
            coros.append(self.driver.acquire_time_ms.set(livetime * 1000))
        await asyncio.gather(*coros)

    async def default_trigger_info(self) -> TriggerInfo:
        """Fallback for the default TriggerInfo in plans without prepare.

        Returns
        -------
        TriggerInfo
            A TriggerInfo with default values for the Phantom camera.
        """
        dl_start = await self.driver.download_start_frame.get_value()
        dl_end = await self.driver.download_end_frame.get_value()
        return TriggerInfo(collections_per_event=(dl_end - dl_start + 1))


class PhantomAcquireLogic(ADAcquireLogic):
    """Acquire logic for the Phantom camera.

    0.17's ``PhantomArmLogic``: ``arm()`` is now ``start_acquiring()``, the
    name the 0.19 ``DetectorAcquireLogic`` protocol calls.  The body — start
    acquisition, wait for the event trigger, wait for the post-trigger
    frames, start the download — is unchanged.
    """

    def __init__(self, driver: PhantomIO):
        super().__init__(driver, driver.acquire)
        self.driver = driver

    async def start_acquiring(self):
        """Arm the camera; the rest of the flow runs in ``acquire_status``.

        Per the 0.19 ``DetectorAcquireLogic`` contract this must RETURN once
        the camera is armed — ``prepare()`` awaits it directly, and for
        external triggers the event only arrives during the sweep the plan
        starts AFTER prepare returns, so blocking here on the trigger is a
        deadlock (found against the sim tier, 2026-08-12; the soft-trigger
        plans masked it because ``bps.trigger(wait=False)`` hides the block
        inside a status). The trigger-wait → post-trig count → download flow
        is stashed as ``self.acquire_status``, which ``wait_for_idle`` /
        ``complete`` await — the base-class shape.
        """
        # Start the acquisition, and wait for waiting for trigger to be True.
        # wait_for_set_completion=False is LOAD-BEARING: Acquire is a busy
        # record whose put-completion only fires when acquisition ENDS, so
        # the default (True) deadlocks the arm against the trigger it is
        # arming for — the same reason the ADAcquireLogic base passes False.
        await set_and_wait_for_other_value(
            self.driver.acquire,
            True,
            self.driver.waiting_for_trigger,
            1,
            timeout=DEFAULT_TIMEOUT,
            wait_for_set_completion=False,
        )
        self.acquire_status = AsyncStatus(self._acquire_flow())

    async def _acquire_flow(self):
        """Wait for the event trigger, the post-trigger frames, then download.

        Raises
        ------
        RuntimeError
            If acquisition stops before we receive the event trigger.
        TimeoutError
            If we timeout waiting for the cine to be marked as valid.
        ValueError
            If the number of post trigger frames recorded is not what was expected.
        """
        # Wait for trigger_received to go to 1. If trigger_received does not go
        # to 1 within the timeout, check if acquisition stopped, and if so raise
        # a timeout error indicating acquisition stopped while waiting for trigger.
        # Otherwise, if acq is still running, keep waiting for trigger_received to
        # go to 1.
        while True:
            try:
                async for trigger_received in observe_value(
                    self.driver.trigger_received, done_timeout=DEFAULT_TIMEOUT
                ):
                    if trigger_received == 1:
                        break
                break
            except TimeoutError as exc:
                acquiring = await self.driver.acquire.get_value()
                if not acquiring:
                    raise RuntimeError(
                        "Acquisition stopped while waiting for event trigger!"
                    ) from exc

        # After we receive the event trigger, we want to wait until the number of
        # post trigger frames have been recorded before we start the download.
        target_post_trig = await self.driver.post_trig_frames.get_value()
        try:
            async for actual_post_trig in observe_value(
                self.driver.array_counter, done_timeout=DEFAULT_TIMEOUT
            ):
                if target_post_trig == actual_post_trig:
                    break
        except TimeoutError as exc:
            (
                actual_post_trig,
                complete_and_valid,
                trigger_received,
            ) = await asyncio.gather(
                self.driver.array_counter.get_value(),
                self.driver.complete_and_valid.get_value(),
                self.driver.trigger_received.get_value(),
            )
            if trigger_received == 1 and complete_and_valid != 1:
                raise TimeoutError(
                    "Received event trigger, but writing to cine was not completed!"
                ) from exc
            elif actual_post_trig != target_post_trig:
                raise ValueError(
                    f"Expected number of post trig frames {target_post_trig} "
                    f"does not match actual number {actual_post_trig}"
                ) from exc

        # Start the download and watch it to completion HERE, in the same
        # task: subscribing in a separate later coroutine (as wait_for_idle
        # once did) is a race — a fast download (small frame count, 1G
        # wire) completes and the driver resets the counter to 0 before
        # the watcher subscribes, and the wait then times out on a
        # download that succeeded (found against the sim tier, 2026-08-12).
        await self._download_and_watch()

    async def wait_for_idle(self):
        """Wait for the camera to finish downloading and return to idle state.

        Raises
        ------
        TimeoutError
            If we timeout waiting for the download to complete.
        """
        # The acquire task triggers the download and watches it to
        # completion (_download_and_watch), so its status ending means the
        # camera is idle again.
        if self.acquire_status:
            await self.acquire_status

    async def _download_and_watch(self):
        """Start the RAM download and watch the frame counter to completion.

        Subscribe-then-trigger in one task: the first ``anext()``
        establishes the monitor (yielding the current value), so no counter
        update can be missed however fast the download completes.

        Raises
        ------
        TimeoutError
            If we timeout waiting for the download to complete.
        """
        # Check how many frames we are supposed to download
        target_num_saved = await self.driver.total_download_frames.get_value()

        observation = observe_value(
            self.driver.download_count, done_timeout=DEFAULT_TIMEOUT
        )
        last_value = await anext(observation)
        await self.driver.download.set(True)

        # As long as our download counter is counting up and has not reached the target
        # number of frames, keep waiting. If we timeout, check if the download count
        # has increased since the last time we checked, and if so keep waiting,
        # otherwise raise a timeout error.
        while True:
            try:
                async for num_saved in observation:
                    if num_saved == target_num_saved:
                        return
                    # The driver zeroes DownloadCount when readoutDataStream
                    # finishes, and CA monitor coalescing on a fast download
                    # can skip the final per-frame update — so a reset to 0
                    # after observed progress IS completion, not a restart.
                    if num_saved == 0 and last_value not in (None, 0):
                        return
                    last_value = num_saved
            except TimeoutError as err:
                current = await self.driver.download_count.get_value()
                if current == last_value:
                    raise TimeoutError(
                        "Timeout waiting for download to complete! "
                        f"Target number of downloaded frames: {target_num_saved}"
                    ) from err
                if current == target_num_saved:
                    return
                last_value = current


class PhantomDetector(AreaDetector[PhantomIO]):
    """Detector class for Phantom cameras."""

    def __init__(
        self,
        prefix: str,
        path_provider: PathProvider,
        driver_suffix: str = "cam1:",
        plugins: dict[str, NDPluginBaseIO] | None = None,
        config_sigs: Sequence[SignalR] = (),
        name: str = "",
    ) -> None:
        driver = PhantomIO(prefix + driver_suffix)
        super().__init__(
            driver,
            prefix,
            ADWriterFactory(
                writer_cls=NDFileHDF5IO,
                writer_suffix="HDF1:",
                writer_name="hdf",
                datakey_suffix="",
                # The downloaded frames' dtype follows the selected pixel
                # format, not the driver's DataType_RBV — describe them via
                # the derived download_data_type signal (the 0.19 form of the
                # original's post-hoc data_type_signal override).
                array_description=lambda drv: NDArrayDescription(
                    shape_signals=[
                        drv.array_size_z,
                        drv.array_size_y,
                        drv.array_size_x,
                    ],
                    data_type_signal=drv.download_data_type,
                    color_mode_signal=drv.color_mode,
                ),
                data_logic_factory=lambda writer, desc, drv, plgs: HEXADHDFDataLogic(
                    array_description=desc,
                    path_provider=path_provider,
                    driver=drv,
                    writer=writer,
                    plugins=list(plgs),
                    datakey_suffix="",
                ),
            ),
            acquire_logic=PhantomAcquireLogic(driver),
            trigger_logic=PhantomTriggerLogic(driver),
            plugins=plugins,
            config_sigs=config_sigs,
            name=name,
        )


def make_phantom(
    path_provider: SettablePathProvider | None = None,
) -> PhantomDetector:
    """
    Instantiate the HEX Phantom (``XF:27ID1-ES{Phantom-Det:1}``).

    Parameters
    ----------
    path_provider:
        Decides where HDF files are written.  Defaults to a fresh
        ``SettablePathProvider`` so plans can retarget it per scan; it is
        also exposed as ``detector.path_provider`` (see
        ``lib.detectors.set_output_dir``).
    """
    if path_provider is None:
        path_provider = SettablePathProvider()

    detector = PhantomDetector(
        "XF:27ID1-ES{Phantom-Det:1}",
        path_provider=path_provider,
        name="phantom1",
    )
    # Not an ophyd-async child device — just a handle so plans can retarget
    # the output directory (same pattern as make_kinetix).
    detector.path_provider = path_provider
    return detector
