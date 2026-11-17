"""Amaranth simulation tests for the M3 PC register and branch targets.

Everything here drives the RTL through ``amaranth.sim.Simulator``; no signal is
read from inside the elaboratables.  The PC harness declares the ``sync`` domain
itself and ties ``cd.rst`` to a port (the same pattern as
``tests/test_regfile.py``), so the synchronous reset is exercised for real.

Contract under test (``docs/ISA.md`` sections 3 and 5, ``docs/MILESTONES.md``
M3):

* ``PCUnit``: synchronous 16-bit PC, ``redirect`` beats ``hold`` beats
  increment, wraps modulo 2^16, reset clears it to 0;
* ``BranchTarget``: branches and JAL use ``pc + 1 + signed(imm)``; JALR uses
  ``rs1 + signed(imm)`` with no ``+ 1``; targets wrap at 16 bits;
* both measurement tops register their inputs and outputs.
"""

import random

from amaranth import ClockDomain, Elaboratable, Module, Signal
from amaranth.sim import Simulator

from risc18 import isa
from risc18.pc import PCUnit, BranchTarget
from risc18.tops import BranchTargetTop, PcTop

IMM_WIDTH = isa.WIDTH
PC_WIDTH = 16
PC_MASK = (1 << PC_WIDTH) - 1
IMM_MASK = (1 << IMM_WIDTH) - 1
IMM_SIGN = 1 << (IMM_WIDTH - 1)
SETTLE = 1e-9
CLK_PERIOD = 1e-6


# --- independent Python models ---------------------------------------------

def sext18(value):
    """Signed value of an 18-bit two's-complement word."""
    value &= IMM_MASK
    return value - (1 << IMM_WIDTH) if value & IMM_SIGN else value


def model_target(pc, imm, rs1, jump_reg):
    """Reference target generation, straight from docs/ISA.md section 3."""
    offset = sext18(imm)
    if jump_reg:
        return (rs1 + offset) & PC_MASK
    return (pc + 1 + offset) & PC_MASK


def model_pc_step(pc, redirect, target, hold):
    """Reference PC update: redirect, then hold, then sequential increment."""
    if redirect:
        return target & PC_MASK
    if hold:
        return pc
    return (pc + 1) & PC_MASK


# --- harnesses --------------------------------------------------------------

class PcHarness(Elaboratable):
    """``PCUnit`` with the ``sync`` domain reset exposed as an input port."""

    def __init__(self):
        self.rst = Signal()
        self.dut = PCUnit()

    def elaborate(self, platform):
        m = Module()
        cd = ClockDomain("sync")
        m.domains.sync = cd
        m.d.comb += cd.rst.eq(self.rst)
        m.submodules.dut = self.dut
        return m


class BranchTargetHarness(Elaboratable):
    """``BranchTarget`` on a clocked domain so the simulator has an edge."""

    def __init__(self):
        self.dut = BranchTarget()

    def elaborate(self, platform):
        m = Module()
        m.domains.sync = ClockDomain("sync")
        m.submodules.dut = self.dut
        return m


class PcAndTargetHarness(Elaboratable):
    """Both units in one design, target PC wired to the PC register output.

    The random lockstep test drives this: ``BranchTarget.i_pc`` is the real PC,
    so target generation is always checked against the PC the hardware holds.
    """

    def __init__(self):
        self.rst = Signal()
        self.pc = PCUnit()
        self.target = BranchTarget()

    def elaborate(self, platform):
        m = Module()
        cd = ClockDomain("sync")
        m.domains.sync = cd
        m.d.comb += cd.rst.eq(self.rst)
        m.submodules.pc_unit = self.pc
        m.submodules.branch_target = self.target
        m.d.comb += self.target.i_pc.eq(self.pc.o_pc)
        return m


class TopHarness(Elaboratable):
    """Drive a top's ``clk`` port from a private ``tck`` clock domain.

    The tops own a reset-less ``sync`` domain internally, so the testbench
    clocks them through their ``clk`` port instead of adding a clock process on
    their own domain.
    """

    def __init__(self, top):
        self.top = top

    def elaborate(self, platform):
        m = Module()
        cd = ClockDomain("tck")
        m.domains.tck = cd
        m.d.comb += self.top.clk.eq(cd.clk)
        m.submodules.top = self.top
        return m


# --- testbench helpers ------------------------------------------------------

async def _reset(ctx, h):
    ctx.set(h.dut.i_redirect, 0)
    ctx.set(h.dut.i_hold, 0)
    ctx.set(h.rst, 1)
    await ctx.tick()
    # Release without another edge so the caller observes PC == 0 as the reset
    # result instead of an already-incremented PC.
    ctx.set(h.rst, 0)
    await ctx.delay(SETTLE)


def _run(h, tb, domain="sync"):
    sim = Simulator(h)
    sim.add_clock(CLK_PERIOD, domain=domain)
    sim.add_testbench(tb, background=False)
    sim.run()


async def _pc_step(ctx, h, redirect=0, target=0, hold=0):
    """Apply one control word, tick, and return the PC after the edge."""
    ctx.set(h.dut.i_redirect, redirect)
    ctx.set(h.dut.i_target, target)
    ctx.set(h.dut.i_hold, hold)
    await ctx.tick()
    await ctx.delay(SETTLE)
    return ctx.get(h.dut.o_pc)


async def _target(ctx, h, pc, imm, rs1=0, jump_reg=0):
    """Sample ``o_target`` combinationally for the given inputs."""
    ctx.set(h.dut.i_pc, pc)
    ctx.set(h.dut.i_imm, imm)
    ctx.set(h.dut.i_rs1, rs1)
    ctx.set(h.dut.i_jump_reg, jump_reg)
    await ctx.delay(SETTLE)
    return ctx.get(h.dut.o_target)


# --- PCUnit: sequential behaviour ------------------------------------------

def test_pc_reset_is_zero_and_increments_sequentially():
    h = PcHarness()
    seen = []

    async def tb(ctx):
        await _reset(ctx, h)
        seen.append(ctx.get(h.dut.o_pc))
        for _ in range(5):
            seen.append(await _pc_step(ctx, h))

    _run(h, tb)
    assert seen == [0, 1, 2, 3, 4, 5], seen


def test_pc_wraps_at_16_bits():
    h = PcHarness()
    seen = []

    async def tb(ctx):
        await _reset(ctx, h)
        seen.append(await _pc_step(ctx, h, redirect=1, target=PC_MASK))
        seen.append(await _pc_step(ctx, h))
        seen.append(await _pc_step(ctx, h))

    _run(h, tb)
    # 0xFFFF -> 0x0000 -> 0x0001
    assert seen == [PC_MASK, 0, 1], seen


def test_redirect_beats_increment_and_hold():
    h = PcHarness()
    seen = []

    async def tb(ctx):
        await _reset(ctx, h)
        seen.append(await _pc_step(ctx, h, redirect=1, target=0x0A5A))
        # redirect and hold asserted together: redirect must win.
        seen.append(await _pc_step(ctx, h, redirect=1, target=0x1234, hold=1))
        # redirect alone from a held PC.
        seen.append(await _pc_step(ctx, h, hold=1))
        seen.append(await _pc_step(ctx, h, redirect=1, target=0x0007))

    _run(h, tb)
    assert seen == [0x0A5A, 0x1234, 0x1234, 0x0007], seen


def test_hold_freezes_pc_for_multiple_cycles():
    h = PcHarness()
    held = []

    async def tb(ctx):
        await _reset(ctx, h)
        for _ in range(3):
            await _pc_step(ctx, h)
        assert ctx.get(h.dut.o_pc) == 3, ctx.get(h.dut.o_pc)
        for _ in range(6):
            held.append(await _pc_step(ctx, h, hold=1))
        held.append(await _pc_step(ctx, h))

    _run(h, tb)
    assert held[:6] == [3] * 6, held
    assert held[6] == 4, held


def test_reset_clears_pc_after_progress():
    h = PcHarness()
    seen = []

    async def tb(ctx):
        await _reset(ctx, h)
        for _ in range(7):
            await _pc_step(ctx, h)
        seen.append(ctx.get(h.dut.o_pc))
        await _reset(ctx, h)
        seen.append(ctx.get(h.dut.o_pc))
        # Reset must also override a pending redirect/hold.
        ctx.set(h.dut.i_redirect, 1)
        ctx.set(h.dut.i_target, 0x0BAD)
        ctx.set(h.dut.i_hold, 1)
        ctx.set(h.rst, 1)
        await ctx.tick()
        ctx.set(h.rst, 0)
        await ctx.delay(SETTLE)
        seen.append(ctx.get(h.dut.o_pc))

    _run(h, tb)
    assert seen == [7, 0, 0], seen


# --- BranchTarget: combinational targets ------------------------------------

def test_branch_positive_offset():
    h = BranchTargetHarness()
    seen = []

    async def tb(ctx):
        seen.append(await _target(ctx, h, pc=0x0100, imm=0x0006))
        seen.append(await _target(ctx, h, pc=0x0100, imm=0x0000))
        seen.append(await _target(ctx, h, pc=0x0010, imm=0x001F))

    _run(h, tb)
    # pc + 1 + imm, not pc + imm: the branch offset is relative to pc + 1.
    assert seen == [0x0107, 0x0101, 0x0030], [hex(v) for v in seen]


def test_branch_negative_offset():
    h = BranchTargetHarness()
    seen = []

    async def tb(ctx):
        seen.append(await _target(ctx, h, pc=0x0100, imm=0x3FFFA))  # -6
        seen.append(await _target(ctx, h, pc=0x0100, imm=0x3FFFF))  # -1
        seen.append(await _target(ctx, h, pc=0x0000, imm=0x3FFFE))  # -2

    _run(h, tb)
    assert seen == [0x00FB, 0x0100, 0xFFFF], [hex(v) for v in seen]


def test_jal_target_is_pc_plus_one_plus_imm():
    h = BranchTargetHarness()
    seen = []

    async def tb(ctx):
        seen.append(await _target(ctx, h, pc=0x0200, imm=0x0000))
        seen.append(await _target(ctx, h, pc=0x0200, imm=0x0009))
        seen.append(await _target(ctx, h, pc=0x0200, imm=0x3FFF8))  # -8
        # JAL ignores i_rs1 entirely.
        seen.append(await _target(ctx, h, pc=0x0200, imm=0x0009, rs1=0x3FFFF))

    _run(h, tb)
    assert seen == [0x0201, 0x020A, 0x01F9, 0x020A], [hex(v) for v in seen]


def test_jalr_target_is_rs1_plus_imm_without_plus_one():
    h = BranchTargetHarness()
    seen = []

    async def tb(ctx):
        seen.append(await _target(ctx, h, pc=0x0300, imm=0x0000,
                                  rs1=0x0010, jump_reg=1))
        seen.append(await _target(ctx, h, pc=0x0300, imm=0x0004,
                                  rs1=0x0010, jump_reg=1))
        # pc is irrelevant for JALR.
        seen.append(await _target(ctx, h, pc=0x3FFF, imm=0x0004,
                                  rs1=0x0010, jump_reg=1))
        seen.append(await _target(ctx, h, pc=0x0000, imm=0x3FFFC,
                                  rs1=0x0010, jump_reg=1))  # -4

    _run(h, tb)
    assert seen == [0x0010, 0x0014, 0x0014, 0x000C], [hex(v) for v in seen]


def test_signed_immediate_minus_two():
    h = BranchTargetHarness()
    seen = []

    async def tb(ctx):
        # imm = 0x3FFFE is -2 in 18-bit two's complement.
        assert sext18(0x3FFFE) == -2
        seen.append(await _target(ctx, h, pc=0x0010, imm=0x3FFFE))
        seen.append(await _target(ctx, h, pc=0x0010, imm=0x3FFFE,
                                  rs1=0x0010, jump_reg=1))
        # The most negative immediate: -2^17.
        seen.append(await _target(ctx, h, pc=0x0000, imm=0x20000))

    _run(h, tb)
    assert seen == [0x000F, 0x000E, (1 + (-(1 << 17))) & PC_MASK], \
        [hex(v) for v in seen]


def test_target_wraps_at_16_bits():
    h = BranchTargetHarness()
    seen = []

    async def tb(ctx):
        # pc + 1 + imm above 0xFFFF wraps down.
        seen.append(await _target(ctx, h, pc=0xFFFF, imm=0x0005))
        seen.append(await _target(ctx, h, pc=0xFFFE, imm=0x0003))
        # JALR: rs1 is an 18-bit architectural value, the target keeps 16 bits.
        seen.append(await _target(ctx, h, pc=0x0000, imm=0x0001,
                                  rs1=0x3FFFF, jump_reg=1))
        seen.append(await _target(ctx, h, pc=0x0000, imm=0x0000,
                                  rs1=0x10000, jump_reg=1))

    _run(h, tb)
    assert seen == [0x0005, 0x0002, 0x0000, 0x0000], [hex(v) for v in seen]


def test_branch_target_ignores_rs1_when_not_register_jump():
    h = BranchTargetHarness()
    pair = []

    async def tb(ctx):
        pair.append(await _target(ctx, h, pc=0x0040, imm=0x0002, rs1=0))
        pair.append(await _target(ctx, h, pc=0x0040, imm=0x0002, rs1=0x2AAAA))

    _run(h, tb)
    assert pair == [0x0043, 0x0043], [hex(v) for v in pair]


# --- lockstep random sequence ----------------------------------------------

def test_random_sequence_against_python_model():
    """>= 400 seeded steps driving both units against independent models."""
    rng = random.Random(0xB4A2C)
    n_steps = 400
    script = []
    model_pc = 0
    for _ in range(n_steps):
        imm = rng.randrange(1 << IMM_WIDTH)
        rs1 = rng.randrange(1 << IMM_WIDTH)
        jump_reg = rng.randrange(2)
        redirect = 1 if rng.random() < 0.25 else 0
        target = rng.randrange(1 << PC_WIDTH)
        hold = 1 if rng.random() < 0.25 else 0
        exp_target = model_target(model_pc, imm, rs1, jump_reg)
        script.append((imm, rs1, jump_reg, redirect, target, hold,
                       model_pc, exp_target))
        model_pc = model_pc_step(model_pc, redirect, target, hold)

    assert len(script) >= 300
    # The script must actually exercise every path, not just increments.
    assert sum(1 for s in script if s[3]) >= 60          # redirects
    assert sum(1 for s in script if s[5] and not s[3]) >= 20   # plain holds
    assert sum(1 for s in script if s[2]) >= 100         # JALR targets
    assert sum(1 for s in script if sext18(s[0]) < 0) >= 100   # negative imms

    h = PcAndTargetHarness()
    bad = []

    async def tb(ctx):
        ctx.set(h.rst, 1)
        await ctx.tick()
        # Release the reset without another edge: the first scripted step must
        # see PC == 0, not an incremented PC.
        ctx.set(h.rst, 0)
        await ctx.delay(SETTLE)
        for imm, rs1, jump_reg, redirect, target, hold, pc_before, exp in script:
            ctx.set(h.target.i_imm, imm)
            ctx.set(h.target.i_rs1, rs1)
            ctx.set(h.target.i_jump_reg, jump_reg)
            ctx.set(h.pc.i_redirect, redirect)
            ctx.set(h.pc.i_target, target)
            ctx.set(h.pc.i_hold, hold)
            await ctx.delay(SETTLE)
            got_pc = ctx.get(h.pc.o_pc)
            got_target = ctx.get(h.target.o_target)
            if got_pc != pc_before or got_target != exp:
                bad.append((pc_before, exp, got_pc, got_target,
                            imm, rs1, jump_reg))
            await ctx.tick()
            await ctx.delay(SETTLE)
            exp_pc = model_pc_step(pc_before, redirect, target, hold)
            if ctx.get(h.pc.o_pc) != exp_pc:
                bad.append(("post-tick", pc_before, redirect, target, hold,
                            ctx.get(h.pc.o_pc)))

    _run(h, tb)
    assert not bad, f"{len(bad)} mismatches, first: {bad[0]}"
    assert len(script) == n_steps


# --- measurement tops -------------------------------------------------------

def test_branch_target_top_registers_inputs_and_outputs():
    top = BranchTargetTop()
    h = TopHarness(top)
    seen = []

    async def tb(ctx):
        ctx.set(top.i_jump_reg, 0)
        await ctx.tick("tck")
        await ctx.tick("tck")
        # Reset-less registers start at 0, so the first latched target is
        # pc + 1 + 0 = 1.
        seen.append(ctx.get(top.o_target))

        # Branch: pc=0x0100, imm=+5 -> 0x0106 after two cycles.
        ctx.set(top.i_pc, 0x0100)
        ctx.set(top.i_imm, 0x0005)
        ctx.set(top.i_rs1, 0x0000)
        ctx.set(top.i_jump_reg, 0)
        await ctx.tick("tck")          # samples inputs into *_q
        await ctx.delay(SETTLE)
        # Output register still holds the previous target: one stage only.
        seen.append(ctx.get(top.o_target))
        await ctx.tick("tck")          # combinational target latched
        await ctx.delay(SETTLE)
        seen.append(ctx.get(top.o_target))

        # JALR: rs1=0x0300, imm=0x3FFFE (-2) -> 0x02FE.
        ctx.set(top.i_rs1, 0x0300)
        ctx.set(top.i_imm, 0x3FFFE)
        ctx.set(top.i_jump_reg, 1)
        await ctx.tick("tck")
        await ctx.tick("tck")
        await ctx.delay(SETTLE)
        seen.append(ctx.get(top.o_target))

    _run(h, tb, domain="tck")
    assert seen == [0x0001, 0x0001, 0x0106, 0x02FE], [hex(v) for v in seen]


def test_pc_top_registers_inputs_and_outputs():
    top = PcTop()
    h = TopHarness(top)
    seen = []

    async def tb(ctx):
        ctx.set(top.i_redirect, 0)
        ctx.set(top.i_hold, 0)
        await ctx.tick("tck")          # pc <= 1, o_pc <= 0
        await ctx.tick("tck")          # pc <= 2, o_pc <= 1
        seen.append(ctx.get(top.o_pc))

        ctx.set(top.i_hold, 1)
        for _ in range(3):
            await ctx.tick("tck")
        await ctx.delay(SETTLE)
        seen.append(ctx.get(top.o_pc))
        await ctx.tick("tck")
        await ctx.delay(SETTLE)
        seen.append(ctx.get(top.o_pc))

        ctx.set(top.i_hold, 0)
        ctx.set(top.i_redirect, 1)
        ctx.set(top.i_target, 0x2ABC)
        await ctx.tick("tck")          # input registers capture redirect
        await ctx.delay(SETTLE)
        seen.append(ctx.get(top.o_pc))     # unchanged: no visible effect yet
        await ctx.tick("tck")          # PC register loads target
        await ctx.delay(SETTLE)
        seen.append(ctx.get(top.o_pc))     # still the pre-redirect PC
        await ctx.tick("tck")          # output register follows
        await ctx.delay(SETTLE)
        seen.append(ctx.get(top.o_pc))

    _run(h, tb, domain="tck")
    # Three-stage port latency: input register -> PCUnit -> output register.
    assert seen == [1, 3, 3, 3, 3, 0x2ABC], [hex(v) for v in seen]


def test_branch_target_consumes_decoder_immediates():
    """Seam test: the decoder's imm/jump_reg fields drive BranchTarget."""
    cases = [
        (0x000A, isa.b_type(isa.OP_BEQ, 2, 1, -3), 0x0000),
        (0x0064, isa.j_type(isa.OP_JAL, 9, -2), 0x0000),
        (0x0100, isa.i_type(isa.OP_JALR, 1, 3, -1), 0x0300),
    ]
    h = BranchTargetHarness()
    seen = []

    async def tb(ctx):
        for pc, word, rs1_value in cases:
            d = isa.decode(word)
            got = await _target(ctx, h, pc, d.imm, rs1=rs1_value,
                                jump_reg=d.jump_reg)
            seen.append((word, got, model_target(pc, d.imm, rs1_value,
                                                 d.jump_reg)))

    _run(h, tb)
    for word, got, exp in seen:
        assert got == exp, f"0x{word:05X}: {got:#06x} != {exp:#06x}"
    assert [got for _, got, _ in seen] == [8, 99, 0x2FF]
