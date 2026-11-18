"""Read the HIL status line from the board and check it against expectations.

The board prints exactly one line per configuration (see ``risc18.hil``)::

    OK retires=%04x cycles=%08x pc=%04x value=%05x\\r\\n

This tool opens the serial port (``/dev/ttyUSB1`` on this board -- ``ttyUSB0``
is the JTAG channel of the same FT2232H, never open it as a serial port), reads
with a deadline, parses the line with ``risc18.hil.parse_line`` and compares it
with the values given on the command line.  It prints what it received either
way and exits non-zero on a timeout or a mismatch.

``--load`` runs ``openFPGALoader -b <device> <bitstream>`` *after* the port is
open and keeps reading while the loader runs, so neither the line (printed
~5 ms after the FPGA reports Done) nor anything the previous design was still
sending can be lost to a full kernel tty buffer.  The loader is always invoked
without ``-f``: that writes SRAM and is volatile, ``-f`` would program the
board's flash, which belongs to another project.

Examples::

    # read whatever the board is doing right now
    python tools/hil_status.py

    # load the harness into SRAM and check the measurement, 5% on the cycles
    python tools/hil_status.py --load build/synth/hil5-board/top.bit \\
        --expect-retires 13 --expect-cycles 26 --expect-pc 0xf --expect-value 0
"""

import argparse
import os
import pathlib
import select
import subprocess
import sys
import termios
import time

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from risc18.hil import BAUD, parse_line  # noqa: E402

DEFAULT_PORT = "/dev/ttyUSB1"      # UART; /dev/ttyUSB0 is JTAG
DEFAULT_DEVICE = "colorlight"      # openFPGALoader board profile
DEFAULT_TIMEOUT = 10.0


def open_serial(port, baud=BAUD):
    """Open ``port`` raw, 8N1, no flow control (termios only, no pyserial)."""
    try:
        speed = getattr(termios, f"B{baud}")
    except AttributeError:
        raise SystemExit(f"baud rate {baud} has no termios constant on this host")
    fd = os.open(port, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
    iflag, oflag, cflag, lflag, _, _, cc = termios.tcgetattr(fd)
    iflag &= ~(termios.IGNBRK | termios.BRKINT | termios.PARMRK
               | termios.ISTRIP | termios.INLCR | termios.IGNCR
               | termios.ICRNL | termios.IXON | termios.IXOFF | termios.IXANY)
    oflag &= ~termios.OPOST
    lflag &= ~(termios.ECHO | termios.ECHONL | termios.ICANON | termios.ISIG
               | termios.IEXTEN)
    cflag &= ~(termios.CSIZE | termios.PARENB | termios.CSTOPB)
    cflag |= termios.CS8 | termios.CREAD | termios.CLOCAL
    cc[termios.VMIN] = 0
    cc[termios.VTIME] = 0
    termios.tcsetattr(fd, termios.TCSANOW,
                      [iflag, oflag, cflag, lflag, speed, speed, cc])
    return fd


class StatusReader:
    """Accumulate bytes from the port and hand back the first parseable line.

    Everything received is echoed, and a line that does not match the format is
    reported and skipped (a board that is still transmitting an older design
    must not be mistaken for a timeout).
    """

    def __init__(self, fd, verbose=True):
        self.fd = fd
        self.buffer = bytearray()
        self.verbose = verbose

    def pump(self, timeout):
        """Read once (waiting up to ``timeout``); return ``(raw, parsed)`` or None."""
        ready, _, _ = select.select([self.fd], [], [], max(0.0, timeout))
        if not ready:
            return None
        chunk = os.read(self.fd, 4096)
        if not chunk:
            return None
        if self.verbose:
            print(f"  rx: {chunk!r}", flush=True)
        self.buffer.extend(chunk)
        while b"\n" in self.buffer:
            raw, _, rest = self.buffer.partition(b"\n")
            self.buffer = bytearray(rest)
            line = raw + b"\n"
            try:
                return line, parse_line(line)
            except ValueError as exc:
                if self.verbose:
                    print(f"  !! ignoring: {exc}", flush=True)
        return None


def load_bitstream(bitstream, device, reader=None, pump=0.05):
    """Program SRAM (never flash), printing the loader's own output.

    With a ``reader`` the port keeps being drained while the loader runs, so a
    line that arrives right after the FPGA reports Done is captured rather than
    dropped when the kernel tty buffer fills with the previous design's output.
    """
    if not pathlib.Path(bitstream).is_file():
        raise SystemExit(f"no bitstream at {bitstream}")
    cmd = ["openFPGALoader", "-b", str(device), str(bitstream)]
    print(f"+ {' '.join(cmd)}", flush=True)
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True)
    found = None
    while proc.poll() is None:
        if reader is not None and found is None:
            found = reader.pump(pump)
        else:
            time.sleep(pump)
    out, err = proc.communicate()
    for stream in (out or "", err or ""):
        for line in stream.splitlines():
            if line.strip():
                print(f"  | {line}", flush=True)
    if proc.returncode != 0:
        raise SystemExit(f"openFPGALoader failed ({proc.returncode}); "
                         "the bitstream was not loaded")
    return found


def read_line(reader, timeout, found=None):
    """Return the first ``(raw, parsed)`` status line within ``timeout``."""
    if found is not None:
        return found
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        line = reader.pump(min(0.2, max(0.0, deadline - time.monotonic())))
        if line is not None:
            return line
    if reader.verbose and reader.buffer:
        print(f"  rx: {bytes(reader.buffer)!r} (no newline before the deadline)",
              flush=True)
    return None, None


def compare(got, want):
    """``(ok, [message, ...])`` for one parsed line against the expectations."""
    notes, ok = [], True
    for field in ("retires", "cycles", "pc", "value"):
        if field not in want:
            continue
        expected = want[field]
        actual = got[field]
        if field == "cycles":
            tolerance = want["cycles_tolerance"]
            delta = actual - expected
            good = abs(delta) <= tolerance * abs(expected)
            notes.append(
                f"cycles  got {actual} (0x{actual:x}) want {expected} "
                f"+-{tolerance * 100:.1f}% -> delta {delta:+d} "
                f"({delta / expected * 100:+.2f} %) {'OK' if good else 'MISMATCH'}")
        else:
            good = actual == expected
            notes.append(f"{field:7s} got {actual} (0x{actual:x}) "
                         f"want {expected} (0x{expected:x}) "
                         f"{'OK' if good else 'MISMATCH'}")
        ok = ok and good
    return ok, notes


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--port", default=DEFAULT_PORT,
                    help=f"serial port (default {DEFAULT_PORT}; "
                         "ttyUSB0 is JTAG)")
    ap.add_argument("--baud", type=int, default=BAUD,
                    help=f"line rate (default {BAUD})")
    ap.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT,
                    help=f"seconds to wait for the line (default {DEFAULT_TIMEOUT})")
    ap.add_argument("--load", metavar="BITSTREAM",
                    help="program this bitstream into SRAM after opening the "
                         "port (openFPGALoader -b <device>, never -f)")
    ap.add_argument("--device", default=DEFAULT_DEVICE,
                    help=f"openFPGALoader board profile (default {DEFAULT_DEVICE})")
    ap.add_argument("--expect-retires", type=int,
                    help="expected retire count (exact)")
    ap.add_argument("--expect-cycles", type=int,
                    help="expected reset-to-halt cycle count")
    ap.add_argument("--cycles-tolerance", type=float, default=0.05,
                    help="relative tolerance on cycles (default 0.05 = 5%%)")
    ap.add_argument("--expect-pc", type=lambda s: int(s, 0),
                    help="expected PC of the last retire (hex or decimal)")
    ap.add_argument("--expect-value", type=lambda s: int(s, 0),
                    help="expected value of the last retire (hex or decimal)")
    args = ap.parse_args(argv)

    want = {}
    if args.expect_retires is not None:
        want["retires"] = args.expect_retires
    if args.expect_cycles is not None:
        want["cycles"] = args.expect_cycles
        want["cycles_tolerance"] = args.cycles_tolerance
    if args.expect_pc is not None:
        want["pc"] = args.expect_pc
    if args.expect_value is not None:
        want["value"] = args.expect_value

    fd = open_serial(args.port, args.baud)
    try:
        print(f"opened {args.port} at {args.baud} 8N1", flush=True)
        reader = StatusReader(fd)
        found = None
        if args.load:
            found = load_bitstream(args.load, args.device, reader=reader)
        raw, got = read_line(reader, args.timeout, found=found)
        if got is None:
            print(f"TIMEOUT: no parseable status line on {args.port} within "
                  f"{args.timeout:.1f} s", flush=True)
            return 1
        print(f"line: {raw!r}", flush=True)
        if not want:
            print("decoded:", got, "(nothing to compare: no --expect-* given)",
                  flush=True)
            return 0
        ok, notes = compare(got, want)
        for note in notes:
            print(f"  {note}", flush=True)
        print("PASS" if ok else "FAIL", flush=True)
        return 0 if ok else 1
    finally:
        os.close(fd)


if __name__ == "__main__":
    sys.exit(main())
