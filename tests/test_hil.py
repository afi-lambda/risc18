"""Simulation gates for the HIL telemetry harness (stage 3a).

Three things are pinned here, in the order the hardware depends on them:

1. the UART bit timing -- one start bit, eight data bits LSB first, one stop
   bit, each exactly ``divisor`` core clocks long, checked both by sampling the
   line at the bit centres and by measuring the length of every run of equal
   samples (a one-cycle error in the bit counter fails both);
2. the line format, byte for byte -- :data:`risc18.hil.LINE_FORMAT` is rendered
   by a combinational character ROM in the RTL and by ``printf`` in Python, and
   the two are compared through the *actual* transmitted waveform as well as
   directly;
3. the reset-to-halt convention -- the counters the harness transmits are
   compared with a direct simulation of the same program on the bare
   :class:`risc18.core.Core`, driven by a testbench that counts exactly the same
   edges.  This is the number the board run is checked against.

The reference count for (3) is defined by ``_direct_run`` below: cycle 1 is the
first clock edge after ``i_rst`` falls, and the count is the cycle on which
``o_halt`` is first seen high, inclusive.  The harness's counter sees the same
edges because both its domain and the core's are reset by the same signal (in
``HilTop``: ``BootReset.o_rst``).  On ``CORE_TOP_PROGRAM`` that is 26 cycles for
13 retires; the 21 cycles quoted in ``docs/RESULTS.md`` is the *retire span*
(cycle 5 through cycle 25), which is a different quantity and is asserted here
so the two cannot be confused.
"""

import importlib.util
import pathlib
import re
import warnings

import pytest
from amaranth import Elaboratable, Module
from amaranth.back import verilog
from amaranth.sim import Simulator

from risc18.core import Core
from risc18.hil import (
    BAUD,
    F_IN_MHZ,
    F_OUT_MHZ,
    CLK_HZ,
    LINE_FORMAT,
    LINE_ITEMS,
    LINE_LEN,
    UART_DIVISOR,
    HilStatus,
    UartTx,
    baud_divisor,
    baud_error,
    format_line,
    parse_line,
)
from risc18.tops import CORE_TOP_PROGRAM, FullTop, HilTop

ROOT = pathlib.Path(__file__).resolve().parents[1]
CLK_PERIOD = 1 / CLK_HZ
SETTLE = 1e-12


# --- baud divisor -----------------------------------------------------------

def test_uart_divisor_is_the_nearest_integer_and_its_error_is_negligible():
    # 60 MHz / 115200 = 520.8333..; 521 is the nearest integer divisor.
    assert CLK_HZ / BAUD == pytest.approx(520.8333333, abs=1e-6)
    assert baud_divisor() == UART_DIVISOR == 521

    actual, error = baud_error()
    assert actual == pytest.approx(CLK_HZ / UART_DIVISOR)
    assert actual == pytest.approx(115163.148, abs=1e-3)
    # -320 ppm: five times closer than the other candidate divisor, 520.
    assert error * 1e6 == pytest.approx(-320.0, abs=0.5)
    other = (CLK_HZ / 520 - BAUD) / BAUD
    assert other * 1e6 == pytest.approx(1602.6, abs=0.5)
    assert abs(error) < abs(other) / 4
    # A UART receiver tolerates thousands of ppm; this is a 25x margin.
    assert abs(error) < 0.002


def test_uart_divisor_rejects_a_one_clock_bit_slot():
    with pytest.raises(ValueError):
        UartTx(divisor=1)


# --- UART framing and bit timing -------------------------------------------

def _uart_waveform(byte, divisor=None, idle=3):
    """One 8N1 frame, sampled once per core clock, starting from idle.

    The returned list holds ``o_tx`` after each clock edge; sample 0 is the
    first edge after the strobe was applied.  Capturing one sample per clock is
    the strongest form of the timing claim: every transition lands on the cycle
    the divisor says it should.
    """
    dut = UartTx() if divisor is None else UartTx(divisor=divisor)

    class _Harness(Elaboratable):
        def __init__(self):
            self.dut = dut

        def elaborate(self, platform):
            m = Module()
            m.submodules.dut = dut
            return m

    sim = Simulator(_Harness())
    sim.add_clock(CLK_PERIOD, domain="sync")
    out = {}

    async def tb(ctx):
        samples = []
        for _ in range(idle):
            await ctx.tick()
            await ctx.delay(SETTLE)
            samples.append(ctx.get(dut.o_tx))
        ctx.set(dut.i_data, byte)
        ctx.set(dut.i_stb, 1)
        await ctx.tick()
        await ctx.delay(SETTLE)
        samples.append(ctx.get(dut.o_tx))
        ctx.set(dut.i_stb, 0)
        # Capture every clock of the frame, then a few idle slots after it.
        for _ in range(40 * dut.divisor + 32):
            await ctx.tick()
            await ctx.delay(SETTLE)
            samples.append(ctx.get(dut.o_tx))
            if not ctx.get(dut.o_busy):
                break
        else:
            raise AssertionError("frame never finished")
        for _ in range(4 * dut.divisor):
            await ctx.tick()
            await ctx.delay(SETTLE)
            samples.append(ctx.get(dut.o_tx))
        out["samples"] = samples
        out["busy_at_end"] = ctx.get(dut.o_busy)

    sim.add_testbench(tb)
    sim.run()
    assert out["busy_at_end"] == 0
    return out["samples"], dut.divisor


def _runs(samples):
    """``[(value, length), ...]`` -- maximal runs of equal samples."""
    runs = []
    for value in samples:
        if runs and runs[-1][0] == value:
            runs[-1][1] += 1
        else:
            runs.append([value, 1])
    return [(value, length) for value, length in runs]


def test_uart_tx_frames_a_byte_with_exact_115200_bit_timing():
    # 0x55 is the alternating byte: no two adjacent data bits are equal and the
    # top data bit is 0, so every bit is its own run and the stop bit is too.
    samples, divisor = _uart_waveform(0x55)
    assert divisor == 521
    runs = _runs(samples)

    # Idle high, then the start bit: exactly one bit slot low.
    assert runs[0][0] == 1
    assert runs[1] == (0, divisor), f"start bit was {runs[1]}, want {divisor} clocks"

    # 0x55 LSB first: 1 0 1 0 1 0 1 0 -- one run per data bit of exactly one slot.
    bits = [(0x55 >> k) & 1 for k in range(8)]
    assert runs[2:10] == [(bit, divisor) for bit in bits], runs

    # Then the stop bit: high for a full slot and on into idle.
    assert runs[10][0] == 1
    assert runs[10][1] >= divisor, "stop bit was shorter than a bit slot"
    assert len(runs) == 11, f"unexpected extra transitions: {runs}"

    # Same claim at the bit centres, the way a receiver samples the line.
    start = runs[0][1]
    for k, bit in enumerate(bits):
        centre = start + divisor * (k + 1) + divisor // 2
        assert samples[centre] == bit, f"data bit {k} read {samples[centre]}"
    assert samples[start + divisor // 2] == 0            # start bit centre
    assert samples[start + 9 * divisor + divisor // 2] == 1   # stop bit centre


@pytest.mark.parametrize("byte,low_slots,high_slots", [
    (0x00, 9, 1),      # start + eight zero bits merge into one long low run
    (0xFF, 1, 9),      # start, then eight ones that merge with the stop bit
])
def test_uart_tx_has_no_framing_glitch_on_the_extreme_bytes(byte, low_slots,
                                                            high_slots):
    # divisor=2 keeps this cheap; the run *structure* is what is being tested,
    # the real divisor's exact slot length is pinned by the test above.
    samples, divisor = _uart_waveform(byte, divisor=2)
    runs = _runs(samples)
    assert runs[0][0] == 1
    assert runs[1] == (0, low_slots * divisor), runs
    assert runs[2][0] == 1 and runs[2][1] >= high_slots * divisor, runs
    assert len(runs) == 3, f"a merged frame must not glitch: {runs}"


# --- the line: format, and the RTL's character ROM -------------------------

def _render_rom(values):
    """Render the line from :data:`LINE_ITEMS`, exactly as the RTL mux does."""
    out = bytearray()
    for item in LINE_ITEMS:
        if item[0] == "lit":
            out.append(item[1])
        else:
            _, name, nibble = item
            digit = (values[name] >> (4 * nibble)) & 0xF
            out.append(digit + (ord("a") - 10 if digit > 9 else ord("0")))
    return bytes(out)


@pytest.mark.parametrize("values", [
    {"retires": 13, "cycles": 26, "pc": 0xF, "value": 0},
    {"retires": 0, "cycles": 0, "pc": 0, "value": 0},
    {"retires": 0xFFFF, "cycles": 0xFFFFFFF, "pc": 0xFFFF, "value": 0x3FFFF},
    {"retires": 0xA, "cycles": 0x10, "pc": 0xAB, "value": 0x1C},
])
def test_character_rom_matches_the_documented_format(values):
    # The RTL's switch table and ``LINE_FORMAT % ...`` must agree character for
    # character: order, lowercase, zero padding and the fixed literal text.
    assert _render_rom(values) == format_line(**values)


def test_line_is_the_documented_53_bytes():
    assert LINE_LEN == 53
    sample = format_line(retires=13, cycles=26, pc=0xF, value=0)
    assert sample == b"OK retires=000d cycles=0000001a pc=000f value=00000\r\n"
    assert len(sample) == LINE_LEN
    assert LINE_FORMAT % (0xD, 0x1A, 0xF, 0) == sample.decode()


def test_parse_line_round_trips_and_rejects_anything_else():
    parsed = parse_line(b"OK retires=000d cycles=0000001a pc=000f value=00000\r\n")
    assert parsed == {"retires": 13, "cycles": 26, "pc": 15, "value": 0}
    # str input is accepted too (the serial tool reads bytes, logs text).
    assert parse_line("OK retires=000d cycles=0000001a pc=000f value=00000\r\n") == parsed

    for bad in (
        b"OK retires=d cycles=0000001a pc=000f value=00000\r\n",   # unpadded
        b"OK retires=000D cycles=0000001a pc=000f value=00000\r\n",  # uppercase
        b"OK retires=000d cycles=0000001a pc=000f value=0000\r\n",   # short value
        b"OK retires=000d cycles=0000001a pc=000f value=00000\n",    # no CR
        b"OK retires=000d cycles=0000001a pc=000f value=00000\r\n\r\n",
        b"OK retires=000d cycles=0000001a pc=000f value=00000\r\nOK re",
        b"",
        b"garbage\r\n",
    ):
        with pytest.raises(ValueError):
            parse_line(bad)


# --- harness against a direct core simulation -------------------------------

class _SimHil(Elaboratable):
    """Core + harness on one 60 MHz clock, wired the way :class:`HilTop` does.

    The PLL and the boot sequencer have no simulator model, so the two things
    they provide here -- one 60 MHz clock net and one shared reset -- are driven
    by the testbench: ``add_clock`` on ``sync`` and ``i_rst`` on both domains.
    The harness's own domain is clocked from the core's net and reset from the
    same signal, which is exactly the ``pll_clk``/``boot_rst`` wiring of the top
    (pinned structurally further down).
    """

    def __init__(self, program=CORE_TOP_PROGRAM, divisor=None):
        self.core = Core(list(program))
        self.hil = HilStatus() if divisor is None else HilStatus(divisor=divisor)

    def elaborate(self, platform):
        m = Module()
        m.submodules.core = self.core
        m.submodules.hil = self.hil
        m.d.comb += [
            self.hil.cd.clk.eq(self.core.cd.clk),
            self.hil.cd.rst.eq(self.core.i_rst),
            self.hil.i_retire.eq(self.core.o_retire),
            self.hil.i_retire_pc.eq(self.core.o_retire_pc),
            self.hil.i_retire_value.eq(self.core.o_retire_value),
            self.hil.i_halt.eq(self.core.o_halt),
        ]
        return m


def _direct_run(program=CORE_TOP_PROGRAM, max_cycles=400):
    """Reference measurement on the bare core: the reset-to-halt convention.

    Cycle 1 is the first edge after ``i_rst`` falls; the run stops on the edge
    on which ``o_halt`` is first observed, and that cycle index is the count.
    """
    core = Core(list(program))
    sim = Simulator(core)
    sim.add_clock(CLK_PERIOD, domain="sync")
    out = {}

    async def tb(ctx):
        ctx.set(core.i_rst, 1)
        await ctx.tick()
        await ctx.delay(SETTLE)
        ctx.set(core.i_rst, 0)
        trace = []
        for cycle in range(1, max_cycles + 1):
            await ctx.tick()
            await ctx.delay(SETTLE)
            if ctx.get(core.o_retire):
                trace.append((cycle, ctx.get(core.o_retire_pc),
                              ctx.get(core.o_retire_value)))
            if ctx.get(core.o_halt):
                out.update(cycles=cycle, retires=len(trace), trace=trace,
                           pc=trace[-1][1], value=trace[-1][2])
                return
        raise AssertionError(f"core did not halt in {max_cycles} cycles")

    sim.add_testbench(tb)
    sim.run()
    return out


def _harness_run(divisor=None, capture=False, max_cycles=400):
    """Same program through ``Core`` + ``HilStatus``; returns latched values.

    With ``capture`` the ``o_tx`` line is sampled once per core clock for long
    enough to hold the whole line, so the transmitted bytes can be decoded
    independently of the latched attributes.
    """
    top = _SimHil(divisor=divisor)
    sim = Simulator(top)
    sim.add_clock(CLK_PERIOD, domain="sync")
    out = {}

    async def tb(ctx):
        ctx.set(top.core.i_rst, 1)
        await ctx.tick()
        await ctx.delay(SETTLE)
        ctx.set(top.core.i_rst, 0)
        samples = []
        for cycle in range(1, max_cycles + 1):
            await ctx.tick()
            await ctx.delay(SETTLE)
            if capture:
                samples.append(ctx.get(top.hil.o_tx))
            if ctx.get(top.hil.done):
                if not capture:
                    break
                # let the whole line clock out (plus the per-byte handshake
                # and a few idle slots), so the capture ends in silence
                budget = cycle + LINE_LEN * (10 * top.hil.divisor + 8) \
                    + 16 * top.hil.divisor + 32
                while cycle < budget:
                    await ctx.tick()
                    await ctx.delay(SETTLE)
                    samples.append(ctx.get(top.hil.o_tx))
                    cycle += 1
                break
        else:
            raise AssertionError("harness never snapshotted: no halt reached")
        out.update(cycles=ctx.get(top.hil.cycles),
                   retires=ctx.get(top.hil.retires),
                   pc=ctx.get(top.hil.pc), value=ctx.get(top.hil.value),
                   done=ctx.get(top.hil.done), samples=samples)

    sim.add_testbench(tb)
    sim.run()
    assert out["done"] == 1
    return out


def _decode_uart_line(samples, divisor):
    """Decode every 8N1 frame in a one-sample-per-clock waveform.

    Asserts the framing as it goes: the start bit is low for exactly one slot,
    every data bit is constant across its slot, and the stop bit is high for a
    full slot.  Returns the bytes in transmission order.
    """
    frames = []
    index = 0
    while index < len(samples) and samples[index]:
        index += 1
    while index < len(samples):
        start = index
        assert samples[start:start + divisor] == [0] * divisor, "bad start bit"
        bits = []
        for bit in range(8):
            slot = samples[start + (bit + 1) * divisor:
                           start + (bit + 2) * divisor]
            assert len(slot) == divisor and len(set(slot)) == 1, \
                f"data bit {bit} is not constant across its slot"
            bits.append(slot[0])
        stop = samples[start + 9 * divisor:start + 10 * divisor]
        assert len(stop) == divisor and set(stop) == {1}, "framing error"
        frames.append(sum(bit << k for k, bit in enumerate(bits)))
        index = start + 10 * divisor
        while index < len(samples) and samples[index]:
            index += 1
    return bytes(frames)


def test_harness_counters_equal_a_direct_core_simulation():
    direct = _direct_run()
    harness = _harness_run()

    # The reference program itself (docs/RESULTS.md: 13 retires, 1.615 CPI).
    assert direct["retires"] == 13
    assert direct["trace"][0][0] == 5 and direct["trace"][-1][0] == 25
    assert direct["trace"][-1][0] - direct["trace"][0][0] + 1 == 21   # retire span

    # Same reset-to-halt convention on both sides, so equality is exact.
    assert direct["cycles"] == 26
    assert harness["cycles"] == direct["cycles"]
    assert harness["retires"] == direct["retires"] == 13
    assert harness["pc"] == direct["pc"] == 0xF
    assert harness["value"] == direct["value"] == 0


def test_harness_transmits_the_documented_line_once_and_stops():
    # divisor=2: the same RTL and the same line, 20 clocks per byte instead of
    # 5210, so the byte-for-byte claim can be checked end to end cheaply.  The
    # shipped divisor and its bit timing are pinned by the UART tests above.
    run = _harness_run(divisor=2, capture=True)
    line = _decode_uart_line(run["samples"], 2)

    assert line == format_line(retires=13, cycles=26, pc=0xF, value=0)
    assert line == b"OK retires=000d cycles=0000001a pc=000f value=00000\r\n"
    assert len(line) == LINE_LEN
    assert parse_line(line) == {"retires": run["retires"], "cycles": run["cycles"],
                               "pc": run["pc"], "value": run["value"]}

    # One line and then silence: a second line would add frames to the decode
    # (they would have to start inside the capture), and the capture ends high.
    assert len(line) == LINE_LEN
    assert run["samples"][-32:] == [1] * 32


# --- tools/hil_status.py -----------------------------------------------------

def _load_tool():
    """Import ``tools/hil_status.py`` (a script, not a package module)."""
    path = ROOT / "tools" / "hil_status.py"
    spec = importlib.util.spec_from_file_location("hil_status_tool", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _FakePort:
    """Minimal stand-in for a raw fd: ``pump`` reads from a byte list."""

    def __init__(self, chunks):
        self.chunks = list(chunks)

    def fileno(self):
        return 0     # select() is patched out in the test


def test_tool_compare_is_exact_except_for_the_cycles_tolerance():
    tool = _load_tool()
    line = parse_line(b"OK retires=000d cycles=0000001a pc=000f value=00000\r\n")

    ok, notes = tool.compare(line, {"retires": 13, "cycles": 26,
                                    "cycles_tolerance": 0.05,
                                    "pc": 0xF, "value": 0})
    assert ok and len(notes) == 4
    assert all("MISMATCH" not in note for note in notes)

    # 5 % of 26 is 1.3 cycles, so 27 still passes and 28 does not; the counts
    # that are not allowed a tolerance never do.
    for cycles, expected_pass in ((27, True), (25, True), (28, False), (24, False)):
        got = dict(line, cycles=cycles)
        ok, _ = tool.compare(got, {"cycles": 26, "cycles_tolerance": 0.05})
        assert ok is expected_pass, cycles
    for field in ("retires", "pc", "value"):
        got = dict(line, **{field: line[field] + 1})
        ok, notes = tool.compare(got, {field: line[field]})
        assert not ok and notes[0].count("MISMATCH") == 1


def test_tool_reader_skips_an_older_designs_output(monkeypatch):
    """A leftover line from a previous design must not be taken for ours.

    This is not hypothetical: on the board the harness line arrived glued to
    the tail of the design that had been running before, so the reader has to
    keep scanning instead of giving up on the first bad line.
    """
    tool = _load_tool()
    good = b"OK retires=000d cycles=0000001a pc=000f value=00000\r\n"
    chunks = [b"\x00PLL60\r\n\x00PLL6", b"0\r\n" + good]
    port = _FakePort(chunks)

    def fake_select(rlist, wlist, xlist, timeout):
        return ([port] if port.chunks else [], [], [])

    def fake_read(fd, size):
        return port.chunks.pop(0) if port.chunks else b""

    monkeypatch.setattr(tool.select, "select", fake_select)
    monkeypatch.setattr(tool.os, "read", fake_read)
    reader = tool.StatusReader(port, verbose=False)
    first = reader.pump(0.1)          # ends inside the old design's line
    second = reader.pump(0.1)         # completes both lines
    assert first is None
    assert second is not None
    assert second[0] == good
    assert second[1] == {"retires": 13, "cycles": 26, "pc": 15, "value": 0}


# --- HilTop -----------------------------------------------------------------

def _hil_verilog():
    top = HilTop()
    return top, verilog.convert(top, name="top", ports=top.ports)


def test_hil_top_ports_are_full_top_plus_one_uart_output():
    top, _ = _hil_verilog()
    assert top.ports[0] is top.clk
    names = [port.name for port in top.ports]
    # ``FullTop.ports`` is built in __init__, so it needs no elaboration; the
    # instance is only read for its port names.
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        reference = [port.name for port in FullTop().ports]
    assert names == reference + ["uart_tx"]
    assert top.ports[-1] is top.uart_tx
    # The harness adds no input and no other output.
    assert names.count("uart_tx") == 1
    assert top.hil.divisor == UART_DIVISOR == 521
    assert len(top.hil.cycles) == 27


def test_hil_top_wiring_is_exactly_the_documented_one():
    """The top is synthesis-only, so its wiring is pinned on the Verilog."""
    top, v = _hil_verilog()

    # PLL: 25 MHz pad in, CLKOP/LOCK out on the named nets, as in FullTop.
    assert ".i_clk(clk)" in v
    assert ".o_clk(pll_clk)" in v
    assert ".i_locked(o_locked)" in v
    assert "assign pll_locked = o_locked;" in v

    # BootReset still owns the core's reset and still watches the halt.
    assert ".o_rst(boot_rst)" in v
    assert ".rst(boot_rst)" in v
    assert ".i_halt(i_halt)" in v

    # The harness runs in its own domain off the *PLL* clock and the *boot*
    # reset -- the two facts that make the on-board count comparable.
    assert "assign hil_clk = pll_clk;" in v
    assert "assign hil_rst = boot_rst;" in v

    # ... and it reads the core's retire trace, nothing else.
    assert re.search(r"\\top\.hil\s+hil \(", v)
    assert ".i_retire(i_retire)" in v
    assert ".i_retire_pc(i_retire_pc)" in v
    assert ".i_retire_value(i_retire_value)" in v
    assert ".o_tx(uart_tx)" in v

    # The pad is the harness output; nothing else drives it.
    assert v.count("assign uart_tx") == 1 or ".o_tx(uart_tx)" in v
    assert "posedge clk" not in v.split("module \\top")[1]

    # The ecppll 25 -> 60 MHz divider set, not FullTop's (1, 24, 10): see
    # test_hil_pll_uses_the_set_that_actually_locks_on_the_board.
    assert (top.pll.clki_div, top.pll.clkfb_div, top.pll.clkop_div) == (5, 12, 10)
    assert top.pll.f_out_mhz == pytest.approx(60.0)
    assert top.pll.f_vco_mhz == pytest.approx(600.0)


def test_hil_pll_uses_the_set_that_actually_locks_on_the_board():
    """``HilPll`` is the ``ecppll -i 25 -o 60`` instance, and why it must be.

    With ``FEEDBK_PATH="CLKOP"`` the feedback divides CLKOP (not the VCO) down
    to the PFD, so the loop locks ``f_out = f_in * CLKFB_DIV / CLKI_DIV`` and
    the VCO runs at ``f_out * CLKOP_DIV``.  ``risc18.pll.Pll`` divides by
    ``CLKOP_DIV`` a second time in its own arithmetic, so its (1, 24, 10)
    default asks for 25*24 = 600 MHz of CLKOP and a 6 GHz VCO -- nextpnr's
    derived constraint on that build reads 600.2 MHz with "VCO 6024.1 MHz out
    of legal range", and a heartbeat design clocked by it emits nothing on the
    board.
    """
    from risc18.hil import HilPll
    from risc18.pll import Pll

    top = HilTop()
    assert isinstance(top.pll, HilPll)
    assert (top.pll.clki_div, top.pll.clkfb_div, top.pll.clkop_div) == (5, 12, 10)
    assert top.pll.f_pfd_mhz == pytest.approx(5.0)
    assert top.pll.f_vco_mhz == pytest.approx(600.0)
    assert top.pll.f_out_mhz == pytest.approx(60.0)
    assert CLK_HZ == pytest.approx(top.pll.f_out_mhz * 1e6)

    # The shipped class cannot even express the working set (its 5 MHz PFD floor
    # rejects the 12/5 ratio that 60 MHz from 25 MHz needs)...
    with pytest.raises(ValueError):
        Pll(clki_div=5, clkfb_div=12, clkop_div=10)
    # ... and on its own default set its model says 60 MHz where the CLKOP
    # feedback loop gives 600 MHz, i.e. it is off by exactly CLKOP_DIV.
    assert Pll().f_out_mhz == pytest.approx(60.0)
    assert F_IN_MHZ * Pll().clkfb_div / Pll().clki_div == pytest.approx(600.0)
    assert Pll().f_out_mhz * Pll().clkop_div == pytest.approx(600.0)


def test_hil_top_keeps_the_full_top_program_and_core():
    top, _ = _hil_verilog()
    assert top.program == list(CORE_TOP_PROGRAM)
    assert type(top.core).__name__ == "Core"
    assert top.core.program == list(CORE_TOP_PROGRAM)
    # The harness's counter widths must cover the watchdog window (2**26), so a
    # re-reset run can never wrap the cycle count into a plausible-looking one.
    assert top.hil.cycles_bits >= 27
