"""
Protocol smoke test for the Phantom camera simulator
(hex-simulated-beamline/iocs/phantom/sim_camera.py) — zero EPICS, zero
containers: a loopback TCP client speaking the PH16 command surface the
deployed ADPhantom driver uses, asserting the reply framing that driver
actually parses (newline-terminated, ``Ok!`` success, ``ERR:`` errors) and
the cine state machine the ophyd/pyepics layers depend on
(WTR -> TRG/STR, lastfr+1 == post-trigger count).

Run from the hex-ob root (same path CI uses):
    pixi run test-mock
Or directly:
    python tests/phantom_simcam_mock_test.py
"""

import socket
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]
                       / "hex-simulated-beamline/iocs/phantom"))

from sim_camera import CtrlHandler, DataHandler, ReusableServer, SimCamera


def start_servers():
    cam = SimCamera(num_cines=63)
    ctrl = ReusableServer(("127.0.0.1", 0), CtrlHandler)
    data = ReusableServer(("127.0.0.1", 0), DataHandler)
    ctrl.cam = data.cam = cam
    for srv in (ctrl, data):
        threading.Thread(target=srv.serve_forever, daemon=True).start()
    return cam, ctrl, data


class CtrlClient:
    """Newline-framed control client, reading exactly like the driver:
    one reply terminated by \\n, with backslash-CRLF continuations kept."""

    def __init__(self, port):
        self.sock = socket.create_connection(("127.0.0.1", port), timeout=5)
        self.buf = b""

    def ask(self, command: str) -> str:
        self.sock.sendall(command.encode() + b"\n")
        # A struct reply's inner lines end "\\\r\n" — the terminating bare
        # "\n" is the first newline NOT preceded by "\\\r".
        while True:
            idx = self.buf.find(b"\n")
            while idx != -1 and self.buf[max(0, idx - 2):idx] == b"\\\r":
                idx = self.buf.find(b"\n", idx + 1)
            if idx != -1:
                reply, self.buf = self.buf[:idx], self.buf[idx + 1:]
                return reply.decode()
            chunk = self.sock.recv(65536)
            assert chunk, "server closed the control connection"
            self.buf += chunk


def main() -> None:
    cam, ctrl_srv, data_srv = start_servers()
    ctrl = CtrlClient(ctrl_srv.server_address[1])

    # -- struct get: ancestor-format dict reply -----------------------------
    reply = ctrl.ask("get cam")
    assert reply.startswith("cam : {"), reply[:40]
    assert "cines : 63" in reply, "NUM_CINES should match the HEX deploy"
    reply = ctrl.ask("get info")
    assert '"Phantom T2410 (sim)"' in reply
    reply = ctrl.ask("get c1")
    assert "state" in reply and "frcount" in reply
    print("PASS  struct gets (cam/info/c1, ancestor reply format)")

    # -- dotted get / set ---------------------------------------------------
    assert ctrl.ask("set defc.ptframes 25") == "Ok!"
    assert "25" in ctrl.ask("get defc.ptframes")
    assert ctrl.ask("set defc.rate 500.0") == "Ok!"
    bad = ctrl.ask("get nosuch.param")
    assert bad.startswith("ERR:"), bad
    print("PASS  set/get dotted paths; unknown parameter -> ERR:")

    # -- recording state machine -------------------------------------------
    assert ctrl.ask("rec 1") == "Ok!"
    reply = ctrl.ask("get c1")
    assert "WTR" in reply and "lastfr : 0" in reply, reply
    assert ctrl.ask("trig") == "Ok!"
    reply = ctrl.ask("get c1")
    assert "TRG" in reply and "STR" in reply, reply
    # driver derives post-trig count as lastfr+1 -> 25 frames means lastfr 24
    assert "lastfr : 24" in reply, reply
    assert ctrl.ask("trig").startswith("ERR:"), "trig without rec must ERR"
    print("PASS  cine state machine (WTR -> TRG/STR, lastfr+1 == ptframes)")

    # -- data stream: attach + img ------------------------------------------
    assert ctrl.ask("attach {port:7116}").startswith("ERR:"), \
        "attach before a data connection must ERR"
    data_sock = socket.create_connection(
        ("127.0.0.1", data_srv.server_address[1]), timeout=5)
    for _ in range(50):
        if cam.data_socket is not None:
            break
        time.sleep(0.05)
    assert ctrl.ask("attach {port:7116}") == "Ok!"
    assert ctrl.ask("img {cine:1, start:0, cnt:3, fmt:P16}") == "Ok!"
    # Per-frame size is the DRIVER'S read contract: width*height*bits/8
    # with bits from the fmt token (P16 -> 16), NOT the cine's frsize.
    w, h = (int(v) for v in cam.params["c1"]["res"].split("x"))
    frame_bytes = w * h * 16 // 8
    expected = 3 * frame_bytes
    received = 0
    data_sock.settimeout(5)
    while received < expected:
        chunk = data_sock.recv(65536)
        assert chunk, "data stream closed early"
        received += len(chunk)
    assert received == expected, (received, expected)
    print(f"PASS  attach + img (3 frames x {frame_bytes} B P16 on the data port)")

    # -- time streams 12 bytes per frame BEFORE ack-only verbs ---------------
    assert ctrl.ask("time {cine:1, start:0, cnt:4}") == "Ok!"
    got = b""
    while len(got) < 48:
        chunk = data_sock.recv(4096)
        assert chunk, "time stream closed early"
        got += chunk
    assert len(got) == 48, len(got)
    print("PASS  time (4 x 12-byte timestamps on the data port)")

    # -- misc acks ----------------------------------------------------------
    for cmd in ("setrtc 0", "rel 1", "del", "bref"):
        assert ctrl.ask(cmd) == "Ok!", cmd
    assert ctrl.ask("bogus 1").startswith("ERR:")
    print("PASS  misc verbs acknowledged; unknown verb -> ERR:")

    print("\nALL PHANTOM SIM-CAMERA TESTS PASS")


if __name__ == "__main__":
    main()
