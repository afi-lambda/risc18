"""Amaranth simulation tests for the M4 instruction EBR and fetch stage.

Everything here drives the RTL through ``amaranth.sim.Simulator``; no signal is
read from inside the elaboratables.  The harnesses declare the ``sync`` domain
themselves and tie ``cd.rst`` to a port (the pattern of ``tests/test_regfile.py``
and ``tests/test_pc.py``), so the synchronous reset the PC uses is exercised for
real.

Contract under test (``docs/ISA.md`` sections 1 and 5, ``docs/MILESTONES.md``
M4):

* ``InstrMemory``: 1024 x 18, preloaded through ``init`` (padded/truncated to
  1024 words), synchronous read with exactly one cycle of latency, ``i_en``
  low freezes the output register, addresses ``>= 1024`` read 0, no write
  port;
* ``Fetch``: ``PCUnit`` + ``InstrMemory``, ``o_pc`` is the fetch address,
  ``o_instr`` the EBR output tagged by ``o_instr_pc``; the instruction for a PC
  value arrives on the second clock edge after that PC became current
  (redirect included); ``i_hold`` freezes PC, instruction and tag;
* both measurement tops register their inputs and outputs.
"""

import random

from amaranth import ClockDomain, Elaboratable, Module, Signal
from amaranth.hdl import Fragment
from amaranth.sim import Simulator

from risc18 import isa
from risc18.imem import DEPTH, Fetch, InstrMemory
from risc18.tops import FetchTop, InstrMemoryTop

PC_WIDTH = 16
PC_MASK = (1 << PC_WIDTH) - 1
DEPTH_MASK = DEPTH - 1
SETTLE = 1e-9
CLK_PERIOD = 1e-6


# --- independent Python models ---------------------------------------------

def model_rom(rom, addr):
    """Reference read: in-range word, otherwise 0 (docs/ISA.md section 1)."""
    addr &= PC_MASK
    return rom[addr] if addr < DEPTH else 0


def model_pc_next(pc, redirect, target, hold):
    """Reference PC update: redirect, then hold, then sequential increment."""
    if redirect:
        return target & PC_MASK
    if hold:
        return pc
    return (pc + 1) & PC_MASK


def model_fetch_step(rom, state, redirect, target, hold):
    """One edge of the reference F stage.

    ``state`` is ``(pc, instr_pc, instr)`` as observed during the current
    cycle.  The EBR samples the current PC on the edge (unless held) and its
    output register becomes the instruction for it; the tag register follows
    the same rule, so ``instr_pc`` becomes the sampled PC.
    """
    pc, instr_pc, instr = state
    nxt_pc = model_pc_next(pc, redirect, target, hold)
    if hold:
        return (nxt_pc, instr_pc, instr)
    return (nxt_pc, pc, model_rom(rom, pc))


# --- harnesses --------------------------------------------------------------

class RstHarness(Elaboratable):
    """A unit on a ``sync`` domain whose reset is an input port."""

    def __init__(self, dut):
        self.rst = Signal()
        self.dut = dut

    def elaborate(self, platform):
        m = Module()
        cd = ClockDomain("sync")
        m.domains.sync = cd
        m.d.comb += cd.rst.eq(self.rst)
        m.submodules.dut = self.dut
        return m


class ImemHarness(RstHarness):
    def __init__(self, init=()):
        super().__init__(InstrMemory(init))


class FetchHarness(RstHarness):
    def __init__(self, init=()):
        super().__init__(Fetch(init))


class TopHarness(Elaboratable):
    """Drive a top's ``clk`` port from a private ``tck`` clock domain."""

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

def _run(h, tb, domain="sync"):
    sim = Simulator(h)
    sim.add_clock(CLK_PERIOD, domain=domain)
    sim.add_testbench(tb, background=False)
    sim.run()


async def _reset(ctx, h):
    if hasattr(h.dut, "i_en"):
        ctx.set(h.dut.i_en, 1)
    if hasattr(h.dut, "i_redirect"):
        ctx.set(h.dut.i_redirect, 0)
    if hasattr(h.dut, "i_hold"):
        ctx.set(h.dut.i_hold, 0)
    ctx.set(h.rst, 1)
    await ctx.tick()
    ctx.set(h.rst, 0)
    await ctx.delay(SETTLE)


async def _imem_read(ctx, h, addr, en=1):
    """Present an address, take one edge, return ``o_data``."""
    ctx.set(h.dut.i_addr, addr)
    ctx.set(h.dut.i_en, en)
    await ctx.tick()
    await ctx.delay(SETTLE)
    return ctx.get(h.dut.o_data)


async def _imem_idle(ctx, h, addr, en=0, cycles=1):
    """Hold the inputs without reading, to expose the output register."""
    ctx.set(h.dut.i_addr, addr)
    ctx.set(h.dut.i_en, en)
    for _ in range(cycles):
        await ctx.tick()
    await ctx.delay(SETTLE)


async def _fetch_step(ctx, h, redirect=0, target=0, hold=0):
    """Apply one control word, take one edge, return the post-edge state."""
    ctx.set(h.dut.i_redirect, redirect)
    ctx.set(h.dut.i_target, target)
    ctx.set(h.dut.i_hold, hold)
    await ctx.tick()
    await ctx.delay(SETTLE)
    return (ctx.get(h.dut.o_pc), ctx.get(h.dut.o_instr_pc),
            ctx.get(h.dut.o_instr))


def _fetch_state(ctx, h):
    return (ctx.get(h.dut.o_pc), ctx.get(h.dut.o_instr_pc),
            ctx.get(h.dut.o_instr))


ROM = [0x11111, 0x22222, 0x33333, 0x04444, 0x15555, 0x26666, 0x37777, 0x08888]


# --- InstrMemory ------------------------------------------------------------

def test_memory_init_words_read_back():
    """>= 6 distinct addresses return the initialised word, not a neighbour."""
    rom = [0] * DEPTH
    addrs = [0, 1, 2, 17, 255, 512, 1023]
    for i, a in enumerate(addrs):
        rom[a] = 0x20000 | (i * 0x1234 + 1)      # non-zero, distinct, 18-bit
    h = ImemHarness(rom)
    got = []

    async def tb(ctx):
        await _reset(ctx, h)
        # Prime the output register with a word that is not the first target.
        await _imem_read(ctx, h, 512)
        for a in addrs:
            got.append(await _imem_read(ctx, h, a))

    _run(h, tb)
    assert got == [rom[a] for a in addrs], [hex(v) for v in got]


def test_read_latency_is_exactly_one_cycle():
    """One edge after the address is sampled the word is there, not before."""
    rom = [0x10000 + i for i in range(16)]
    h = ImemHarness(rom)
    seen = []

    async def tb(ctx):
        await _reset(ctx, h)
        ctx.set(h.dut.i_addr, 3)
        ctx.set(h.dut.i_en, 1)
        await ctx.delay(SETTLE)
        seen.append(ctx.get(h.dut.o_data))        # nothing yet: sim starts at 0
        await ctx.tick()
        await ctx.delay(SETTLE)
        seen.append(ctx.get(h.dut.o_data))        # word 3, exactly one edge

        ctx.set(h.dut.i_addr, 9)
        await ctx.delay(SETTLE)
        seen.append(ctx.get(h.dut.o_data))        # still word 3
        await ctx.tick()
        await ctx.delay(SETTLE)
        seen.append(ctx.get(h.dut.o_data))        # word 9

    _run(h, tb)
    assert seen == [0, 0x10003, 0x10003, 0x10009], [hex(v) for v in seen]


def test_enable_low_holds_output():
    """``i_en = 0`` freezes the output register for several cycles."""
    rom = [0x20000 + 7 * i for i in range(16)]
    h = ImemHarness(rom)
    seen = []

    async def tb(ctx):
        await _reset(ctx, h)
        seen.append(await _imem_read(ctx, h, 1))         # 0x20007
        for a in (2, 3, 4, 5):
            await _imem_idle(ctx, h, a, en=0)
            seen.append(ctx.get(h.dut.o_data))
        seen.append(await _imem_read(ctx, h, 4))         # 0x2001C

    _run(h, tb)
    assert seen == [0x20007, 0x20007, 0x20007, 0x20007, 0x20007, 0x2001C], \
        [hex(v) for v in seen]


def test_enable_low_holds_through_out_of_range_address():
    """A disabled read outside the memory still holds the last word."""
    rom = [0x3AAAA, 0x0BBBB]
    h = ImemHarness(rom)
    seen = []

    async def tb(ctx):
        await _reset(ctx, h)
        seen.append(await _imem_read(ctx, h, 0))
        await _imem_idle(ctx, h, 0x0400, en=0, cycles=3)
        seen.append(ctx.get(h.dut.o_data))
        seen.append(await _imem_read(ctx, h, 0x0400))     # enabled: 0
        await _imem_idle(ctx, h, 1, en=0, cycles=2)
        seen.append(ctx.get(h.dut.o_data))                # still 0
        seen.append(await _imem_read(ctx, h, 1))          # 0x0BBBB

    _run(h, tb)
    assert seen == [0x3AAAA, 0x3AAAA, 0, 0, 0x0BBBB], [hex(v) for v in seen]


def test_out_of_range_addresses_read_zero():
    """``addr >= 1024`` reads 0 even though the EBR address aliases."""
    rom = [0x2FFFF - i for i in range(DEPTH)]
    h = ImemHarness(rom)
    seen = []

    async def tb(ctx):
        await _reset(ctx, h)
        seen.append(await _imem_read(ctx, h, 1))          # prove output is live
        for a in (DEPTH, DEPTH + 1, DEPTH + 0x2A5, 0x8000, 0xFFFF):
            seen.append(await _imem_read(ctx, h, a))

    _run(h, tb)
    assert seen == [rom[1]] + [0] * 5, [hex(v) for v in seen]


def test_depth_boundary_is_exactly_1024():
    """0x03FF is the last real word; 0x0400 is already outside."""
    rom = [0] * DEPTH
    rom[DEPTH - 1] = 0x3FEDC
    rom[0] = 0x01234
    h = ImemHarness(rom)
    seen = []

    async def tb(ctx):
        await _reset(ctx, h)
        seen.append(await _imem_read(ctx, h, DEPTH - 1))
        seen.append(await _imem_read(ctx, h, DEPTH))
        seen.append(await _imem_read(ctx, h, DEPTH - 2))
        seen.append(await _imem_read(ctx, h, 0))

    _run(h, tb)
    assert seen == [0x3FEDC, 0, 0, 0x01234], [hex(v) for v in seen]


def test_init_is_padded_and_truncated_to_1024():
    """Short ``init`` zero-pads, long ``init`` truncates at the memory depth."""
    long_init = [0x10000 | i for i in range(DEPTH + 5)]
    h = ImemHarness(long_init[:3])
    h_long = ImemHarness(long_init)
    seen = []
    seen_long = []

    async def tb(ctx):
        await _reset(ctx, h)
        seen.append(await _imem_read(ctx, h, 0))
        seen.append(await _imem_read(ctx, h, 2))
        seen.append(await _imem_read(ctx, h, 3))

    async def tb_long(ctx):
        await _reset(ctx, h_long)
        seen_long.append(await _imem_read(ctx, h_long, DEPTH - 1))
        seen_long.append(await _imem_read(ctx, h_long, DEPTH))

    _run(h, tb)
    _run(h_long, tb_long)
    assert seen == [0x10000, 0x10002, 0], [hex(v) for v in seen]
    assert seen_long == [0x10000 | (DEPTH - 1), 0], [hex(v) for v in seen_long]
    # Default construction starts empty, so the memory reads all zeros.
    empty = InstrMemory()
    assert empty.init == []
    Fragment.get(empty, None)


def test_memory_has_no_write_port():
    """The frozen interface is read-only: only i_addr/i_en/o_data exist."""
    h = ImemHarness(ROM)
    sigs = {name for name, value in vars(h.dut).items()
            if isinstance(value, Signal)}
    assert sigs == {"i_addr", "i_en", "o_data"}, sigs
    assert not any(name.startswith("i_w") for name in sigs)
    before = []
    after = []

    async def tb(ctx):
        await _reset(ctx, h)
        for a in range(8):
            before.append(await _imem_read(ctx, h, a))
        # Every "write-shaped" attempt is just another read: data presented on
        # the address pins cannot reach the array.
        for a in range(16):
            await _imem_read(ctx, h, a & DEPTH_MASK)
        for a in range(8):
            after.append(await _imem_read(ctx, h, a))

    _run(h, tb)
    assert before == after == ROM[:8], [hex(v) for v in before]


# --- Fetch: latency, order, control ----------------------------------------

def test_fetch_program_appears_in_order_with_matching_pc_tag():
    """>= 6 preloaded words come out in order, each tagged with its PC."""
    h = FetchHarness(ROM)
    seen = []

    async def tb(ctx):
        await _reset(ctx, h)
        for _ in range(len(ROM) + 2):
            await _fetch_step(ctx, h)
            seen.append(_fetch_state(ctx, h))

    _run(h, tb)
    # PCs advance one per edge; the instruction and its tag lag by one edge.
    assert [s[0] for s in seen] == list(range(1, len(ROM) + 3)), \
        [s[0] for s in seen]
    assert [s[1] for s in seen[:len(ROM)]] == list(range(len(ROM))), \
        [s[1] for s in seen]
    assert [s[2] for s in seen[:len(ROM)]] == ROM, [hex(s[2]) for s in seen]
    # Addresses past the 8-word program are in range but zero-filled.
    assert seen[len(ROM)][2] == 0 and seen[len(ROM)][1] == len(ROM)


def test_first_instruction_two_cycles_after_reset():
    """Edge 1 makes PC 0 current, edge 2 delivers word 0 and its tag."""
    h = FetchHarness(ROM)
    seen = []

    async def tb(ctx):
        ctx.set(h.dut.i_redirect, 0)
        ctx.set(h.dut.i_hold, 0)
        ctx.set(h.rst, 1)
        await ctx.tick()
        await ctx.delay(SETTLE)
        # Reset edge: PC is 0 but its instruction cannot be there yet.
        seen.append(_fetch_state(ctx, h))
        ctx.set(h.rst, 0)
        await ctx.tick()                       # second edge
        await ctx.delay(SETTLE)
        seen.append(_fetch_state(ctx, h))
        await ctx.tick()                       # third edge: next instruction
        await ctx.delay(SETTLE)
        seen.append(_fetch_state(ctx, h))

    _run(h, tb)
    assert seen[0][0] == 0 and seen[0][1] == 0, seen[0]
    assert seen[0][2] == 0, hex(seen[0][2])       # masked, not word 0
    assert seen[1] == (1, 0, ROM[0]), [hex(v) for v in seen[1]]
    assert seen[2] == (2, 1, ROM[1]), [hex(v) for v in seen[2]]


def test_redirect_latency_is_exact():
    """A redirect asserted in cycle k gives word[target] in cycle k + 2."""
    rom = [0x10000 | (7 * i) for i in range(32)]
    h = FetchHarness(rom)
    target = 0x0014
    seen = []

    async def tb(ctx):
        await _reset(ctx, h)
        for _ in range(4):
            await _fetch_step(ctx, h)
        # Cycle 4: PC == 4, instruction word[3] just arrived.
        seen.append(_fetch_state(ctx, h))
        # Assert the redirect for one cycle and watch the next two edges.
        ctx.set(h.dut.i_redirect, 1)
        ctx.set(h.dut.i_target, target)
        await ctx.tick()
        await ctx.delay(SETTLE)
        seen.append(_fetch_state(ctx, h))       # PC == target, old instruction
        ctx.set(h.dut.i_redirect, 0)
        await ctx.tick()
        await ctx.delay(SETTLE)
        seen.append(_fetch_state(ctx, h))       # word[target] arrives
        await ctx.tick()
        await ctx.delay(SETTLE)
        seen.append(_fetch_state(ctx, h))

    _run(h, tb)
    assert seen[0] == (4, 3, rom[3]), [hex(v) for v in seen[0]]
    # One edge after the redirect: the new PC is current, the fetch is not.
    assert seen[1] == (target, 4, rom[4]), [hex(v) for v in seen[1]]
    # Second edge after the redirect: the target's instruction and its tag.
    assert seen[2] == (target + 1, target, rom[target]), \
        [hex(v) for v in seen[2]]
    assert seen[3] == (target + 2, target + 1, rom[target + 1]), \
        [hex(v) for v in seen[3]]
    assert seen[1][1:] != seen[2][1:]          # the latency is observable


def test_hold_freezes_pc_instruction_and_tag():
    """``i_hold`` keeps all three outputs stable for several cycles."""
    h = FetchHarness(ROM)
    held = []
    resume = []

    async def tb(ctx):
        await _reset(ctx, h)
        for _ in range(4):
            await _fetch_step(ctx, h)
        for _ in range(6):
            held.append(await _fetch_step(ctx, h, hold=1))
        for _ in range(3):
            resume.append(await _fetch_step(ctx, h))

    _run(h, tb)
    assert held == [(4, 3, ROM[3])] * 6, ([hex(v) for v in held[0]], len(set(held)))
    assert resume == [(5, 4, ROM[4]), (6, 5, ROM[5]), (7, 6, ROM[6])], \
        [tuple(hex(v) for v in s) for s in resume]


def test_hold_with_redirect_moves_pc_but_not_the_fetch():
    """Redirect beats hold for the PC; the EBR output stays frozen."""
    rom = [0x10000 | i for i in range(32)]
    h = FetchHarness(rom)
    seen = []

    async def tb(ctx):
        await _reset(ctx, h)
        for _ in range(3):
            await _fetch_step(ctx, h)
        seen.append(_fetch_state(ctx, h))
        seen.append(await _fetch_step(ctx, h, redirect=1, target=0x0010,
                                      hold=1))
        seen.append(await _fetch_step(ctx, h, hold=1))
        seen.append(await _fetch_step(ctx, h))
        seen.append(await _fetch_step(ctx, h))

    _run(h, tb)
    assert seen[0] == (3, 2, rom[2]), [hex(v) for v in seen[0]]
    # PC jumps, the frozen instruction/tag do not.
    assert seen[1] == (0x0010, 2, rom[2]), [hex(v) for v in seen[1]]
    assert seen[2] == (0x0010, 2, rom[2]), [hex(v) for v in seen[2]]
    # Release: word[0x10] arrives, tagged with 0x10.
    assert seen[3] == (0x0011, 0x0010, rom[0x10]), [hex(v) for v in seen[3]]
    assert seen[4] == (0x0012, 0x0011, rom[0x11]), [hex(v) for v in seen[4]]


def test_fetch_wraps_at_ffff_and_reads_out_of_range_as_zero():
    """Sequential PC wrap matches PCUnit; 0xFFFE/0xFFFF read as 0."""
    h = FetchHarness(ROM)
    pcs = []
    tags = []
    instrs = []

    async def tb(ctx):
        await _reset(ctx, h)
        await _fetch_step(ctx, h, redirect=1, target=0xFFFE)
        for _ in range(6):
            pcs.append(ctx.get(h.dut.o_pc))
            tags.append(ctx.get(h.dut.o_instr_pc))
            instrs.append(ctx.get(h.dut.o_instr))
            await _fetch_step(ctx, h)
        pcs.append(ctx.get(h.dut.o_pc))
        tags.append(ctx.get(h.dut.o_instr_pc))
        instrs.append(ctx.get(h.dut.o_instr))

    _run(h, tb)
    assert pcs == [0xFFFE, 0xFFFF, 0x0000, 0x0001, 0x0002, 0x0003, 0x0004], \
        [hex(v) for v in pcs]
    assert tags == [0x0000, 0xFFFE, 0xFFFF, 0x0000, 0x0001, 0x0002, 0x0003], \
        [hex(v) for v in tags]
    # 0xFFFE/0xFFFF are outside the 1024-word memory: they read 0, then the
    # program starts again from word 0 once the PC wraps.
    assert instrs == [ROM[0], 0, 0, ROM[0], ROM[1], ROM[2], ROM[3]], \
        [hex(v) for v in instrs]


# --- Fetch: seeded random lockstep -----------------------------------------

def test_random_script_against_python_model():
    """>= 200 seeded redirect/hold/advance steps vs an independent model."""
    rng = random.Random(0x4E4E)
    n_steps = 300
    rom = [rng.randrange(1 << isa.WIDTH) for _ in range(DEPTH)]
    script = []
    model_pc = 0
    model_tag = 0
    model_instr = 0
    for _ in range(n_steps):
        redirect = 1 if rng.random() < 0.25 else 0
        hold = 1 if rng.random() < 0.30 else 0
        target = rng.randrange(1 << PC_WIDTH)
        if rng.random() < 0.15:
            target = rng.choice([0, DEPTH - 1, DEPTH, DEPTH + 3, 0xFFFF,
                                 0xFFFE])
        script.append((redirect, target, hold, model_pc, model_tag,
                       model_instr))
        model_pc, model_tag, model_instr = model_fetch_step(
            rom, (model_pc, model_tag, model_instr), redirect, target, hold)

    assert len(script) >= 200
    # The script must exercise every path, not just increments.
    assert sum(1 for s in script if s[0]) >= 40                 # redirects
    assert sum(1 for s in script if s[2]) >= 30                 # holds
    assert sum(1 for s in script if not s[0] and not s[2]) >= 120
    assert sum(1 for s in script if s[0] and s[2]) >= 15        # redirect+hold
    assert sum(1 for s in script if s[3] >= DEPTH) >= 40        # out-of-range

    h = FetchHarness(rom)
    bad = []

    async def tb(ctx):
        await _reset(ctx, h)
        for redirect, target, hold, pc_before, tag_before, instr_before \
                in script:
            got = _fetch_state(ctx, h)
            if got != (pc_before, tag_before, instr_before):
                bad.append(("pre", pc_before, tag_before, instr_before, got))
            post = await _fetch_step(ctx, h, redirect=redirect, target=target,
                                     hold=hold)
            exp = model_fetch_step(rom, (pc_before, tag_before, instr_before),
                                   redirect, target, hold)
            if post != exp:
                bad.append(("post", (redirect, target, hold), exp, post))

    _run(h, tb)
    assert not bad, f"{len(bad)} mismatches, first: {bad[0]}"


# --- measurement tops -------------------------------------------------------

def test_instr_memory_top_registers_inputs_and_outputs():
    """Port-to-port latency and the enable freeze through the wrapper."""
    rom = [0x30000 | i for i in range(32)]
    top = InstrMemoryTop(init=rom)
    h = TopHarness(top)
    seen = []

    async def tb(ctx):
        ctx.set(top.i_en, 1)
        ctx.set(top.i_addr, 5)
        await ctx.tick("tck")                  # input register loads 5
        await ctx.delay(SETTLE)
        seen.append(ctx.get(top.o_data))       # 0: nothing read yet
        await ctx.tick("tck")                  # EBR reads 5
        await ctx.delay(SETTLE)
        seen.append(ctx.get(top.o_data))       # still the earlier EBR output
        await ctx.tick("tck")                  # output register follows
        await ctx.delay(SETTLE)
        seen.append(ctx.get(top.o_data))       # rom[5]

        ctx.set(top.i_en, 0)
        ctx.set(top.i_addr, 9)
        for _ in range(2):
            await ctx.tick("tck")
        await ctx.delay(SETTLE)
        seen.append(ctx.get(top.o_data))       # frozen: rom[5]
        ctx.set(top.i_en, 1)
        await ctx.tick("tck")                  # en_q reloads 1
        await ctx.tick("tck")                  # EBR reads 9
        await ctx.tick("tck")                  # output register follows
        await ctx.delay(SETTLE)
        seen.append(ctx.get(top.o_data))       # rom[9]

    _run(h, tb, domain="tck")
    assert seen == [0, 0, rom[5], rom[5], rom[9]], [hex(v) for v in seen]


def test_fetch_top_registers_inputs_and_outputs():
    """Four-stage port latency and the hold freeze through the wrapper."""
    rom = [0x20000 | (3 * i) for i in range(32)]
    top = FetchTop(init=rom)
    h = TopHarness(top)
    seen = []

    def snap(ctx):
        return (ctx.get(top.o_pc), ctx.get(top.o_instr_pc),
                ctx.get(top.o_instr))

    async def tb(ctx):
        ctx.set(top.i_redirect, 0)
        ctx.set(top.i_hold, 0)
        for _ in range(6):
            await ctx.tick("tck")
        await ctx.delay(SETTLE)
        seen.append(snap(ctx))               # 0: free running

        ctx.set(top.i_hold, 1)
        await ctx.tick("tck")          # hold_q propagates
        await ctx.tick("tck")          # wrapper output stage settles
        await ctx.delay(SETTLE)
        seen.append(snap(ctx))               # 1: held
        for _ in range(4):
            await ctx.tick("tck")
        await ctx.delay(SETTLE)
        seen.append(snap(ctx))               # 2: still held

        ctx.set(top.i_hold, 0)
        ctx.set(top.i_redirect, 1)
        ctx.set(top.i_target, 0x0010)
        for _ in range(6):
            await ctx.tick("tck")
        await ctx.delay(SETTLE)
        seen.append(snap(ctx))               # 3: redirected

    _run(h, tb, domain="tck")
    # Input register + PC register + EBR + output register, so the PC port
    # trails the input by two edges and the instruction/tag by one more stage.
    assert seen[0] == (5, 4, rom[4]), [hex(v) for v in seen[0]]
    assert seen[1] == seen[2], ([hex(v) for v in seen[1]],
                                [hex(v) for v in seen[2]])
    assert seen[1][1] == seen[1][0] - 1, [hex(v) for v in seen[1]]
    assert seen[1][2] == rom[seen[1][1]], [hex(v) for v in seen[1]]
    # Held redirect: PC pins to the target, the fetch follows one edge later.
    assert seen[3] == (0x0010, 0x0010, rom[0x10]), \
        [hex(v) for v in seen[3]]


def test_measurement_tops_preload_a_full_nonzero_image():
    """The synth tops must carry 1024 distinct words (DP16KD precondition)."""
    for top in (InstrMemoryTop(), FetchTop()):
        assert len(top.init) == DEPTH
        assert len(set(top.init)) == DEPTH
        assert all(0 < w <= 0x3FFFF for w in top.init)
        assert top.ports[0] is top.clk
        Fragment.get(top, None)      # elaborate: also silences UnusedElaboratable
