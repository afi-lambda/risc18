"""Elaboration and reset-sequencer tests for the M9b full top.

``FullTop`` is synthesis-only: the ``EHXPLLL`` primitive has no model in
Amaranth's simulator (its outputs read 0) and none in the Yosys ECP5 library,
so the top cannot be simulated as a whole.  The two halves are therefore tested
where each one is testable:

* :class:`risc18.pll.Pll` -- structurally, on the Verilog the flow hands to
  yosys: exactly one ``EHXPLLL`` cell, with the divider parameters and the
  documented wiring.  Frequency arithmetic is checked directly (PFD/VCO/output,
  exact zero error) and illegal divider sets must raise.
* :class:`risc18.tops.BootReset` -- behaviourally, in ``amaranth.sim``: it is
  plain RTL in its own ``boot`` domain, so the lock gating, the hold window and
  the watchdog are all exercised as hardware.  This is the part of the top that
  decides whether the core runs at all, which is why it is not left to a
  structural check.
"""

import re

import pytest
from amaranth import ClockDomain, Elaboratable, Module
from amaranth.back import verilog
from amaranth.sim import Simulator

from risc18.pll import (CLKFB_DIV, CLKI_DIV, CLKOP_CPHASE, CLKOP_DIV,
                        F_IN_MHZ, F_OUT_MHZ, F_VCO_MHZ, Pll)
from risc18.tops import (
    BOOT_HOLD as PROD_BOOT_HOLD,
    WDOG_BITS as PROD_WDOG_BITS,
    CORE_TOP_PROGRAM,
    BootReset,
    CoreTop,
    FullTop,
)

CLK_PERIOD = 1e-6
SETTLE = 1e-9


# --- PLL configuration ------------------------------------------------------

def test_pll_default_is_exactly_60_mhz():
    # The ecppll -i 25 -o 60 set: CLKOP is fed back, so the loop locks
    # f_in * CLKFB / CLKI and the VCO runs at that times CLKOP_DIV.
    pll = Pll()
    assert (pll.clki_div, pll.clkfb_div, pll.clkop_div, pll.clkop_cphase) == \
        (CLKI_DIV, CLKFB_DIV, CLKOP_DIV, CLKOP_CPHASE) == (5, 12, 10, 4)
    assert F_IN_MHZ == 25.0
    assert pll.f_in_mhz == 25.0
    assert pll.f_pfd_mhz == 5.0
    assert pll.f_vco_mhz == 600.0
    assert pll.f_out_mhz == 60.0
    # 60.0 MHz is reachable exactly: no frequency error at all.
    assert pll.error_hz() == 0.0
    assert (F_OUT_MHZ, F_VCO_MHZ) == (60.0, 600.0)


def test_pll_accepts_the_other_exact_divider_set():
    # 5 / 24 / 5 is the 120 MHz top's set: equally exact, same 600 MHz VCO,
    # 5 MHz PFD.  It documents that the output is set by CLKFB_DIV/CLKI_DIV
    # and that CLKOP_DIV only chooses the VCO multiple.
    pll = Pll(clki_div=5, clkfb_div=24, clkop_div=5, clkop_cphase=2)
    assert pll.f_pfd_mhz == 5.0
    assert pll.f_vco_mhz == 600.0
    assert pll.f_out_mhz == 120.0
    assert pll.error_hz(120.0) == 0.0


@pytest.mark.parametrize("kwargs", [
    {"clki_div": 1, "clkfb_div": 24, "clkop_div": 10},   # the old "60 MHz" set
    {"clkfb_div": 12, "clkop_div": 5},      # 60 MHz out but VCO = 300 MHz
    {"clki_div": 0},                        # divider below its legal minimum
    {"clkfb_div": 96},                      # counter above its legal maximum
    {"clkop_div": 129},                     # CLKOP divider above 128
    {"clkop_cphase": 10},                   # phase shift >= CLKOP_DIV
])
def test_pll_rejects_configurations_out_of_the_ecp5_bands(kwargs):
    with pytest.raises(ValueError):
        Pll(**kwargs)


def test_pll_clkfb_div_clki_div_sets_the_output():
    # f_out = f_in * CLKFB_DIV / CLKI_DIV: the feedback ratio is the output,
    # CLKOP_DIV is only the VCO multiple -- the property every report uses.
    assert Pll(clkfb_div=12, clkop_div=10).f_out_mhz == 60.0
    assert Pll(clkfb_div=24, clkop_div=5).f_out_mhz == 120.0
    assert Pll(clkfb_div=24, clkop_div=5).f_vco_mhz == 600.0


# --- netlist structure ------------------------------------------------------

def _full_verilog():
    top = FullTop()
    return top, verilog.convert(top, name="top", ports=top.ports)


def test_full_top_has_exactly_one_pll_cell_with_the_documented_parameters():
    _, v = _full_verilog()
    assert v.count("EHXPLLL") == 1
    for param, value in (
        ("CLKI_DIV", "32'd5"),
        ("CLKFB_DIV", "32'd12"),
        ("CLKOP_DIV", "32'd10"),
        ("CLKOP_CPHASE", "32'd4"),
        ("CLKOP_ENABLE", '"ENABLED"'),
        ("CLKOS_ENABLE", '"DISABLED"'),
        ("CLKOS2_ENABLE", '"DISABLED"'),
        ("CLKOS3_ENABLE", '"DISABLED"'),
        ("FEEDBK_PATH", '"CLKOP"'),
        ("PLLRST_ENA", '"DISABLED"'),
        ("INT_LOCK_STICKY", '"ENABLED"'),
    ):
        assert f".{param}({value})" in v, param
    # Only CLKOP is enabled, and CLKI is the 25 MHz board clock: the Pll module
    # exposes i_clk (CLKI) and the top connects it to the ``clk`` port.
    assert ".ENCLKOP(1'h0)" in v       # ecppll's value; CLKOP is still driven
    assert ".ENCLKOS(1'h0)" in v
    assert ".CLKI(i_clk)" in v
    assert ".i_clk(clk)" in v
    # CLKFB is tied to the CLKOP net, as ecppll emits it and as the board run
    # required; it is the same wire as .CLKOP.
    assert ".CLKFB(1'h0)" not in v
    assert ".CLKOP(o_clk)" in v and ".CLKFB(o_clk)" in v
    assert ".RST(1'h0)" in v            # PLLRST_ENA=DISABLED
    assert ".CLKOP_CPHASE(32'd4)" in v


def test_full_top_ports_start_with_the_25_mhz_clock():
    top, _ = _full_verilog()
    assert top.ports[0] is top.clk
    assert top.clk.name == "clk"          # tools/synth.py constrains NET "clk"
    names = [p.name for p in top.ports]
    assert names[0] == "clk"
    assert set(names) == {"clk", "o_locked", "o_pc", "o_retire",
                          "o_retire_pc", "o_retire_we", "o_retire_rd",
                          "o_retire_value", "o_halt"}


def test_full_top_runs_the_same_program_as_core_top():
    # The metric is only comparable to CoreTop if the instruction image is the
    # same one, i.e. the short real program, not the pad image.
    top = FullTop()
    assert top.program == list(CORE_TOP_PROGRAM)
    assert top.program == CoreTop().program


# --- BootReset (lock gating, hold window, watchdog) -------------------------

class _BootHarness(Elaboratable):
    """Tiny top around :class:`BootReset` so the sim can own its clock.

    ``BootReset`` declares its ``boot`` domain but does not drive its clock --
    ``FullTop`` drives it from the PLL output, which a simulator cannot do
    (the clock would be combinational).  Here the harness leaves it free so
    ``add_clock`` can drive it, exactly like ``Core`` is simulated through its
    own undriven ``sync`` domain.
    """

    def __init__(self, dut):
        self.dut = dut

    def elaborate(self, platform):
        m = Module()
        m.submodules.dut = self.dut
        return m


def _sim_boot(dut, scenario):
    sim = Simulator(_BootHarness(dut))
    sim.add_clock(CLK_PERIOD, domain="boot")

    async def tb(ctx):
        await scenario(ctx, dut)

    sim.add_testbench(tb)
    sim.run()


HOLD = 3
WDOG_BITS = 4          # watchdog expires after 2**4 = 16 boot clocks


def _new_dut():
    return BootReset(hold=HOLD, wdog_bits=WDOG_BITS)


async def _tick(ctx):
    await ctx.tick("boot")
    await ctx.delay(SETTLE)


def test_boot_reset_holds_the_core_while_the_pll_is_unlocked():
    dut = _new_dut()
    seen = []

    async def scenario(ctx, dut):
        # No clock edge has happened yet: every FF the sequencer owns powers up
        # at 0, and that state must already mean "in reset".  This is why the
        # register is ``released`` and not ``hold`` (no init value is available
        # in this flow: synth_ecp5 runs without -dff, so zinit never runs).
        seen.append(ctx.get(dut.o_rst))
        ctx.set(dut.i_locked, 0)
        ctx.set(dut.i_halt, 0)
        for _ in range(10):
            await _tick(ctx)
            seen.append(ctx.get(dut.o_rst))

    _sim_boot(dut, scenario)
    assert seen == [1] * 11


def test_boot_reset_releases_the_core_only_after_lock_plus_hold():
    dut = _new_dut()
    seen = []

    async def scenario(ctx, dut):
        ctx.set(dut.i_locked, 1)          # PLL locks on the very first cycle
        ctx.set(dut.i_halt, 0)
        for _ in range(HOLD + 3):
            await _tick(ctx)
            seen.append(ctx.get(dut.o_rst))

    _sim_boot(dut, scenario)
    # Never released early: high for the whole hold window, low afterwards.
    assert seen[:HOLD - 1] == [1] * (HOLD - 1)
    assert seen[HOLD - 1] == 0
    assert seen[HOLD:] == [0] * 3


def test_boot_reset_regroups_the_core_when_lock_drops_again():
    dut = _new_dut()
    seen = []

    async def scenario(ctx, dut):
        ctx.set(dut.i_locked, 1)
        ctx.set(dut.i_halt, 1)            # halt would otherwise stop the wdog
        for _ in range(HOLD + 2):
            await _tick(ctx)
        seen.append(ctx.get(dut.o_rst))
        ctx.set(dut.i_locked, 0)          # lose lock
        await _tick(ctx)
        seen.append(ctx.get(dut.o_rst))
        ctx.set(dut.i_locked, 1)          # and lock again
        for _ in range(HOLD):
            await _tick(ctx)
            seen.append(ctx.get(dut.o_rst))

    _sim_boot(dut, scenario)
    assert seen[0] == 0                   # released while locked
    assert seen[1] == 1                   # lock lost -> reset asserted
    assert seen[2:-1] == [1] * (HOLD - 1)  # full hold window again
    assert seen[-1] == 0


def test_boot_reset_watchdog_reresets_a_hung_core():
    dut = _new_dut()
    released = []
    seen = []

    async def scenario(ctx, dut):
        ctx.set(dut.i_locked, 1)
        ctx.set(dut.i_halt, 0)            # core hangs: never halts
        for _ in range(HOLD):
            await _tick(ctx)
        for _ in range(2 ** WDOG_BITS + 2 * HOLD + 4):
            await _tick(ctx)
            released.append(ctx.get(dut.o_rst))

    _sim_boot(dut, scenario)
    # Not before the counter is full (the hold window after lock already
    # cleared it, so the first expiry is 2**WDOG_BITS clocks after release).
    assert released[:2 ** WDOG_BITS - 1] == [0] * (2 ** WDOG_BITS - 1)
    # Then it pulses reset for exactly the hold window, and releases again.
    pulse = released[2 ** WDOG_BITS - 1:2 ** WDOG_BITS - 1 + HOLD]
    assert pulse == [1] * HOLD
    assert released[2 ** WDOG_BITS - 1 + HOLD] == 0


def test_boot_reset_watchdog_is_off_once_the_program_halts():
    dut = _new_dut()
    seen = []

    async def scenario(ctx, dut):
        ctx.set(dut.i_locked, 1)
        ctx.set(dut.i_halt, 0)
        for _ in range(HOLD):
            await _tick(ctx)
        ctx.set(dut.i_halt, 1)            # program completed
        for _ in range(3 * 2 ** WDOG_BITS):
            await _tick(ctx)
            seen.append(ctx.get(dut.o_rst))

    _sim_boot(dut, scenario)
    assert seen == [0] * (3 * 2 ** WDOG_BITS)


def test_boot_reset_rejects_a_zero_hold_window():
    with pytest.raises(ValueError):
        BootReset(hold=0)
    with pytest.raises(ValueError):
        BootReset(wdog_bits=0)


def test_boot_reset_domain_is_reset_less_and_owns_no_clock():
    # The top must be the one that drives boot.clk from the PLL output; the
    # sequencer itself only declares the domain (a reset-less one, so the core
    # has exactly one reset source).
    dut = _new_dut()
    assert isinstance(dut.cd, ClockDomain)
    assert dut.cd.name == "boot"
    assert dut.cd.rst is None


# --- inter-module wiring ---------------------------------------------------

def test_full_top_wiring_is_exactly_the_documented_one():
    """Every connection that makes this top work is pinned in the Verilog.

    The top is synthesis-only, so these strings are the only automated gate on
    the wiring: bypassing the PLL, the lock gate, the sequencer reset or the
    output shell must fail here.
    """
    _, v = _full_verilog()
    # PLL: 25 MHz pad in, CLKOP/LOCK out on the named nets.
    assert ".i_clk(clk)" in v
    assert ".o_clk(pll_clk)" in v
    assert ".o_locked(o_locked)" in v          # PLL LOCK drives the port
    assert "assign pll_locked = o_locked;" in v
    # The sequencer sees LOCK and the core's halt, and owns the core's reset.
    assert ".i_locked(o_locked)" in v        # sequencer lock gate = PLL LOCK
    assert "assign i_locked = o_locked;" in v
    assert ".i_halt(i_halt)" in v
    assert ".o_rst(boot_rst)" in v
    assert ".rst(boot_rst)" in v             # the core's only reset source
    # Every registered domain is clocked by the PLL output, none by the pad.
    assert "assign boot_clk = pll_clk;" in v
    assert "assign measure_clk = pll_clk;" in v
    assert "assign measure_rst = boot_rst;" in v
    assert re.search(r"assign \\clk\$\d+\s*=\s*pll_clk;", v), "core domain not on pll_clk"
    assert "posedge clk" not in v.split("module \\top")[1]
    # Output shell: each port samples its own core output, through the
    # registered measure domain (a swapped source must fail here).
    core_ports = dict(re.findall(r"\.([\\\w$]+)\s*\(([^)]*)\)", v[v.index("\\top.core  core ("):
                                                          v.index("\\top.pll  pll (")]))
    # The retire value's port name is mangled by Amaranth, so find it by stem.
    # The writeback port's Verilog name depends on how the register file
    # consumes it: the write-through mux of the flop file mangles ``i_wdata``
    # into ``i_wdata$N``, and a build that drives the memory write port directly
    # can leave it as ``w_value``.  No committed top produces the second form
    # today; the fallback keeps the pin working for a ``--ram-rf`` FullTop build.
    value_port = next(k for k in core_ports
                      if "wdata" in k or k == "w_value")
    for out, port in (("o_pc", "o_pc"), ("o_retire", "w_valid"),
                      ("o_retire_pc", "o_retire_pc"),
                      ("o_retire_we", "o_retire_we"),
                      ("o_retire_rd", "i_rd"),
                      ("o_retire_value", value_port),
                      ("o_halt", "o_halt")):
        assert port in core_ports, (
            f"the core instance has no {port} port, so {out} cannot sample it")
        temp = re.search(rf"\b{out}\s*<=\s*(\\\$\d+)\s*;", v).group(1)
        src = re.search(rf"{re.escape(temp)}\s*=\s*([^;]+);", v).group(1).strip()
        assert src == core_ports[port].strip(), (
            f"{out} samples {src}, not the core's {port} ({core_ports[port]})")


def test_pll_error_is_measured_against_the_requested_target():
    # error_hz takes its target, so a legal non-60 MHz set is not nonsense:
    # 5 / 8 / 10 is 40 MHz on a 400 MHz VCO.
    slow = Pll(clkfb_div=8)
    assert slow.f_out_mhz == pytest.approx(40.0)
    assert slow.f_vco_mhz == pytest.approx(400.0)
    assert slow.error_hz() == pytest.approx(20.0e6)
    assert slow.error_hz(40.0) == pytest.approx(0.0)


def test_full_top_uses_the_documented_sequencer_constants():
    # RESULTS/NOTES quote these as design behaviour (8-clock hold, 1.12 s
    # watchdog), so they must not drift silently.
    assert (PROD_BOOT_HOLD, PROD_WDOG_BITS) == (8, 26)
    top = FullTop()
    assert top.boot.hold == PROD_BOOT_HOLD
    assert top.boot.wdog_bits == PROD_WDOG_BITS


def test_ram_rf_knob_builds_the_ram_register_file():
    """``ram_rf=True`` reaches the core through both measurement tops.

    The knob is how the M11 numbers are produced (``tools/synth.py --ram-rf``),
    so both tops must accept it and the core must actually switch files.
    """
    for top_cls in (CoreTop, FullTop):
        flop = top_cls()
        ram = top_cls(ram_rf=True)
        assert flop.core.ram_rf is False
        assert ram.core.ram_rf is True
        # Elaborate both, so a mis-wired variant cannot hide behind the default.
        for top in (flop, ram):
            from amaranth.hdl import Fragment
            Fragment.get(top, None).prepare()

