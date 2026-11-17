"""Amaranth simulation tests for the M5 four-stage core (F/D/X/W, no dmem).

Every test drives ``risc18.core.Core`` through ``amaranth.sim.Simulator``; the
core owns its reset-bearing ``sync`` domain, so the testbench only drives
``i_rst`` and the simulator supplies the clock for that domain.  Nothing here
reads an internal signal: the observation surface is the retire port
(``o_retire``/``o_retire_pc``/``o_retire_we``/``o_retire_rd``/``o_retire_value``)
plus ``o_pc`` and ``o_halt``.

The acceptance oracle is ``risc18.golden.Machine``: the ``we=1, rd!=0`` retire
rows must equal its ``.trace`` in order, and the number of retire pulses must
equal ``.retired`` -- the golden model counts every instruction it executed,
including branches, stores, NOPs and the HALT, so a lost or duplicated
instruction cannot hide behind a register-write-only comparison.

M5 scope: LD/ST are no-ops (see ``test_ld_st_are_no_ops_in_m5``), so programs
containing them are checked against a hand-written trace rather than golden,
which does model data memory.
"""

import random
from typing import NamedTuple

from amaranth import ClockDomain, Elaboratable, Module, Signal
from amaranth.hdl import Fragment
from amaranth.sim import Simulator

from risc18 import isa
from risc18.core import Core
from risc18.golden import Machine
from risc18.tops import CORE_TOP_PROGRAM, CoreTop

CLK_PERIOD = 1e-6
SETTLE = 1e-9


# --- instruction encoders ---------------------------------------------------
# Thin wrappers over risc18.isa so the programs below read as assembly.

def addi(rd, rs1, imm6):
    return isa.i_type(isa.OP_ADDI, rd, rs1, imm6)


def andi(rd, rs1, imm6):
    return isa.i_type(isa.OP_ANDI, rd, rs1, imm6)


def ori(rd, rs1, imm6):
    return isa.i_type(isa.OP_ORI, rd, rs1, imm6)


def alu(fn, rd, rs1, rs2):
    return isa.r_type(isa.OP_ALU, fn, rd, rs1, rs2)


def mul(rd, rs1, rs2):
    return isa.r_type(isa.OP_MUL, 0, rd, rs1, rs2)


def shift(rd, rs1, kind, amount):
    return isa.i_type(isa.OP_SHIFT, rd, rs1, (kind << 4) | amount)


def lui(rd, imm10):
    return isa.u_type(isa.OP_LUI, rd, imm10)


def br(op, rs1, rs2, imm6):
    return isa.b_type(op, rs2, rs1, imm6)


def jal(rd, imm10):
    return isa.j_type(isa.OP_JAL, rd, imm10)


def jalr(rd, rs1, imm6):
    return isa.i_type(isa.OP_JALR, rd, rs1, imm6)


def ld(rd, rs1, imm6):
    return isa.i_type(isa.OP_LD, rd, rs1, imm6)


def st(rs2, rs1, imm6):
    return isa.s_type(isa.OP_ST, rs2, rs1, imm6)


def nop():
    return isa.i_type(isa.OP_SYSTEM, 0, 0, isa.SYS_NOP)


def halt():
    return isa.i_type(isa.OP_SYSTEM, 0, 0, isa.SYS_HALT)


# --- testbench driver -------------------------------------------------------

class Run(NamedTuple):
    """Retire trace of one simulation run.

    ``retires`` holds ``(cycle, pc, we, rd, value)`` for every cycle
    ``o_retire`` was high, ``cycle`` counted from the first edge after the
    reset edge; ``post`` holds ``(retire, pc, halt)`` sampled after ``o_halt``
    was first observed; ``halted`` says whether the run stopped on ``o_halt``
    instead of the cycle cap.
    """

    retires: list
    post: list
    halted: bool

    def writes(self):
        """Architectural write trace: ``(pc, rd, value)`` for we=1, rd != 0."""
        return [(pc, rd, v) for _, pc, we, rd, v in self.retires if we and rd != 0]

    @property
    def cycles(self):
        """Cycles from the first retire to the last, inclusive."""
        if not self.retires:
            return 0
        return self.retires[-1][0] - self.retires[0][0] + 1


async def _drive(ctx, dut, max_cycles, post_cycles):
    retires, post = [], []
    ctx.set(dut.i_rst, 1)
    await ctx.tick()
    await ctx.delay(SETTLE)
    ctx.set(dut.i_rst, 0)

    halted = False
    for cycle in range(1, max_cycles + 1):
        await ctx.tick()
        await ctx.delay(SETTLE)
        if ctx.get(dut.o_retire):
            retires.append((cycle, ctx.get(dut.o_retire_pc),
                            ctx.get(dut.o_retire_we), ctx.get(dut.o_retire_rd),
                            ctx.get(dut.o_retire_value)))
        if ctx.get(dut.o_halt):
            halted = True
            break

    for _ in range(post_cycles):
        await ctx.tick()
        await ctx.delay(SETTLE)
        post.append((ctx.get(dut.o_retire), ctx.get(dut.o_pc),
                     ctx.get(dut.o_halt)))

    return Run(retires, post, halted)


def run_core(program, max_cycles=4000, post_cycles=8):
    """Run ``program`` on a :class:`Core` and return its retire trace."""
    core = Core(program)
    sim = Simulator(core)
    sim.add_clock(CLK_PERIOD, domain="sync")

    holder = {}

    async def tb(ctx):
        holder["run"] = await _drive(ctx, core, max_cycles, post_cycles)

    sim.add_testbench(tb)
    sim.run()
    return holder["run"]


def assert_lockstep(program, max_cycles=None):
    """Compare a whole run against ``golden.Machine``; return ``(machine, run)``."""
    machine = Machine(program).run(100_000)
    assert machine.halted, "golden model did not halt: bad test program"
    if max_cycles is None:
        max_cycles = 10 * machine.retired + 100
    run = run_core(program, max_cycles=max_cycles)
    assert run.halted, f"core did not halt in {max_cycles} cycles"
    assert run.writes() == machine.trace, _diff(run.writes(), machine.trace)
    assert len(run.retires) == machine.retired, (
        f"{len(run.retires)} retire pulses for {machine.retired} executed "
        f"instructions")
    return machine, run


def _diff(got, want):
    for i, (a, b) in enumerate(zip(got, want)):
        if a != b:
            return (f"write {i}: got pc={a[0]} r{a[1]}={a[2]:#07x}, "
                    f"want pc={b[0]} r{b[1]}={b[2]:#07x}")
    return f"{len(got)} vs {len(want)} writes"


# --- straight-line arithmetic ----------------------------------------------

def test_straight_line_alu_addi_matches_golden():
    """ADDI + ALU add/sub/and/or, plus the ANDI/ORI immediates, lockstep."""
    program = [
        addi(1, 0, 5),
        addi(2, 0, 7),
        alu(isa.ALU_ADD, 3, 1, 2),
        alu(isa.ALU_SUB, 4, 2, 1),
        alu(isa.ALU_AND, 5, 1, 2),
        alu(isa.ALU_OR, 6, 1, 3),
        addi(7, 6, -1),
        andi(8, 7, 0x2A),
        ori(9, 8, 0x15),
        addi(10, 0, -32),          # sign-extended imm6 boundary
        halt(),
    ]
    machine, run = assert_lockstep(program)
    assert len(machine.trace) == 10
    # The retired write trace is in program order, non-writing rows included.
    assert [pc for _, pc, _, _, _ in run.retires] == list(range(len(program)))


def test_back_to_back_raw_chains_match_golden():
    """Distance-1 RAW chains: ALU->ALU, ALU->ADDI->ALU, in both operands."""
    program = [
        addi(1, 0, 3),
        addi(2, 1, 4),             # r2 = r1 + 4   (distance 1)
        alu(isa.ALU_SUB, 3, 2, 1),  # r3 = r2 - r1  (distance 1)
        alu(isa.ALU_ADD, 4, 3, 3),  # both operands are the previous result
        alu(isa.ALU_AND, 5, 4, 2),  # rs1 distance 1, rs2 distance 3
        addi(6, 5, -1),
        alu(isa.ALU_OR, 7, 6, 5),
        halt(),
    ]
    machine, run = assert_lockstep(program)
    assert machine.trace[-1][1] == 7           # the chain really is chained
    assert run.halted


def test_raw_distance_two_and_three_do_not_stall():
    """W-stage write-through covers a producer two words ahead (and beyond).

    M5 has no forwarding: a dependency on X costs one bubble, but a dependency
    on W is served by the RegFile's same-cycle write-through, so the retire
    stream must stay gapless.
    """
    program = [addi(1, 0, 1), addi(2, 0, 2)]
    for rd in range(3, 12):
        program.append(addi(rd, rd - 2, 1))    # each reads two words back
    program.append(halt())
    machine, run = assert_lockstep(program)
    n = len(program)
    assert len(run.retires) == n
    assert run.cycles == n, f"{run.cycles} cycles for {n} instructions"


# --- control flow -----------------------------------------------------------

def test_branches_all_conditions_taken_and_not_taken():
    """BEQ/BNE/BLT/BGE, each taken and not taken, with negative operands."""
    program = [
        addi(1, 0, -2),                 # r1 = 0x3FFFE (-2)
        addi(2, 0, 3),                  # r2 = 3
        br(isa.OP_BLT, 1, 2, 2),        # taken: -2 < 3
        addi(5, 0, 111),                # squashed
        addi(6, 0, 222),                # squashed
        br(isa.OP_BGE, 1, 2, 2),        # not taken: -2 >= 3 is false
        addi(7, 0, 7),
        br(isa.OP_BGE, 2, 1, 2),        # taken: 3 >= -2
        addi(8, 0, 8),                  # squashed
        addi(9, 0, 9),                  # squashed
        br(isa.OP_BEQ, 1, 1, 2),        # taken: equal
        addi(10, 0, 10),                # squashed
        addi(11, 0, 11),                # squashed
        br(isa.OP_BNE, 1, 1, 2),        # not taken: equal
        addi(12, 0, 12),
        br(isa.OP_BLT, 2, 1, 2),        # not taken: 3 < -2 is false
        addi(13, 0, 13),
        br(isa.OP_BNE, 1, 2, 1),        # taken: not equal, skips one word
        addi(14, 0, 14),                # squashed
        addi(15, 0, 15),
        halt(),
    ]
    machine, run = assert_lockstep(program)
    written = {pc for pc, _, _ in run.writes()}
    # The squashed fall-throughs never retire...
    assert not written & {3, 4, 8, 9, 11, 12, 18}
    # ...while the instructions at every branch target do.
    assert {6, 14, 16, 19} <= written
    retired_pcs = [pc for _, pc, _, _, _ in run.retires]
    assert retired_pcs.count(2) == 1 and retired_pcs.count(3) == 0
    assert retired_pcs.count(5) == 1 and retired_pcs.count(13) == 1


def test_branch_over_a_halt_does_not_halt():
    """A HALT in a squashed slot must never reach D, or the machine freezes."""
    program = [
        addi(1, 0, 1),
        br(isa.OP_BEQ, 1, 1, 1),       # taken: skips the HALT on the next word
        halt(),                        # squashed: must not stop the machine
        addi(2, 0, 2),
        halt(),                        # the real one
    ]
    machine, run = assert_lockstep(program)
    assert [pc for _, pc, _, _, _ in run.retires] == [0, 1, 3, 4]
    assert run.writes() == [(0, 1, 1), (3, 2, 2)] == machine.trace
    assert run.halted and run.retires[-1][1] == 4


def test_counted_loop_matches_golden():
    """A five-iteration decrement loop: backward taken branch every pass."""
    program = [
        addi(1, 0, 5),                 # 0: counter
        addi(2, 0, 0),                 # 1: accumulator
        addi(2, 2, 3),                 # 2: acc += 3
        addi(1, 1, -1),                # 3: counter -= 1
        br(isa.OP_BNE, 1, 0, -3),      # 4: loop while counter != 0
        ori(3, 2, 0x20),               # 5: acc | 0x20
        halt(),                        # 6
    ]
    machine, run = assert_lockstep(program)
    assert run.writes()[2] == (2, 2, 3)         # first pass through the loop
    assert len(run.retires) == machine.retired == 19
    assert run.writes()[-1] == (5, 3, 15 | 0x20)


def test_jal_link_value_and_target():
    """JAL writes pc+1 and redirects; the two slots after it never retire."""
    program = [
        jal(1, 2),                     # 0: r1 = 1, -> 3
        addi(2, 0, 1),                 # 1: squashed
        addi(2, 0, 2),                 # 2: squashed
        addi(3, 0, 3),                 # 3
        jal(4, 1),                     # 4: r4 = 5, -> 6
        addi(5, 0, 5),                 # 5: squashed
        addi(6, 0, 6),                 # 6
        halt(),                        # 7
    ]
    machine, run = assert_lockstep(program)
    assert run.writes() == [(0, 1, 1), (3, 3, 3), (4, 4, 5), (6, 6, 6)]
    assert [pc for _, pc, _, _, _ in run.retires] == [0, 3, 4, 6, 7]


def test_jalr_indirect_target_and_link():
    """JALR jumps to rs1+imm (no +1) and links pc+1."""
    program = [
        addi(7, 0, 9),                 # 0: r7 = 9
        jalr(1, 7, 1),                 # 1: r1 = 2, -> 10 (rs1 + imm, no +1)
        addi(2, 0, 1),                 # 2: squashed
        addi(2, 0, 2),                 # 3: squashed
        nop(),                         # 4
        nop(),                         # 5
        nop(),                         # 6
        nop(),                         # 7
        nop(),                         # 8
        nop(),                         # 9
        addi(3, 0, 3),                 # 10: the indirect target
        addi(8, 0, 13),                # 11: second base
        jalr(4, 8, 1),                 # 12: r4 = 13, -> 14
        addi(5, 0, 5),                 # 13: fall-through still executes
        addi(6, 0, 6),                 # 14
        halt(),                        # 15
    ]
    machine, run = assert_lockstep(program)
    assert run.writes() == machine.trace
    assert (1, 1, 2) in run.writes() and (12, 4, 13) in run.writes()
    retired_pcs = [pc for _, pc, _, _, _ in run.retires]
    assert retired_pcs[:3] == [0, 1, 10]
    assert 2 not in retired_pcs and 3 not in retired_pcs


def test_lui_writes_imm10_shifted():
    """LUI: rd = imm10 << 8, truncated to 18 bits."""
    program = [
        lui(1, 0x000),
        lui(2, 0x001),
        lui(3, 0x2AA),
        lui(4, 0x3FF),
        halt(),
    ]
    machine, run = assert_lockstep(program)
    assert run.writes() == [(0, 1, 0), (1, 2, 0x100), (2, 3, 0x2AA00),
                            (3, 4, 0x3FF00)]
    assert machine.trace == run.writes()


# --- X-stage operations -----------------------------------------------------

def test_shifts_all_kinds_with_amounts_zero_and_fifteen():
    """SLL/SRL/SRA/ROR with amounts 0 and 15 (and a few in between)."""
    program = [addi(1, 0, -3)]         # 0x3FFFD: sign bit set, low bits set
    rd = 2
    for kind in (isa.SHIFT_SLL, isa.SHIFT_SRL, isa.SHIFT_SRA, isa.SHIFT_ROR):
        for amount in (0, 15):
            program.append(shift(rd, 1, kind, amount))
            rd += 1
    for kind in (isa.SHIFT_SLL, isa.SHIFT_SRA, isa.SHIFT_ROR):
        program.append(shift(rd, 1, kind, 7))
        rd += 1
    program.append(halt())
    machine, run = assert_lockstep(program)
    w = run.writes()
    assert len(w) == 1 + (rd - 2)                  # the ADDI plus every shift
    value = 0x3FFFD                                # -3: sign bit and low bits set
    # Amount 0 is the identity for every kind, including the rotate.
    assert [w[i][2] for i in (1, 3, 5, 7)] == [value] * 4
    assert w[2][2] == (value << 15) & isa.MASK     # SLL 15 keeps the low 3 bits
    assert w[4][2] == 0x00007                      # SRL 15: three bits left
    assert w[6][2] == 0x3FFFF                      # SRA 15 fills with the sign
    assert w[8][2] == ((value >> 15) | (value << 3)) & isa.MASK   # ROR 15


def test_mul_low_eighteen_bits():
    """MUL returns (rs1 * rs2)[17:0] for large, negative and zero operands."""
    program = [
        lui(1, 0x3FF),                 # r1 = 0x3FF00
        addi(2, 0, -1),                # r2 = 0x3FFFF
        mul(3, 1, 2),
        mul(4, 2, 2),
        mul(5, 0, 4),
        addi(6, 0, 7),
        mul(7, 6, 6),
        lui(8, 0x300),                 # r8 = 0x30000: bit 17 of the result
        addi(9, 0, 2),
        mul(10, 8, 9),                 # 0x30000 * 2 = 0x20000
        mul(11, 2, 9),                 # 0x3FFFF * 2 = 0x3FFFE
        halt(),
    ]
    machine, run = assert_lockstep(program)
    w = {rd: value for _, rd, value in run.writes()}
    assert w[3] == (0x3FF00 * 0x3FFFF) & isa.MASK
    assert w[4] == (0x3FFFF * 0x3FFFF) & isa.MASK
    assert w[5] == 0
    assert w[7] == 49
    assert w[10] == 0x20000 and w[10] >> 17 == 1
    assert w[11] == 0x3FFFE and w[11] >> 17 == 1


def test_r0_writes_are_discarded_and_r0_reads_zero():
    """Every write port shape aimed at r0 retires with we=0."""
    program = [
        addi(0, 0, 5),
        alu(isa.ALU_ADD, 0, 0, 0),
        lui(0, 0x3FF),
        mul(0, 0, 0),
        jal(0, 0),                     # link value aimed at r0
        nop(),
        addi(1, 0, 1),                 # r1 = 1 proves r0 still reads 0
        halt(),
    ]
    machine, run = assert_lockstep(program)
    assert len(run.retires) == len(program)
    assert all(not we for _, pc, we, _, _ in run.retires if pc in (0, 1, 2, 3, 4))
    assert run.writes() == [(6, 1, 1)]
    assert machine.trace == run.writes()


# --- HALT and reset ---------------------------------------------------------

def test_halt_is_sticky_and_freezes_pc_and_retire():
    """HALT retires once, then o_halt sticks, o_pc freezes, no retire pulses."""
    program = [
        addi(1, 0, 4),
        addi(2, 1, 1),
        halt(),
        addi(3, 0, 9),                 # squashed
        addi(4, 0, 9),                 # squashed
        addi(5, 0, 9),                 # squashed
    ]
    run = run_core(program, post_cycles=12)
    assert run.halted
    # HALT is the last instruction to retire, and it retires with we=0.
    assert run.retires[-1][1] == 2 and run.retires[-1][2] == 0
    assert len(run.retires) == 3
    assert run.writes() == [(0, 1, 4), (1, 2, 5)]
    # Nothing retires after the halt, the halt flag stays set, PC is frozen.
    assert all(retire == 0 for retire, _, _ in run.post)
    assert all(flag == 1 for _, _, flag in run.post)
    assert len({pc for _, pc, _ in run.post}) == 1


def test_reset_clears_pipeline_so_first_write_is_instruction_zero():
    """While i_rst is high nothing retires; the first write is word 0's."""
    program = [addi(1, 0, 21), addi(2, 1, 1), halt()]
    core = Core(program)
    seen = {"during": [], "pc": []}
    sim = Simulator(core)
    sim.add_clock(CLK_PERIOD, domain="sync")

    async def tb(ctx):
        ctx.set(core.i_rst, 1)
        for _ in range(5):
            await ctx.tick()
            await ctx.delay(SETTLE)
            seen["during"].append((ctx.get(core.o_retire), ctx.get(core.o_halt)))
            seen["pc"].append(ctx.get(core.o_pc))
        ctx.set(core.i_rst, 0)
        rows = []
        for _ in range(40):
            await ctx.tick()
            await ctx.delay(SETTLE)
            if ctx.get(core.o_retire):
                rows.append((ctx.get(core.o_retire_pc), ctx.get(core.o_retire_we),
                             ctx.get(core.o_retire_rd),
                             ctx.get(core.o_retire_value)))
            if ctx.get(core.o_halt):
                break
        seen["rows"] = rows

    sim.add_testbench(tb)
    sim.run()

    assert seen["during"] == [(0, 0)] * 5
    assert set(seen["pc"]) == {0}
    assert seen["rows"][0] == (0, 1, 1, 21)
    assert seen["rows"] == [(0, 1, 1, 21), (1, 1, 2, 22), (2, 0, 0, 0)]


# --- M5 scope: no data memory ----------------------------------------------

def test_mid_run_reset_restarts_the_program_cleanly():
    """A reset pulse mid-program replays the run from PC 0 with no stale state."""
    program = [addi(1, 0, 3), addi(2, 1, 4), addi(3, 2, 5), halt()]
    core = Core(program)
    runs = []
    sim = Simulator(core)
    sim.add_clock(CLK_PERIOD, domain="sync")

    async def one_run(ctx, prime_cycles, reset_cycles):
        ctx.set(core.i_rst, 1)
        for _ in range(2):
            await ctx.tick()
        ctx.set(core.i_rst, 0)
        for _ in range(prime_cycles):
            await ctx.tick()
        ctx.set(core.i_rst, 1)
        for _ in range(reset_cycles):
            await ctx.tick()
            assert ctx.get(core.o_retire) == 0
        ctx.set(core.i_rst, 0)
        rows = []
        for _ in range(60):
            await ctx.tick()
            await ctx.delay(SETTLE)
            if ctx.get(core.o_retire):
                rows.append((ctx.get(core.o_retire_pc),
                             ctx.get(core.o_retire_we),
                             ctx.get(core.o_retire_rd),
                             ctx.get(core.o_retire_value)))
            if ctx.get(core.o_halt):
                break
        return rows

    async def tb(ctx):
        runs.append(await one_run(ctx, 1, 1))
        runs.append(await one_run(ctx, 3, 2))

    sim.add_testbench(tb)
    sim.run()

    want = [(0, 1, 1, 3), (1, 1, 2, 7), (2, 1, 3, 12), (3, 0, 0, None)]
    got = [(pc, we, rd, (v if we else None)) for pc, we, rd, v in runs[0]]
    assert got == want, got
    assert runs[1] == runs[0], runs[1]


def test_ld_st_are_no_ops_in_m5():
    """M5 has no data memory: LD/ST retire but write nothing and touch nothing.

    This is the documented M5 gap -- ``LD`` would write ``rd`` and ``ST`` would
    write memory from M6 on.  Here both decode to no-ops: LD leaves its target
    register untouched (r2 stays 9), ST writes no memory, neither stalls and
    neither can hang the pipeline.  M6 replaces this behaviour.
    """
    program = [
        addi(1, 0, 5),                 # 0: address base
        addi(2, 0, 9),                 # 1: r2 = 9
        ld(2, 1, 0),                   # 2: M5 no-op (M6: r2 = mem[5])
        st(2, 1, 0),                   # 3: M5 no-op (M6: mem[5] = 9)
        addi(3, 2, 1),                 # 4: 10 -> r2 still holds 9
        halt(),                        # 5
    ]
    run = run_core(program)
    assert run.halted, "LD/ST must not hang the M5 pipeline"
    assert len(run.retires) == len(program)
    assert [(pc, we) for _, pc, we, _, _ in run.retires] == [
        (0, 1), (1, 1), (2, 0), (3, 0), (4, 1), (5, 0)]
    assert run.writes() == [(0, 1, 5), (1, 2, 9), (4, 3, 10)]


# --- cycle accounting -------------------------------------------------------

def _independent_program():
    """20 instructions, no distance-1 or distance-2 RAW dependency.

    Everything reads r1, which word 0 writes and word 1 (a NOP) separates from
    the first reader, so every read is at least two instructions away from its
    producer.
    """
    program = [addi(1, 0, 3), nop()]
    program += [
        shift(2, 1, isa.SHIFT_SLL, 1),
        shift(3, 1, isa.SHIFT_SRL, 2),
        shift(4, 1, isa.SHIFT_SRA, 3),
        shift(5, 1, isa.SHIFT_ROR, 4),
        alu(isa.ALU_ADD, 6, 1, 1),
        alu(isa.ALU_SUB, 7, 1, 1),
        alu(isa.ALU_AND, 8, 1, 1),
        alu(isa.ALU_OR, 9, 1, 1),
        mul(10, 1, 1),
        andi(11, 1, 0x1F),
        ori(12, 1, 0x10),
        lui(13, 0x155),
        addi(14, 1, 5),
        addi(15, 1, -5),
        shift(2, 1, isa.SHIFT_SLL, 0),
        shift(3, 1, isa.SHIFT_SRL, 15),
        mul(4, 1, 1),
    ]
    program.append(halt())
    return program


def test_straight_line_retires_without_gratuitous_stalls():
    """20 independent instructions retire in exactly 20 cycles after the first."""
    program = _independent_program()
    assert len(program) == 20
    machine, run = assert_lockstep(program)
    assert len(run.retires) == machine.retired == 20
    assert run.cycles == 20, f"{run.cycles} cycles: a stall slipped in"


def test_dependent_chain_stalls_at_most_once_per_dependency():
    """Each distance-1 RAW costs exactly one bubble, and no more."""
    chain = [addi(1, 0, 1)]
    for rd in range(2, 12):
        chain.append(addi(rd, rd - 1, 1))
    program = chain + [halt()]
    machine, run = assert_lockstep(program)
    dependencies = len(chain) - 1
    assert len(run.retires) == machine.retired == 12
    # 12 instructions + one bubble per dependency (11 writes, 10 dependencies).
    assert run.cycles == len(program) + dependencies, run.cycles


# --- seeded random lockstep -------------------------------------------------

def _random_program(rng, n, target_base):
    """``n`` instructions from the M5 subset, ending in HALT.

    Word 0 puts a known address in r15 (reserved, never written again) so JALR
    can be generated with a predictable in-program target.  Branches and jumps
    only target words inside the program, so the PC can never run off the end;
    data-dependent loops may still not terminate, which the caller detects
    through the instruction cap and the generator retries.
    """
    program = [addi(15, 0, target_base)]
    for pc in range(1, n - 1):
        kind = rng.choices(
            ["addi", "alu", "andi", "ori", "mul", "shift", "lui", "jal",
             "jalr", "branch", "nop"],
            weights=[18, 16, 5, 5, 6, 10, 5, 5, 4, 20, 4])[0]
        rd = rng.randrange(15)                  # r15 holds the JALR base
        rs1 = rng.randrange(16)
        rs2 = rng.randrange(16)
        if kind == "addi":
            program.append(addi(rd, rs1, rng.randrange(64)))
        elif kind == "alu":
            program.append(alu(rng.randrange(4), rd, rs1, rs2))
        elif kind == "andi":
            program.append(andi(rd, rs1, rng.randrange(64)))
        elif kind == "ori":
            program.append(ori(rd, rs1, rng.randrange(64)))
        elif kind == "mul":
            program.append(mul(rd, rs1, rs2))
        elif kind == "shift":
            program.append(shift(rd, rs1, rng.randrange(4), rng.randrange(16)))
        elif kind == "lui":
            program.append(lui(rd, rng.randrange(1024)))
        elif kind == "jal":
            off = rng.choice([-4, -3, -2, -1, 1, 2, 3, 4, 5, 6])
            target = pc + 1 + off
            if not 0 <= target <= n - 1:
                target = pc + 1
            program.append(jal(rd, (target - (pc + 1)) & 0x3FF))
        elif kind == "jalr":
            off = rng.choice([-1, 0, 1])
            if 0 <= target_base + off <= n - 1:
                program.append(jalr(rd, 15, off & 0x3F))
            else:
                program.append(nop())
        elif kind == "branch":
            off = rng.choice([-4, -3, -2, -1, 1, 2, 3, 4, 5, 6])
            target = pc + 1 + off
            if not 0 <= target <= n - 1:
                target = pc + 1
            program.append(br(rng.choice([isa.OP_BEQ, isa.OP_BNE, isa.OP_BLT,
                                          isa.OP_BGE]), rs1, rs2,
                              (target - (pc + 1)) & 0x3F))
        else:
            program.append(nop())
    program.append(halt())
    return program


def test_seeded_random_programs_lockstep_with_golden():
    """>= 20 random 30..60-instruction programs, zero mismatches."""
    rng = random.Random(0xC0DE)
    programs = 0
    attempts = 0
    total_writes = 0
    total_instr = 0
    while programs < 20 and attempts < 400:
        attempts += 1
        n = rng.randint(30, 60)
        target_base = rng.randrange(min(n - 1, 31) + 1)
        program = _random_program(rng, n, target_base)

        machine = Machine(program).run(4000)
        if not machine.halted:
            continue                        # data-dependent loop: draw again
        if machine.retired < 15 or len(machine.trace) < 5:
            continue                        # too little evidence to be useful

        run = run_core(program, max_cycles=10 * machine.retired + 100)
        assert run.halted, f"RTL did not halt for program {programs}"
        assert run.writes() == machine.trace, (
            f"program {programs} (n={n}): " + _diff(run.writes(), machine.trace))
        assert len(run.retires) == machine.retired, (
            f"program {programs}: {len(run.retires)} retire pulses vs "
            f"{machine.retired} executed instructions")

        programs += 1
        total_writes += len(run.writes())
        total_instr += machine.retired

    assert programs >= 20, f"only {programs} random programs halted"
    assert total_instr > total_writes           # branches/NOPs retire too
    assert total_writes >= 200, total_writes
    print(f"fuzz: {programs} programs, {total_instr} instructions, "
          f"{total_writes} compared writes")


# --- measurement top --------------------------------------------------------

class TopHarness(Elaboratable):
    """Drive a top's ``clk`` port from a private ``tck`` domain."""

    def __init__(self, top):
        self.top = top

    def elaborate(self, platform):
        m = Module()
        cd = ClockDomain("tck")
        m.domains.tck = cd
        m.d.comb += self.top.clk.eq(cd.clk)
        m.submodules.top = self.top
        return m


def test_core_top_ports_program_and_elaboration():
    """CoreTop exposes the frozen ports, clk first, with a halting program."""
    top = CoreTop()
    assert top.ports[0] is top.clk
    expected = {"clk", "i_rst", "o_pc", "o_retire", "o_retire_pc",
                "o_retire_we", "o_retire_rd", "o_retire_value", "o_halt"}
    assert {name for name, value in vars(top).items()
            if isinstance(value, Signal) or name == "clk"} >= expected
    assert len(top.program) == len(CORE_TOP_PROGRAM)
    assert len(set(top.program)) > 8
    # The image exercises every op family the milestone asks for, and halts.
    ops = {isa.decode(word).op for word in top.program}
    assert {isa.OP_ADDI, isa.OP_ALU, isa.OP_MUL, isa.OP_SHIFT, isa.OP_LUI,
            isa.OP_BEQ, isa.OP_JAL, isa.OP_SYSTEM} <= ops
    assert isa.decode(top.program[-1]).halt == 1
    machine = Machine(top.program).run()
    assert machine.halted and len(machine.trace) >= 8
    Fragment.get(top, None)             # elaborates: no duplicate domains


def test_core_top_reproduces_the_golden_trace_one_cycle_late():
    """The registered shell delays the core's retire stream by one cycle."""
    top = CoreTop()
    harness = TopHarness(top)
    rows = []
    sim = Simulator(harness)
    sim.add_clock(CLK_PERIOD, domain="tck")

    async def tb(ctx):
        ctx.set(top.i_rst, 1)
        await ctx.tick("tck")
        await ctx.delay(SETTLE)
        ctx.set(top.i_rst, 0)
        for _ in range(400):
            await ctx.tick("tck")
            await ctx.delay(SETTLE)
            if ctx.get(top.o_retire):
                rows.append((ctx.get(top.o_retire_pc), ctx.get(top.o_retire_we),
                             ctx.get(top.o_retire_rd),
                             ctx.get(top.o_retire_value)))
            if ctx.get(top.o_halt):
                break

    sim.add_testbench(tb)
    sim.run()

    machine = Machine(CORE_TOP_PROGRAM).run()
    assert machine.halted
    assert [(pc, rd, v) for pc, we, rd, v in rows if we and rd != 0] == machine.trace
    assert len(rows) == machine.retired
