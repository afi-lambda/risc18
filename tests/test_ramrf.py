"""Tests for :class:`risc18.ramrf.RegFileRam` and ``Core(ram_rf=True)``.

The RAM register file maps to two replicated banks of ``TRELLIS_DPR16X4``
(distributed RAM) instead of 270 flops plus two 16:1 x 18-bit read muxes.  Its
reads are registered, so it has a different pipeline contract from the flop
:class:`risc18.regfile.RegFile`: an address presented in cycle *C* appears as
data in *C + 1*, and there is no same-cycle write-through.  This module pins
both the primitive contract and the core-level consequences (W and W-1
bypasses, the stall-cycle address hold).
"""

import random

from amaranth import ClockDomain, Elaboratable, Module, Signal
from amaranth.sim import Simulator

from risc18 import isa
from risc18.core import Core
from risc18.golden import Machine
from risc18.ramrf import RegFileRam

CLK_PERIOD = 1e-6
SETTLE = 1e-9


# --- instruction encoders ---------------------------------------------------

def addi(rd, rs1, imm6):
    return isa.i_type(isa.OP_ADDI, rd, rs1, imm6)


def alu(fn, rd, rs1, rs2):
    return isa.r_type(isa.OP_ALU, fn, rd, rs1, rs2)


def ld(rd, rs1, imm6):
    return isa.i_type(isa.OP_LD, rd, rs1, imm6)


def st(rs2, rs1, imm6):
    return isa.s_type(isa.OP_ST, rs2, rs1, imm6)


def br(op, rs1, rs2, imm8):
    return isa.b_type(op, rs1, rs2, imm8)


def halt():
    return isa.i_type(isa.OP_SYSTEM, 0, 0, isa.SYS_HALT)


def nop():
    return isa.i_type(isa.OP_SYSTEM, 0, 0, isa.SYS_NOP)


# --- primitive harness ------------------------------------------------------

class RamHarness(Elaboratable):
    """RegFileRam with the ``sync`` domain reset exposed as an input port."""

    def __init__(self, clear=False):
        self.clear = clear
        self.i_rst = Signal()
        self.i_rs1 = Signal(4)
        self.i_rs2 = Signal(4)
        self.o_rs1 = Signal(isa.WIDTH)
        self.o_rs2 = Signal(isa.WIDTH)
        self.i_rd = Signal(4)
        self.i_wdata = Signal(isa.WIDTH)
        self.i_we = Signal(1)

    def elaborate(self, platform):
        m = Module()
        cd = ClockDomain("sync")
        m.domains += cd
        m.d.comb += cd.rst.eq(self.i_rst)
        m.submodules.dut = dut = RegFileRam(clear=self.clear)
        m.d.comb += [
            dut.i_rs1.eq(self.i_rs1),
            dut.i_rs2.eq(self.i_rs2),
            dut.i_rd.eq(self.i_rd),
            dut.i_wdata.eq(self.i_wdata),
            dut.i_we.eq(self.i_we),
            self.o_rs1.eq(dut.o_rs1),
            self.o_rs2.eq(dut.o_rs2),
        ]
        return m


def run_ram(steps):
    """Drive the harness: ``steps`` is a list of dicts of input assignments.

    Returns one ``(pre, post)`` pair per step: ``pre`` is ``(o_rs1, o_rs2)``
    sampled in the same cycle as the step's inputs (after a settle delay, so a
    combinational read would show up there), ``post`` is sampled after that
    step's rising edge.  A registered read leaves ``pre`` at the previous
    value and delivers the addressed data in ``post``.
    """
    harness = RamHarness()
    sim = Simulator(harness)
    sim.add_clock(CLK_PERIOD, domain="sync")
    out = []

    async def tb(ctx):
        ctx.set(harness.i_rst, 1)
        await ctx.tick()
        await ctx.delay(SETTLE)
        ctx.set(harness.i_rst, 0)
        for step in steps:
            ctx.set(harness.i_we, step.get("we", 0))
            ctx.set(harness.i_rd, step.get("rd", 0))
            ctx.set(harness.i_wdata, step.get("wdata", 0))
            ctx.set(harness.i_rs1, step.get("rs1", 0))
            ctx.set(harness.i_rs2, step.get("rs2", 0))
            await ctx.delay(SETTLE)
            pre = (ctx.get(harness.o_rs1), ctx.get(harness.o_rs2))
            await ctx.tick()
            await ctx.delay(SETTLE)
            post = (ctx.get(harness.o_rs1), ctx.get(harness.o_rs2))
            out.append((pre, post))

    sim.add_testbench(tb)
    sim.run()
    return out


# --- primitive contract -----------------------------------------------------

def test_read_data_arrives_one_cycle_after_the_address():
    steps = [
        {"we": 1, "rd": 5, "wdata": 0x123},   # cycle 0: write r5
        {"we": 1, "rd": 6, "wdata": 0x456},   # cycle 1: write r6
        {"rs1": 5, "rs2": 6},                 # cycle 2: address r5/r6
        {"rs1": 0, "rs2": 0},                 # cycle 3: data for cycle 2 appears
    ]
    out = run_ram(steps)
    # The address in step 2 must not change the outputs in that same cycle
    # (pre == the value the previous edge produced) ...
    assert out[2][0] == (0, 0), out[2]
    # ... and its data appears after that step's edge.
    assert out[2][1] == (0x123, 0x456), out[2]
    # Address 0 then reads 0 in the next cycle.
    assert out[3][1] == (0, 0), out[3]


def test_r0_reads_zero_on_both_ports():
    out = run_ram([
        {"we": 1, "rd": 0, "wdata": 0x3FF},   # a write to r0 is discarded
        {"rs1": 0, "rs2": 0},
        {"rs1": 0, "rs2": 0},
    ])
    assert out[2][1] == (0, 0), out[2]


def test_write_is_visible_two_cycles_later():
    out = run_ram([
        {"we": 1, "rd": 7, "wdata": 0x0AB},   # write lands on edge 0
        {"rs1": 7, "rs2": 7},                 # address in cycle 1
        {"rs1": 0, "rs2": 0},
    ])
    assert out[1][1] == (0x0AB, 0x0AB), out[1]


def _drive_with_reset(clear):
    """Write r9, pulse reset, read r9 back: returns the value after the reset."""
    harness = RamHarness(clear=clear)
    sim = Simulator(harness)
    sim.add_clock(CLK_PERIOD, domain="sync")
    got = {}

    async def tb(ctx):
        ctx.set(harness.i_rst, 1)
        await ctx.tick()
        ctx.set(harness.i_rst, 0)
        # write r9 = 0x155
        ctx.set(harness.i_we, 1)
        ctx.set(harness.i_rd, 9)
        ctx.set(harness.i_wdata, 0x155)
        await ctx.tick()
        ctx.set(harness.i_we, 0)
        # pulse the synchronous reset long enough for the clear sequence
        ctx.set(harness.i_rst, 1)
        for _ in range(20):
            await ctx.tick()
        ctx.set(harness.i_rst, 0)
        # address r9 and sample the data one cycle later
        ctx.set(harness.i_rs1, 9)
        ctx.set(harness.i_rs2, 9)
        await ctx.tick()
        await ctx.tick()
        await ctx.delay(SETTLE)
        got["rs1"] = ctx.get(harness.o_rs1)
        got["rs2"] = ctx.get(harness.o_rs2)

    sim.add_testbench(tb)
    sim.run()
    return got


def test_reset_does_not_clear_the_array_by_default():
    """Documented deviation from section 5: DPR16X4 has no reset.

    The flop register file clears every register on the synchronous reset; this
    primitive cannot, so an opt-in RAM file keeps its contents across a reset.
    Designers who need the frozen contract use ``RegFileRam(clear=True)``.
    """
    got = _drive_with_reset(clear=False)
    assert got == {"rs1": 0x155, "rs2": 0x155}, got


def test_clear_option_zeroes_the_array_on_reset():
    got = _drive_with_reset(clear=True)
    assert got == {"rs1": 0, "rs2": 0}, got


# --- core-level: the bypasses the RAM form needs ----------------------------

def _run_core(program, data=None, ram_rf=True, max_cycles=4000):
    core = Core(program, data=data, ram_rf=ram_rf)
    sim = Simulator(core)
    sim.add_clock(CLK_PERIOD, domain="sync")
    rows = []

    async def tb(ctx):
        ctx.set(core.i_rst, 1)
        await ctx.tick()
        ctx.set(core.i_rst, 0)
        for _ in range(max_cycles):
            await ctx.tick()
            await ctx.delay(SETTLE)
            if ctx.get(core.o_retire):
                rows.append((ctx.get(core.o_retire_pc),
                             ctx.get(core.o_retire_we),
                             ctx.get(core.o_retire_rd),
                             ctx.get(core.o_retire_value)))
            if ctx.get(core.o_halt):
                break

    sim.add_testbench(tb)
    sim.run()
    return rows


def _golden(program, data=None):
    machine = Machine(program)
    if data:
        preloaded = [w & isa.MASK for w in list(data)[:isa.MEM_WORDS]]
        machine.mem[:len(preloaded)] = preloaded
    return machine.run(100_000)


def _check(program, data=None):
    machine = _golden(program, data)
    rows = _run_core(program, data)
    assert [(pc, rd, v) for pc, we, rd, v in rows if we and rd != 0] == machine.trace
    assert len(rows) == machine.retired
    return rows


def test_distance_one_two_three_and_four_dependencies_are_all_forwarded():
    """Every producer distance the pipeline can present: X, M, W and W-1.

    ``i`` produces, ``i+1`` is distance 1 (X bypass), ``i+2`` distance 2 (M),
    ``i+3`` distance 3 (W, and on the RAM file the ``w_hit`` bypass) and
    ``i+4`` distance 4 (W-1, the ``wq_hit`` bypass).
    """
    program = [
        addi(1, 0, 1),        # 0: producer, consumer at 1 is distance 1
        addi(2, 1, 1),        # 1: r2 = r1 + 1 = 2
        nop(),                # 2
        addi(3, 2, 1),        # 3: r3 = r2 + 1 = 3, distance 2
        nop(),                # 4
        nop(),                # 5
        addi(4, 3, 1),        # 6: r4 = r3 + 1 = 4, distance 3
        nop(),                # 7
        nop(),                # 8
        nop(),                # 9
        addi(5, 4, 1),        # 10: r5 = r4 + 1 = 5, distance 4
        alu(isa.ALU_ADD, 6, 5, 5),   # 11: r6 = 10, distance 1
        halt(),               # 12
    ]
    rows = _check(program)
    writes = {rd: v for _, rd, v in
              [(pc, rd, v) for pc, we, rd, v in rows if we and rd != 0]}
    assert writes[2] == 2 and writes[3] == 3 and writes[4] == 4
    assert writes[5] == 5, writes
    assert writes[6] == 10, writes


def test_load_use_stall_reads_the_right_operand_after_the_bubble():
    """The RAM address is held to D's own operands while D is stalled."""
    program = [
        addi(1, 0, 0),
        addi(2, 0, 7),
        st(2, 1, 0),          # mem[0] = 7
        ld(3, 1, 0),          # r3 = mem[0]
        alu(isa.ALU_ADD, 4, 3, 2),   # distance 1 behind the load: one bubble
        halt(),
    ]
    _check(program)


def test_accumulation_loop_with_loads_and_stores_matches_golden():
    program = [
        addi(1, 0, 0),
        addi(2, 0, 0),
        addi(3, 0, 3),
        addi(6, 0, -5),
        st(3, 1, 0),
        ld(4, 1, 0),
        alu(isa.ALU_ADD, 2, 2, 4),
        addi(1, 1, 1),
        addi(6, 6, 1),
        br(isa.OP_BNE, 6, 0, -6),
        halt(),
    ]
    rows = _check(program)
    assert [v for _, rd, v in
            [(pc, rd, v) for pc, we, rd, v in rows if we and rd != 0] if rd == 2][-1] == 15


def test_seeded_random_programs_lockstep_with_golden():
    """Same generator shape as the flop-file random test, on the RAM file."""
    rng = random.Random(0x5EED)
    for _ in range(3):
        program = []
        live = [0]
        for _ in range(24):
            kind = rng.choice(["addi", "alu", "ld", "st"])
            if kind == "addi":
                rd = rng.randrange(1, 9)
                program.append(addi(rd, rng.choice(live + [0]), rng.randrange(-8, 8)))
                live.append(rd)
            elif kind == "alu":
                rd = rng.randrange(1, 9)
                program.append(alu(rng.choice([isa.ALU_ADD, isa.ALU_SUB,
                                               isa.ALU_OR, isa.ALU_AND]),
                                   rd, rng.choice(live + [0]), rng.choice(live + [0])))
                live.append(rd)
            elif kind == "ld":
                rd = rng.randrange(1, 9)
                program.append(ld(rd, rng.choice(live + [0]), rng.randrange(0, 12)))
                live.append(rd)
            else:
                program.append(st(rng.choice(live + [0]), rng.choice(live + [0]),
                                  rng.randrange(0, 12)))
        program.append(halt())
        _check(program)

def test_clear_walk_latency_matches_the_documented_count():
    """Pin the documented count: r14 reads zero on cycle 16, r15 on 17.

    The convention is explicit, because an off-by-one here is exactly what this
    test exists to stop: cycle 1 is the first rising edge after the deasserting
    reset edge, and the value sampled on cycle *k* is the data the read issued on
    cycle *k - 1* produced.  The 16-cycle walk then leaves r14 (written one step
    before the end) reading zero on cycle 16 and r15 on cycle 17 -- one cycle
    later because r15's write shares an edge with the read that would expose it
    (read-first).  Both numbers appear in ramrf.py and docs/metrics/NOTES.md.
    """
    harness = RamHarness(clear=True)
    sim = Simulator(harness)
    sim.add_clock(CLK_PERIOD, domain="sync")
    samples = []

    async def tb(ctx):
        ctx.set(harness.i_rst, 1)
        await ctx.tick()
        ctx.set(harness.i_rst, 0)
        for _ in range(20):          # let the power-on walk finish
            await ctx.tick()
        for reg in (14, 15):         # dirty the two registers the walk ends on
            ctx.set(harness.i_we, 1)
            ctx.set(harness.i_rd, reg)
            ctx.set(harness.i_wdata, 0x100 + reg)
            await ctx.tick()
        ctx.set(harness.i_we, 0)
        ctx.set(harness.i_rst, 1)
        await ctx.tick()             # exactly one reset cycle
        ctx.set(harness.i_rst, 0)
        ctx.set(harness.i_rs1, 15)
        ctx.set(harness.i_rs2, 14)
        for cycle in range(1, 25):
            await ctx.tick()         # after this edge we are in cycle `cycle`
            await ctx.delay(SETTLE)
            samples.append((cycle, ctx.get(harness.o_rs1),
                            ctx.get(harness.o_rs2)))

    sim.add_testbench(tb)
    sim.run()

    r15_zero = next(c for c, a, b in samples if a == 0)
    r14_zero = next(c for c, a, b in samples if b == 0)
    assert (r14_zero, r15_zero) == (16, 17), samples[:20]
    assert samples[-1][1] == 0 and samples[-1][2] == 0, samples[-1]
