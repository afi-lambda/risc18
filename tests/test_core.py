"""Amaranth simulation tests for the risc18 core (M5 four-stage, M6 five-stage).

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
instruction cannot hide behind a register-write-only comparison.  For the
load/store tests the golden model is also the *memory* oracle: its ``.mem``
list is preloaded from the same ``data`` image handed to ``Core`` and compared
against words read back by the program itself.

M6 scope: ``LD`` writes ``rd`` from the data EBR, ``ST`` commits to it, the
pipeline is F/D/X/M/W, and a consumer one instruction behind a load costs
exactly one bubble (``test_load_use_interlock_costs_exactly_one_bubble``)
while independent loads run at CPI 1.0.

M7 scope: the X-stage result is forwarded into D (X > M > W), so distance-1
dependencies of every result kind -- ALU, ADDI, SHIFT, MUL, LUI and the
JAL/JALR link -- retire at exactly one instruction per cycle, and the only
remaining stall is load-use (``dx_mem_read``), still exactly one bubble.
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


def run_core(program, max_cycles=4000, post_cycles=8, data=None):
    """Run ``program`` on a :class:`Core` and return its retire trace."""
    core = Core(program, data=data)
    sim = Simulator(core)
    sim.add_clock(CLK_PERIOD, domain="sync")

    holder = {}

    async def tb(ctx):
        holder["run"] = await _drive(ctx, core, max_cycles, post_cycles)

    sim.add_testbench(tb)
    sim.run()
    return holder["run"]


def golden_machine(program, data=None, cap=100_000):
    """``golden.Machine`` for ``program`` with the same preloaded data image."""
    machine = Machine(program)
    if data:
        preloaded = [w & isa.MASK for w in list(data)[:isa.MEM_WORDS]]
        machine.mem[:len(preloaded)] = preloaded
    return machine.run(cap)


def assert_lockstep(program, max_cycles=None, data=None):
    """Compare a whole run against ``golden.Machine``; return ``(machine, run)``."""
    machine = golden_machine(program, data)
    assert machine.halted, "golden model did not halt: bad test program"
    if max_cycles is None:
        max_cycles = 10 * machine.retired + 100
    run = run_core(program, max_cycles=max_cycles, data=data)
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


# --- M6: data memory and LD/ST ----------------------------------------------

def data_image(words=isa.MEM_WORDS, mix=0x1D):
    """Deterministic non-zero data image: bit 17 set, low bits distinct."""
    return [0x20000 | ((i * mix) & 0x3FF) for i in range(words)]


def test_ld_st_write_and_read_data_memory():
    """LD writes rd from the data EBR; ST commits to it (M6 replaces M5's gap).

    r2 holds 9, ``LD r2,[5]`` overwrites it with ``mem[5]`` (0: the image is
    empty here), ``ST`` writes that back to word 5, and the following ADDI
    proves the pipeline saw the loaded value.  Both instructions retire, and
    only the LD produces a register write.
    """
    program = [
        addi(1, 0, 5),                 # 0: address base
        addi(2, 0, 9),                 # 1: r2 = 9
        ld(2, 1, 0),                   # 2: r2 = mem[5] = 0
        st(2, 1, 0),                   # 3: mem[5] = r2 = 0
        addi(3, 2, 1),                 # 4: r3 = r2 + 1 = 1
        halt(),                        # 5
    ]
    machine, run = assert_lockstep(program)
    assert [(pc, we) for _, pc, we, _, _ in run.retires] == [
        (0, 1), (1, 1), (2, 1), (3, 0), (4, 1), (5, 0)]
    assert run.writes() == [(0, 1, 5), (1, 2, 9), (2, 2, 0), (4, 3, 1)]
    assert run.writes() == machine.trace
    assert machine.mem[5] == 0


def test_st_then_ld_round_trip():
    """A store and the load of the same word one instruction later."""
    program = [
        addi(1, 0, 0),                 # 0: base
        nop(), nop(),                  # 1,2: distance for r1
        addi(4, 0, 21),                # 3: r4 = 21
        st(4, 1, 5),                   # 4: mem[5] = 21
        ld(5, 1, 5),                   # 5: r5 = mem[5]
        halt(),                        # 6
    ]
    machine, run = assert_lockstep(program)
    assert run.writes() == [(0, 1, 0), (3, 4, 21), (5, 5, 21)] == machine.trace
    assert machine.mem[5] == 21
    assert [pc for _, pc, we, _, _ in run.retires if not we] == [1, 2, 4, 6]


def test_ld_reads_a_preloaded_data_image():
    """``Core(program, data=...)`` preloads the data EBR; LD returns its words."""
    image = data_image()
    program = [
        addi(1, 0, 0),                 # 0: base 0
        nop(), nop(),                  # 1,2
        ld(2, 1, 0),                   # 3: image[0]
        ld(3, 1, 1),                   # 4: image[1]
        ld(4, 1, 31),                  # 5: image[31]
        ld(5, 1, 30),                  # 6: image[30]
        halt(),                        # 7
    ]
    machine, run = assert_lockstep(program, data=image)
    got = {rd: value for _, rd, value in run.writes() if rd != 1}
    assert got == {2: image[0], 3: image[1], 4: image[31], 5: image[30]}, \
        {k: hex(v) for k, v in got.items()}
    assert run.writes() == machine.trace


def test_ld_st_base_plus_offset_including_negative_offsets():
    """The effective address is rs1 + sext(imm6): negative offsets and wrap."""
    image = data_image()
    program = [
        lui(1, 0x001),                 # 0: base 0x100
        nop(), nop(),                  # 1,2
        addi(4, 0, 7),                 # 3: r4 = 7
        st(4, 1, -1),                  # 4: mem[0x0FF] = 7
        st(4, 1, 5),                   # 5: mem[0x105] = 7
        ld(5, 1, -1),                  # 6: r5 = mem[0x0FF]
        ld(6, 1, 5),                   # 7: r6 = mem[0x105]
        ld(7, 1, 0),                   # 8: r7 = mem[0x100] (image word)
        halt(),                        # 9
    ]
    machine, run = assert_lockstep(program, data=image)
    got = {rd: value for _, rd, value in run.writes()}
    assert got[1] == 0x100 and got[4] == 7
    assert got[5] == 7 and got[6] == 7, (hex(got[5]), hex(got[6]))
    assert got[7] == image[0x100], hex(got[7])
    assert machine.mem[0x0FF] == machine.mem[0x105] == 7
    assert run.writes() == machine.trace


def test_ld_out_of_range_reads_zero():
    """Loads outside 0x0000..0x03FF return 0, never an aliased word."""
    image = data_image()
    program = [
        lui(1, 0x00F),                 # 0: base 0xF00 -- outside the memory
        nop(), nop(),                  # 1,2
        ld(2, 1, 0),                   # 3: 0
        ld(3, 1, 17),                  # 4: 0
        ld(4, 0, 31),                  # 5: in range, image[31]
        addi(9, 0, 0),                 # 6
        ld(5, 9, 0),                   # 7: image[0]
        halt(),                        # 8
    ]
    machine, run = assert_lockstep(program, data=image)
    got = {rd: value for _, rd, value in run.writes()}
    assert got[2] == 0 and got[3] == 0, (hex(got[2]), hex(got[3]))
    assert got[4] == image[31] and got[5] == image[0]
    assert run.writes() == machine.trace


def test_st_out_of_range_is_ignored():
    """Stores outside the memory change nothing, not even the aliased word."""
    image = data_image()
    program = [
        lui(1, 0x00F),                 # 0: base 0xF00 (aliases word 0)
        nop(), nop(),                  # 1,2
        addi(4, 0, 13),                # 3
        st(4, 1, 0),                   # 4: ignored
        st(4, 1, 7),                   # 5: ignored
        ld(5, 0, 0),                   # 6: word 0 is still the image word
        halt(),                        # 7
    ]
    machine, run = assert_lockstep(program, data=image)
    got = {rd: value for _, rd, value in run.writes()}
    assert got[5] == image[0], hex(got[5])
    assert machine.mem[0] == image[0]
    assert machine.mem[7] == image[7]
    assert all(machine.mem[i] == image[i] for i in range(isa.MEM_WORDS))


def test_ld_to_r0_writes_nothing():
    """``LD r0`` reads the word but retires with ``we = 0`` and changes no r0."""
    image = data_image()
    program = [
        addi(1, 0, 3),                 # 0: base
        nop(), nop(),                  # 1,2
        ld(0, 1, 4),                   # 3: read, discard
        ld(0, 0, 9),                   # 4: read image[9], discard
        addi(2, 0, 1),                 # 5: prove r0 still reads 0
        halt(),                        # 6
    ]
    machine, run = assert_lockstep(program, data=image)
    assert run.writes() == [(0, 1, 3), (5, 2, 1)] == machine.trace
    rows = {pc: (we, rd) for _, pc, we, rd, _ in run.retires}
    assert rows[3] == (0, 0) and rows[4] == (0, 0), rows
    # The load result is still visible on the retire port, it just writes nothing.
    values = {pc: value for _, pc, _, _, value in run.retires}
    assert values[3] == image[7] and values[4] == image[9], \
        (hex(values[3]), hex(values[4]))


def test_store_load_at_distances_one_two_and_three():
    """ST -> LD back at distance 1, 2 and 3: no stall buys correctness."""
    image = data_image()
    for gap in (0, 1, 2):
        program = [
            addi(1, 0, 0),             # 0: base
            nop(), nop(),              # 1,2
            addi(4, 0, 20 + gap),      # 3: value (imm6 stays positive)
            st(4, 1, 7),               # 4: mem[7] = value
        ]
        program += [nop()] * gap
        program += [ld(5, 1, 7), halt()]      # distance 1/2/3 from the store
        machine, run = assert_lockstep(program, data=image)
        want = 20 + gap
        got = {rd: value for _, rd, value in run.writes()}
        assert got[5] == want, (gap, hex(got[5]))
        assert machine.mem[7] == want, (gap, machine.mem[7])
        assert machine.mem[8] == image[8]
        assert run.writes() == machine.trace


def test_memory_accumulation_loop_matches_golden():
    """A five-iteration loop that stores through r3 and accumulates the loads."""
    program = [
        addi(1, 0, 0),                 # 0: address pointer
        addi(2, 0, 0),                 # 1: accumulator
        addi(3, 0, 3),                 # 2: value stored every pass
        addi(6, 0, -5),                # 3: iteration counter
        st(3, 1, 0),                   # 4: mem[ptr] = 3
        ld(4, 1, 0),                   # 5: r4 = mem[ptr]
        alu(isa.ALU_ADD, 2, 2, 4),     # 6: acc += r4
        addi(1, 1, 1),                 # 7: ptr++
        addi(6, 6, 1),                 # 8: counter++
        br(isa.OP_BNE, 6, 0, -6),      # 9: back to word 4 while != 0
        halt(),                        # 10
    ]
    machine, run = assert_lockstep(program)
    assert machine.retired == 4 + 5 * 6 + 1      # prologue + loop + HALT
    sums = [value for _, rd, value in run.writes() if rd == 2]
    assert sums[-1] == 15, sums                 # 5 x 3
    assert machine.mem[:5] == [3] * 5
    assert machine.mem[5] == 0
    assert run.writes() == machine.trace


def test_whole_data_memory_matches_golden_after_the_program():
    """Read all 1024 words back with LD and compare them byte-exactly.

    The program stores two words through the data EBR, then walks the whole
    memory with ``LD r5,[ptr]``; the retire values of that load are the memory
    image as the RTL sees it, so the comparison covers every word -- the ones
    the program wrote, the ones it only read and the ones nothing touched.
    """
    image = data_image()
    program = [
        lui(1, 0x003),                 # 0: r1 = 0x300
        nop(), nop(),                  # 1,2
        addi(2, 0, 7),                 # 3
        st(2, 1, 5),                   # 4: mem[0x305] = 7
        addi(3, 0, -1),                # 5: r3 = 0x3FFFF
        st(3, 1, 0),                   # 6: mem[0x300] = 0x3FFFF
        addi(4, 0, 0),                 # 7: read pointer
        lui(6, 0x004),                 # 8: counter = 1024
        ld(5, 4, 0),                   # 9: <- readback loop
        addi(6, 6, -1),                # 10
        addi(4, 4, 1),                 # 11
        br(isa.OP_BNE, 6, 0, -4),      # 12: back to word 9 while != 0
        halt(),                        # 13
    ]
    max_cycles = 20_000
    machine, run = assert_lockstep(program, max_cycles=max_cycles, data=image)
    readback = [value for _, pc, we, rd, value in run.retires if pc == 9]
    assert len(readback) == isa.MEM_WORDS, len(readback)
    assert readback == machine.mem, _first_mem_diff(readback, machine.mem)
    # The two stores are visible, and the rest of the image is the preload.
    assert readback[0x305] == 7 and readback[0x300] == 0x3FFFF
    assert [readback[i] for i in range(0, 0x300)] == \
        [image[i] for i in range(0, 0x300)]


def _first_mem_diff(got, want):
    for i, (a, b) in enumerate(zip(got, want)):
        if a != b:
            return f"word {i:#05x}: got {a:#07x}, want {b:#07x}"
    return f"{len(got)} vs {len(want)} words"


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


def test_memory_survives_a_reset_inside_the_core():
    """Reset clears the pipeline and the registers, never the data EBR.

    The program increments ``mem[9]`` and halts; a reset pulse then replays it,
    so the second run must read the value the first one left behind.
    """
    program = [
        ld(1, 0, 9),                   # 0: r1 = mem[9]
        addi(2, 1, 1),                 # 1: r2 = mem[9] + 1
        st(2, 0, 9),                   # 2: mem[9] = mem[9] + 1
        halt(),                        # 3
    ]
    core = Core(program)                  # mem[9] starts at 0: increments 0,1,2
    runs = []
    sim = Simulator(core)
    sim.add_clock(CLK_PERIOD, domain="sync")

    async def one_run(ctx):
        ctx.set(core.i_rst, 1)
        for _ in range(2):
            await ctx.tick()
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
        runs.append(await one_run(ctx))
        runs.append(await one_run(ctx))
        runs.append(await one_run(ctx))

    sim.add_testbench(tb)
    sim.run()

    # Every run increments by one: the EBR contents outlive the reset, the
    # register file's r1 does not.
    for i, rows in enumerate(runs):
        writes = [(pc, rd, value) for pc, we, rd, value in rows if we and rd]
        assert writes == [(0, 1, i), (1, 2, i + 1)], (i, writes)
    assert runs[0][0][0] == 0 and runs[0][-1][0] == 3
    assert all(len(rows) == 4 for rows in runs)


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


def test_dependent_addi_chain_needs_no_bubble():
    """A 12-word distance-1 ADDI chain retires in exactly 12 cycles.

    M5 counted this chain at one bubble per dependency (12 + 10 cycles)
    because only M and W were forwarded.  M7 forwards the X-stage result into
    D, so the chain is gapless; the exact count is asserted, so a single
    leftover stall fails here.
    """
    chain = [addi(1, 0, 1)]
    for rd in range(2, 12):
        chain.append(addi(rd, rd - 1, 1))
    program = chain + [halt()]
    machine, run = assert_lockstep(program)
    dependencies = len(chain) - 1
    assert dependencies == 10
    assert len(run.retires) == machine.retired == 12
    assert run.cycles == len(program), run.cycles


# --- M6: load-use interlock and store behaviour -----------------------------

def test_load_use_interlock_costs_exactly_one_bubble():
    """``LD rX,[rY]`` followed by a consumer costs exactly one bubble."""
    program = [
        addi(1, 0, 0),                 # 0: base
        nop(), nop(),                  # 1,2: keep r1 out of the X stage
        ld(2, 1, 0),                   # 3: load
        addi(3, 2, 1),                 # 4: consumer, distance 1
        halt(),                        # 5
    ]
    machine, run = assert_lockstep(program, data=data_image())
    assert len(run.retires) == machine.retired == 6
    assert run.cycles == len(program) + 1, run.cycles
    assert run.writes() == [(0, 1, 0), (3, 2, 0x20000), (4, 3, 0x20001)]

    # Four independent LD/consumer pairs: four bubbles, never more.
    program = [addi(1, 0, 0), nop(), nop()]
    for k in range(4):
        program += [ld(2, 1, k), addi(3, 2, 1)]
    program.append(halt())
    machine, run = assert_lockstep(program, data=data_image())
    assert len(run.retires) == machine.retired == len(program)
    assert run.cycles == len(program) + 4, run.cycles
    # CPI = cycles / instructions: 1.33 for one bubble in every four.
    assert round(run.cycles / len(run.retires), 3) == 1.333


def test_independent_loads_pipeline_at_cpi_one():
    """Ten loads with no consumer between them retire without a single stall."""
    image = data_image()
    program = [addi(1, 0, 0), nop(), nop()]
    program += [ld(2 + k, 1, k) for k in range(10)]
    program.append(halt())
    machine, run = assert_lockstep(program, data=image)
    assert len(run.retires) == machine.retired == len(program)
    assert run.cycles == len(program), f"{run.cycles} cycles for {len(program)}"
    assert run.cycles / len(run.retires) == 1.0
    loaded = [value for _, pc, we, rd, value in run.retires if 3 <= pc < 13]
    assert loaded == [image[k] for k in range(10)], [hex(v) for v in loaded]
    assert run.writes() == machine.trace


def test_store_after_a_load_use_stall_commits_the_bypassed_word():
    """A store held in D while its source load stalls must still commit data."""
    program = [
        addi(2, 0, 0x10),              # base A (imm6 is 6-bit signed)
        addi(3, 0, 0x1F),              # base B (largest positive imm6)
        ld(1, 2, 0),                   # r1 = data[0x10]
        st(1, 3, 0),                   # st stalls in D while the load is in X
        ld(4, 3, 0),                   # read back 0x1F
        halt(),
    ]
    data = [0] * 1024
    data[0x10] = 0x12345
    machine = Machine(program)
    machine.mem[:] = data[:]
    machine.run(4000)
    assert machine.halted
    run = run_core(program, data=data)
    assert run.halted
    assert run.writes() == machine.trace, _diff(run.writes(), machine.trace)
    assert len(run.retires) == machine.retired
    assert (4, 4, 0x12345) in run.writes()


def test_two_consecutive_stores_to_one_word_last_wins():
    program = [
        addi(2, 0, 0x10),
        addi(1, 0, 7),
        addi(4, 0, 9),
        st(1, 2, 0),
        st(4, 2, 0),
        ld(3, 2, 0),
        halt(),
    ]
    machine, run = assert_lockstep(program)
    assert run.writes()[-1] == (5, 3, 9)


def test_store_in_the_second_squash_slot_does_not_commit():
    """BEQ over two words: neither the slot-1 nor the slot-2 store lands."""
    program = [
        br(isa.OP_BEQ, 1, 1, 2),       # taken: 0 -> 3
        st(1, 0, 4),                   # slot 1, squashed
        st(1, 0, 5),                   # slot 2, squashed
        ld(2, 0, 4),                   # reads data[4], untouched
        ld(3, 0, 5),                   # reads data[5], untouched
        halt(),
    ]
    data = [0] * 1024
    data[4] = 0x2AAAA
    data[5] = 0x15555
    machine = Machine(program)
    machine.mem[:] = data[:]
    machine.run(4000)
    run = run_core(program, data=data)
    assert run.writes() == machine.trace, _diff(run.writes(), machine.trace)
    assert run.writes()[-2] == (3, 2, 0x2AAAA)
    assert run.writes()[-1] == (4, 3, 0x15555)


def test_reset_on_the_stores_x_cycle_does_not_commit_it():
    """A store whose X cycle is a reset cycle must not reach memory.

    The program is 27 NOPs followed by ``ld r1,[9] / addi r2,r1,1 / st r2,[9] /
    ld r3,[9]``.  The store is word 29; the cycles are calibrated from an
    undisturbed run (the store retires two cycles after its X cycle), so the
    test does not hard-code the pipeline depth.  A one-cycle reset while the
    store is in X discards it, so the replay's first load sees 0, its store
    writes 1 and the final ``ld r3,[9]`` retires 1.  Without the reset gate the
    store commits anyway: the replay loads 1, stores 2, the final load retires
    2.
    """
    program = [nop() for _ in range(27)] + [
        ld(1, 0, 9),
        addi(2, 1, 1),
        st(2, 0, 9),
        ld(3, 0, 9),
        halt(),
    ]
    assert len(program) == 32

    def run(reset_cycle=None):
        core = Core(program, data=[0] * 1024)
        seen = {}

        sim = Simulator(core)
        sim.add_clock(CLK_PERIOD, domain="sync")

        async def tb(ctx):
            rows = []
            ctx.set(core.i_rst, 1)
            await ctx.tick()
            await ctx.delay(SETTLE)
            ctx.set(core.i_rst, 0)
            for cycle in range(1, 300):
                if cycle == reset_cycle:
                    ctx.set(core.i_rst, 1)
                    await ctx.tick()
                    await ctx.delay(SETTLE)
                    ctx.set(core.i_rst, 0)
                    continue
                await ctx.tick()
                await ctx.delay(SETTLE)
                if ctx.get(core.o_retire):
                    rows.append((cycle, ctx.get(core.o_retire_pc),
                                 ctx.get(core.o_retire_we),
                                 ctx.get(core.o_retire_rd),
                                 ctx.get(core.o_retire_value)))
                if ctx.get(core.o_halt):
                    break
            seen["rows"] = rows

        sim.add_testbench(tb)
        sim.run()
        return seen["rows"]

    def final_load(rows):
        loads = [r for r in rows if r[1] == 30 and r[2]]
        assert loads, rows
        return loads[-1][4]

    reference = run()
    store_retire = [r[0] for r in reference if r[1] == 29]
    assert len(store_retire) == 1, store_retire
    commit = store_retire[0]           # retire cycle: already in the array

    assert final_load(run(commit - 2)) == 1   # well before the commit
    assert final_load(run(commit - 1)) == 1   # on its X cycle: discarded
    assert final_load(run(commit)) == 2       # already committed: survives


def test_store_never_stalls_a_follower_on_its_rs2_field():
    """A store writes no register, so it can never cause a RAW stall.

    The S-format word puts ``rs2`` in the rd field the decoder reports, so a
    naive hazard check on ``rd`` would stall the instruction that reads that
    register one cycle after the store.  A store's ``dx_we`` is 0, so nothing
    stalls and the retire stream stays gapless.
    """
    program = [
        addi(1, 0, 0),                 # 0: base
        addi(4, 0, 9),                 # 1: store data
        nop(),                         # 2: keep r4 out of X for the store
        st(4, 1, 5),                   # 3: mem[5] = 9, "rd" field == r4
        addi(2, 4, 1),                 # 4: reads r4 one behind: must not stall
        nop(),                         # 5
        st(4, 1, 6),                   # 6: back-to-back store, same data
        addi(3, 4, 2),                 # 7: reads r4 one behind: must not stall
        halt(),                        # 8
    ]
    machine, run = assert_lockstep(program)
    assert len(run.retires) == machine.retired == 9
    assert run.cycles == 9, f"{run.cycles} cycles: a store stalled the pipeline"
    assert run.writes() == [(0, 1, 0), (1, 4, 9), (4, 2, 10), (7, 3, 11)] \
        == machine.trace
    assert machine.mem[5] == 9 and machine.mem[6] == 9


def test_squashed_store_never_writes_memory():
    """A store in a squashed fetch slot must not reach the data EBR."""
    image = data_image()
    program = [
        addi(1, 0, 0),                 # 0: base
        nop(), nop(),                  # 1,2
        addi(4, 0, 13),                # 3
        br(isa.OP_BEQ, 4, 4, 1),       # 4: taken, skips word 5
        st(4, 1, 6),                   # 5: squashed: must not write
        ld(5, 1, 6),                   # 6: still the preloaded word
        st(4, 1, 7),                   # 7: this one executes
        halt(),                        # 8
    ]
    machine, run = assert_lockstep(program, data=image)
    assert 5 not in [pc for _, pc, _, _, _ in run.retires]
    assert machine.mem[6] == image[6], hex(machine.mem[6])
    assert machine.mem[7] == 13
    assert {rd: value for _, rd, value in run.writes()}[5] == image[6]


def test_load_feeds_control_transfer_resolution():
    """A load's word drives the D-stage branch compare and a JALR target.

    Both consumers sit one instruction behind their load, so they exercise the
    interlock and the M-stage bypass on the control path: the branch is taken
    because of the loaded word, and the JALR jumps through a loaded register.
    """
    image = data_image()
    program = [
        addi(1, 0, 0),                 # 0: base
        addi(9, 0, 3),                 # 1: value 3
        st(9, 1, 2),                   # 2: mem[2] = 3
        nop(),                         # 3
        ld(2, 1, 0),                   # 4: r2 = image[0] (non-zero)
        br(isa.OP_BNE, 2, 0, 2),       # 5: taken because of the load
        addi(5, 0, 1),                 # 6: squashed
        addi(5, 0, 2),                 # 7: squashed
        ld(3, 1, 2),                   # 8: r3 = 3
        jalr(4, 3, 8),                 # 9: -> 11, link 10
        addi(6, 0, 1),                 # 10: squashed
        addi(7, 0, 7),                 # 11: the indirect target
        halt(),                        # 12
    ]
    machine, run = assert_lockstep(program, data=image)
    pcs = [pc for _, pc, _, _, _ in run.retires]
    assert not {6, 7, 10} & set(pcs), pcs
    assert {8, 11} <= set(pcs)
    got = {rd: value for _, rd, value in run.writes()}
    assert got[2] == image[0] and got[3] == 3
    assert got[4] == 10 and got[7] == 7          # JALR link value
    assert run.writes() == machine.trace


# --- M7: X-stage forwarding -------------------------------------------------

def _chain_cycles(program, data=None):
    """Assert lockstep and gapless retire for a straight-line chain."""
    machine, run = assert_lockstep(program, data=data)
    assert len(run.retires) == machine.retired == len(program)
    assert run.cycles == len(program), (
        f"{run.cycles} cycles for {len(program)} chained instructions")
    return machine, run


def test_distance_one_alu_chain_is_gapless():
    """add/sub/and/or chains, both operands distance 1, retire at CPI 1.0."""
    program = [
        addi(1, 0, 1),                 # 0
        addi(2, 1, 1),                 # 1: distance 1
        alu(isa.ALU_ADD, 3, 2, 2),     # 2: both operands distance 1
        alu(isa.ALU_SUB, 4, 3, 1),     # 3: rs1 distance 1
        alu(isa.ALU_AND, 5, 4, 3),     # 4
        alu(isa.ALU_OR, 6, 5, 4),      # 5
        addi(7, 6, 1),                 # 6
        halt(),                        # 7
    ]
    machine, run = _chain_cycles(program)
    assert [value for _, _, value in run.writes()][-1] == machine.trace[-1][2]
    assert run.cycles / len(run.retires) == 1.0


def test_distance_one_shift_chain_is_gapless():
    """All four shift kinds chained back to back."""
    program = [
        addi(1, 0, 3),
        shift(2, 1, isa.SHIFT_SLL, 2),
        shift(3, 2, isa.SHIFT_SRL, 1),
        shift(4, 3, isa.SHIFT_SRA, 1),
        shift(5, 4, isa.SHIFT_ROR, 1),
        shift(6, 5, isa.SHIFT_SLL, 0),
        addi(7, 6, 1),
        halt(),
    ]
    _chain_cycles(program)


def test_distance_one_mul_chain_is_gapless():
    """MUL products forwarded straight into the next multiply."""
    program = [
        addi(1, 0, 3),
        mul(2, 1, 1),
        mul(3, 2, 2),
        mul(4, 3, 3),
        mul(5, 4, 4),
        addi(6, 5, 1),
        halt(),
    ]
    machine, run = _chain_cycles(program)
    got = {rd: value for _, rd, value in run.writes()}
    assert got[5] == machine.trace[-2][2]
    assert got[2] == 9 and got[3] == 81        # 3*3, 9*9


def test_distance_one_lui_chain_is_gapless():
    """The LUI immediate is forwarded like any other X result."""
    program = [
        lui(1, 0x155),                 # r1 = 0x15500
        addi(2, 1, 0),                 # distance 1
        lui(3, 0x00A),
        alu(isa.ALU_ADD, 4, 3, 2),     # rs1 distance 1
        addi(5, 4, 1),
        halt(),
    ]
    machine, run = _chain_cycles(program)
    got = {rd: value for _, rd, value in run.writes()}
    assert got[1] == 0x15500 and got[2] == 0x15500
    assert got[3] == 0x00A00


def test_a_branch_takes_the_direction_of_the_forwarded_alu_result():
    """BEQ/BLT right behind their producer see the X result, not a stale rs."""
    # r3 = 0 - 3 = -3 on the X->D edge the branch reads r3; a stale r3 = 0
    # would make BLT not taken and BGE taken.
    program = [
        addi(1, 0, 0),                 # 0
        addi(2, 0, 3),                 # 1
        alu(isa.ALU_SUB, 3, 1, 2),     # 2: r3 = -3 (0x3FFFD)
        br(isa.OP_BLT, 3, 1, 2),       # 3: taken (distance 1) -> 6
        addi(5, 0, 111),               # 4: squashed
        addi(5, 0, 222),               # 5: squashed
        br(isa.OP_BGE, 3, 1, 2),       # 6: not taken (-3 >= 0 false)
        addi(6, 0, 6),                 # 7
        br(isa.OP_BNE, 3, 3, 2),       # 8: not taken (r3 == r3)
        addi(7, 0, 7),                 # 9: executes
        halt(),                        # 10
    ]
    machine, run = assert_lockstep(program)
    pcs = [pc for _, pc, _, _, _ in run.retires]
    assert 4 not in pcs and 5 not in pcs, pcs
    assert {6, 7, 9} <= set(pcs), pcs
    got = {rd: value for _, rd, value in run.writes()}
    assert got[3] == 0x3FFFD and got[7] == 7


def test_a_store_commits_the_word_forwarded_from_the_alu():
    """ST data (rs2) and ST base (rs1) both taken from the X stage."""
    image = data_image()
    program = [
        addi(1, 0, 10),                # 0: base
        addi(2, 0, 7),                 # 1
        alu(isa.ALU_ADD, 3, 2, 2),     # 2: r3 = 14
        st(3, 1, 0),                   # 3: mem[10] = r3, data distance 1
        ld(4, 1, 0),                   # 4: read it back
        halt(),                        # 5
    ]
    machine, run = _chain_cycles(program, data=image)
    assert machine.mem[10] == 14
    got = {rd: value for _, rd, value in run.writes()}
    assert got[4] == 14

    # Same for the address base: ST whose rs1 is the previous ALU result.
    program = [
        addi(1, 0, 4),                 # 0
        addi(2, 0, 7),                 # 1
        alu(isa.ALU_ADD, 3, 1, 1),     # 2: r3 = 8, the address
        st(2, 3, 0),                   # 3: mem[8] = 7, base distance 1
        ld(4, 1, 8),                   # 4: mem[12] = 7 read back via base+8
        halt(),                        # 5
    ]
    machine, run = _chain_cycles(program, data=image)
    assert machine.mem[8] == 7


def test_a_jalr_jumps_through_the_forwarded_alu_base():
    """JALR's rs1 comes from the immediately preceding ALU result."""
    program = [
        addi(1, 0, 0),                 # 0
        addi(2, 0, 5),                 # 1
        alu(isa.ALU_ADD, 3, 2, 1),     # 2: r3 = 5
        jalr(4, 3, 2),                 # 3: -> 5 + 2 = 7, link 4
        addi(8, 0, 1),                 # 4: squashed
        addi(9, 0, 1),                 # 5: squashed
        addi(10, 0, 1),                # 6: squashed
        addi(7, 0, 7),                 # 7: the indirect target
        halt(),                        # 8
    ]
    machine, run = assert_lockstep(program)
    pcs = [pc for _, pc, _, _, _ in run.retires]
    assert not {4, 5, 6} & set(pcs), pcs
    assert 7 in pcs, pcs
    got = {rd: value for _, rd, value in run.writes()}
    assert got[4] == 4 and got[7] == 7      # link value and target both right


def test_a_jal_link_value_is_consumed_without_a_dependency_bubble():
    """The instruction after a JAL's target reads the link value it wrote.

    A taken jump empties the two younger fetch slots by construction (D-stage
    resolution, unchanged since M5), so the link value's consumer can never be
    *pipeline* distance 1; what M7 must guarantee is that the read costs no
    extra cycle over the same program with an independent operand.  The link
    value itself travels through the same ``x_value`` forward mux.
    """
    chained = [
        jal(1, 0),                     # 0: -> 1, link = 1
        nop(),                         # 1: squashed
        addi(2, 1, 1),                 # 2: r2 = r1 + 1 = 2, reads the link
        addi(3, 2, 1),                 # 3
        halt(),                        # 4
    ]
    free = [
        jal(1, 0),
        nop(),
        addi(2, 0, 1),                 # same shape, no dependency on r1
        addi(3, 2, 1),
        halt(),
    ]
    machine, chained_run = assert_lockstep(chained)
    _, free_run = assert_lockstep(free)
    got = {rd: value for _, rd, value in chained_run.writes()}
    assert got[1] == 1 and got[2] == 2      # pc + 1, then consumed
    assert chained_run.cycles == free_run.cycles, (
        chained_run.cycles, free_run.cycles)
    assert machine.trace == chained_run.writes()


def test_a_load_use_still_costs_exactly_one_bubble():
    """The narrowed interlock: a load's word is only there in M."""
    image = data_image()
    program = [addi(1, 0, 0), nop(), nop(), ld(2, 1, 0), addi(3, 2, 1),
               halt()]
    machine, run = assert_lockstep(program, data=image)
    assert len(run.retires) == machine.retired == 6
    assert run.cycles == len(program) + 1, run.cycles

    # A store consuming the load one instruction behind stalls the same way.
    program = [addi(1, 0, 0), nop(), nop(), ld(2, 1, 0), st(2, 1, 4), halt()]
    machine, run = assert_lockstep(program, data=image)
    assert run.cycles == len(program) + 1, run.cycles
    assert machine.mem[4] == image[0]


def test_a_load_consumed_at_distance_two_needs_no_bubble():
    """Two instructions behind the load, the M bypass already covers it."""
    image = data_image()
    for gap in (1, 2):
        program = [addi(1, 0, 0), nop(), nop(), ld(2, 1, 0)]
        program += [nop()] * gap
        program += [addi(3, 2, 1), halt()]
        machine, run = assert_lockstep(program, data=image)
        assert len(run.retires) == machine.retired == len(program)
        assert run.cycles == len(program), (gap, run.cycles)
        got = {rd: value for _, rd, value in run.writes()}
        assert got[3] == (image[0] + 1) & isa.MASK


def test_r0_is_never_forwarded_and_never_stalls():
    """Writes to r0 are discarded, so r0 reads stay 0 at every distance."""
    program = [
        addi(5, 0, 7),                 # 0
        addi(0, 5, 0),                 # 1: "writes" r0 (discarded)
        addi(1, 0, 1),                 # 2: r0 read distance 1 -> 0, so r1 = 1
        alu(isa.ALU_ADD, 2, 0, 0),     # 3: r2 = 0 + 0
        halt(),                        # 4
    ]
    machine, run = _chain_cycles(program)
    got = {rd: value for _, rd, value in run.writes()}
    assert 0 not in got
    assert got[1] == 1 and got[2] == 0

    # A load into r0 neither forwards nor stalls its consumer.
    image = data_image()
    program = [addi(1, 0, 0), nop(), nop(), ld(0, 1, 0), addi(2, 0, 1),
               halt()]
    machine, run = _chain_cycles(program, data=image)
    assert run.cycles == len(program)
    got = {rd: value for _, rd, value in run.writes()}
    assert 0 not in got and got[2] == 1


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


# --- seeded random lockstep with LD/ST --------------------------------------

#: Registers the load/store fuzz generator reserves for address bases.
STORE_BASE = 14      # 0: stores land in words 0..31 (the readback window)
MID_BASE = 13        # 0x100: in-range loads away from the window
HIGH_BASE = 12       # 0xF00: out-of-range loads (must read 0)
JALR_BASE = 15       # the JALR target word

READBACK_WORDS = 32


def _random_mem_program(rng, n, target_base):
    """``n`` body words with LD/ST from the M6 subset, then a readback loop.

    Layout: 0..2 set the three address bases, 3..n-2 are random instructions,
    ``n - 1`` is a JAL into the epilogue that starts at word ``n``, and the
    epilogue reads words 0..31 back through ``LD`` and halts.  The body can
    only leave through that JAL (every branch and jump target stays inside
    ``0..n-1``), and the epilogue holds the only HALT, so a golden run that
    halts necessarily walked the whole readback loop.

    Register roles: r12 = 0xF00, r13 = 0x100, r14 = 0, r15 = ``target_base``.
    The body never writes r12..r15, and stores only use r14 with an imm6 in
    ``0..31``, so every word the program can write lies in the readback window.
    """
    program = [
        addi(JALR_BASE, 0, target_base),        # 0
        lui(MID_BASE, 0x001),                   # 1: r13 = 0x100
        lui(HIGH_BASE, 0x00F),                  # 2: r12 = 0xF00
    ]
    for pc in range(3, n - 1):
        kind = rng.choices(
            ["addi", "alu", "andi", "ori", "mul", "shift", "lui", "jal",
             "jalr", "branch", "nop", "ld", "st"],
            weights=[16, 14, 5, 5, 6, 9, 4, 4, 3, 18, 4, 12, 8])[0]
        rd = rng.randrange(12)                  # r12..r15 are the bases
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
                program.append(jalr(rd, JALR_BASE, off & 0x3F))
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
        elif kind == "ld":
            base = rng.choice([STORE_BASE, MID_BASE, HIGH_BASE])
            program.append(ld(rd, base, rng.randrange(32)))
        elif kind == "st":
            program.append(st(rs2, STORE_BASE, rng.randrange(32)))
        else:
            program.append(nop())
    program.append(jal(0, 0))                   # n - 1: enter the epilogue

    readback_pc = n + 2
    program += [
        addi(7, 0, 0),                          # n: read pointer
        addi(6, 0, -READBACK_WORDS),            # n+1: count -32 -> 0
        ld(1, 7, 0),                            # n+2: <- readback loop
        addi(6, 6, 1),                          # n+3
        addi(7, 7, 1),                          # n+4
        br(isa.OP_BNE, 6, 0, -4),               # n+5: back to n+2
        halt(),                                 # n+6
    ]
    return program, readback_pc


def test_seeded_random_programs_with_loads_and_stores_lockstep():
    """>= 20 random LD/ST programs: register trace and memory both match golden."""
    rng = random.Random(0x51AC)
    image = data_image(mix=0x35)
    programs = 0
    attempts = 0
    total_writes = 0
    total_instr = 0
    total_loads = 0
    total_stores = 0
    while programs < 20 and attempts < 400:
        attempts += 1
        n = rng.randint(34, 64)
        target_base = rng.randrange(min(n - 1, 31) + 1)
        program, readback_pc = _random_mem_program(rng, n, target_base)
        body = [isa.decode(word) for word in program[:n]]
        if not any(d.mem_read for d in body) or not any(d.mem_write
                                                        for d in body):
            continue

        machine = golden_machine(program, image, cap=8000)
        if not machine.halted:
            continue                        # data-dependent loop: draw again
        if machine.retired < 40 or len(machine.trace) < 5:
            continue                        # too little evidence to be useful

        run = run_core(program, max_cycles=10 * machine.retired + 300,
                       data=image)
        assert run.halted, f"RTL did not halt for program {programs}"
        assert run.writes() == machine.trace, (
            f"program {programs} (n={n}): " + _diff(run.writes(), machine.trace))
        assert len(run.retires) == machine.retired, (
            f"program {programs}: {len(run.retires)} retire pulses vs "
            f"{machine.retired} executed instructions")

        # Memory oracle: the epilogue's LD walk is the RTL's view of words 0..31.
        readback = [value for _, pc, _, _, value in run.retires
                    if pc == readback_pc]
        assert len(readback) == READBACK_WORDS, len(readback)
        assert readback == machine.mem[:READBACK_WORDS], (
            f"program {programs}: " +
            _first_mem_diff(readback, machine.mem[:READBACK_WORDS]))

        programs += 1
        total_writes += len(run.writes())
        total_instr += machine.retired
        total_loads += sum(1 for d in body if d.mem_read)
        total_stores += sum(1 for d in body if d.mem_write)

    assert programs >= 20, f"only {programs} random programs halted"
    assert total_loads >= 40 and total_stores >= 25, (total_loads, total_stores)
    assert total_writes >= 200, total_writes
    print(f"fuzz ld/st: {programs} programs, {total_instr} instructions, "
          f"{total_writes} compared writes, {total_loads} loads, "
          f"{total_stores} stores, {programs * READBACK_WORDS} compared words")


# --- seeded random lockstep over long distance-1 chains (M7) ----------------

CHAIN_READBACK = 32


def _chained_program(rng, n):
    """``n`` straight-line words built around long distance-1 RAW chains.

    Word 0 sets r15 = 0 (the store base).  The body alternates between *chain
    segments* of 24..48 words -- every one of them reading the register the
    word directly in front wrote (rs1, and sometimes rs2) and writing a fresh
    one, so the segment is one unbroken distance-1 dependency chain of
    ALU/ADDI/SHIFT/MUL and LD results -- and *boundary* words: a store that
    consumes the tail of the finished chain one instruction behind it (rs2,
    distance 1, into the readable window 0..31) and a fresh chain root.  There
    is no control flow, so the static order is the dynamic order and the cycle
    count is predicted from the load-use pairs alone.
    """
    program = [addi(15, 0, 0)]
    last_rd = None
    chain_left = 0
    for pc in range(1, n):
        if last_rd is not None and (chain_left == 0
                                    and rng.random() < 0.5):
            # Boundary: store the finished chain's tail one instruction behind
            # it (forwarded store data) into the readback window, then start a
            # new chain on the next word.
            program.append(st(last_rd, 15, rng.randrange(CHAIN_READBACK)))
            continue
        rd = rng.randrange(1, 15)               # never r0 or the base r15
        if last_rd is None or chain_left == 0:
            # Chain root: writes a register without reading one.
            kind = rng.choice(["addi", "addi", "lui", "ld"])
            if kind == "addi":
                program.append(addi(rd, 0, rng.randrange(1, 32)))
            elif kind == "lui":
                program.append(lui(rd, rng.randrange(1024)))
            else:
                program.append(ld(rd, 15, rng.randrange(CHAIN_READBACK)))
            last_rd = rd
            chain_left = rng.randint(24, 48)
            continue

        # Chain member: reads the previous word's result at distance 1.
        kind = rng.choices(
            ["addi", "alu", "andi", "ori", "mul", "shift", "ld"],
            weights=[30, 25, 7, 7, 10, 14, 7])[0]
        if kind == "addi":
            program.append(addi(rd, last_rd, rng.randrange(32)))
        elif kind == "alu":
            rs2 = last_rd if rng.random() < 0.5 else rng.randrange(15)
            program.append(alu(rng.randrange(4), rd, last_rd, rs2))
        elif kind == "andi":
            program.append(andi(rd, last_rd, rng.randrange(64)))
        elif kind == "ori":
            program.append(ori(rd, last_rd, rng.randrange(64)))
        elif kind == "mul":
            rs2 = last_rd if rng.random() < 0.5 else rng.randrange(15)
            program.append(mul(rd, last_rd, rs2))
        elif kind == "shift":
            program.append(shift(rd, last_rd, rng.randrange(4),
                                 rng.randrange(16)))
        else:
            program.append(ld(rd, last_rd, rng.randrange(32)))
        last_rd = rd
        chain_left -= 1

    # Straight-line readback of the store window: the retire values of these
    # loads are the RTL's view of words 0..31, so a store that committed the
    # wrong data (or none at all) shows up as a memory mismatch.
    readback_pc = len(program)
    for k in range(CHAIN_READBACK):
        program.append(ld(1 + (k % 14), 15, k))
    program.append(halt())
    return program, readback_pc


def _load_use_bubbles(program):
    """Adjacent pairs that must cost one bubble: a load, then its consumer."""
    bubbles = 0
    for i in range(len(program) - 1):
        d = isa.decode(program[i])
        if not d.mem_read or d.rd == 0:
            continue
        e = isa.decode(program[i + 1])
        if (e.rs1_used and e.rs1 == d.rd) or (e.rs2_used and e.rs2 == d.rd):
            bubbles += 1
    return bubbles


def _distance_one_edges(program):
    """``[(pc, run_length)]`` for every word reading the previous word's rd."""
    edges = []
    run = 0
    for pc in range(1, len(program) - 1):
        prev = isa.decode(program[pc - 1])
        cur = isa.decode(program[pc])
        hit = prev.reg_write and prev.rd != 0 and (
            (cur.rs1_used and cur.rs1 == prev.rd)
            or (cur.rs2_used and cur.rs2 == prev.rd))
        run = run + 1 if hit else 0
        if hit:
            edges.append((pc, run))
    return edges


def test_seeded_random_distance_one_chains_lockstep_and_stay_gapless():
    """>= 20 random chain programs: lockstep and cycles <= N + load-use."""
    rng = random.Random(0xC4A1)
    image = data_image(mix=0x2B)
    programs = 0
    attempts = 0
    total_instr = 0
    total_edges = 0
    total_bubbles = 0
    longest_chain = 0
    while programs < 25 and attempts < 60:
        attempts += 1
        n = rng.randint(40, 80)
        program, readback_pc = _chained_program(rng, n)

        machine = golden_machine(program, image, cap=8000)
        assert machine.halted, "the chain body has no control flow"
        assert machine.retired == len(program)

        edges = _distance_one_edges(program)
        bubbles = _load_use_bubbles(program)
        run = run_core(program, max_cycles=10 * len(program) + 300,
                       data=image)
        assert run.halted, f"RTL did not halt for chain program {programs}"
        assert run.writes() == machine.trace, (
            f"chain program {programs} (n={n}): "
            + _diff(run.writes(), machine.trace))
        assert len(run.retires) == machine.retired == len(program)

        # The whole point of M7: no dependency bubble that is not load-use.
        assert run.cycles <= len(program) + bubbles, (
            f"chain program {programs}: {run.cycles} cycles for "
            f"{len(program)} instructions with {bubbles} load-use bubbles")

        readback = [value for _, pc, we, rd, value in run.retires
                    if readback_pc <= pc < readback_pc + CHAIN_READBACK
                    and we and rd != 0]
        assert len(readback) == CHAIN_READBACK, len(readback)
        assert readback == machine.mem[:CHAIN_READBACK], (
            f"chain program {programs}: "
            + _first_mem_diff(readback, machine.mem[:CHAIN_READBACK]))

        programs += 1
        total_instr += machine.retired
        total_edges += len(edges)
        total_bubbles += bubbles
        longest_chain = max([longest_chain]
                            + [run_len for _, run_len in edges])

    assert programs >= 25, f"only {programs} chain programs ran"
    # The fuzz must actually contain long distance-1 chains, or it proves
    # nothing about forwarding.
    assert longest_chain >= 20, longest_chain
    assert total_edges >= 500, total_edges
    print(f"fuzz chains: {programs} programs, {total_instr} instructions, "
          f"{total_edges} distance-1 edges (longest {longest_chain}), "
          f"{total_bubbles} load-use bubbles")


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
