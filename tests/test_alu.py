"""Amaranth simulation tests for the M2 18-bit ALU.

Every test drives the RTL through ``amaranth.sim.Simulator``; the expected
values come from hand-computed literals and from an independent Python
reference (``ref_alu``) written here, not from the RTL or from ``isa`` helpers
that the RTL shares.  Contract under test (docs/ISA.md sections 4 and 5):

* 8 functions — 0 ADD, 1 SUB, 2 AND, 3 OR, 4 SLL, 5 SRL, 6 SRA, 7 ROR;
* the shift amount is ``i_b[3:0]`` (bits [17:4] are ignored);
* results are 18 bits and wrap; SRA sign-fills; ROR rotates within 18 bits;
* a shift by 0 returns the operand unchanged.
"""

import random

from amaranth import ClockDomain, Elaboratable, Module
from amaranth.sim import Simulator

from risc18 import isa
from risc18.alu import ALU
from risc18.tops import AluTop

MASK = isa.MASK
WIDTH = isa.WIDTH
SETTLE = 1e-9
CLK_PERIOD = 1e-6

OP_ADD, OP_SUB, OP_AND, OP_OR, OP_SLL, OP_SRL, OP_SRA, OP_ROR = range(8)
SHIFT_OPS = (OP_SLL, OP_SRL, OP_SRA, OP_ROR)
SHIFT_AMOUNTS = (0, 1, 4, 15)

#: 0, 1, 2, -1, min signed, max signed.
BOUNDARIES = (0x00000, 0x00001, 0x00002, 0x3FFFF, 0x20000, 0x1FFFF)


def to_signed(value, bits=WIDTH):
    """Two's-complement interpretation of an 18-bit word."""
    value &= (1 << bits) - 1
    if value & (1 << (bits - 1)):
        value -= 1 << bits
    return value


def ref_alu(op, a, b):
    """Independent Python model of the ALU contract (18-bit, wrapping)."""
    a &= MASK
    b &= MASK
    amt = b & 0xF
    if op == OP_ADD:
        return (a + b) & MASK
    if op == OP_SUB:
        return (a - b) & MASK
    if op == OP_AND:
        return a & b
    if op == OP_OR:
        return a | b
    if op == OP_SLL:
        return (a << amt) & MASK
    if op == OP_SRL:
        return a >> amt
    if op == OP_SRA:
        return (to_signed(a) >> amt) & MASK
    if op == OP_ROR:
        # Python's >> is logical for non-negative ints, so this is a rotate
        # inside 18 bits; amt == 0 leaves ``a`` untouched.
        return ((a >> amt) | (a << (WIDTH - amt))) & MASK
    raise AssertionError(f"bad op {op}")


class AluHarness(Elaboratable):
    """The ALU is combinational; a clock domain exists so delays settle."""

    def __init__(self):
        self.dut = ALU()

    def elaborate(self, platform):
        m = Module()
        m.domains.tck = ClockDomain("tck")
        m.submodules.dut = self.dut
        return m


class AluTopHarness(Elaboratable):
    """Drive :class:`AluTop`'s ``clk`` port from a separate sim domain."""

    def __init__(self):
        self.top = AluTop()

    def elaborate(self, platform):
        m = Module()
        cd = ClockDomain("tck")
        m.domains.tck = cd
        m.d.comb += self.top.clk.eq(cd.clk)
        m.submodules.top = self.top
        return m


async def _eval(ctx, dut, op, a, b):
    ctx.set(dut.i_a, a & MASK)
    ctx.set(dut.i_b, b & MASK)
    ctx.set(dut.i_op, op)
    await ctx.delay(SETTLE)
    return ctx.get(dut.o_y)


def _run(h, tb, domain="tck"):
    sim = Simulator(h)
    sim.add_clock(CLK_PERIOD, domain=domain)
    sim.add_testbench(tb, background=False)
    sim.run()


# --- op encodings -----------------------------------------------------------

def test_op_encodings_match_frozen_isa():
    """The ALU's op numbering is the decoder's ``alu_op`` numbering."""
    from risc18 import alu

    assert (alu.OP_ADD, alu.OP_SUB, alu.OP_AND, alu.OP_OR) == (
        isa.ALU_ADD, isa.ALU_SUB, isa.ALU_AND, isa.ALU_OR)
    assert (alu.OP_SLL, alu.OP_SRL, alu.OP_SRA, alu.OP_ROR) == (
        4, 5, 6, 7)
    # SHIFT imm6[5:4] kinds map onto ops 4..7 in the same order.
    for kind, op in ((isa.SHIFT_SLL, alu.OP_SLL), (isa.SHIFT_SRL, alu.OP_SRL),
                     (isa.SHIFT_SRA, alu.OP_SRA), (isa.SHIFT_ROR, alu.OP_ROR)):
        assert op == 4 + kind


# --- hand-computed single vectors -------------------------------------------

def test_add_sub_and_or_hand_computed():
    cases = [
        # ADD: plain sum, carry-out wrap, both-max sum.
        (OP_ADD, 0x00005, 0x00007, 0x0000C),
        (OP_ADD, 0x3FFFF, 0x00001, 0x00000),
        (OP_ADD, 0x1FFFF, 0x1FFFF, 0x3FFFE),
        (OP_ADD, 0x20000, 0x20000, 0x00000),
        # SUB: plain difference, borrow wrap, min-minus-one wrap.
        (OP_SUB, 0x00007, 0x00003, 0x00004),
        (OP_SUB, 0x00000, 0x00001, 0x3FFFF),
        (OP_SUB, 0x20000, 0x00001, 0x1FFFF),
        (OP_SUB, 0x00000, 0x20000, 0x20000),
        # AND / OR are bitwise and never widen.
        (OP_AND, 0x3FFFF, 0x12345, 0x12345),
        (OP_AND, 0x2AAAA, 0x0FFFF, 0x0AAAA),
        (OP_AND, 0x15555, 0x2AAAA, 0x00000),
        (OP_AND, 0x1FFFF, 0x20000, 0x00000),
        (OP_OR, 0x12345, 0x0AAAA, 0x1ABEF),
        (OP_OR, 0x20000, 0x00001, 0x20001),
        (OP_OR, 0x15555, 0x2AAAA, 0x3FFFF),
        (OP_OR, 0x00000, 0x00000, 0x00000),
    ]
    h = AluHarness()
    got = []

    async def tb(ctx):
        for op, a, b, exp in cases:
            got.append((op, a, b, exp, await _eval(ctx, h.dut, op, a, b)))

    _run(h, tb)
    bad = [row for row in got if row[4] != row[3]]
    assert not bad, f"{len(bad)} mismatches, first: {bad[0]}"


def test_sll_srl_hand_computed():
    a = 0x12345
    cases = [
        (OP_SLL, 0, 0x12345),   # shift by 0 is the identity
        (OP_SLL, 1, 0x2468A),
        (OP_SLL, 4, 0x23450),
        (OP_SLL, 15, 0x28000),  # only the low 3 bits survive
        (OP_SLL, 0x10, 0x12345),  # i_b[3:0] == 0 -> identity, bit 4 ignored
        (OP_SRL, 0, 0x12345),
        (OP_SRL, 1, 0x091A2),
        (OP_SRL, 4, 0x01234),
        (OP_SRL, 15, 0x00002),
    ]
    h = AluHarness()
    got = []

    async def tb(ctx):
        for op, amt, exp in cases:
            got.append((op, amt, exp, await _eval(ctx, h.dut, op, a, amt)))

    _run(h, tb)
    bad = [row for row in got if row[3] != row[2]]
    assert not bad, f"{len(bad)} mismatches, first: {bad[0]}"


def test_sra_sign_fill():
    cases = [
        # -1 stays -1 at every amount.
        (0x3FFFF, 0, 0x3FFFF),
        (0x3FFFF, 1, 0x3FFFF),
        (0x3FFFF, 4, 0x3FFFF),
        (0x3FFFF, 15, 0x3FFFF),
        # min signed: -131072 -> -65536, -8192, -4.
        (0x20000, 1, 0x30000),
        (0x20000, 4, 0x3E000),
        (0x20000, 15, 0x3FFFC),
        # negative with a set bit in the shifted-out field.
        (0x20001, 1, 0x30000),
        (0x20001, 4, 0x3E000),
        (0x20001, 15, 0x3FFFC),
        # non-negative: no sign fill, same as SRL.
        (0x1FFFF, 0, 0x1FFFF),
        (0x1FFFF, 1, 0x0FFFF),
        (0x1FFFF, 4, 0x01FFF),
        (0x1FFFF, 15, 0x00003),
        (0x00000, 15, 0x00000),
    ]
    h = AluHarness()
    got = []

    async def tb(ctx):
        for a, amt, exp in cases:
            got.append((a, amt, exp, await _eval(ctx, h.dut, OP_SRA, a, amt)))

    _run(h, tb)
    bad = [row for row in got if row[3] != row[2]]
    assert not bad, f"{len(bad)} mismatches, first: {bad[0]}"
    # Sign bit must stay set for every negative operand, cleared otherwise.
    for a, amt, exp, y in got:
        assert (y >> (WIDTH - 1)) == (to_signed(a) < 0), (a, amt, y)


def test_ror_rotates_inside_18_bits():
    cases = [
        (0x12345, 0, 0x12345),
        (0x12345, 1, 0x291A2),   # bit 0 wraps to bit 17
        (0x12345, 4, 0x15234),
        (0x12345, 15, 0x11A2A),
        (0x3FFFF, 1, 0x3FFFF),   # all ones is a fixed point
        (0x3FFFF, 15, 0x3FFFF),
        (0x20000, 1, 0x10000),   # pure rotate, not a sign-filling shift
        (0x20000, 4, 0x02000),
        (0x20000, 15, 0x00004),
        (0x00001, 1, 0x20000),
        (0x00001, 4, 0x04000),
        (0x00001, 15, 0x00008),
    ]
    h = AluHarness()
    got = []

    async def tb(ctx):
        for a, amt, exp in cases:
            got.append((a, amt, exp, await _eval(ctx, h.dut, OP_ROR, a, amt)))

    _run(h, tb)
    bad = [row for row in got if row[3] != row[2]]
    assert not bad, f"{len(bad)} mismatches, first: {bad[0]}"


def test_ror_eighteen_steps_return_operand():
    """18 single-bit rotations are the identity; 0x12345 has full period 18."""
    starts = (0x12345, 0x00001, 0x20000, 0x3FFFF, 0x15555)
    h = AluHarness()
    seen = []

    async def tb(ctx):
        for a in starts:
            value = a
            chain = [value]
            for _ in range(WIDTH):
                value = await _eval(ctx, h.dut, OP_ROR, value, 1)
                chain.append(value)
            seen.append((a, chain))

    _run(h, tb)
    for a, chain in seen:
        assert chain[-1] == a, f"rotate of {a:#07x} after 18 steps: {chain[-1]:#07x}"
    by_start = dict(seen)
    # An aperiodic bit pattern must visit 18 distinct values before repeating.
    assert len(set(by_start[0x12345][:WIDTH])) == WIDTH
    assert by_start[0x12345][1] == 0x291A2
    # Degenerate operands keep their period, which is still a correct rotate.
    assert set(by_start[0x3FFFF]) == {0x3FFFF}
    assert set(by_start[0x15555]) == {0x15555, 0x2AAAA}


def test_shift_amount_is_low_four_bits_of_b():
    """Only ``i_b[3:0]`` matters: the upper 14 bits must not change a shift."""
    a = 0x12345
    h = AluHarness()
    got = []

    async def tb(ctx):
        for op in SHIFT_OPS:
            for amt in SHIFT_AMOUNTS:
                plain = await _eval(ctx, h.dut, op, a, amt)
                noisy = await _eval(ctx, h.dut, op, a, amt | 0x3FFF0)
                got.append((op, amt, plain, noisy))

    _run(h, tb)
    bad = [row for row in got if row[2] != row[3]]
    assert not bad, f"upper i_b bits leaked into the amount: {bad[:3]}"


def test_add_sub_boundary_operand_matrix():
    """Every ordered pair of boundary operands, ADD and SUB, vs masking maths."""
    h = AluHarness()
    got = []

    async def tb(ctx):
        for op in (OP_ADD, OP_SUB):
            for a in BOUNDARIES:
                for b in BOUNDARIES:
                    got.append((op, a, b, await _eval(ctx, h.dut, op, a, b)))

    _run(h, tb)
    assert len(got) == 2 * len(BOUNDARIES) ** 2
    bad = []
    for op, a, b, y in got:
        exp = (a + b) & MASK if op == OP_ADD else (a - b) & MASK
        if y != exp:
            bad.append((op, a, b, exp, y))
    assert not bad, f"{len(bad)} mismatches, first: {bad[0]}"


def test_shift_matrix_over_boundary_operands():
    """All 4 shift kinds x amounts 0/1/4/15 x boundary operands vs reference."""
    h = AluHarness()
    got = []

    async def tb(ctx):
        for op in SHIFT_OPS:
            for a in BOUNDARIES:
                for amt in SHIFT_AMOUNTS:
                    got.append((op, a, amt, await _eval(ctx, h.dut, op, a, amt)))

    _run(h, tb)
    assert len(got) == len(SHIFT_OPS) * len(BOUNDARIES) * len(SHIFT_AMOUNTS)
    bad = [(op, a, amt, ref_alu(op, a, amt), y)
           for op, a, amt, y in got if y != ref_alu(op, a, amt)]
    assert not bad, f"{len(bad)} mismatches, first: {bad[0]}"


def test_shift_by_zero_is_identity_for_every_operand():
    h = AluHarness()
    got = []

    async def tb(ctx):
        for op in SHIFT_OPS:
            for a in BOUNDARIES + (0x12345, 0x2AAAA, 0x15555):
                got.append((op, a, await _eval(ctx, h.dut, op, a, 0)))

    _run(h, tb)
    bad = [(op, a, y) for op, a, y in got if y != a]
    assert not bad, f"shift by 0 changed the operand: {bad[:3]}"


# --- random vectors ---------------------------------------------------------

def test_random_vectors_against_python_reference():
    """>= 500 seeded (op, a, b) triples covering all 8 ops vs ``ref_alu``."""
    rng = random.Random(0x51C18)
    vectors = [(op, rng.randrange(1 << WIDTH), rng.randrange(1 << WIDTH))
               for op in range(8) for _ in range(100)]
    assert len(vectors) >= 500
    assert len({op for op, _, _ in vectors}) == 8

    h = AluHarness()
    got = []

    async def tb(ctx):
        for op, a, b in vectors:
            got.append((op, a, b, await _eval(ctx, h.dut, op, a, b)))

    _run(h, tb)
    bad = [(op, a, b, ref_alu(op, a, b), y)
           for op, a, b, y in got if y != ref_alu(op, a, b)]
    assert not bad, f"{len(bad)} mismatches, first: {bad[0]}"


def test_random_small_shift_amounts():
    """Random operands with amounts restricted to 0..15, all shift kinds."""
    rng = random.Random(0x5A17)
    vectors = [(op, rng.randrange(1 << WIDTH), rng.randrange(16))
               for op in SHIFT_OPS for _ in range(60)]
    h = AluHarness()
    got = []

    async def tb(ctx):
        for op, a, amt in vectors:
            got.append((op, a, amt, await _eval(ctx, h.dut, op, a, amt)))

    _run(h, tb)
    bad = [(op, a, amt, ref_alu(op, a, amt), y)
           for op, a, amt, y in got if y != ref_alu(op, a, amt)]
    assert not bad, f"{len(bad)} mismatches, first: {bad[0]}"


# --- structural -------------------------------------------------------------

def test_output_is_eighteen_bits_and_in_range():
    h = AluHarness()
    alu = h.dut
    assert len(alu.i_a) == 18
    assert len(alu.i_b) == 18
    assert len(alu.i_op) == 3
    assert len(alu.o_y) == 18

    rng = random.Random(0x18)
    seen = []

    async def tb(ctx):
        for _ in range(200):
            op = rng.randrange(8)
            a = rng.randrange(1 << WIDTH)
            b = rng.randrange(1 << WIDTH)
            seen.append((op, a, b, await _eval(ctx, h.dut, op, a, b)))
        # Extremes for each op must land in the 18-bit window too.
        for op in range(8):
            for a, b in ((MASK, MASK), (0, MASK), (MASK, 0)):
                seen.append((op, a, b, await _eval(ctx, h.dut, op, a, b)))

    _run(h, tb)
    for op, a, b, y in seen:
        assert 0 <= y <= MASK, f"o_y out of 18-bit range: op={op} a={a:#x} b={b:#x} y={y:#x}"
    # Every op produced at least one value with bit 17 clear and one set is not
    # guaranteed, but ADD/SUB/OR at the extremes must reach the top bit.
    assert any(y >> 17 for op, _, _, y in seen if op in (OP_ADD, OP_SUB, OP_OR))


# --- synthesis top ----------------------------------------------------------

def test_alu_top_registers_inputs_and_outputs():
    h = AluTopHarness()
    top = h.top
    observed = []

    async def tb(ctx):
        await ctx.tick("tck")
        ctx.set(top.i_a, 0x12345)
        ctx.set(top.i_b, 4)
        ctx.set(top.i_op, OP_SLL)
        await ctx.tick("tck")        # samples the inputs
        await ctx.tick("tck")        # ALU result captured into o_y
        await ctx.delay(SETTLE)
        observed.append(ctx.get(top.o_y))
        # Change the inputs; the visible output must still be the old result
        # for one more cycle (registered out), then follow.
        ctx.set(top.i_a, 0x20000)
        ctx.set(top.i_b, 1)
        ctx.set(top.i_op, OP_SRA)
        await ctx.delay(SETTLE)
        observed.append(ctx.get(top.o_y))
        await ctx.tick("tck")
        await ctx.tick("tck")
        await ctx.delay(SETTLE)
        observed.append(ctx.get(top.o_y))

    _run(h, tb)
    assert observed[0] == 0x23450, observed
    assert observed[1] == 0x23450, observed
    assert observed[2] == 0x30000, observed
