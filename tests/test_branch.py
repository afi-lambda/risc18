"""Amaranth simulation tests for the M2 branch condition unit.

Contract under test (docs/ISA.md sections 4 and 5): ``i_cond`` 0 EQ, 1 NE,
2 LT, 3 GE; ``o_taken`` is combinational; LT and GE compare the operands as
signed 18-bit two's-complement words.  Expected values come from an
independent Python predicate written here (``ref_taken``), never from RTL.

Every branch test drives the RTL through ``amaranth.sim.Simulator``.
"""

import random

from amaranth import ClockDomain, Elaboratable, Module
from amaranth.sim import Simulator

from risc18 import isa
from risc18.branch import BranchUnit
from risc18.tops import BranchTop

MASK = isa.MASK
WIDTH = isa.WIDTH
SETTLE = 1e-9
CLK_PERIOD = 1e-6

COND_EQ, COND_NE, COND_LT, COND_GE = range(4)
CONDS = (COND_EQ, COND_NE, COND_LT, COND_GE)

#: 0, 1, 2, -1, min signed, max signed, plus two mid-range negatives.
SIGNED_EDGES = (0x00000, 0x00001, 0x00002, 0x3FFFF, 0x20000, 0x1FFFF,
                0x3FFFE, 0x20001)


def to_signed(value, bits=WIDTH):
    value &= (1 << bits) - 1
    if value & (1 << (bits - 1)):
        value -= 1 << bits
    return value


def ref_taken(cond, a, b):
    """Independent Python predicate for the four branch conditions."""
    a &= MASK
    b &= MASK
    if cond == COND_EQ:
        return int(a == b)
    if cond == COND_NE:
        return int(a != b)
    if cond == COND_LT:
        return int(to_signed(a) < to_signed(b))
    if cond == COND_GE:
        return int(to_signed(a) >= to_signed(b))
    raise AssertionError(f"bad cond {cond}")


class BranchHarness(Elaboratable):
    """The branch unit is combinational; a clock domain lets delays settle."""

    def __init__(self):
        self.dut = BranchUnit()

    def elaborate(self, platform):
        m = Module()
        m.domains.tck = ClockDomain("tck")
        m.submodules.dut = self.dut
        return m


class BranchTopHarness(Elaboratable):
    def __init__(self):
        self.top = BranchTop()

    def elaborate(self, platform):
        m = Module()
        cd = ClockDomain("tck")
        m.domains.tck = cd
        m.d.comb += self.top.clk.eq(cd.clk)
        m.submodules.top = self.top
        return m


async def _eval(ctx, dut, cond, a, b):
    ctx.set(dut.i_cond, cond)
    ctx.set(dut.i_a, a & MASK)
    ctx.set(dut.i_b, b & MASK)
    await ctx.delay(SETTLE)
    return ctx.get(dut.o_taken)


def _run(h, tb, domain="tck"):
    sim = Simulator(h)
    sim.add_clock(CLK_PERIOD, domain=domain)
    sim.add_testbench(tb, background=False)
    sim.run()


# --- encodings --------------------------------------------------------------

def test_condition_encodings_match_frozen_isa():
    h = BranchHarness()

    async def tb(ctx):
        await ctx.delay(SETTLE)

    _run(h, tb)
    assert len(h.dut.i_cond) == 2          # 4 conditions in 2 bits
    assert (COND_EQ, COND_NE, COND_LT, COND_GE) == (
        isa.COND_EQ, isa.COND_NE, isa.COND_LT, isa.COND_GE)


# --- hand-computed predicates ----------------------------------------------

def test_hand_computed_conditions():
    cases = [
        # equality / inequality
        (COND_EQ, 0x00000, 0x00000, 1),
        (COND_EQ, 0x12345, 0x12345, 1),
        (COND_EQ, 0x3FFFF, 0x3FFFF, 1),
        (COND_EQ, 0x3FFFF, 0x1FFFF, 0),
        (COND_EQ, 0x20000, 0x20001, 0),
        (COND_NE, 0x00000, 0x00000, 0),
        (COND_NE, 0x12345, 0x12345, 0),
        (COND_NE, 0x3FFFF, 0x1FFFF, 1),
        (COND_NE, 0x00000, 0x20000, 1),
        # signed LT: -1 < 0, min < max, 1 < 2, equal is not less
        (COND_LT, 0x3FFFF, 0x00000, 1),
        (COND_LT, 0x20000, 0x1FFFF, 1),
        (COND_LT, 0x00001, 0x00002, 1),
        (COND_LT, 0x00002, 0x00001, 0),
        (COND_LT, 0x12345, 0x12345, 0),
        (COND_LT, 0x00000, 0x3FFFF, 0),
        (COND_LT, 0x3FFFE, 0x3FFFF, 1),   # -2 < -1 is true (signed)
        (COND_LT, 0x3FFFF, 0x3FFFE, 0),   # -1 < -2 is false
        # signed GE: -1 >= 0 false, 0 >= -1 true, equal true
        (COND_GE, 0x3FFFF, 0x00000, 0),
        (COND_GE, 0x00000, 0x3FFFF, 1),
        (COND_GE, 0x1FFFF, 0x20000, 1),
        (COND_GE, 0x20000, 0x1FFFF, 0),
        (COND_GE, 0x12345, 0x12345, 1),
        (COND_GE, 0x3FFFE, 0x3FFFF, 0),   # -2 >= -1 is false
        (COND_GE, 0x3FFFF, 0x3FFFE, 1),   # -1 >= -2 is true
    ]
    h = BranchHarness()
    got = []

    async def tb(ctx):
        for cond, a, b, exp in cases:
            got.append((cond, a, b, exp, await _eval(ctx, h.dut, cond, a, b)))

    _run(h, tb)
    bad = [row for row in got if row[4] != row[3]]
    assert not bad, f"{len(bad)} mismatches, first: {bad[0]}"


def test_all_conditions_over_signed_edges():
    """Every condition x every ordered pair of signed edge operands."""
    h = BranchHarness()
    got = []

    async def tb(ctx):
        for cond in CONDS:
            for a in SIGNED_EDGES:
                for b in SIGNED_EDGES:
                    got.append((cond, a, b, await _eval(ctx, h.dut, cond, a, b)))

    _run(h, tb)
    assert len(got) == len(CONDS) * len(SIGNED_EDGES) ** 2
    bad = [(c, a, b, ref_taken(c, a, b), y)
           for c, a, b, y in got if y != ref_taken(c, a, b)]
    assert not bad, f"{len(bad)} mismatches, first: {bad[0]}"


def test_signed_comparison_is_not_unsigned():
    """The two orderings disagree exactly on operand pairs with mixed signs."""
    pairs = [(0x3FFFF, 0x00000), (0x20000, 0x1FFFF), (0x3FFFE, 0x00001)]
    h = BranchHarness()
    seen = []

    async def tb(ctx):
        for a, b in pairs:
            lt = await _eval(ctx, h.dut, COND_LT, a, b)
            ge = await _eval(ctx, h.dut, COND_GE, a, b)
            eq = await _eval(ctx, h.dut, COND_EQ, a, b)
            ne = await _eval(ctx, h.dut, COND_NE, a, b)
            seen.append((a, b, lt, ge, eq, ne))

    _run(h, tb)
    for a, b, lt, ge, eq, ne in seen:
        # Signed: a is negative, b is not, so signed a < b holds.
        assert (lt, ge) == (1, 0), (hex(a), hex(b), lt, ge)
        assert lt != int(a < b), f"unsigned comparison leaked in for {a:#x},{b:#x}"
        assert (eq, ne) == (0, 1)


def test_conditions_are_mutually_consistent():
    """EQ/NE and LT/GE are complements, for every edge pair."""
    h = BranchHarness()
    seen = []

    async def tb(ctx):
        for a in SIGNED_EDGES:
            for b in SIGNED_EDGES:
                row = [a, b]
                for cond in CONDS:
                    row.append(await _eval(ctx, h.dut, cond, a, b))
                seen.append(tuple(row))

    _run(h, tb)
    for a, b, eq, ne, lt, ge in seen:
        assert eq ^ ne == 1, (hex(a), hex(b), eq, ne)
        assert lt ^ ge == 1, (hex(a), hex(b), lt, ge)
        # Trichotomy: every ordered pair satisfies EQ, LT or GE, and never
        # both EQ and LT.
        assert eq | lt | ge == 1, (hex(a), hex(b), eq, lt, ge)
        assert eq & lt == 0, (hex(a), hex(b), eq, lt)


# --- random pairs -----------------------------------------------------------

def test_random_pairs_against_python_predicate():
    """>= 300 seeded (cond, a, b) triples vs an independent predicate."""
    rng = random.Random(0xB4A2C)
    vectors = [(cond, rng.randrange(1 << WIDTH), rng.randrange(1 << WIDTH))
               for cond in CONDS for _ in range(100)]
    assert len(vectors) >= 300
    assert len({c for c, _, _ in vectors}) == 4

    h = BranchHarness()
    got = []

    async def tb(ctx):
        for cond, a, b in vectors:
            got.append((cond, a, b, await _eval(ctx, h.dut, cond, a, b)))

    _run(h, tb)
    bad = [(c, a, b, ref_taken(c, a, b), y)
           for c, a, b, y in got if y != ref_taken(c, a, b)]
    assert not bad, f"{len(bad)} mismatches, first: {bad[0]}"


def test_random_near_equal_pairs():
    """Pairs that differ in one bit, or by one, stress the comparator chains."""
    rng = random.Random(0xC0DE)
    vectors = []
    for cond in CONDS:
        for _ in range(60):
            a = rng.randrange(1 << WIDTH)
            b = a ^ (1 << rng.randrange(WIDTH))
            vectors.append((cond, a, b))
            vectors.append((cond, a, (a + 1) & MASK))
            vectors.append((cond, a, (a - 1) & MASK))
    h = BranchHarness()
    got = []

    async def tb(ctx):
        for cond, a, b in vectors:
            got.append((cond, a, b, await _eval(ctx, h.dut, cond, a, b)))

    _run(h, tb)
    assert len(got) == len(vectors)
    bad = [(c, a, b, ref_taken(c, a, b), y)
           for c, a, b, y in got if y != ref_taken(c, a, b)]
    assert not bad, f"{len(bad)} mismatches, first: {bad[0]}"


# --- structural -------------------------------------------------------------

def test_output_widths():
    h = BranchHarness()
    dut = h.dut
    assert len(dut.i_a) == 18
    assert len(dut.i_b) == 18
    assert len(dut.i_cond) == 2
    assert len(dut.o_taken) == 1

    rng = random.Random(0x77)
    seen = []

    async def tb(ctx):
        for cond in CONDS:
            for _ in range(25):
                seen.append(await _eval(ctx, h.dut, cond,
                                        rng.randrange(1 << WIDTH),
                                        rng.randrange(1 << WIDTH)))
        for cond in CONDS:
            seen.append(await _eval(ctx, h.dut, cond, MASK, MASK))

    _run(h, tb)
    assert all(v in (0, 1) for v in seen), sorted(set(seen))


# --- synthesis top ----------------------------------------------------------

def test_branch_top_registers_inputs_and_outputs():
    h = BranchTopHarness()
    top = h.top
    observed = []

    async def tb(ctx):
        await ctx.tick("tck")
        ctx.set(top.i_a, 0x3FFFF)      # -1
        ctx.set(top.i_b, 0x00000)
        ctx.set(top.i_cond, COND_LT)
        await ctx.tick("tck")          # samples the inputs
        await ctx.tick("tck")          # predicate captured into o_taken
        await ctx.delay(SETTLE)
        observed.append(ctx.get(top.o_taken))
        # Change the inputs: registered output must hold one more cycle, then
        # follow the new (false) predicate.
        ctx.set(top.i_a, 0x00002)
        ctx.set(top.i_b, 0x00001)
        await ctx.delay(SETTLE)
        observed.append(ctx.get(top.o_taken))
        await ctx.tick("tck")
        await ctx.delay(SETTLE)
        # One edge captures the new operands; the predicate register still
        # holds the old result, so a single-stage top is distinguishable.
        observed.append(ctx.get(top.o_taken))
        await ctx.tick("tck")
        await ctx.delay(SETTLE)
        observed.append(ctx.get(top.o_taken))

    _run(h, tb)
    assert observed[0] == 1, observed
    assert observed[1] == 1, observed
    assert observed[2] == 1, observed
    assert observed[3] == 0, observed


def test_branch_agrees_with_frozen_golden_model():
    """The milestone gate: predicate vs the frozen instruction-accurate model."""
    from risc18 import golden

    h = BranchHarness()
    seen = []

    async def tb(ctx):
        for cond in CONDS:
            for a in SIGNED_EDGES:
                for b in SIGNED_EDGES:
                    seen.append((cond, a, b,
                                 await _eval(ctx, h.dut, cond, a, b)))

    _run(h, tb)
    for cond, a, b, got in seen:
        assert got == int(golden._cond(cond, a, b)), (cond, a, b, got)
