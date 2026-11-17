"""Amaranth simulation tests for the M1 16 x 18 register file.

Every test drives the RTL through ``amaranth.sim.Simulator``; nothing here
reaches into the register array.  Reset is exercised for real: the harness
declares the ``sync`` domain itself and ties ``cd.rst`` to a port, so the
testbench can pulse reset and observe that all 16 registers clear.

Contract under test (docs/ISA.md section 5): two independent combinational
read ports, one synchronous write port, ``r0`` reads 0 / discards writes, and
same-cycle write-through on whichever read port addresses the written register.
"""

import random

from amaranth import ClockDomain, Elaboratable, Module, Signal
from amaranth.sim import Simulator

from risc18 import isa
from risc18.regfile import RegFile
from risc18.tops import RegFileTop

MASK = isa.MASK
N_REGS = 16
SETTLE = 1e-9
CLK_PERIOD = 1e-6


class RegFileHarness(Elaboratable):
    """RegFile with the ``sync`` domain reset exposed as a real input port."""

    def __init__(self):
        self.rst = Signal()
        self.dut = RegFile()

    def elaborate(self, platform):
        m = Module()
        cd = ClockDomain("sync")
        m.domains.sync = cd
        m.d.comb += cd.rst.eq(self.rst)
        m.submodules.dut = self.dut
        return m


class RegFileTopHarness(Elaboratable):
    """Drive :class:`RegFileTop`'s ``clk`` port from a simulation clock domain.

    ``RegFileTop`` owns a reset-less ``sync`` domain internally, so the
    testbench drives ``top.clk`` from a separate ``tck`` domain instead of
    adding a clock process directly on the top's own domain.
    """

    def __init__(self):
        self.top = RegFileTop()

    def elaborate(self, platform):
        m = Module()
        cd = ClockDomain("tck")
        m.domains.tck = cd
        m.d.comb += self.top.clk.eq(cd.clk)
        m.submodules.top = self.top
        return m


# --- testbench helpers ------------------------------------------------------

async def _reset(ctx, h):
    """Hold the synchronous reset across one edge, then release it."""
    ctx.set(h.dut.i_we, 0)
    ctx.set(h.rst, 1)
    await ctx.tick()
    ctx.set(h.rst, 0)
    await ctx.tick()


async def _write(ctx, h, rd, data):
    ctx.set(h.dut.i_rd, rd)
    ctx.set(h.dut.i_wdata, data & MASK)
    ctx.set(h.dut.i_we, 1)
    await ctx.tick()
    ctx.set(h.dut.i_we, 0)
    await ctx.delay(SETTLE)


async def _read(ctx, h, rs1, rs2):
    ctx.set(h.dut.i_rs1, rs1)
    ctx.set(h.dut.i_rs2, rs2)
    ctx.set(h.dut.i_we, 0)
    await ctx.delay(SETTLE)
    return ctx.get(h.dut.o_rs1), ctx.get(h.dut.o_rs2)


def _run(h, tb):
    sim = Simulator(h)
    sim.add_clock(CLK_PERIOD)
    sim.add_testbench(tb, background=False)
    sim.run()


# --- tests ------------------------------------------------------------------

def test_reset_clears_all_registers():
    h = RegFileHarness()
    values = {r: 0x20000 + r for r in range(1, N_REGS)}
    observed = {}

    async def tb(ctx):
        await _reset(ctx, h)
        for r, v in values.items():
            await _write(ctx, h, r, v)
        # Non-zero before the second reset, so a stale array would show.
        observed["pre"] = {r: await _read(ctx, h, r, r) for r in values}
        await _reset(ctx, h)
        observed["post"] = {r: await _read(ctx, h, r, r) for r in values}

    _run(h, tb)
    for r, v in values.items():
        assert observed["pre"][r] == (v, v), \
            f"r{r} before reset: {observed['pre'][r]} != {(v, v)}"
        assert observed["post"][r] == (0, 0), \
            f"r{r} survived reset: {observed['post'][r]}"


def test_write_then_read_both_ports():
    h = RegFileHarness()
    seen = []

    async def tb(ctx):
        await _reset(ctx, h)
        await _write(ctx, h, 5, 0x12345)
        await _write(ctx, h, 9, 0x2AAAA)
        seen.append(await _read(ctx, h, 5, 5))
        seen.append(await _read(ctx, h, 5, 9))
        seen.append(await _read(ctx, h, 9, 5))
        seen.append(await _read(ctx, h, 9, 9))

    _run(h, tb)
    assert seen == [(0x12345, 0x12345), (0x12345, 0x2AAAA),
                    (0x2AAAA, 0x12345), (0x2AAAA, 0x2AAAA)], seen


def test_two_reads_same_cycle_are_independent():
    h = RegFileHarness()
    seen = []

    async def tb(ctx):
        await _reset(ctx, h)
        await _write(ctx, h, 2, 0x00003)
        await _write(ctx, h, 11, 0x3FFFC)
        ctx.set(h.dut.i_we, 0)
        # One settle, both ports sampled in the same cycle.
        ctx.set(h.dut.i_rs1, 2)
        ctx.set(h.dut.i_rs2, 11)
        await ctx.delay(SETTLE)
        seen.append((ctx.get(h.dut.o_rs1), ctx.get(h.dut.o_rs2)))
        ctx.set(h.dut.i_rs1, 11)
        ctx.set(h.dut.i_rs2, 2)
        await ctx.delay(SETTLE)
        seen.append((ctx.get(h.dut.o_rs1), ctx.get(h.dut.o_rs2)))

    _run(h, tb)
    assert seen == [(0x00003, 0x3FFFC), (0x3FFFC, 0x00003)], seen


def test_write_to_r0_ignored():
    h = RegFileHarness()
    seen = []

    async def tb(ctx):
        await _reset(ctx, h)
        await _write(ctx, h, 1, 0x0002A)
        await _write(ctx, h, 0, 0x3FFFF)
        seen.append((await _read(ctx, h, 0, 0), await _read(ctx, h, 0, 1),
                     await _read(ctx, h, 1, 0)))

    _run(h, tb)
    r0, r0_r1, r1_r0 = seen[0]
    assert r0 == (0, 0), r0
    assert r0_r1 == (0, 0x0002A), r0_r1
    assert r1_r0 == (0x0002A, 0), r1_r0


def test_r0_reads_zero_even_during_write_attempt():
    h = RegFileHarness()
    seen = []

    async def tb(ctx):
        await _reset(ctx, h)
        # Same-cycle write-through must not leak into r0 on either port.
        ctx.set(h.dut.i_rd, 0)
        ctx.set(h.dut.i_wdata, 0x3ABCD)
        ctx.set(h.dut.i_we, 1)
        ctx.set(h.dut.i_rs1, 0)
        ctx.set(h.dut.i_rs2, 0)
        await ctx.delay(SETTLE)
        seen.append((ctx.get(h.dut.o_rs1), ctx.get(h.dut.o_rs2)))
        await ctx.tick()
        ctx.set(h.dut.i_we, 0)
        seen.append(await _read(ctx, h, 0, 0))
        await _write(ctx, h, 7, 0x1F0F0)
        await _write(ctx, h, 0, 0x00001)
        seen.append(await _read(ctx, h, 0, 7))

    _run(h, tb)
    assert seen[0] == (0, 0), seen[0]
    assert seen[1] == (0, 0), seen[1]
    assert seen[2] == (0, 0x1F0F0), seen[2]


def test_write_through_both_ports_same_cycle():
    h = RegFileHarness()
    seen = []

    async def tb(ctx):
        await _reset(ctx, h)
        await _write(ctx, h, 7, 0x0DEAD)
        # With the write port idle both ports see the stored value.
        seen.append(await _read(ctx, h, 7, 7))
        # Write r7 while both ports read r7: same-cycle bypass on both.
        ctx.set(h.dut.i_rd, 7)
        ctx.set(h.dut.i_wdata, 0x2BEEF)
        ctx.set(h.dut.i_we, 1)
        ctx.set(h.dut.i_rs1, 7)
        ctx.set(h.dut.i_rs2, 7)
        await ctx.delay(SETTLE)
        seen.append((ctx.get(h.dut.o_rs1), ctx.get(h.dut.o_rs2)))
        # Only rs1 matches the destination: rs2 must not be bypassed.
        ctx.set(h.dut.i_rs2, 4)
        await ctx.delay(SETTLE)
        seen.append((ctx.get(h.dut.o_rs1), ctx.get(h.dut.o_rs2)))
        # Only rs2 matches.
        ctx.set(h.dut.i_rs1, 4)
        ctx.set(h.dut.i_rs2, 7)
        await ctx.delay(SETTLE)
        seen.append((ctx.get(h.dut.o_rs1), ctx.get(h.dut.o_rs2)))
        # After the edge the value is committed and persists.
        await ctx.tick()
        ctx.set(h.dut.i_we, 0)
        seen.append(await _read(ctx, h, 7, 7))

    _run(h, tb)
    assert seen[0] == (0x0DEAD, 0x0DEAD), seen[0]
    assert seen[1] == (0x2BEEF, 0x2BEEF), seen[1]
    assert seen[2] == (0x2BEEF, 0), seen[2]
    assert seen[3] == (0, 0x2BEEF), seen[3]
    assert seen[4] == (0x2BEEF, 0x2BEEF), seen[4]


def test_write_enable_low_leaves_state_unchanged():
    h = RegFileHarness()
    seen = []

    async def tb(ctx):
        await _reset(ctx, h)
        await _write(ctx, h, 4, 0x15555)
        # Present a different value on the write port with i_we low across
        # several edges; nothing may change.
        ctx.set(h.dut.i_rd, 4)
        ctx.set(h.dut.i_wdata, 0x0AAAA)
        ctx.set(h.dut.i_we, 0)
        for _ in range(3):
            await ctx.tick()
        seen.append(await _read(ctx, h, 4, 4))
        await _write(ctx, h, 4, 0x0AAAA)
        seen.append(await _read(ctx, h, 4, 4))

    _run(h, tb)
    assert seen[0] == (0x15555, 0x15555), seen[0]
    assert seen[1] == (0x0AAAA, 0x0AAAA), seen[1]


def test_random_sequence_against_python_model():
    """>= 300 seeded ops, interleaved writes/reads vs a plain 16-entry model."""
    rng = random.Random(0xC0FFEE)
    model = [0] * N_REGS
    script = []
    for _ in range(360):
        if rng.random() < 0.5:
            rd = rng.randrange(N_REGS)
            wdata = rng.randrange(1 << isa.WIDTH)
            script.append(("w", rd, wdata, None))
            if rd != 0:
                model[rd] = wdata
        else:
            rs1 = rng.randrange(N_REGS)
            rs2 = rng.randrange(N_REGS)
            script.append(("r", rs1, rs2, (model[rs1], model[rs2])))
    assert sum(1 for op in script if op[0] == "w") >= 100
    assert sum(1 for op in script if op[0] == "r") >= 100

    h = RegFileHarness()
    seen = []

    async def tb(ctx):
        await _reset(ctx, h)
        for kind, a, b, expected in script:
            if kind == "w":
                await _write(ctx, h, a, b)
            else:
                seen.append((a, b, expected, await _read(ctx, h, a, b)))

    _run(h, tb)
    bad = [row for row in seen if row[3] != row[2]]
    assert not bad, f"{len(bad)} mismatches, first: {bad[0]}"
    assert len(seen) >= 150, len(seen)


def test_regfile_top_registers_inputs_and_outputs():
    """Synthesis top: registered in, registered out."""
    h = RegFileTopHarness()
    top = h.top
    seen = []

    async def tb(ctx):
        ctx.set(top.i_we, 0)
        await ctx.tick("tck")
        await ctx.tick("tck")
        ctx.set(top.i_rs1, 3)
        ctx.set(top.i_rs2, 3)
        ctx.set(top.i_rd, 3)
        ctx.set(top.i_wdata, 0x07777)
        ctx.set(top.i_we, 1)
        await ctx.tick("tck")          # samples inputs
        ctx.set(top.i_we, 0)
        await ctx.tick("tck")          # commits the write, captures o_rs1
        await ctx.delay(SETTLE)
        seen.append((ctx.get(top.o_rs1), ctx.get(top.o_rs2)))
        ctx.set(top.i_rs2, 0)
        await ctx.tick("tck")
        await ctx.tick("tck")     # two-cycle port-to-port latency
        await ctx.delay(SETTLE)
        seen.append((ctx.get(top.o_rs1), ctx.get(top.o_rs2)))

    sim = Simulator(h)
    sim.add_clock(CLK_PERIOD, domain="tck")
    sim.add_testbench(tb, background=False)
    sim.run()
    assert seen[0] == (0x07777, 0x07777), seen[0]
    assert seen[1] == (0x07777, 0), seen[1]
