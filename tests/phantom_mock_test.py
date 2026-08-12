"""
Phantom device mock tests: the 0.19.x migration of Jakub Wlodek's
PhantomDetector (lib/phantom.py) exercised against ophyd-async mock signal
backends — zero EPICS, zero containers.

Ports the upstream unit suite (hextools tests/detectors/test_phantom.py,
written for the 0.17a4 lock) to this repo's script-style mock tier:
dtype/frame-count derivations, trigger-logic download setup, every
acquire-logic error path plus the success paths, describe() shape/dtype per
pixel format, and a full bp.count stack run asserting the emitted documents.
(The upstream suite's tiled read-back stays upstream — the mock tier here
has no tiled.)

Run from the hex-ob root (same path CI uses):
    pixi run test-mock
Or directly:
    python tests/phantom_mock_test.py
"""

import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import bluesky.plans as bp
from bluesky.run_engine import RunEngine
from ophyd_async.core import (
    DetectorTrigger,
    StaticFilenameProvider,
    StaticPathProvider,
    TriggerInfo,
    init_devices,
)
from ophyd_async.epics.adcore import ADBaseDataType

from mock_beamline import callback_on_mock_put, set_mock_value

import lib.phantom as phantom_module
from lib.phantom import (
    PhantomAcquireLogic,
    PhantomDetector,
    PhantomIO,
    PhantomPixelDataFormat,
    PhantomTriggerLogic,
)

SHORT_TIMEOUT = 0.1


def expect_raises(exc_type, match: str, coro):
    """asyncio.run *coro*, asserting it raises *exc_type* mentioning *match*."""
    try:
        asyncio.run(coro)
    except exc_type as exc:
        assert match in str(exc), f"expected {match!r} in {exc}"
        return
    raise AssertionError(f"expected {exc_type.__name__}({match!r}), none raised")


def make_mock_phantom(write_path: Path, name: str = "phantom") -> PhantomDetector:
    with init_devices(mock=True):
        phantom = PhantomDetector(
            "TEST:PHANTOM",
            path_provider=StaticPathProvider(
                StaticFilenameProvider("scan"), write_path
            ),
            name=name,
        )
    set_mock_value(phantom.hdf.file_path_exists, True)
    return phantom


def test_io_derivations(io: PhantomIO) -> None:
    for fmt in PhantomPixelDataFormat:
        expected = (
            ADBaseDataType.UINT8
            if fmt in (PhantomPixelDataFormat.EIGHT, PhantomPixelDataFormat.EIGHT_R)
            else ADBaseDataType.UINT16
        )
        assert io.get_downloaded_dtype(fmt) == expected, fmt
    for start, end, expected_num in (
        (0, 1, 2), (-15, 10, 26), (5, 15, 11), (10, 10, 1), (15, 5, 0),
    ):
        got = io.get_total_downloaded_frames(start, end)
        assert got == expected_num, (start, end, got)
    print("PASS  IO derivations (download dtype + frame count)")


def test_trigger_logic(logic: PhantomTriggerLogic) -> None:
    config_sigs = logic.config_sigs()
    assert isinstance(config_sigs, set) and len(config_sigs) == 10
    assert logic.driver.acquire_time_ms in config_sigs

    for bad_num in (0, -1, -10):
        expect_raises(
            ValueError, "must be greater than 0", logic.setup_download(bad_num)
        )

    async def check(post_trig, num, expected_start, expected_end):
        set_mock_value(logic.driver.post_trig_frames, post_trig)
        await logic.setup_download(num)
        start = await logic.driver.download_start_frame.get_value()
        end = await logic.driver.download_end_frame.get_value()
        assert (start, end) == (expected_start, expected_end), (start, end)

    for case in ((1, 10, -9, 0), (5, 10, -5, 4), (10, 10, 0, 9), (15, 10, 0, 9)):
        asyncio.run(check(*case))
    print("PASS  trigger logic (config_sigs + setup_download)")


def test_acquire_logic(driver: PhantomIO) -> None:
    logic = PhantomAcquireLogic(driver)

    def reset():
        set_mock_value(driver.acquire, False)
        set_mock_value(driver.waiting_for_trigger, 0)
        set_mock_value(driver.trigger_received, 0)
        set_mock_value(driver.complete_and_valid, 0)
        set_mock_value(driver.array_counter, 0)
        set_mock_value(driver.download, 0)
        set_mock_value(driver.download_count, 0)

    async def run_acquire():
        """start_acquiring arms and returns; the trigger-wait -> count ->
        download flow (and its exceptions) live in acquire_status."""
        await logic.start_acquiring()
        await logic.acquire_status

    # -- acquisition stops while waiting for the event trigger --------------
    reset()
    set_mock_value(driver.waiting_for_trigger, 1)

    async def arm_with_acq_stop():
        async def _stop_acquisition():
            await asyncio.sleep(0.05)
            set_mock_value(driver.acquire, False)

        stop_task = asyncio.create_task(_stop_acquisition())
        try:
            await run_acquire()
        finally:
            stop_task.cancel()

    expect_raises(
        RuntimeError, "Acquisition stopped while waiting", arm_with_acq_stop()
    )
    print("PASS  acquire logic: stop-while-waiting-for-trigger raises")

    # -- trigger received but cine write never completes --------------------
    reset()
    set_mock_value(driver.waiting_for_trigger, 1)
    set_mock_value(driver.trigger_received, 1)
    set_mock_value(driver.post_trig_frames, 10)
    expect_raises(
        TimeoutError, "writing to cine was not completed", run_acquire()
    )
    print("PASS  acquire logic: cine-not-completed raises")

    # -- post-trigger frame count mismatch ----------------------------------
    reset()
    set_mock_value(driver.waiting_for_trigger, 1)
    set_mock_value(driver.trigger_received, 1)
    set_mock_value(driver.post_trig_frames, 10)
    set_mock_value(driver.complete_and_valid, 1)
    set_mock_value(driver.array_counter, 5)
    expect_raises(
        ValueError, "does not match actual number", run_acquire()
    )
    print("PASS  acquire logic: post-trig mismatch raises")

    # The download watch (subscribe -> trigger -> count to completion) now
    # lives inside the acquire task (_download_and_watch); mimic the camera
    # by ramping the counter when the Download put lands, gated so the
    # stall test below can leave it dead.
    ramp = {"on": False, "to": 0}

    def _ramp_download(value, **kw):
        if value and ramp["on"]:
            for i in range(1, ramp["to"] + 1):
                set_mock_value(driver.download_count, i)

    callback_on_mock_put(driver.download, _ramp_download)

    # -- success: download started and watched to completion ----------------
    reset()
    set_mock_value(driver.waiting_for_trigger, 1)
    set_mock_value(driver.trigger_received, 1)
    set_mock_value(driver.post_trig_frames, 10)
    set_mock_value(driver.complete_and_valid, 1)
    set_mock_value(driver.array_counter, 10)
    set_mock_value(driver.download_start_frame, 0)
    set_mock_value(driver.download_end_frame, 9)
    ramp.update(on=True, to=10)
    asyncio.run(run_acquire())
    assert asyncio.run(driver.download.get_value())
    print("PASS  acquire logic: success path runs the download to completion")

    # -- download stalls -> timeout ------------------------------------------
    reset()
    set_mock_value(driver.download_start_frame, -5)
    set_mock_value(driver.download_end_frame, 5)
    ramp.update(on=False)
    expect_raises(
        TimeoutError,
        "Target number of downloaded frames: 11",
        logic._download_and_watch(),
    )
    print("PASS  acquire logic: stalled download times out")

    # -- download completes --------------------------------------------------
    reset()
    set_mock_value(driver.download_start_frame, -5)
    set_mock_value(driver.download_end_frame, 5)
    ramp.update(on=True, to=11)  # -5..5 inclusive
    asyncio.run(logic._download_and_watch())
    print("PASS  acquire logic: completed download returns")

    # -- counter reset-to-zero after progress counts as completion ----------
    # (the driver zeroes DownloadCount when readoutDataStream ends; monitor
    # coalescing on a fast download can skip the final per-frame update)
    reset()
    set_mock_value(driver.download_start_frame, -5)
    set_mock_value(driver.download_end_frame, 5)
    ramp.update(on=False)  # silence _ramp_download if both callbacks fire

    def _ramp_with_skip(value, **kw):
        if value:
            set_mock_value(driver.download_count, 7)   # partial progress
            set_mock_value(driver.download_count, 0)   # driver's end reset

    callback_on_mock_put(driver.download, _ramp_with_skip)
    asyncio.run(logic._download_and_watch())
    print("PASS  acquire logic: reset-after-progress is completion")


def test_describe(tmp_dir: Path) -> None:
    for pixel_format, x_size, y_size, expected_shape, expected_dtype in (
        (PhantomPixelDataFormat.EIGHT, 10, 20, [1, 20, 10], "|u1"),
        (PhantomPixelDataFormat.EIGHT_R, 15, 25, [1, 25, 15], "|u1"),
        (PhantomPixelDataFormat.P_TEN, 5, 5, [1, 5, 5], "<u2"),
        (PhantomPixelDataFormat.P_SIXTEEN, 8, 12, [1, 12, 8], "<u2"),
    ):
        phantom = make_mock_phantom(tmp_dir)
        set_mock_value(phantom.driver.select_pixel_data_format, pixel_format)
        set_mock_value(phantom.driver.array_size_x, x_size)
        set_mock_value(phantom.driver.array_size_y, y_size)

        async def prepare_and_describe(det):
            await det.prepare(
                TriggerInfo(
                    trigger=DetectorTrigger.INTERNAL,
                    livetime=0,
                    deadtime=0,
                    exposures_per_collection=1,
                    collections_per_event=1,
                    number_of_events=1,
                )
            )
            return await det.describe()

        desc = asyncio.run(prepare_and_describe(phantom))
        assert desc["phantom"]["shape"] == expected_shape, desc["phantom"]
        assert desc["phantom"]["dtype"] == "array"
        assert desc["phantom"]["dtype_numpy"] == expected_dtype, desc["phantom"]
        assert desc["phantom"]["source"].endswith("scan.h5")
    print("PASS  describe: shape/dtype follow the selected pixel format")


def test_full_stack(tmp_dir: Path) -> None:
    # Keep prepared trigger/download config across the count's stage cycle
    # (same switch the upstream suite sets).
    os.environ["OPHYD_ASYNC_PRESERVE_DETECTOR_STATE"] = "YES"

    RE = RunEngine({})
    phantom = make_mock_phantom(tmp_dir)
    docs_cache: dict[str, list] = {}
    RE.subscribe(lambda name, doc: docs_cache.setdefault(name, []).append(doc))

    set_mock_value(phantom.driver.select_pixel_data_format,
                   PhantomPixelDataFormat.P_TEN)
    set_mock_value(phantom.driver.array_size_x, 4)
    set_mock_value(phantom.driver.array_size_y, 3)
    set_mock_value(phantom.driver.post_trig_frames, 15)
    set_mock_value(phantom.driver.download_start_frame, -5)
    set_mock_value(phantom.driver.download_end_frame, 5)

    def _on_acquire(value, **kwargs):
        if value:
            set_mock_value(phantom.driver.waiting_for_trigger, 1)
            set_mock_value(phantom.driver.trigger_received, 1)
            set_mock_value(phantom.driver.array_counter, 15)
            set_mock_value(phantom.driver.complete_and_valid, 1)

    def _on_download(value, **kwargs):
        if value:
            for count in range(1, 12):
                set_mock_value(phantom.driver.download_count, count)
                set_mock_value(phantom.hdf.num_captured, count)

    callback_on_mock_put(phantom.driver.acquire, _on_acquire)
    callback_on_mock_put(phantom.driver.download, _on_download)

    RE(bp.count([phantom]))

    for doc_name in ("start", "descriptor", "event", "stream_resource",
                     "stream_datum", "stop"):
        assert doc_name in docs_cache, f"no {doc_name} document emitted"
        assert len(docs_cache[doc_name]) == 1, (doc_name, len(docs_cache[doc_name]))
    assert docs_cache["stop"][0]["exit_status"] == "success"

    desc = docs_cache["descriptor"][0]["data_keys"]["phantom"]
    # 11 frames per event: download window -5..5 via default_trigger_info.
    assert desc["shape"] == [11, 3, 4], desc
    assert desc["dtype"] == "array"
    assert desc["dtype_numpy"] == "<u2"
    assert desc["source"].endswith("scan.h5")

    sres = docs_cache["stream_resource"][0]
    assert sres["uri"] == f"file://localhost{tmp_dir / 'scan.h5'}"
    assert sres["parameters"]["dataset"] == "/entry/data/data"
    assert tuple(sres["parameters"]["chunk_shape"]) == (1, 3, 4)

    datum = docs_cache["stream_datum"][0]
    assert datum["stream_resource"] == sres["uid"]
    assert datum["indices"] == {"start": 0, "stop": 1}
    assert datum["seq_nums"] == {"start": 1, "stop": 2}
    print("PASS  full stack: bp.count emits the full document set")


def main() -> None:
    phantom_module.DEFAULT_TIMEOUT = SHORT_TIMEOUT

    tmp_dir = Path("/tmp/hex-ob-mock/phantom")
    tmp_dir.mkdir(parents=True, exist_ok=True)

    # A RunEngine provides the event loop init_devices connects under, and
    # runs the full-stack plan (same pattern as mock_beamline).
    RE = RunEngine({})
    with init_devices(mock=True):
        io = PhantomIO("TEST:PHANTOM:IO:")

    test_io_derivations(io)
    test_trigger_logic(PhantomTriggerLogic(io))
    test_acquire_logic(io)

    # describe/full-stack use a longer timeout: real waits, mocked instantly.
    phantom_module.DEFAULT_TIMEOUT = 5.0
    test_describe(tmp_dir)
    test_full_stack(tmp_dir)

    print("\nALL PHANTOM MOCK TESTS PASS")


if __name__ == "__main__":
    main()
