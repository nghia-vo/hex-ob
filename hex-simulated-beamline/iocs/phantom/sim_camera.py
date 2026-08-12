#!/usr/bin/env python3
"""Phantom camera protocol simulator — the fake camera behind the REAL
ADPhantom IOC in the simulated HEX beamline (dec: real-IOC + ported
SimServer, 2026-08-11).

Python-3 port of Diamond's miroCamera ``sim/SimServer.py`` (byte-identical
copy ships in the deployed adphantom_329598e module), extended for the
deployed fork's protocol surface. Sources of truth, in order:

- the deployed driver source (``~/git_projects/ADPhantom-deployed``,
  ADPhantomApp/src/ADPhantom.cpp) — command verbs, reply framing
  (newline-terminated both ways, success sentinel ``Ok!``, error prefix
  ``ERR:`` — the ancestor sim's bare ``OK`` would NOT satisfy this driver);
- the live record snapshot (``~/git_projects/phantom-det1-ioc-snapshot``,
  records.dbl, NUM_CINES=63);
- the ancestor sim server (struct-reply format: ``\\t``-indented dict with
  backslash-CRLF line continuations).

Protocol surface the fork's driver uses (recon 2026-08-11):
  get <struct>       struct in {cam, info, defc, auto, irig, meta, c0, c<n>}
  get <a.b[.c]>      dotted single-parameter read   [reply format UNVERIFIED]
  set <a.b> <value>  parameter write
  rec <n>            start recording into cine n (0 = default/current)
  trig               software event trigger
  attach {port:N}    attach the data-stream connection
  img {cine:N, start:S, cnt:C, fmt:TOK}   download frames to the data port
  ximg / time / setrtc / rel <n> / del    acknowledged, minimally modeled

Cine state machine (tokens the driver parses from ``c<n>.state``):
  WTR (waiting for trigger) -> TRG (triggered) -> ACT (active)
  -> STR (stored).  During recording the driver derives:
    ArrayCounter_RBV      = lastfr + 1  (post-trigger frames; 0 pre-trigger)
    TotalFrameCount_RBV   = frcount     (pre+post total)

TODO markers below are honest gaps, not oversights — each needs either the
driver run against this server (the IOC-tier bring-up) or a wire capture
from the real camera:
  - TODO(format): dotted-path get reply framing.
  - TODO(data): real download stream layout (readoutDataStream expects
    per-frame headers + pixels in the fmt token's packing; we send sized
    zero-frames as a placeholder).
  - TODO(timing): recording advances instantly on trig; real cameras pace
    at the programmed rate.

Only ever bind loopback: this is a stand-in for hardware, never a service.
"""

import argparse
import copy
import fcntl
import re
import signal
import socketserver
import sys
import threading
import time

OK = "Ok!"          # PHANTOM_OK_STRING (ADPhantom.h:58)
ERR = "ERR:"        # PHANTOM_ERROR_STRING (ADPhantom.h:59)


# --------------------------------------------------------------------------
# Parameter structures. cam/info bases carried over from the ancestor sim
# (a Miro M310 identity); defc/auto/irig/meta added for the fork's driver,
# keyed off the parameter paths ADPhantom.cpp reads/writes. Cine structs
# (c0 = "current", c1..cN) are generated from CINE_TEMPLATE.
# --------------------------------------------------------------------------

CAM = {
    "syncimg": 0, "master": 0, "tcmode": 0, "trigpol": 1, "trigfilt": 1,
    "frdelay": 0, "startonacq": 1, "aux1mode": 0, "aux2mode": 0,
    "aux4mode": 0, "memgateen": 1, "rtoen": 0, "membpp": 12, "tsformat": 1,
    "longready": 0, "dark": 0, "quiet": 0, "tsetsns": 30, "tsetcam": 50,
    "cines": 63, "timezone": -3600, "lang": "en_US",
}

INFO = {
    "hwver": 8001, "pver": 16, "sver": 1023, "fver": 39,
    "model": "Phantom T2410 (sim)", "sensor": 62, "serial": 17277,
    "memsz": 12288, "maxcines": 64, "name": "hexsim-phantom",
    "xmax": 1280, "ymax": 800, "xinc": 64, "yinc": 8,
    "minfrate": 24, "maxrate": 1000000, "expdead": 427, "minexp": 1000,
    "cinemem": 12181, "snstemp": 30, "tepower": 54, "camtemp": 52,
    "fanpower": 28,
    "features": "bref blk4 burst edr attach earlyimg notify atrig aexp cf quiet shtr",
    "imgformats": "8 8R P16 P16R P10 P12L",
    "setup": "hexsim",
}

# Default-cine settings: what "set defc.*" writes and "get defc" reads.
DEFC = {
    "res": "1280 x 800", "rate": 1000.0, "exp": 5000, "edrexp": 0,
    "ptframes": 100, "frcount": 12181, "shoff": 0, "hqenable": 0,
    "decimation": 1, "format": 0,
}

AUTO = {
    "acqrestart": 0, "bref": 0, "filesave": 0,
    "trigger": {
        "mode": 0, "x": 0, "y": 0, "w": 0, "h": 0,
        "area": 0, "speed": 0, "threshold": 0, "interval": 0,
    },
}

# yearbegin: epoch seconds at Jan-1 that the driver ADDS to each frame's
# timestamp; readoutDataStream stringToInteger()s it BEFORE the first img
# request, so a missing key silently aborts every download (found live,
# 2026-08-12). 0 = timestamps stay at epoch, consistent with the zeroed
# time-stream this sim serves.
IRIG = {"synced": 0, "offset": 0, "yearbegin": 0}

META = {"name": "", "comment": "", "lens": "", "fstop": 0, "flen": 0}

CINE_TEMPLATE = {
    "res": "1280 x 800", "rate": 1000.0, "exp": 5000, "edrexp": 0,
    "ptframes": 0, "frcount": 0, "state": "{ DEF }",
    # Nested substructs the driver reads per-frame as NDArray attributes
    # (c<n>.meta.name/vw/vh, c<n>.cam.trigpol/trigfilt/syncimg) and in
    # updateCine; values mirror the ancestor sim's cine 'cam' block.
    "meta": {"name": "", "vw": 1280, "vh": 800},
    "cam": {"syncimg": 0, "trigpol": 1, "trigfilt": 1},
    "firstfr": 0, "lastfr": 0, "format": 0, "decimation": 1,
    "frsize": 40336,
    "trigtime": {"secs": 0, "frac": 0},
}


def dict_to_response(name, dict_in, level=None):
    """Ancestor sim's struct-reply serializer, kept byte-compatible:
    tab-indented ``key : value`` lines with backslash-CRLF continuations."""
    tabs = "\t" * (level or 0)
    reply = tabs + name + " : {\t\\\r\n"
    new_level = 1 if level is None else level + 1
    for item, value in dict_in.items():
        if isinstance(value, dict):
            reply += dict_to_response(item, value, new_level) + "\t\\\r\n"
        else:
            # Flag lists ("{ WTR ACT }") go UNQUOTED: the driver's
            # parseDataStruc only files a flag-list item through its
            # repeat-terminator special case, which quoting defeats — with
            # quotes, c<n>.state silently never reaches paramMap_ and the
            # State_RBV records stay 0 (found live, 2026-08-12; the ancestor
            # sim quotes them too and carries the same latent defect).
            is_flags = isinstance(value, str) and value.lstrip().startswith("{")
            quote = '"' if isinstance(value, str) and not is_flags else ""
            reply += tabs + "\t" + item + " : " + quote + str(value) + quote + ",\t\\\r\n"
    reply += tabs + "}"
    return reply


class SimCamera:
    """The camera model: parameter store + cine state machine + data port."""

    def __init__(self, num_cines: int = 63):
        self.lock = threading.RLock()
        self.params = {
            "cam": dict(CAM, cines=num_cines),
            "info": dict(INFO),
            "defc": dict(DEFC),
            "auto": copy.deepcopy(AUTO),
            "irig": dict(IRIG),
            "meta": dict(META),
        }
        for i in range(0, num_cines + 1):  # c0 = current/default view
            self.params[f"c{i}"] = copy.deepcopy(CINE_TEMPLATE)
        self.recording_cine = None
        self.data_socket = None

    # -- parameter access ---------------------------------------------------

    def resolve(self, path):
        """Walk a dotted path to (container, leaf_key), or None."""
        parts = path.split(".")
        node = self.params
        for part in parts[:-1]:
            if not isinstance(node, dict) or part not in node:
                return None
            node = node[part]
        if not isinstance(node, dict) or parts[-1] not in node:
            return None
        return node, parts[-1]

    def get(self, name):
        with self.lock:
            if name in self.params:
                return dict_to_response(name, self.params[name])
            hit = self.resolve(name)
            if hit is None:
                return f"{ERR} Parameter {name} not known"
            node, key = hit
            value = node[key]
            # Flag lists unquoted — same driver-parser rule as
            # dict_to_response (see there).
            is_flags = isinstance(value, str) and value.lstrip().startswith("{")
            quote = '"' if isinstance(value, str) and not is_flags else ""
            # TODO(format): single-parameter reply framing unverified against
            # the driver's parser — mirrors the struct line style for now.
            return f"{name} : {quote}{value}{quote}"

    def set(self, name, raw_value):
        with self.lock:
            hit = self.resolve(name)
            if hit is None:
                return f"{ERR} Parameter {name} not known"
            node, key = hit
            current = node[key]
            try:
                if isinstance(current, float):
                    node[key] = float(raw_value)
                elif isinstance(current, int):
                    node[key] = int(float(raw_value))
                else:
                    node[key] = raw_value.strip('"')
            except ValueError:
                return f"{ERR} Bad value for {name}: {raw_value}"
            return OK

    # -- cine state machine -------------------------------------------------

    def rec(self, cine: int):
        """Start recording into cine *cine* (0 = the default cine)."""
        with self.lock:
            name = f"c{cine}" if f"c{cine}" in self.params else "c1"
            c = self.params[name]
            c["ptframes"] = self.params["defc"]["ptframes"]
            c["rate"] = self.params["defc"]["rate"]
            c["exp"] = self.params["defc"]["exp"]
            # Pre-trigger: recording circulates, no post-trigger frames yet.
            c["state"] = "{ WTR ACT }"
            c["frcount"] = 0
            c["firstfr"] = 0
            c["lastfr"] = 0  # driver: lastfr+1 == post-trig count, 0 pre-trig
            self.recording_cine = name
            return OK

    def trig(self):
        """Software event trigger: complete the post-trigger phase.

        TODO(timing): instantaneous — the full post-trigger count lands at
        once. Real cameras record ptframes at the programmed rate first.
        """
        with self.lock:
            if self.recording_cine is None:
                return f"{ERR} Not recording"
            c = self.params[self.recording_cine]
            pt = int(c["ptframes"])
            c["state"] = "{ TRG STR }"
            c["trigtime"] = {"secs": int(time.time()), "frac": 0}
            c["firstfr"] = -max(0, int(c["frcount"]))
            c["lastfr"] = pt - 1
            c["frcount"] = int(c["frcount"]) + pt
            self.recording_cine = None
            return OK

    # -- data stream --------------------------------------------------------

    # fmt token -> bits per pixel, from the driver's format switch
    # (ADPhantom.cpp ~2195: P10/P12L/8/8R/P16).
    _FMT_BITS = {"P10": 10, "P12L": 12, "8": 8, "8R": 8, "P16": 16}

    def _spec_fields(self, spec: str):
        m = re.search(r"cine\s*:\s*(-?\d+).*?start\s*:\s*(-?\d+).*?cnt\s*:\s*(\d+)", spec)
        if not m:
            return None
        cine, start, count = int(m.group(1)), int(m.group(2)), int(m.group(3))
        name = f"c{max(cine, 0)}" if f"c{max(cine, 0)}" in self.params else "c1"
        return name, start, count

    def _pump(self, payload: bytes, count: int):
        sock = self.data_socket

        # Pace at 1G wire speed: the real camera streams over gigabit, so a
        # 1.28 MB frame takes ~10 ms on the wire. Instant delivery is not
        # just unphysical — it coalesces the driver's per-frame
        # DownloadCount monitor updates into nothing.
        frame_s = len(payload) * 8 / 1e9

        def pump():
            try:
                for _ in range(count):
                    sock.sendall(payload)
                    time.sleep(frame_s)
                print(f"pump: sent {count} x {len(payload)} bytes to "
                      f"{sock.getpeername()}", flush=True)
            except OSError as exc:
                print(f"pump: aborted ({exc})", flush=True)

        threading.Thread(target=pump, daemon=True).start()

    def time_stamps(self, spec: str):
        """``time {cine,start,cnt}``: 12 bytes per frame on the data socket
        (short_time_stamp32 — the driver readFrame()s cnt*12 bytes BEFORE
        requesting any image data; an Ok! with no stream stalls the whole
        download). Zero timestamps decode to valid epoch-start values."""
        fields = self._spec_fields(spec)
        if fields is None or self.data_socket is None:
            return f"{ERR} time: no attach / bad spec {spec!r}"
        _, _, count = fields
        self._pump(b"\x00" * 12, count)
        return OK

    def img(self, spec: str):
        """Download request: stream frames to the attached data socket.

        Per-frame byte count is the DRIVER'S read contract, not the cine's
        frsize field: readoutDataStream reads width*height*bits/8 where the
        bits come from the img request's fmt token. Zero bytes are valid
        pixels in every packing, so zero-frames of the right SIZE satisfy
        the full parse-convert-NDArray path.
        """
        fields = self._spec_fields(spec)
        if fields is None or self.data_socket is None:
            return f"{ERR} img: no attach / bad spec {spec!r}"
        name, _, count = fields
        fmt = re.search(r"fmt\s*:\s*(\w+)", spec)
        bits = self._FMT_BITS.get(fmt.group(1) if fmt else "P10", 10)
        w, h = (int(v) for v in self.params[name]["res"].split("x"))
        self._pump(b"\x00" * (w * h * bits // 8), count)
        return OK


class CtrlHandler(socketserver.StreamRequestHandler):
    """One control connection (the driver holds it open)."""

    def handle(self):
        cam = self.server.cam
        while True:
            line = self.rfile.readline()
            if not line:
                return
            command = line.decode("ascii", "replace").strip()
            if not command:
                continue
            reply = self.dispatch(cam, command)
            if not command.startswith("get"):
                # log state-changing traffic (gets are the poll firehose)
                print(f"ctrl: {command}  ->  {reply}", flush=True)
            if reply is None:
                return
            self.wfile.write((reply + "\n").encode("ascii"))

    def dispatch(self, cam, command):
        verb, _, rest = command.partition(" ")
        rest = rest.strip()
        if verb == "get":
            return cam.get(rest)
        if verb == "set":
            name, _, value = rest.partition(" ")
            return cam.set(name, value.strip())
        if verb == "rec":
            return cam.rec(int(rest or 0))
        if verb == "trig":
            return cam.trig()
        if verb == "attach":
            # attach {port:N} — the driver opened the data connection first;
            # we bound it in DataHandler, so just acknowledge.
            return OK if cam.data_socket is not None else f"{ERR} no data connection"
        if verb in ("img", "ximg"):
            return cam.img(rest)
        if verb == "time":
            return cam.time_stamps(rest)
        if verb in ("setrtc", "rel", "del", "bref"):
            return OK  # acknowledged; no deeper model yet
        if verb == "exit":
            return None
        return f"{ERR} Unknown command {command!r}"


class DataHandler(socketserver.BaseRequestHandler):
    """The data-stream connection: registered, then written to by img().

    FIRST-writer-wins: the driver holds one persistent data connection for
    the IOC's lifetime; a later client (a protocol probe, a stray test)
    must NOT steal the stream slot — that silently starves the driver's
    readFrame() and wedges its download thread. The slot frees when its
    owner disconnects (IOC restart), so a reconnecting driver reclaims it.
    """

    def handle(self):
        cam = self.server.cam
        if cam.data_socket is None:
            cam.data_socket = self.request
            print(f"data: {self.client_address} registered", flush=True)
        else:
            print(f"data: {self.client_address} REFUSED slot (driver holds it); "
                  "connection held open unregistered", flush=True)
        # Hold the connection open until the peer closes it.
        while True:
            try:
                if not self.request.recv(1024):
                    break
            except OSError:
                break
        if cam.data_socket is self.request:
            cam.data_socket = None
            print(f"data: {self.client_address} disconnected, slot freed", flush=True)


class ReusableServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True
    cam: SimCamera  # attached after construction (shared by ctrl + data)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--host", default="127.0.0.1",
                        help="bind address (loopback only — never expose)")
    parser.add_argument("--ctrl-port", type=int, default=7115)
    parser.add_argument("--data-port", type=int, default=7116)
    parser.add_argument("--num-cines", type=int, default=63,
                        help="cine partitions (HEX deploys 63)")
    args = parser.parse_args(argv)

    if not args.host.startswith("127."):
        sys.exit("REFUSING to bind non-loopback: this fakes hardware.")

    # Single instance per port pair (armed_gate_bridge lesson: an orphaned
    # helper silently corrupts later runs). flock dies with the process.
    lock = open(f"/tmp/hexsim-phantom-cam-{args.ctrl_port}.lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        sys.exit("ERROR: another sim_camera holds this port's lock — kill it first.")

    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))

    cam = SimCamera(num_cines=args.num_cines)
    ctrl = ReusableServer((args.host, args.ctrl_port), CtrlHandler)
    data = ReusableServer((args.host, args.data_port), DataHandler)
    ctrl.cam = data.cam = cam

    threading.Thread(target=data.serve_forever, daemon=True).start()
    print(f"sim_camera up: ctrl {args.host}:{args.ctrl_port}, "
          f"data {args.host}:{args.data_port}, {args.num_cines} cines",
          flush=True)
    try:
        ctrl.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        ctrl.shutdown()
        data.shutdown()


if __name__ == "__main__":
    main()
