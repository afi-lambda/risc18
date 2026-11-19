"""Simulation tests for the seven-stage :class:`risc18.core7.Core7`.

The schedule under test is F/IQ/D/R/X/M/W (``docs/ISA.md`` section 7): the PC
register addresses the instruction EBR (F), the EBR's output lands in the fetch
register ``fq`` and is decoded (IQ), the decode fields are registered into FD
and drive the register-file read address (D), the file data and the bypass
select are captured into the operand registers (R), the ALU / compare /
multiply run (X), the data EBR is addressed in X and its word is captured in M,
and W writes back the registered word.

The oracle is the same one every other core in this tree uses:
``golden.Machine``.  Every retire row with ``we=1, rd!=0`` must equal its
``.trace`` in order, and the number of retire pulses must equal its
``.retired`` -- so a killed slot that retired, or a live one that vanished,
cannot hide.  ``test_retire_trace_matches_core_and_core6`` additionally pins
the three cores to each other on the same programs.

What this file pins, beyond lockstep:

* the fill: word 0 retires on cycle 6 (seven edges counting the reset edge that
  loaded PC 0, one edge later than ``Core6``);
* the bubble table of section 7.2, measured per distance: ALU and MUL cost
  nothing at distance 1, 2 and 3, a load costs exactly one bubble at distance 1
  and nothing from distance 2 on;
* the taken-transfer cost in retire slots (5, the same as ``Core6``: the R stage
  Core7 adds sits on the fetch side of X in place of Core6's D stage) and that a
  not-taken branch costs nothing;
* that a killed slot in *every* position of the window never retires, never
  writes a register, never writes memory, never halts and never links;
* the CPI of the three measurement programs of ``docs/RESULTS.md``.
"""

import pytest
from amaranth.sim import Simulator

from risc18 import isa
from risc18.core import Core
from risc18.core6 import Core6
from risc18.core7 import Core7
from risc18.tops import CORE_TOP_PROGRAM

# Same instruction builders, test driver and golden-model helper as the other
# two core suites: the programs below are the ones they use, and the
# retire-trace comparison is the same.
from test_core import (_diff, _drive, addi, alu, br, data_image, halt, jal,
                       jalr, ld, mul, nop, st)
from test_core6 import (counted_loop_program, jal_program, jalr_program,
                        load_store_program, store_load_loop_program,
                        sum_loop_program)

CLK_PERIOD = 1e-6

#: Retire slots a taken control transfer costs: the two-cycle squash window
#: plus the words in flight behind it (section 7.3's "recovery", measured in
#: retire slots the way ``tests/test_core6.py`` measures Core6's).
TAKEN = 5


# --- testbench driver -------------------------------------------------------

def run_core7(program, max_cycles=4000, post_cycles=8, data=None):
    """Run ``program`` on a :class:`Core7` and return its retire trace."""
    core = Core7(program, data=data)
    sim = Simulator(core)
    sim.add_clock(CLK_PERIOD, domain="sync")

    holder = {}

    async def tb(ctx):
        holder["run"] = await _drive(ctx, core, max_cycles, post_cycles)

    sim.add_testbench(tb)
    sim.run()
    return holder["run"]


def run_dut(core, max_cycles=4000, post_cycles=8):
    """Drive an already-constructed core (any of the three) and return its run."""
    sim = Simulator(core)
    sim.add_clock(CLK_PERIOD, domain="sync")

    holder = {}

    async def tb(ctx):
        holder["run"] = await _drive(ctx, core, max_cycles, post_cycles)

    sim.add_testbench(tb)
    sim.run()
    return holder["run"]


def run_core(program, max_cycles=4000, post_cycles=8, data=None, cls=Core):
    core = cls(program, data=data)
    sim = Simulator(core)
    sim.add_clock(CLK_PERIOD, domain="sync")

    holder = {}

    async def tb(ctx):
        holder["run"] = await _drive(ctx, core, max_cycles, post_cycles)

    sim.add_testbench(tb)
    sim.run()
    return holder["run"]


def assert_lockstep(program, max_cycles=None, data=None):
    """Compare a whole ``Core7`` run against ``golden.Machine``."""
    machine = golden_machine(program, data)
    assert machine.halted, "golden model did not halt: bad test program"
    if max_cycles is None:
        max_cycles = 10 * machine.retired + 100
    run = run_core7(program, max_cycles=max_cycles, data=data)
    assert run.halted, f"core did not halt in {max_cycles} cycles"
    assert run.writes() == machine.trace, _diff(run.writes(), machine.trace)
    assert len(run.retires) == machine.retired, (
        f"{len(run.retires)} retire pulses for {machine.retired} executed "
        f"instructions")
    return machine, run


def golden_machine(program, data=None, cap=100_000):
    from risc18.golden import Machine
    machine = Machine(program)
    if data:
        preloaded = [w & isa.MASK for w in list(data)[:isa.MEM_WORDS]]
        machine.mem[:len(preloaded)] = preloaded
    return machine.run(cap)


# --- the fill ---------------------------------------------------------------

def test_post_reset_first_retire_is_instruction_zero():
    """Reset empties ``fq`` too; word 0 retires on cycle 6 (seven edges).

    Cycles are counted from the first edge after the reset edge, as in
    ``tests/test_core.py``: the reset edge loads PC 0, so the word is fetched on
    edge 1 (F), sits in ``fq`` on edge 2 (IQ), is registered in D on 3, in R on
    4, in M on 5 and in W on 6 — the same fill ``Core6`` has, because F and IQ
    are the two edges of one EBR access and Core6's ``F1`` covers both.
    """
    program = [addi(1, 0, 21), addi(2, 1, 1), halt()]
    run = run_core7(program)
    assert run.halted
    assert run.retires[0] == (6, 0, 1, 1, 21), run.retires[0]
    assert run.writes() == [(0, 1, 21), (1, 2, 22)]


def test_a_straight_line_program_retires_at_one_per_cycle():
    program = [addi(1, 0, 5), addi(2, 1, 3),
               alu(isa.ALU_SUB, 3, 2, 1), halt()]
    machine, run = assert_lockstep(program)
    assert len(run.retires) == machine.retired == 4
    assert run.cycles == 4, run.cycles
    assert run.cycles / len(run.retires) == 1.0


# --- cycle accounting: the taken-transfer cost ------------------------------

def test_a_taken_branch_costs_five_retire_slots():
    """One taken branch: 5 slots, the same as Core6.

    The redirect is registered in the X cycle and applied on the following
    edge, so the two-cycle squash window covers the R slot, the D slot, the
    ``fq`` slot and the two words the EBR holds from the old PC.
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
    assert run.cycles == len(run.retires) + TAKEN


def test_a_not_taken_branch_costs_nothing():
    program = [
        addi(1, 0, 0),
        nop(), nop(),
        addi(2, 0, 1),
        br(isa.OP_BEQ, 1, 2, 2),       # not taken: r1 != r2
        addi(3, 0, 9),
        addi(3, 0, 8),
        addi(4, 0, 4),
        halt(),
    ]
    machine, run = assert_lockstep(program)
    assert len(run.retires) == machine.retired == len(program)
    assert run.cycles == len(program), run.cycles


def test_two_taken_branches_cost_ten_slots():
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
    assert run.cycles == len(run.retires) + 2 * TAKEN


def test_a_taken_jal_costs_five_slots():
    program = [
        addi(1, 0, 0),
        jal(2, 2),                     # 1: r2 = 2, -> 4
        addi(3, 0, 1),                 # 2: squashed
        addi(3, 0, 2),                 # 3: squashed
        addi(4, 0, 4),                 # 4
        halt(),                        # 5
    ]
    machine, run = assert_lockstep(program)
    assert len(run.retires) == machine.retired == 4
    assert run.cycles == 9, run.cycles
    assert run.cycles == len(run.retires) + TAKEN


def test_a_taken_jalr_costs_five_slots():
    program = [
        addi(7, 0, 9),                 # 0: r7 = 9
        jalr(1, 7, 1),                 # 1: r1 = 2, -> 10
        addi(2, 0, 1),                 # 2..9: squashed
        addi(2, 0, 2),
        addi(2, 0, 3),
        addi(2, 0, 4),
        addi(2, 0, 5),
        addi(2, 0, 6),
        addi(2, 0, 7),
        addi(2, 0, 8),
        addi(8, 0, 1),                 # 10: the indirect target
        halt(),                        # 11
    ]
    machine, run = assert_lockstep(program)
    assert len(run.retires) == machine.retired == 4
    assert run.cycles == 9, run.cycles
    assert run.cycles == len(run.retires) + TAKEN
    assert run.writes() == [(0, 7, 9), (1, 1, 2), (10, 8, 1)] == machine.trace


# --- cycle accounting: ALU dependencies are free at every distance ----------

def test_alu_dependency_at_distance_one_costs_no_bubble():
    """The producer is in X while the consumer reads operands: 0 bubbles."""
    program = [
        addi(1, 0, 3),                 # 0
        addi(2, 0, 4),                 # 1
        alu(isa.ALU_ADD, 3, 1, 2),     # 2: r3 = 7
        alu(isa.ALU_SUB, 4, 3, 1),     # 3: distance-1 consumer: 7 - 3 = 4
        halt(),                        # 4
    ]
    machine, run = assert_lockstep(program)
    assert len(run.retires) == machine.retired == len(program)
    assert run.cycles == len(program), run.cycles
    assert run.writes() == [(0, 1, 3), (1, 2, 4), (2, 3, 7), (3, 4, 4)]


@pytest.mark.parametrize("distance", [1, 2, 3])
def test_alu_dependency_costs_no_bubble_at_any_distance(distance):
    program = [addi(1, 0, 3), alu(isa.ALU_ADD, 2, 1, 1)]
    program += [nop()] * (distance - 1)
    program += [addi(3, 2, 1), halt()]
    machine, run = assert_lockstep(program)
    assert len(run.retires) == machine.retired == len(program)
    assert run.cycles == len(program), (distance, run.cycles)


@pytest.mark.parametrize("distance", [1, 2])
def test_mul_dependency_costs_no_bubble_at_any_distance(distance):
    """The product is registered in X and the consumer takes it from M.

    Distance 1 is the M->X input bypass (section 7.2's first row); distance 2
    is the R latch reading ``m_value``.  Neither stalls.
    """
    program = [addi(1, 0, 3), mul(2, 1, 1)]      # r2 = 9
    program += [nop()] * (distance - 1)
    program += [addi(3, 2, 1), halt()]
    machine, run = assert_lockstep(program)
    got = {rd: value for _, rd, value in run.writes()}
    assert got[2] == 9 and got[3] == 10
    assert len(run.retires) == machine.retired == len(program)
    assert run.cycles == len(program), (distance, run.cycles)


def test_a_mul_chain_costs_no_bubble():
    """Four back-to-back MULs: every dependency at distance 1, none stalls."""
    program = [
        addi(1, 0, 3),
        mul(2, 1, 1),                  # 9
        mul(3, 2, 2),                  # 81
        mul(4, 3, 3),                  # 81 * 81 -> low 18 bits
        mul(5, 4, 4),
        addi(6, 5, 1),
        halt(),
    ]
    machine, run = assert_lockstep(program)
    got = {rd: value for _, rd, value in run.writes()}
    assert got[2] == 9 and got[3] == 81
    assert got[5] == machine.trace[-2][2]
    assert len(run.retires) == machine.retired == len(program)
    assert run.cycles == len(program), run.cycles


# --- cycle accounting: the one-bubble load interlock ------------------------

def test_distance_one_load_use_costs_exactly_one_bubble():
    """``LD rX,[rY]`` then a consumer: 1 bubble, word taken from M."""
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


@pytest.mark.parametrize("distance", [2, 3])
def test_load_use_from_distance_two_needs_no_bubble(distance):
    program = [addi(1, 0, 0), nop(), nop(), ld(2, 1, 0)]
    program += [nop()] * (distance - 1)
    program += [addi(3, 2, 1), halt()]
    machine, run = assert_lockstep(program, data=data_image())
    assert run.writes() == machine.trace
    assert len(run.retires) == machine.retired == len(program)
    assert run.cycles == len(program), (distance, run.cycles)


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


def test_a_load_feeds_a_taken_branch_and_a_jalr_target():
    """The interlock and the M bypass also serve the X-stage control path."""
    image = data_image()
    program = [
        addi(1, 0, 0),                 # 0: base
        addi(9, 0, 3),                 # 1: value 3
        st(9, 1, 2),                   # 2: mem[2] = 3
        nop(),                         # 3
        ld(2, 1, 0),                   # 4: r2 = image[0]
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
    # 10 retires: 2 taken transfers (+5 each) and 2 distance-1 load uses (+1).
    assert len(run.retires) == machine.retired == 10
    assert run.cycles == len(run.retires) + 2 * TAKEN + 2 * 1, run.cycles


# --- the kill window: nothing killed may retire or write --------------------

def test_a_store_in_each_killed_slot_never_commits():
    """Each of the 5 slots behind a taken branch holds a store: none lands."""
    image = [0x20000 | (i & 0x3FF) for i in range(1024)]
    for slot in range(1, 6):
        program = [addi(1, 0, 0x10), addi(2, 0, 7), br(isa.OP_BEQ, 1, 1, 5)]
        program += [st(2, 1, s) for s in range(1, 6)]
        program += [ld(3, 1, slot), halt()]
        machine, run = assert_lockstep(program, data=image)
        assert run.writes()[-1] == (8, 3, image[0x10 + slot]), (slot, run.writes())
        assert machine.mem[0x10 + slot] == image[0x10 + slot], slot
        assert len(run.retires) == machine.retired == 5
        assert run.cycles == 5 + TAKEN, (slot, run.cycles)


@pytest.mark.parametrize("killed", ["addi", "halt", "jal", "mul"])
def test_a_killed_slot_never_retires_halts_links_or_writes(killed):
    """Every one of the 5 killed positions, for each kind of instruction."""
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
    assert run.cycles == 4 + TAKEN, (killed, run.cycles)
    if killed in ("addi", "mul"):
        assert 5 not in {rd for _, rd, _ in run.writes()}


def test_the_squash_window_is_two_cycles():
    """``ct_valid`` pulses once and takes the whole window with it.

    The redirect is applied on the edge after the registering edge, so the
    window is exactly two cycles (``ct_valid`` and the edge behind it) and no
    slot inside it is ever decoded: the FD payload is cleared with the
    validity, which is what keeps a killed branch or HALT out of the control
    logic.
    """
    program = [
        addi(1, 0, 3),                 # 0
        nop(), nop(),                  # 1, 2
        br(isa.OP_BEQ, 1, 1, 2),       # 3: taken -> 6
        addi(5, 0, 1),                 # 4: squashed
        addi(5, 0, 2),                 # 5: squashed
        addi(6, 0, 5),                 # 6
        halt(),                        # 7
    ]
    core = Core7(program)
    sim = Simulator(core)
    sim.add_clock(CLK_PERIOD, domain="sync")
    seen = {"ct": [], "dead_fields": []}

    async def tb(ctx):
        ctx.set(core.i_rst, 1)
        await ctx.tick()
        ctx.set(core.i_rst, 0)
        for _ in range(30):
            await ctx.tick()
            await ctx.delay(1e-9)
            seen["ct"].append(ctx.get(core.ct_valid))
            if not ctx.get(core.fd_valid):
                bits = 0
                for sig in core.fd_fields.values():
                    bits |= ctx.get(sig)
                seen["dead_fields"].append(bits)

    sim.add_testbench(tb)
    sim.run()

    ct = seen["ct"]
    # exactly one contiguous pulse
    assert sum(ct) == 1, ct
    assert seen["dead_fields"] and all(b == 0 for b in seen["dead_fields"])
    assert max(ct) == 1


# --- the three cores agree --------------------------------------------------

@pytest.mark.parametrize("name,program,data", [
    ("measurement-16-word", list(CORE_TOP_PROGRAM), None),
    ("counted-loop", counted_loop_program(), None),
    ("store-load-loop", store_load_loop_program(), None),
    ("sum-loop", sum_loop_program(), None),
    ("load-store", load_store_program(), data_image()),
    ("jal", jal_program(), None),
    ("jalr", jalr_program(), None),
])
def test_retire_trace_matches_core_and_core6(name, program, data):
    """The retire interface is section 6's: identical to both other cores."""
    machine, run7 = assert_lockstep(program, data=data)
    run6 = run_core(program, data=data, cls=Core6)
    run5 = run_core(program, data=data, cls=Core)
    assert run7.writes() == run6.writes() == run5.writes(), name
    assert (len(run7.retires) == len(run6.retires) == len(run5.retires)
            == machine.retired), name
    assert run7.halted and run6.halted and run5.halted, name
    # Same PCs, same order, same pulses -- only the cycle numbers differ.
    assert ([p for _, p, _, _, _ in run7.retires]
            == [p for _, p, _, _, _ in run6.retires]
            == [p for _, p, _, _, _ in run5.retires]), name


# --- CPI -------------------------------------------------------------------

@pytest.mark.parametrize("name,program,retired,cycles", [
    ("measurement-16-word", list(CORE_TOP_PROGRAM), 13, 23),
    ("store-load-loop", store_load_loop_program(), 35, 60),
    ("counted-loop", counted_loop_program(), 19, 39),
])
def test_cpi(name, program, retired, cycles):
    """The three measurement programs, at Core7's measured cycles.

    120 MHz / 1.769 (the worst of the three) = 67.8 MIPS against the 5-stage
    core's 45.50; ``docs/metrics/core7_sim.md`` carries the table.
    """
    machine, run = assert_lockstep(program)
    assert len(run.retires) == machine.retired == retired
    assert run.cycles == cycles, (name, run.cycles)
    cpi = run.cycles / len(run.retires)
    assert round(cpi, 3) == round(cycles / retired, 3)
    assert cpi < 2.64, (name, cpi)


# --- interface parity ------------------------------------------------------

def test_the_one_bubble_fallback_is_selectable_and_costs_one_bubble():
    """Section 7.2's fallback: no input mux, one bubble per distance-1 use.

    ``Core7(..., x_bypass=False)`` removes the M -> X input mux; every
    distance-1 producer then stalls one cycle and its value arrives through the
    R latch from ``m_value``.  The trace is unchanged -- only the cycles are.
    """
    program = [
        addi(1, 0, 3),                 # 0
        addi(2, 0, 4),                 # 1
        alu(isa.ALU_ADD, 3, 1, 2),     # 2: r3 = 7
        alu(isa.ALU_SUB, 4, 3, 1),     # 3: distance-1 consumer: 7 - 3 = 4
        halt(),                        # 4
    ]
    machine = golden_machine(program)
    assert machine.halted
    run = run_dut(Core7(program, x_bypass=False))
    assert run.halted
    assert run.writes() == machine.trace
    assert len(run.retires) == machine.retired == len(program)
    # Two distance-1 dependencies in this program: r2 -> alu(r3) and r3 -> alu(r4).
    assert run.cycles == len(program) + 2, run.cycles


def test_registered_ram_register_file_is_rejected():
    """Section 7.4 forbids the extra stage RegFileRam's read would need."""
    with pytest.raises(ValueError):
        Core7([halt()], ram_rf=True)


def test_ports_are_cores():
    """Exactly ``Core``'s port bundle, and the ``sync`` domain it owns."""
    core = Core7([halt()])
    assert core.cd.name == "sync"
    for name, width in [("i_rst", 1), ("o_pc", 16), ("o_retire", 1),
                        ("o_retire_pc", 16), ("o_retire_we", 1),
                        ("o_retire_rd", 4), ("o_retire_value", 18),
                        ("o_halt", 1)]:
        sig = getattr(core, name)
        assert len(sig) == width, name
