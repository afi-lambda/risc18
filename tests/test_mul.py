"""Amaranth simulation tests for the M8 explicit DSP multiplier.

Every test drives :class:`risc18.mul.Multiplier` through
``amaranth.sim.Simulator``; the expected values come from an independent
Python oracle written here (``ref_mul``), never from the RTL.  Contract under
test (``docs/ISA.md`` sections 2 and 6):

* ``o_y`` is the low 18 bits of the 18x18 product, so it wraps modulo 2^18 and
  is the same for signed and unsigned operands (only the low bits are
  visible);
* ``registered=False``: the product is combinational -- valid in the same
  cycle as the operands, so a MUL retires at CPI 1.0;
* ``registered=True``: the product comes from the DSP's own output register,
  i.e. exactly one clock edge of latency, and it holds that value until the
  next edge (a consumer needs a bubble).

The last tests pin the *netlist intent* without running yosys: the
``cell=True`` elaboration used by :class:`risc18.tops.MulTop` /
:class:`risc18.tops.MulRegTop` must emit exactly one ``MULT18X18D`` primitive,
with ``REG_OUTPUT_CLK`` selecting the internal product register only in the
registered variant, and no multiplicative operator anywhere else in the
design.
"""

import random
import re

from amaranth import ClockDomain, Elaboratable, Module
from amaranth.back import verilog
from amaranth.sim import Simulator

from risc18 import isa
from risc18.core import Core
from risc18.mul import PRODUCT_WIDTH, Multiplier
from risc18.tops import MulRegTop, MulTop

MASK = isa.MASK
WIDTH = isa.WIDTH
SETTLE = 1e-9
CLK_PERIOD = 1e-6

#: 0, 1, 2, -1, max positive, min negative, all-ones: the ISA's operand edges
#: read both ways (0x3FFFF is -1 and max unsigned, 0x20000 is -2^17).
BOUNDARIES = (0x00000, 0x00001, 0x00002, 0x3FFFF, 0x1FFFF, 0x20000)


# --- oracle -----------------------------------------------------------------

def ref_mul(a, b):
    """Independent model: low 18 bits of the 18x18 product."""
    return (a * b) & MASK


def to_signed(value, bits=WIDTH):
    """Two's-complement reading of an 18-bit word."""
    value &= MASK
    if value & (1 << (bits - 1)):
        value -= 1 << bits
    return value


def ref_signed_mul(a, b):
    """Product through the signed reading of both operands, re-encoded."""
    return (to_signed(a) * to_signed(b)) & MASK


# --- harness ----------------------------------------------------------------

class MulHarness(Elaboratable):
    """The unit plus a ``sync`` domain: comb needs no clock, registered does."""

    def __init__(self, registered=False):
        self.dut = Multiplier(registered=registered)

    def elaborate(self, platform):
        m = Module()
        m.domains.sync = ClockDomain("sync")
        m.submodules.dut = self.dut
        return m


async def _eval(ctx, dut, a, b):
    """Sample the combinational product for ``(a, b)``."""
    ctx.set(dut.i_a, a & MASK)
    ctx.set(dut.i_b, b & MASK)
    await ctx.delay(SETTLE)
    return ctx.get(dut.o_y)


async def _edge(ctx, dut, a, b):
    """Present ``(a, b)`` and let exactly one clock edge pass."""
    ctx.set(dut.i_a, a & MASK)
    ctx.set(dut.i_b, b & MASK)
    await ctx.tick()
    await ctx.delay(SETTLE)
    return ctx.get(dut.o_y)


def _run(h, tb, domain="sync"):
    sim = Simulator(h)
    sim.add_clock(CLK_PERIOD, domain=domain)
    sim.add_testbench(tb, background=False)
    sim.run()


# --- combinational: hand-computed edges -------------------------------------

def test_zero_one_and_identity_operands():
    """0 annihilates, 1 is the identity, in both operand positions."""
    cases = [
        (0x00000, 0x00000, 0x00000),
        (0x00000, 0x3FFFF, 0x00000),
        (0x3FFFF, 0x00000, 0x00000),
        (0x00001, 0x00000, 0x00000),
        (0x00001, 0x12345, 0x12345),
        (0x12345, 0x00001, 0x12345),
        (0x00001, 0x3FFFF, 0x3FFFF),
        (0x3FFFF, 0x00001, 0x3FFFF),
    ]
    h = MulHarness()
    got = []

    async def tb(ctx):
        for a, b, exp in cases:
            got.append((a, b, exp, await _eval(ctx, h.dut, a, b)))

    _run(h, tb)
    bad = [row for row in got if row[3] != row[2]]
    assert not bad, f"{len(bad)} mismatches, first: {bad[0]}"


def test_minus_one_operand():
    """0x3FFFF is -1: the product is the negation, masked to 18 bits."""
    cases = [
        (0x3FFFF, 0x00002, 0x3FFFE),        # -1 * 2
        (0x00002, 0x3FFFF, 0x3FFFE),        # operand order
        (0x3FFFF, 0x00003, 0x3FFFD),        # -1 * 3
        (0x3FFFF, 0x20000, 0x20000),        # -1 * -2^17 == 2^17
        (0x3FFFF, 0x3FFFF, 0x00001),        # (-1) * (-1) == 1
        (0x3FFFF, 0x00000, 0x00000),
    ]
    h = MulHarness()
    got = []

    async def tb(ctx):
        for a, b, exp in cases:
            y = await _eval(ctx, h.dut, a, b)
            got.append((a, b, exp, y, ref_mul(a, b)))

    _run(h, tb)
    for a, b, exp, y, ref in got:
        assert y == exp, f"{a:#07x} * {b:#07x}: {y:#07x} != {exp:#07x}"
        assert y == ref, f"{a:#07x} * {b:#07x}: oracle {ref:#07x} != RTL {y:#07x}"


def test_max_positive_and_min_negative():
    """The signed extremes of the 18-bit word, squared and crossed."""
    hi = 0x1FFFF        # max signed positive (131071)
    lo = 0x20000        # min signed negative (-2^17)
    cases = [
        (hi, hi, (131071 * 131071) & MASK),
        (hi, lo, (131071 * -131072) & MASK),
        (lo, hi, (-131072 * 131071) & MASK),
        (lo, lo, ((-131072) * (-131072)) & MASK),
        (lo, 0x00003, 0x20000),             # -2^17 * 3 -> 0x60000 -> bit 17
        (hi, 0x00002, 0x3FFFE),             # max * 2
    ]
    h = MulHarness()
    got = []

    async def tb(ctx):
        for a, b, exp in cases:
            got.append((a, b, exp, await _eval(ctx, h.dut, a, b)))

    _run(h, tb)
    bad = [row for row in got if row[3] != row[2]]
    assert not bad, f"{len(bad)} mismatches, first: {bad[0]}"


def test_max_word_squared_and_truncation():
    """0x3FFFF^2 = 0xFFFFFFF... needs 34 bits; only the low 18 survive."""
    a = b = 0x3FFFF
    full = a * b
    assert full >> WIDTH, "precondition: the product overflows 18 bits"
    h = MulHarness()
    got = {}

    async def tb(ctx):
        got["y"] = await _eval(ctx, h.dut, a, b)

    _run(h, tb)
    assert got["y"] == full & MASK
    assert got["y"] != full


# --- combinational: table-driven -------------------------------------------

def test_every_boundary_pair_matches_oracle():
    """All 36 boundary pairs, RTL vs the Python oracle."""
    h = MulHarness()
    got = []

    async def tb(ctx):
        for a in BOUNDARIES:
            for b in BOUNDARIES:
                got.append((a, b, await _eval(ctx, h.dut, a, b)))

    _run(h, tb)
    bad = [(a, b, y, ref_mul(a, b)) for a, b, y in got if y != ref_mul(a, b)]
    assert not bad, f"{len(bad)} mismatches, first: {bad[0]}"
    assert len(got) == len(BOUNDARIES) ** 2


def test_random_pairs_match_oracle():
    """64 seeded random pairs, including the wide/high-bit region."""
    rng = random.Random(0x38A8)
    pairs = [(rng.randrange(1 << WIDTH), rng.randrange(1 << WIDTH))
             for _ in range(48)]
    pairs += [(rng.randrange(1 << 17, 1 << WIDTH),
               rng.randrange(1 << 17, 1 << WIDTH)) for _ in range(16)]
    h = MulHarness()
    got = []

    async def tb(ctx):
        for a, b in pairs:
            got.append((a, b, await _eval(ctx, h.dut, a, b)))

    _run(h, tb)
    bad = [(a, b, y, ref_mul(a, b)) for a, b, y in got if y != ref_mul(a, b)]
    assert not bad, f"{len(bad)} of {len(got)} mismatches, first: {bad[0]}"


def test_signed_and_unsigned_oracles_agree_with_rtl():
    """The low 18 bits do not care how the operands are read.

    ``ref_signed_mul`` and ``ref_mul`` are computed differently in Python, so a
    unit that (say) zero-extended only one operand would fail here.
    """
    rng = random.Random(0x8A8)
    pairs = [(rng.randrange(1 << WIDTH), rng.randrange(1 << WIDTH))
             for _ in range(24)]
    pairs += [(0x3FFFF, 0x3FFFF), (0x20000, 0x20000), (0x3FFFF, 0x20000),
              (0x1FFFF, 0x20000), (0x20001, 0x3FFFF)]
    h = MulHarness()
    got = []

    async def tb(ctx):
        for a, b in pairs:
            got.append((a, b, await _eval(ctx, h.dut, a, b)))

    _run(h, tb)
    for a, b, y in got:
        assert y == ref_mul(a, b) == ref_signed_mul(a, b), (a, b, y)


def test_operand_order_is_symmetric():
    """``y(a, b) == y(b, a)`` and both match the oracle."""
    rng = random.Random(0x0B0B)
    pairs = [(rng.randrange(1 << WIDTH), rng.randrange(1 << WIDTH))
             for _ in range(16)]
    pairs += [(0x3FFFF, 0x00003), (0x20000, 0x00007), (0x1FFFF, 0x12345)]
    h = MulHarness()
    got = []

    async def tb(ctx):
        for a, b in pairs:
            y_ab = await _eval(ctx, h.dut, a, b)
            y_ba = await _eval(ctx, h.dut, b, a)
            got.append((a, b, y_ab, y_ba))

    _run(h, tb)
    for a, b, y_ab, y_ba in got:
        assert y_ab == y_ba == ref_mul(a, b), (a, b, y_ab, y_ba)


def test_low_bits_with_bit_seventeen_set():
    """Products whose low 18 bits have bit 17 set are not sign-extended."""
    hand = [
        (0x20000, 0x00003, 0x20000),        # 0x60000 -> 0x20000
        (0x00003, 0x20000, 0x20000),
        (0x3FFFF, 0x00002, 0x3FFFE),        # 0x7FFFE -> 0x3FFFE
        (0x1FFFF, 0x00006, 0x3FFFA),        # 0xBFFFA -> 0x3FFFA
        (0x30000, 0x00002, 0x20000),        # 0x60000 -> 0x20000
    ]
    rng = random.Random(0xB17)
    searched = []
    while len(searched) < 6:
        a, b = rng.randrange(1 << WIDTH), rng.randrange(1 << WIDTH)
        if ref_mul(a, b) & (1 << 17):
            searched.append((a, b, ref_mul(a, b)))

    h = MulHarness()
    got = []

    async def tb(ctx):
        for a, b, _ in hand + searched:
            got.append((a, b, await _eval(ctx, h.dut, a, b)))

    _run(h, tb)
    for a, b, y in got:
        assert y & (1 << 17), f"{a:#07x} * {b:#07x} -> {y:#07x} lost bit 17"
        assert y == ref_mul(a, b), (a, b, y)
        assert y < (1 << WIDTH)


# --- registered: one edge of latency, value held ----------------------------

def test_registered_has_exactly_one_edge_of_latency():
    """The product of the operands sampled on edge N appears after edge N.

    One edge after the operands are presented the value is there; presenting
    new operands without an edge must not change it (that is the second half
    of the claim: the register, not a wire).
    """
    a, b = 0x2ABCD, 0x01234
    other = (0x01234, 0x0FEDC)
    seen = {}

    h = MulHarness(registered=True)

    async def tb(ctx):
        # Nothing sampled yet: the register is at its reset value.
        ctx.set(h.dut.i_a, a)
        ctx.set(h.dut.i_b, b)
        await ctx.delay(SETTLE)
        seen["before_first_edge"] = ctx.get(h.dut.o_y)

        await ctx.tick()
        await ctx.delay(SETTLE)
        seen["after_one_edge"] = ctx.get(h.dut.o_y)

        # New operands, no edge: still the previous product.
        ctx.set(h.dut.i_a, other[0])
        ctx.set(h.dut.i_b, other[1])
        await ctx.delay(SETTLE)
        seen["held"] = ctx.get(h.dut.o_y)
        await ctx.delay(4 * CLK_PERIOD / 16)
        seen["held_again"] = ctx.get(h.dut.o_y)

        await ctx.tick()
        await ctx.delay(SETTLE)
        seen["after_second_edge"] = ctx.get(h.dut.o_y)

    _run(h, tb)
    assert seen["before_first_edge"] == 0, seen        # reset value, not a product
    assert seen["after_one_edge"] == ref_mul(a, b)
    assert seen["held"] == ref_mul(a, b) == seen["held_again"]
    assert seen["after_second_edge"] == ref_mul(*other)


def test_registered_streams_one_product_per_edge():
    """A new pair every edge yields the delayed stream, one per edge."""
    rng = random.Random(0x5EED)
    pairs = [(rng.randrange(1 << WIDTH), rng.randrange(1 << WIDTH))
             for _ in range(12)]
    got = []
    h = MulHarness(registered=True)

    async def tb(ctx):
        for a, b in pairs:
            got.append(await _edge(ctx, h.dut, a, b))

    _run(h, tb)
    assert got == [ref_mul(a, b) for a, b in pairs]


def test_registered_matches_combinational_on_boundaries():
    """Same operands, both modes, same low 18 bits (registered lags by an edge)."""
    comb = MulHarness(registered=False)
    pipelined = MulHarness(registered=True)
    got = {"comb": [], "reg": []}

    async def tb_comb(ctx):
        for a in BOUNDARIES:
            for b in BOUNDARIES:
                got["comb"].append(await _eval(ctx, comb.dut, a, b))

    _run(comb, tb_comb, domain="sync")

    async def tb_reg(ctx):
        for a in BOUNDARIES:
            for b in BOUNDARIES:
                got["reg"].append(await _edge(ctx, pipelined.dut, a, b))

    _run(pipelined, tb_reg, domain="sync")
    assert got["comb"] == got["reg"] == [
        ref_mul(a, b) for a in BOUNDARIES for b in BOUNDARIES]


# --- shape and netlist intent ----------------------------------------------

def test_unit_is_eighteen_bits_wide():
    """Ports are 18 bits, the product path is 36, and ``o_y`` masks."""
    h = MulHarness()
    dut = h.dut
    assert len(dut.i_a) == WIDTH == 18
    assert len(dut.i_b) == WIDTH == 18
    assert len(dut.o_y) == WIDTH == 18
    assert PRODUCT_WIDTH == 2 * WIDTH == 36

    got = {}

    async def tb(ctx):
        for a, b in ((0x3FFFF, 0x3FFFF), (0x2AAAA, 0x15555)):
            got[(a, b)] = await _eval(ctx, h.dut, a, b)

    _run(h, tb)
    for (a, b), y in got.items():
        assert 0 <= y <= MASK, (a, b, y)
        assert y == (a * b) & MASK
        assert y & ~MASK == 0


def _verilog(top):
    return verilog.convert(top, name="top", ports=top.ports)


def test_cell_elaborations_instantiate_exactly_one_dsp():
    """Both measurement tops emit one MULT18X18D and no LUT multiplier."""
    for cls in (MulTop, MulRegTop):
        text = _verilog(cls())
        assert text.count("MULT18X18D") == 1, f"{cls.__name__}: not one DSP"
        assert " * " not in text, f"{cls.__name__}: multiplicative operator left"
        assert "$mul" not in text, f"{cls.__name__}: soft multiplier left"


def test_cell_register_parameters_pick_the_internal_pipeline():
    """``REG_OUTPUT_CLK`` is the only register parameter that differs."""
    comb = _verilog(MulTop())
    reg = _verilog(MulRegTop())
    for text in (comb, reg):
        assert '.REG_INPUTA_CLK("NONE")' in text
        assert '.REG_INPUTB_CLK("NONE")' in text
        assert '.REG_PIPELINE_CLK("NONE")' in text
        assert ".SIGNEDA(1'h0)" in text and ".SIGNEDB(1'h0)" in text
        # No accumulate: the C port is tied to zero, so P is exactly A * B.
        assert ".C0(1'h0)" in text and ".C17(1'h0)" in text
        # The DSP is fed from the input register, not straight from the port,
        # which is what makes nextpnr report an interior register-to-register
        # path instead of an I/O path.
        assert re.search(r"\.i_a\(\\i_a\$\d+ ?\)", text), text[:200]
        assert re.search(r"\.i_b\(\\i_b\$\d+ ?\)", text), text[:200]
    assert '.REG_OUTPUT_CLK("NONE")' in comb
    assert '.REG_OUTPUT_CLK("CLK0")' in reg
    # Clock/enable/reset are wired in both variants, so no register input is
    # ever driven by an undriven net if a REG_*_CLK parameter is set later.
    assert ".CLK0(" in comb and ".CLK0(clk)" in reg
    assert ".CE0(1'h1)" in comb and ".CE0(1'h1)" in reg
    assert ".RST0(1'h0)" in comb and ".RST0(1'h0)" in reg


# --- the core actually uses the unit ---------------------------------------

def test_core_mul_result_matches_the_oracle():
    """A MUL retired by the core equals the oracle for the same operands."""
    program = [
        isa.u_type(isa.OP_LUI, 1, 0x3FF),             # r1 = 0x3FF00
        isa.i_type(isa.OP_ADDI, 2, 0, 0x1F),          # r2 = 31
        isa.r_type(isa.OP_MUL, 0, 3, 1, 2),
        isa.i_type(isa.OP_ADDI, 4, 0, 0x1F),          # r4 = 31
        isa.r_type(isa.OP_MUL, 0, 5, 4, 1),           # other operand order
        isa.i_type(isa.OP_SYSTEM, 0, 0, isa.SYS_HALT),
    ]
    core = Core(program)
    sim = Simulator(core)
    sim.add_clock(CLK_PERIOD, domain="sync")
    writes = []

    async def tb(ctx):
        ctx.set(core.i_rst, 1)
        await ctx.tick()
        ctx.set(core.i_rst, 0)
        for _ in range(64):
            await ctx.tick()
            await ctx.delay(SETTLE)
            if ctx.get(core.o_retire) and ctx.get(core.o_retire_we):
                writes.append((ctx.get(core.o_retire_rd),
                               ctx.get(core.o_retire_value)))
            if ctx.get(core.o_halt):
                break

    sim.add_testbench(tb)
    sim.run()
    got = dict(writes)
    assert got[3] == ref_mul(0x3FF00, 0x1F)
    assert got[5] == ref_mul(0x1F, 0x3FF00)
    assert got[3] == got[5]


def test_cell_port_map_is_the_architectural_product():
    """Every A/B/C/P pin is wired to the right bit or to zero.

    The string-based checks are the only way to test the primitive (the
    simulator drives vendor Instance outputs to 0), so they cover the whole
    port map rather than a sample: a bit-reversed A bus or a C accumulator bit
    set to 1 must fail here.
    """
    for text in (_verilog(MulTop()), _verilog(MulRegTop())):
        for i in range(isa.WIDTH):
            assert f".A{i}(i_a[{i}])" in text, f"A{i} not wired to i_a[{i}]"
            assert f".B{i}(i_b[{i}])" in text, f"B{i} not wired to i_b[{i}]"
            assert f".C{i}(1'h0)" in text, f"C{i} not zero"
        for i in range(36):
            assert f".P{i}(" in text, f"P{i} not wired"
        assert ".SIGNEDA(1'h0)" in text and ".SIGNEDB(1'h0)" in text
        assert ".SOURCEA(1'h0)" in text and ".SOURCEB(1'h0)" in text
