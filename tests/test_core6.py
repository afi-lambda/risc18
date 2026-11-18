"""Simulation tests for the six-stage :class:`risc18.core6.Core6`.

The schedule under test is F1/F2/D/X/M/W: the PC register drives the instruction
EBR, the EBR's output lands in the fetch register ``fq`` (F1), the decode runs
combinationally in F2 and its fields are registered into FD, D does the operand
select and the hazard detect, X the ALU / registered multiply / compare and
target, M the data-memory return, W the writeback.  One stage more than
:class:`risc18.core.Core` between the EBR output and the register-file read.

The oracle is the same one the five-stage suite uses: ``golden.Machine``.  Every
retire row with ``we=1, rd!=0`` must equal its ``.trace`` in order, and the
number of retire pulses must equal its ``.retired`` -- so a killed slot that
retired, or a live one that vanished, cannot hide.

What this file pins, beyond lockstep:

* the taken-transfer penalty in cycles: **5** killed fetch slots per taken
  branch or jump (the five-stage core's 4 plus the fetch register's slot);
* the two one-cycle interlocks and their exact cost: distance-1 load-use and
  distance-1 MUL-use each cost one bubble, distance 2 costs nothing;
* that a killed slot in *every* one of the five positions of the window never
  retires, never writes a register, never writes memory, never halts and never
  links;
* the CPI budget: each of the three measurement programs of
  ``docs/RESULTS.md`` retires at CPI <= 2.64, which is break-even at 120 MHz
  against the 45.50 MIPS the 5-stage core delivers at 73.48 MHz / CPI 1.615.
"""

import random

import pytest
from amaranth.sim import Simulator

from risc18 import isa
from risc18.core import Core
from risc18.core6 import Core6
from risc18.tops import CORE_TOP_PROGRAM

# Same instruction builders, test driver and golden-model helper as the
# five-stage suite: the programs below are literally the ones it uses, and the
# retire-trace comparison is the same.
from test_core import (Run, _diff, _drive, addi, alu, andi, br, data_image,
                       golden_machine, halt, jal, jalr, ld, lui, mul, nop, ori,
                       shift, st)

CLK_PERIOD = 1e-6

#: Break-even CPI at 120 MHz against the 5-stage core's 45.50 MIPS
#: (73.48 MHz / 1.615).
CPI_BUDGET = 2.64


# --- testbench driver -------------------------------------------------------

def run_core6(program, max_cycles=4000, post_cycles=8, data=None, ram_rf=False):
    """Run ``program`` on a :class:`Core6` and return its retire trace."""
    core = Core6(program, data=data, ram_rf=ram_rf)
    sim = Simulator(core)
    sim.add_clock(CLK_PERIOD, domain="sync")

    holder = {}

    async def tb(ctx):
        holder["run"] = await _drive(ctx, core, max_cycles, post_cycles)

    sim.add_testbench(tb)
    sim.run()
    return holder["run"]


def assert_lockstep(program, max_cycles=None, data=None, ram_rf=False):
    """Compare a whole ``Core6`` run against ``golden.Machine``."""
    machine = golden_machine(program, data)
    assert machine.halted, "golden model did not halt: bad test program"
    if max_cycles is None:
        max_cycles = 10 * machine.retired + 100
    run = run_core6(program, max_cycles=max_cycles, data=data, ram_rf=ram_rf)
    assert run.halted, f"core did not halt in {max_cycles} cycles"
    assert run.writes() == machine.trace, _diff(run.writes(), machine.trace)
    assert len(run.retires) == machine.retired, (
        f"{len(run.retires)} retire pulses for {machine.retired} executed "
        f"instructions")
    return machine, run


BOTH_FILES = pytest.mark.parametrize("ram_rf", [False, True],
                                     ids=["flop-rf", "ram-rf"])


# --- the programs -----------------------------------------------------------

def sum_loop_program():
    """10-word sum loop: sum(1..10), one taken branch per iteration."""
    return [
        addi(1, 0, 0),                 # 0: sum = 0
        addi(2, 0, 0),                 # 1: i = 0
        addi(3, 0, 10),                # 2: n = 10
        addi(2, 2, 1),                 # 3: i += 1
        alu(isa.ALU_ADD, 1, 1, 2),     # 4: sum += i
        br(isa.OP_BNE, 2, 3, -3),      # 5: while i != n: -> 3
        ori(4, 1, 1),                  # 6
        addi(5, 4, -1),                # 7
        nop(),                         # 8
        halt(),                        # 9
    ]


def load_store_program():
    """The five-stage LD/ST program: load, store back, consume at distance 1."""
    return [
        addi(1, 0, 5),                 # 0: address base
        addi(2, 0, 9),                 # 1: r2 = 9
        ld(2, 1, 0),                   # 2: r2 = mem[5]
        st(2, 1, 0),                   # 3: mem[5] = r2
        addi(3, 2, 1),                 # 4: r3 = r2 + 1 (distance-1 load use)
        halt(),                        # 5
    ]


def jal_program():
    """JAL over two words, then a JAL that jumps one word forward."""
    return [
        jal(1, 2),                     # 0: r1 = 1, -> 3
        addi(2, 0, 1),                 # 1: squashed
        addi(2, 0, 2),                 # 2: squashed
        addi(3, 0, 3),                 # 3
        jal(4, 1),                     # 4: r4 = 5, -> 6
        addi(5, 0, 5),                 # 5: squashed
        addi(6, 0, 6),                 # 6
        halt(),                        # 7
    ]


def jalr_program():
    """JALR to an absolute target, then a JALR immediately behind its base."""
    return [
        addi(7, 0, 9),                 # 0: r7 = 9
        jalr(1, 7, 1),                 # 1: r1 = 2, -> 10
        addi(2, 0, 1),                 # 2: squashed
        addi(2, 0, 2),                 # 3: squashed
        nop(), nop(), nop(), nop(),    # 4-7
        nop(), nop(),                  # 8, 9
        addi(3, 0, 3),                 # 10: the indirect target
        addi(8, 0, 13),                # 11: second base
        jalr(4, 8, 1),                 # 12: r4 = 13, -> 14 (base distance 1)
        addi(5, 0, 5),                 # 13: fall-through still executes
        addi(6, 0, 6),                 # 14
        halt(),                        # 15
    ]


def shift_program():
    """Every shift kind at amounts 0 and 15 on a negative operand."""
    program = [addi(1, 0, -3)]
    rd = 2
    for kind in (isa.SHIFT_SLL, isa.SHIFT_SRL, isa.SHIFT_SRA, isa.SHIFT_ROR):
        for amount in (0, 15):
            program.append(shift(rd, 1, kind, amount))
            rd += 1
    program.append(halt())
    return program


def signed_compare_program():
    """BEQ/BNE/BLT/BGE, each taken and not taken, with negative operands."""
    return [
        addi(1, 0, -2),                # r1 = 0x3FFFE (-2)
        addi(2, 0, 3),                 # r2 = 3
        br(isa.OP_BLT, 1, 2, 2),       # taken: -2 < 3
        addi(5, 0, 111),               # squashed
        addi(6, 0, 222),               # squashed
        br(isa.OP_BGE, 1, 2, 2),       # not taken
        addi(7, 0, 7),
        br(isa.OP_BGE, 2, 1, 2),       # taken: 3 >= -2
        addi(8, 0, 8),                 # squashed
        addi(9, 0, 9),                 # squashed
        br(isa.OP_BEQ, 1, 1, 2),       # taken: equal
        addi(10, 0, 10),               # squashed
        addi(11, 0, 11),               # squashed
        br(isa.OP_BNE, 1, 1, 2),       # not taken: equal
        addi(12, 0, 12),
        br(isa.OP_BLT, 2, 1, 2),       # not taken: 3 < -2 is false
        addi(13, 0, 13),
        br(isa.OP_BNE, 1, 2, 1),       # taken: not equal, skips one word
        addi(14, 0, 14),               # squashed
        addi(15, 0, 15),
        halt(),
    ]


def mul_program():
    """MUL on large, negative and zero operands: low 18 bits of the product."""
    return [
        lui(1, 0x3FF),                 # r1 = 0x3FF00
        addi(2, 0, -1),                # r2 = 0x3FFFF
        mul(3, 1, 2),
        mul(4, 2, 2),
        mul(5, 0, 4),
        addi(6, 0, 7),
        mul(7, 6, 6),
        lui(8, 0x300),                 # bit 17 of the result
        addi(9, 0, 2),
        mul(10, 8, 9),
        mul(11, 2, 9),
        halt(),
    ]


def counted_loop_program():
    """The five-stage CPI table's counted loop: 5 iterations, 4 taken BNE."""
    return [
        addi(1, 0, 5),                 # 0: counter
        addi(2, 0, 0),                 # 1: accumulator
        addi(2, 2, 3),                 # 2: acc += 3
        addi(1, 1, -1),                # 3: counter -= 1
        br(isa.OP_BNE, 1, 0, -3),      # 4: loop while counter != 0
        ori(3, 2, 0x20),               # 5
        halt(),                        # 6
    ]


def store_load_loop_program():
    """The five-stage CPI table's store/load loop: 5 iterations, 5 load-uses."""
    return [
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


# --- lockstep against the golden model --------------------------------------

@BOTH_FILES
def test_sum_loop_lockstep(ram_rf):
    """A 10-word sum loop over both register-file builds."""
    program = sum_loop_program()
    machine, run = assert_lockstep(program, ram_rf=ram_rf)
    assert len(program) == 10
    assert len(run.retires) == machine.retired == 37
    assert [v for _, rd, v in run.writes() if rd == 1][-1] == sum(range(1, 11)) == 55
    assert [pc for _, pc, _, _, _ in run.retires][:4] == [0, 1, 2, 3]


@BOTH_FILES
def test_load_store_program_lockstep(ram_rf):
    """LD -> ST -> distance-1 consumer, on both register-file builds."""
    program = load_store_program()
    machine, run = assert_lockstep(program, ram_rf=ram_rf)
    assert run.writes() == [(0, 1, 5), (1, 2, 9), (2, 2, 0), (4, 3, 1)]
    assert run.writes() == machine.trace
    assert machine.mem[5] == 0


@BOTH_FILES
def test_jal_lockstep(ram_rf):
    program = jal_program()
    machine, run = assert_lockstep(program, ram_rf=ram_rf)
    assert run.writes() == [(0, 1, 1), (3, 3, 3), (4, 4, 5), (6, 6, 6)]
    assert [pc for _, pc, _, _, _ in run.retires] == [0, 3, 4, 6, 7]


@BOTH_FILES
def test_jalr_lockstep(ram_rf):
    """Indirect target and link, including a JALR behind its own base."""
    program = jalr_program()
    machine, run = assert_lockstep(program, ram_rf=ram_rf)
    retired_pcs = [pc for _, pc, _, _, _ in run.retires]
    assert retired_pcs[:3] == [0, 1, 10]
    assert not {2, 3} & set(retired_pcs)
    assert run.writes() == machine.trace
    assert (1, 1, 2) in run.writes() and (12, 4, 13) in run.writes()


def test_shift_program_lockstep():
    """Every shift kind and both boundary amounts, distance-1 chains."""
    machine, run = assert_lockstep(shift_program())
    w = run.writes()
    value = 0x3FFFD
    assert [w[i][2] for i in (1, 3, 5, 7)] == [value] * 4
    assert w[2][2] == (value << 15) & isa.MASK
    assert w[4][2] == 0x00007
    assert w[6][2] == 0x3FFFF


def test_signed_compare_program_lockstep():
    machine, run = assert_lockstep(signed_compare_program())
    written = {pc for pc, _, _ in run.writes()}
    assert not written & {3, 4, 8, 9, 11, 12, 18}
    assert {6, 14, 16, 19} <= written
    retired_pcs = [pc for _, pc, _, _, _ in run.retires]
    assert retired_pcs.count(2) == 1 and retired_pcs.count(3) == 0


@BOTH_FILES
def test_multiply_program_lockstep(ram_rf):
    """The five-stage MUL program: 18x18 low half, now latency 2."""
    program = mul_program()
    machine, run = assert_lockstep(program, ram_rf=ram_rf)
    w = {rd: value for _, rd, value in run.writes()}
    assert w == {rd: value for _, rd, value in machine.trace}
    assert w[3] == (0x3FF00 * 0x3FFFF) & isa.MASK
    assert w[4] == (0x3FFFF * 0x3FFFF) & isa.MASK
    assert w[5] == 0
    assert w[7] == 49
    assert w[10] == 0x20000 and w[11] == 0x3FFFE


@BOTH_FILES
def test_measurement_program_lockstep(ram_rf):
    """``CORE_TOP_PROGRAM``: 16 words, 13 retired, 3 squashed."""
    program = list(CORE_TOP_PROGRAM)
    machine, run = assert_lockstep(program, ram_rf=ram_rf)
    assert len(program) == 16
    assert len(run.retires) == machine.retired == 13
    retired_pcs = [pc for _, pc, _, _, _ in run.retires]
    assert not {10, 11, 13} & set(retired_pcs)
    assert retired_pcs[-3:] == [12, 14, 15]
    assert run.writes() == machine.trace


@BOTH_FILES
def test_cpi_loops_lockstep(ram_rf):
    """The two CPI loops of ``docs/RESULTS.md``."""
    machine, run = assert_lockstep(counted_loop_program(), ram_rf=ram_rf)
    assert len(run.retires) == machine.retired == 19
    assert run.writes()[-1][2] == 15 | 0x20

    machine, run = assert_lockstep(store_load_loop_program(), ram_rf=ram_rf)
    assert len(run.retires) == machine.retired == 35
    assert [v for _, rd, v in run.writes() if rd == 2][-1] == 15
    assert machine.mem[:5] == [3] * 5 and machine.mem[5] == 0


# --- cycle accounting: the taken-transfer penalty ---------------------------

def test_a_taken_branch_costs_exactly_5_cycles():
    """One taken branch costs 5 fetch slots -- one more than the 5-stage core.

    The extra slot is the fetch register's: the word it captured on the edge
    the transfer entered X is killed in D one cycle later than the 5-stage
    core's, and the target's word needs one more edge to reach the decode.
    """
    program = [
        addi(1, 0, 0),                 # 0
        nop(), nop(),                  # 1, 2: keep r2 out of X
        addi(2, 0, 1),                 # 3
        br(isa.OP_BEQ, 2, 2, 2),       # 4: taken -> 7
        addi(3, 0, 9),                 # 5: squashed
        addi(3, 0, 8),                 # 6: squashed
        addi(4, 0, 4),                 # 7
        halt(),                        # 8
    ]
    machine, run = assert_lockstep(program)
    assert len(run.retires) == machine.retired == 7
    assert run.cycles == 12, run.cycles
    assert run.cycles == len(run.retires) + 5


def test_two_taken_branches_cost_ten_cycles():
    """No overlap bonus: 2 taken branches cost 2 x 5 slots."""
    program = [
        addi(1, 0, 0),                 # 0
        nop(), nop(),                  # 1, 2
        br(isa.OP_BEQ, 1, 1, 2),       # 3: taken -> 6
        addi(2, 0, 1),                 # 4: squashed
        addi(3, 0, 2),                 # 5: squashed
        br(isa.OP_BEQ, 1, 1, 1),       # 6: taken -> 8
        addi(4, 0, 3),                 # 7: squashed
        addi(5, 0, 4),                 # 8
        halt(),                        # 9
    ]
    machine, run = assert_lockstep(program)
    assert len(run.retires) == machine.retired == 7
    assert run.cycles == 17, run.cycles
    assert run.cycles == len(run.retires) + 2 * 5


def test_a_taken_jal_costs_exactly_5_cycles():
    """The redirect is the same register for JAL, so the price is identical."""
    program = [
        addi(1, 0, 0),                 # 0
        jal(2, 2),                     # 1: r2 = 2, -> 4
        addi(3, 0, 1),                 # 2: squashed
        addi(3, 0, 2),                 # 3: squashed
        addi(4, 0, 4),                 # 4
        halt(),                        # 5
    ]
    machine, run = assert_lockstep(program)
    assert len(run.retires) == machine.retired == 4
    assert run.cycles == 9, run.cycles
    assert run.cycles == len(run.retires) + 5


# --- cycle accounting: the two one-cycle interlocks -------------------------

def test_distance_one_load_use_costs_exactly_one_bubble():
    """``LD rX,[rY]`` then a consumer: 1 bubble, and the word is bypassed from M."""
    program = [
        addi(1, 0, 0),                 # 0: base
        nop(), nop(),                  # 1, 2
        ld(2, 1, 0),                   # 3: load
        addi(3, 2, 1),                 # 4: consumer, distance 1
        halt(),                        # 5
    ]
    machine, run = assert_lockstep(program, data=data_image())
    assert len(run.retires) == machine.retired == 6
    assert run.cycles == len(program) + 1 == 7, run.cycles
    assert run.writes() == [(0, 1, 0), (3, 2, 0x20000), (4, 3, 0x20001)]

    # Four independent LD/consumer pairs: four bubbles, never more.
    program = [addi(1, 0, 0), nop(), nop()]
    for k in range(4):
        program += [ld(2, 1, k), addi(3, 2, 1)]
    program.append(halt())
    machine, run = assert_lockstep(program, data=data_image())
    assert len(run.retires) == machine.retired == len(program)
    assert run.cycles == len(program) + 4, run.cycles
    assert round(run.cycles / len(run.retires), 3) == 1.333


def test_distance_two_load_use_needs_no_bubble():
    program = [
        addi(1, 0, 0), nop(), nop(),
        ld(2, 1, 0),                   # 3: load
        nop(),                         # 4
        addi(3, 2, 1),                 # 5: consumer, distance 2
        halt(),
    ]
    machine, run = assert_lockstep(program, data=data_image())
    assert len(run.retires) == machine.retired == len(program)
    assert run.cycles == len(program), run.cycles


def test_independent_loads_and_muls_retire_at_cpi_one():
    image = data_image()
    program = [addi(1, 0, 0), nop(), nop()]
    program += [ld(2 + k, 1, k) for k in range(8)]
    program += [mul(10 + k, 1, 1) for k in range(4)]
    program.append(halt())
    machine, run = assert_lockstep(program, data=image)
    assert len(run.retires) == machine.retired == len(program)
    assert run.cycles == len(program), run.cycles
    assert run.cycles / len(run.retires) == 1.0


def test_distance_one_mul_use_costs_exactly_one_bubble():
    """The new contract: MUL is latency 2, so a distance-1 consumer stalls once.

    The product is registered in X (``x_prod``) and only reaches a select tree
    in M, where the D-stage bypass takes it from.  A distance-1 consumer
    therefore waits one cycle for the product to arrive; nothing else stalls.
    """
    program = [
        addi(1, 0, 3),                 # 0
        mul(2, 1, 1),                  # 1: r2 = 9
        addi(3, 2, 1),                 # 2: consumer, distance 1
        halt(),                        # 3
    ]
    machine, run = assert_lockstep(program)
    assert len(run.retires) == machine.retired == 4
    assert run.cycles == len(program) + 1 == 5, run.cycles
    assert run.writes() == [(0, 1, 3), (1, 2, 9), (2, 3, 10)]


def test_distance_two_mul_use_needs_no_bubble():
    for distance in (2, 3):
        program = [addi(1, 0, 3), mul(2, 1, 1)]
        program += [nop()] * (distance - 1)
        program += [addi(3, 2, 1), halt()]
        machine, run = assert_lockstep(program)
        assert len(run.retires) == machine.retired == len(program)
        assert run.cycles == len(program), (distance, run.cycles)
        assert run.writes() == machine.trace


def test_a_load_feeds_a_taken_branch_and_a_jalr_target():
    """The interlock and the M bypass also serve the X-stage control path.

    Both consumers sit one instruction behind their load: the branch is taken
    because of the loaded word, and the JALR jumps (and links) through a loaded
    register.  Each load-use costs its one bubble, and nothing else stalls.
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
    assert got[4] == 10 and got[7] == 7
    assert run.writes() == machine.trace
    # 10 retires: words 0-5, 8, 9, 11, 12.  2 taken transfers (+5 each) and 2
    # distance-1 load uses (+1 each).
    assert len(run.retires) == machine.retired == 10
    assert run.cycles == len(run.retires) + 2 * 5 + 2 * 1, run.cycles


def test_each_distance_one_mul_dependency_costs_one_bubble():
    """A 4-deep MUL chain: 4 bubbles, one per dependency, and the right values."""
    program = [
        addi(1, 0, 3),
        mul(2, 1, 1),                  # 3 * 3
        mul(3, 2, 2),                  # 9 * 9
        mul(4, 3, 3),                  # 81 * 81 -> low 18 bits
        mul(5, 4, 4),
        addi(6, 5, 1),
        halt(),
    ]
    machine, run = assert_lockstep(program)
    got = {rd: value for _, rd, value in run.writes()}
    assert got[2] == 9 and got[3] == 81
    assert got[5] == machine.trace[-2][2]
    assert len(run.retires) == machine.retired == 7
    assert run.cycles == len(program) + 4 == 11, run.cycles


def test_a_mul_in_x_is_stalled_for_rather_than_forwarded():
    """The value behind a MUL is the product from M, never an X-stage operand."""
    program = [
        addi(1, 0, 2),                 # 0
        addi(2, 0, 3),                 # 1
        mul(3, 1, 2),                  # 2: r3 = 6
        alu(isa.ALU_ADD, 5, 3, 1),     # 3: 6 + 2 = 8, not 2 + 2 (distance 1)
        alu(isa.ALU_SUB, 6, 5, 0),     # 4: 8 - 0
        halt(),                        # 5
    ]
    machine, run = assert_lockstep(program)
    got = {rd: value for _, rd, value in run.writes()}
    assert got[3] == 6 and got[5] == 8 and got[6] == 8
    assert len(run.retires) == machine.retired == 6
    assert run.cycles == len(program) + 1 == 7, run.cycles


# --- the kill window: nothing killed may retire or write --------------------

def test_a_store_in_each_killed_slot_never_commits():
    """Each of the 5 slots behind a taken branch holds a store: none lands.

    The load after the target reads the slot address back, so a store that
    committed shows up as the loaded value, not only as a golden-model diff.
    """
    image = [0x20000 | (i & 0x3FF) for i in range(1024)]
    for slot in range(1, 6):
        program = [addi(1, 0, 0x10), addi(2, 0, 7), br(isa.OP_BEQ, 1, 1, 5)]
        program += [st(2, 1, s) for s in range(1, 6)]
        program += [ld(3, 1, slot), halt()]
        machine, run = assert_lockstep(program, data=image)
        assert run.writes()[-1] == (8, 3, image[0x10 + slot]), (slot, run.writes())
        assert machine.mem[0x10 + slot] == image[0x10 + slot], slot
        assert len(run.retires) == machine.retired == 5
        assert run.cycles == 5 + 5, (slot, run.cycles)


@pytest.mark.parametrize("killed", ["addi", "halt", "jal", "mul"])
def test_a_killed_slot_never_retires_halts_links_or_writes(killed):
    """Every one of the 5 killed positions, for each kind of instruction.

    The branch skips exactly five words, so the words in slots 1..5 are all in
    the kill window -- one per slot -- and the surviving tail must retire
    unchanged.
    """
    filler = {
        "addi": lambda: addi(5, 0, 1),
        "halt": halt,
        "jal": lambda: jal(5, 0),
        "mul": lambda: mul(5, 1, 1),
    }[killed]
    program = [addi(1, 0, 3), br(isa.OP_BEQ, 1, 1, 5)]
    program += [filler() for _ in range(5)]
    program += [addi(6, 0, 2), halt()]
    machine, run = assert_lockstep(program)
    retired_pcs = [pc for _, pc, _, _, _ in run.retires]
    assert retired_pcs == [0, 1, 7, 8], (killed, retired_pcs)
    assert run.writes() == [(0, 1, 3), (7, 6, 2)] == machine.trace
    assert run.halted
    assert run.cycles == 4 + 5, (killed, run.cycles)
    if killed in ("addi", "mul"):
        assert 5 not in {rd for _, rd, _ in run.writes()}


# --- the fetch register's invariants ---------------------------------------

def test_a_slot_is_decoded_only_from_a_live_fetch_register():
    """A dead FD slot can only follow a live one (a stall) or a live ``fq``.

    ``fq`` is where a word killed by a redirect dies, but the squash window
    closes one cycle before the target reaches it, so the register's own
    validity -- not the window -- is what stops a stale word from being
    decoded.  This is the invariant that a "just gate the capture with the
    window" implementation would silently break.
    """
    program = [
        addi(1, 0, 3),                 # 0
        addi(2, 0, 0),                 # 1
        ld(3, 2, 0),                   # 2: load-use one word later
        mul(4, 3, 1),                  # 3: distance-1 MUL use, then stall
        br(isa.OP_BNE, 4, 0, 2),       # 4: taken -> 7
        addi(5, 0, 1),                 # 5: squashed
        addi(5, 0, 2),                 # 6: squashed
        addi(6, 0, 5),                 # 7
        halt(),                        # 8
    ]
    core = Core6(program, data=data_image())
    sim = Simulator(core)
    sim.add_clock(CLK_PERIOD, domain="sync")
    seen = {"fq": [], "fd": [], "dead_fields": [], "live": 0}

    async def tb(ctx):
        ctx.set(core.i_rst, 1)
        await ctx.tick()
        ctx.set(core.i_rst, 0)
        for _ in range(40):
            await ctx.tick()
            await ctx.delay(1e-9)
            seen["fq"].append(ctx.get(core.fq_valid))
            valid = ctx.get(core.fd_valid)
            seen["fd"].append(valid)
            bits = 0
            for sig in core.fd_fields.values():
                bits |= ctx.get(sig)
            if valid:
                seen["live"] += 1
            else:
                seen["dead_fields"].append(bits)

    sim.add_testbench(tb)
    sim.run()

    fq, fd = seen["fq"], seen["fd"]
    assert seen["live"] and seen["dead_fields"], "no live and no dead FD slot seen"
    for cycle in range(len(fd) - 1):
        assert not (fd[cycle + 1] and not fq[cycle] and not fd[cycle]), (
            f"cycle {cycle}: FD became valid with a dead fetch register")
    assert all(bits == 0 for bits in seen["dead_fields"]), (
        "a dead FD slot carried decoded fields")


@BOTH_FILES
def test_post_reset_first_retire_is_instruction_zero(ram_rf):
    """Reset empties the fetch register too; word 0 retires on cycle 6."""
    program = [addi(1, 0, 21), addi(2, 1, 1), halt()]
    run = run_core6(program, ram_rf=ram_rf)
    assert run.halted
    assert run.retires[0] == (6, 0, 1, 1, 21), run.retires[0]
    assert run.writes() == [(0, 1, 21), (1, 2, 22)]


# --- CPI budget -------------------------------------------------------------

@pytest.mark.parametrize("name,program,retired,cycles", [
    ("measurement-16-word", list(CORE_TOP_PROGRAM), 13, 24),
    ("store-load-loop", store_load_loop_program(), 35, 60),
    ("counted-loop", counted_loop_program(), 19, 39),
])
def test_cpi_within_the_120_mhz_break_even(name, program, retired, cycles):
    """The three programs of the 5-stage CPI table, at CPI <= 2.64.

    2.64 is break-even: 120 MHz / 2.64 = 45.45 MIPS against the 5-stage core's
    73.48 MHz / 1.615 = 45.50 MIPS.  The exact retirement counts and cycles are
    asserted as well, so a regression cannot hide under the bound.
    """
    machine, run = assert_lockstep(program)
    assert len(run.retires) == machine.retired == retired
    assert run.cycles == cycles, (name, run.cycles)
    cpi = run.cycles / len(run.retires)
    assert cpi <= CPI_BUDGET, (name, cpi)
    assert round(cpi, 3) == round(cycles / retired, 3)


# --- random programs -------------------------------------------------------

def _random_program(rng, length=22, mem=True):
    """Straight-line program over registers 1..8 mixing every result kind."""
    program = []
    for _ in range(length):
        kinds = ["addi", "alu", "mul", "shift", "lui"] + \
            (["ld", "st"] if mem else [])
        kind = rng.choice(kinds)
        rd = rng.randrange(1, 9)
        rs1 = rng.randrange(0, 9)
        rs2 = rng.randrange(0, 9)
        if kind == "addi":
            program.append(addi(rd, rs1, rng.randrange(-8, 8)))
        elif kind == "alu":
            program.append(alu(rng.choice([isa.ALU_ADD, isa.ALU_SUB,
                                            isa.ALU_AND, isa.ALU_OR]),
                               rd, rs1, rs2))
        elif kind == "mul":
            program.append(mul(rd, rs1, rs2))
        elif kind == "shift":
            program.append(shift(rd, rs1, rng.randrange(4), rng.randrange(16)))
        elif kind == "lui":
            program.append(lui(rd, rng.randrange(0x400)))
        elif kind == "ld":
            program.append(ld(rd, rng.randrange(0, 8), rng.randrange(-3, 4)))
        else:
            program.append(st(rs2, rng.randrange(0, 8), rng.randrange(-3, 4)))
    program.append(halt())
    return program


@pytest.mark.parametrize("seed", [0x6C05, 0x51A6])
def test_seeded_random_programs_lockstep(seed):
    """Mixed producers, consumers and memory, both register-file builds."""
    rng = random.Random(seed)
    for _ in range(3):
        program = _random_program(rng)
        for ram_rf in (False, True):
            machine, run = assert_lockstep(program, data=data_image(),
                                            ram_rf=ram_rf)
            assert run.halted
            assert run.writes() == machine.trace


def test_random_forward_branches_lockstep():
    """Forward-only branches: every skipped slot is inside the kill window."""
    rng = random.Random(0xB4A2)
    for _ in range(4):
        program = [addi(1, 0, 0)]
        for _ in range(8):
            kind = rng.choice(["br_taken", "br_not", "addi", "mul", "ld"])
            if kind == "addi":
                program.append(addi(rng.randrange(2, 7), rng.randrange(0, 7),
                                    rng.randrange(-4, 5)))
            elif kind == "mul":
                program.append(mul(rng.randrange(2, 7), rng.randrange(0, 7), 1))
            elif kind == "ld":
                program.append(ld(rng.randrange(2, 7), 1, rng.randrange(0, 4)))
            else:
                cond = isa.OP_BEQ if kind == "br_taken" else isa.OP_BNE
                skip = rng.choice([1, 2])
                program.append(br(cond, 1, 1, skip))
                for _ in range(skip):
                    program.append(addi(rng.randrange(2, 7), 0, 1))
        program.append(halt())
        machine, run = assert_lockstep(program, data=data_image())
        assert run.halted
        assert run.writes() == machine.trace


# --- shape ------------------------------------------------------------------

def test_external_ports_match_the_five_stage_core():
    """The same ports as ``Core``, bit for bit, and both elaborate."""
    core5, core6 = Core([halt()]), Core6([halt()])
    names = lambda c: {n for n in vars(c) if n.startswith(("i_", "o_"))}
    assert names(core6) == names(core5)
    for name in names(core5):
        assert len(getattr(core6, name)) == len(getattr(core5, name)), name
    assert core6.program == [halt()] and core6.data == []
    for core in (core5, core6):
        sim = Simulator(core)
        sim.add_clock(CLK_PERIOD, domain="sync")
        sim.run()
