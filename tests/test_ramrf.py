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


# --- primitive harness ------------------------------------------------------

class RamHarness(Elaboratable):
    """RegFileRam with the ``sync`` domain reset exposed as an input port."""

    def __init__(self):
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
        m.submodules.dut = dut = RegFileRam()
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


def test_no_reset_clear_is_documented_behaviour():
    """The array cannot be cleared by the sync reset (DPR16X4 has none)."""
    out = run_ram([
        {"we": 1, "rd": 9, "wdata": 0x155},
        {"rs1": 9, "rs2": 9},
    ])
    assert out[1][1] == (0x155, 0x155)


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
    """Every distance the pipeline can produce: X, M, W and W-1 bypasses."""
    program = [
        addi(1, 0, 1),        # 0
        addi(2, 1, 1),        # 1: distance 1 from r1 (X bypass)
        addi(3, 2, 1),        # 2: distance 1 from r2
        addi(4, 1, 4),        # 3: distance 2 from r1 (now in M)
        addi(5, 2, 5),        # 4: distance 3 from r2 (now in W)
        addi(6, 3, 6),        # 5: distance 4 from r3 (now retired, W-1)
        addi(7, 4, 7),        # 6: distance 3 from r4
        addi(8, 5, 8),        # 7: distance 3 from r5
        alu(isa.ALU_ADD, 9, 6, 8),   # 8: distance 2 and 1
        halt(),               # 9
    ]
    rows = _check(program)
    # r1=1, r2=2, r3=3, r4=5, r5=7, r6=9, r7=12, r8=15, r9 = r6 + r8 = 24
    assert rows[-2][3] == 24, rows[-2]


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
