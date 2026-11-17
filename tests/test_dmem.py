"""Amaranth simulation tests for the M6 data EBR.

Everything here drives the RTL through ``amaranth.sim.Simulator``; no signal is
read from inside the elaboratables.  The harnesses declare the ``sync`` domain
themselves and tie ``cd.rst`` to a port (the pattern of ``tests/test_imem.py``),
so the synchronous reset the in-range flag uses is exercised for real.

Contract under test (``docs/ISA.md`` sections 1 and 5, ``docs/MILESTONES.md``
M6):

* ``DataMemory``: 1024 x 18, preloaded through ``init`` (padded/truncated to
  1024 words), synchronous write, synchronous read with exactly one cycle of
  latency and no transparency, addresses ``>= 1024`` read 0 and are ignored on
  write (the EBR address is truncated, so the aliasing cases matter), reset
  does not clear the contents;
* ``DataMemoryTop``: registered inputs and outputs, deterministic 1024-word
  preload, one DP16KD.
"""

from amaranth import ClockDomain, Elaboratable, Module, Signal
from amaranth.hdl import Fragment
from amaranth.sim import Simulator

from risc18 import isa
from risc18.dmem import ADDR_BITS, DEPTH, DataMemory
from risc18.tops import DataMemoryTop

SETTLE = 1e-9
CLK_PERIOD = 1e-6
ADDR_MASK = (1 << ADDR_BITS) - 1          # the EBR address after truncation


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


class DmemHarness(RstHarness):
    def __init__(self, init=None):
        super().__init__(DataMemory(init))


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
    ctx.set(h.dut.i_we, 0)
    ctx.set(h.dut.i_wdata, 0)
    ctx.set(h.rst, 1)
    await ctx.tick()
    ctx.set(h.rst, 0)
    await ctx.delay(SETTLE)


async def _read(ctx, h, addr):
    """Present an address, take one edge, return ``o_rdata``."""
    ctx.set(h.dut.i_addr, addr)
    ctx.set(h.dut.i_we, 0)
    await ctx.tick()
    await ctx.delay(SETTLE)
    return ctx.get(h.dut.o_rdata)


async def _write(ctx, h, addr, data):
    """Present a write, take one edge (nothing to read back yet)."""
    ctx.set(h.dut.i_addr, addr)
    ctx.set(h.dut.i_wdata, data)
    ctx.set(h.dut.i_we, 1)
    await ctx.tick()
    await ctx.delay(SETTLE)


IMAGE = [0] * DEPTH
for _i, _a in enumerate((0, 1, 2, 17, 255, 512, 1023)):
    IMAGE[_a] = 0x20000 | (_i * 0x1234 + 1)     # non-zero, distinct, 18-bit


# --- init and read ----------------------------------------------------------

def test_init_words_read_back():
    """>= 6 distinct addresses return the initialised word, not a neighbour."""
    addrs = [0, 1, 2, 17, 255, 512, 1023]
    h = DmemHarness(IMAGE)
    got = []

    async def tb(ctx):
        await _reset(ctx, h)
        await _read(ctx, h, 999)                 # prime with a different word
        for a in addrs:
            got.append(await _read(ctx, h, a))

    _run(h, tb)
    assert got == [IMAGE[a] for a in addrs], [hex(v) for v in got]


def test_read_latency_is_exactly_one_cycle_and_not_transparent():
    """One edge after the address is sampled the word is there, not before."""
    image = [0x10000 | i for i in range(16)]
    h = DmemHarness(image)
    seen = []

    async def tb(ctx):
        await _reset(ctx, h)
        await _read(ctx, h, 11)                  # known previous word
        ctx.set(h.dut.i_addr, 3)
        ctx.set(h.dut.i_we, 0)
        await ctx.delay(SETTLE)
        seen.append(ctx.get(h.dut.o_rdata))      # still word 11: not transparent
        await ctx.tick()
        await ctx.delay(SETTLE)
        seen.append(ctx.get(h.dut.o_rdata))      # word 3, exactly one edge

        ctx.set(h.dut.i_addr, 9)
        await ctx.delay(SETTLE)
        seen.append(ctx.get(h.dut.o_rdata))      # still word 3
        await ctx.tick()
        await ctx.delay(SETTLE)
        seen.append(ctx.get(h.dut.o_rdata))      # word 9

    _run(h, tb)
    # The primed read is word 11, so the "before" sample shows it, not 0.
    assert seen == [0x1000B, 0x10003, 0x10003, 0x10009], [hex(v) for v in seen]


def test_read_port_carries_all_eighteen_bits():
    """Init words with bit 17 set and pattern edges read back bit-exact."""
    image = [0x20001, 0x3FFFF, 0x1AAAA, 0x00000, 0x20000, 0x15555, 0x0AAAA,
             0x3FFFE]
    h = DmemHarness(image)
    got = []

    async def tb(ctx):
        await _reset(ctx, h)
        for a in range(len(image)):
            got.append(await _read(ctx, h, a))

    _run(h, tb)
    assert got == image, [hex(v) for v in got]
    assert got[0] >> 17 == 1 and got[4] >> 17 == 1


# --- writes -----------------------------------------------------------------

def test_write_then_read_back():
    """A word written through the write port is read back exactly."""
    h = DmemHarness()
    got = []

    async def tb(ctx):
        await _reset(ctx, h)
        await _write(ctx, h, 6, 0x2BEEF)
        got.append(await _read(ctx, h, 6))
        await _write(ctx, h, 6, 0x00001)          # overwrite
        got.append(await _read(ctx, h, 6))
        got.append(await _read(ctx, h, 7))        # neighbour untouched

    _run(h, tb)
    assert got == [0x2BEEF, 0x00001, 0], [hex(v) for v in got]


def test_write_between_reads_of_other_addresses():
    """A write cycle does not disturb the read stream for other addresses.

    The frozen interface has a single address port, so a write and a read of a
    *different* address cannot share a cycle: the test interleaves them and
    checks that neither the write nor the reads are disturbed.
    """
    image = [0x20000 | i for i in range(32)]
    h = DmemHarness(image)
    got = []

    async def tb(ctx):
        await _reset(ctx, h)
        got.append(await _read(ctx, h, 5))        # image[5], before the write
        await _write(ctx, h, 4, 0x3AAAA)          # write word 4 in between
        got.append(await _read(ctx, h, 5))        # unchanged by that write
        got.append(await _read(ctx, h, 4))        # the write landed
        got.append(await _read(ctx, h, 3))        # neighbours untouched

    _run(h, tb)
    assert got == [image[5], image[5], 0x3AAAA, image[3]], \
        [hex(v) for v in got]


def test_write_enable_low_does_not_write():
    """``i_we = 0`` leaves the addressed word alone, however long it is held."""
    image = [0x20000 | (3 * i) for i in range(16)]
    h = DmemHarness(image)
    got = []

    async def tb(ctx):
        await _reset(ctx, h)
        ctx.set(h.dut.i_addr, 2)
        ctx.set(h.dut.i_wdata, 0x3FFFF)
        ctx.set(h.dut.i_we, 0)
        for _ in range(4):
            await ctx.tick()
        await ctx.delay(SETTLE)
        got.append(await _read(ctx, h, 2))
        await _write(ctx, h, 2, 0x2AAAA)          # the same address, enabled
        got.append(await _read(ctx, h, 2))

    _run(h, tb)
    assert got == [image[2], 0x2AAAA], [hex(v) for v in got]


def test_back_to_back_writes_land_in_order():
    """Consecutive write cycles each land on their own address."""
    h = DmemHarness()
    got = []

    async def tb(ctx):
        await _reset(ctx, h)
        for i in range(8):
            ctx.set(h.dut.i_addr, i)
            ctx.set(h.dut.i_wdata, 0x20000 | (i * 0x101 + 1))
            ctx.set(h.dut.i_we, 1)
            await ctx.tick()
        ctx.set(h.dut.i_we, 0)
        await ctx.delay(SETTLE)
        for i in range(8):
            got.append(await _read(ctx, h, i))

    _run(h, tb)
    assert got == [0x20000 | (i * 0x101 + 1) for i in range(8)], \
        [hex(v) for v in got]


# --- address decoding -------------------------------------------------------

def test_out_of_range_addresses_read_zero_including_aliases():
    """``addr >= 1024`` reads 0: 0x0400/0x0C00/0x8400 all alias word 0."""
    image = [0x2FFFF - i for i in range(DEPTH)]
    h = DmemHarness(image)
    got = []

    async def tb(ctx):
        await _reset(ctx, h)
        got.append(await _read(ctx, h, 0))        # prove the output is live
        for a in (DEPTH, DEPTH + 1, 0x0400, 0x0C00, 0x8400,
                  DEPTH + 0x2A5, 0x8000, 0xFFFF):
            got.append(await _read(ctx, h, a))

    _run(h, tb)
    assert got == [image[0]] + [0] * 8, [hex(v) for v in got]
    # Sanity: those addresses really would alias inside the array.
    assert {a & ADDR_MASK for a in (0x0400, 0x0C00, 0x8400)} == {0}


def test_out_of_range_writes_are_ignored():
    """A write outside the memory must not alias onto a real word."""
    image = [0x20000 | i for i in range(16)]
    h = DmemHarness(image)
    got = []

    async def tb(ctx):
        await _reset(ctx, h)
        for a in (0x0400, 0x0C00, 0x8400, 0xFFFF):
            await _write(ctx, h, a, 0x3AAAA)
        for a in (0, 1, 2):
            got.append(await _read(ctx, h, a))

    _run(h, tb)
    # 0x0400/0x0C00/0x8400 all truncate to word 0; none of them may write it.
    assert got == [image[0], image[1], image[2]], [hex(v) for v in got]


def test_depth_boundary_is_exactly_1024():
    """0x03FF is the last real word; 0x0400 is already outside."""
    image = [0] * DEPTH
    image[DEPTH - 1] = 0x3FEDC
    image[0] = 0x01234
    h = DmemHarness(image)
    got = []

    async def tb(ctx):
        await _reset(ctx, h)
        got.append(await _read(ctx, h, DEPTH - 1))
        got.append(await _read(ctx, h, DEPTH))
        await _write(ctx, h, DEPTH - 1, 0x20002)  # legal write at the edge
        await _write(ctx, h, DEPTH, 0x20003)      # one past the edge: ignored
        got.append(await _read(ctx, h, DEPTH - 1))
        got.append(await _read(ctx, h, 0))

    _run(h, tb)
    assert got == [0x3FEDC, 0, 0x20002, 0x01234], [hex(v) for v in got]


def test_init_is_padded_and_truncated_to_1024():
    """Short ``init`` zero-pads, long ``init`` truncates at the memory depth."""
    long_init = [0x10000 | i for i in range(DEPTH + 5)]
    short = DmemHarness(long_init[:3])
    long_h = DmemHarness(long_init)
    seen_short = []
    seen_long = []

    async def tb_short(ctx):
        await _reset(ctx, short)
        for a in (0, 2, 3):
            seen_short.append(await _read(ctx, short, a))

    async def tb_long(ctx):
        await _reset(ctx, long_h)
        seen_long.append(await _read(ctx, long_h, DEPTH - 1))
        seen_long.append(await _read(ctx, long_h, DEPTH))

    _run(short, tb_short)
    _run(long_h, tb_long)
    assert seen_short == [0x10000, 0x10002, 0], [hex(v) for v in seen_short]
    assert seen_long == [0x10000 | (DEPTH - 1), 0], \
        [hex(v) for v in seen_long]
    # Default construction starts empty, so the memory reads all zeros, and a
    # long image really is truncated to 1024 entries (not silently grown).
    empty = DataMemory()
    assert empty.init == []
    long = DataMemory(long_init)
    assert len(long.init) == DEPTH + 5      # raw list kept, padded/truncated in
    for unit in (empty, long):              # the elaborated image only
        Fragment.get(unit, None)


# --- reset ------------------------------------------------------------------

def test_memory_contents_survive_a_reset_pulse():
    """Reset clears the in-range flag, never the EBR contents."""
    image = [0x20000 | (7 * i) for i in range(32)]
    h = DmemHarness(image)
    got = {}

    async def tb(ctx):
        await _reset(ctx, h)
        await _write(ctx, h, 5, 0x3BEEF)
        got["before"] = await _read(ctx, h, 5)

        ctx.set(h.rst, 1)
        ctx.set(h.dut.i_we, 0)
        for _ in range(3):
            await ctx.tick()
            await ctx.delay(SETTLE)
            got.setdefault("during", []).append(ctx.get(h.dut.o_rdata))
        ctx.set(h.rst, 0)
        await ctx.delay(SETTLE)
        got["after"] = await _read(ctx, h, 5)
        got["other"] = await _read(ctx, h, 6)

    _run(h, tb)
    assert got["before"] == got["after"] == 0x3BEEF, [hex(v) for v in
                                                      (got["before"], got["after"])]
    # The in-range flag is reset, so the first post-reset read path is masked.
    assert got["during"] == [0, 0, 0]
    assert got["other"] == image[6], hex(got["other"])


# --- frozen interface -------------------------------------------------------

def test_interface_has_exactly_the_frozen_ports():
    """Only i_addr/i_wdata/i_we/o_rdata, at the frozen widths."""
    dut = DataMemory()
    sigs = {name: value for name, value in vars(dut).items()
            if isinstance(value, Signal)}
    assert set(sigs) == {"i_addr", "i_wdata", "i_we", "o_rdata"}, sigs
    assert len(sigs["i_addr"]) == 16
    assert len(sigs["i_wdata"]) == isa.WIDTH
    assert len(sigs["o_rdata"]) == isa.WIDTH
    assert len(sigs["i_we"]) == 1
    Fragment.get(dut, None)


# --- measurement top --------------------------------------------------------

def test_data_memory_top_registers_inputs_and_outputs():
    """Three cycles of port latency (input register, EBR, output register)."""
    top = DataMemoryTop()
    h = TopHarness(top)
    seen = []

    async def tb(ctx):
        ctx.set(top.i_we, 0)
        ctx.set(top.i_addr, 5)
        await ctx.tick("tck")                  # input register loads 5
        await ctx.delay(SETTLE)
        seen.append(ctx.get(top.o_rdata))      # nothing read yet
        await ctx.tick("tck")                  # EBR reads 5
        await ctx.delay(SETTLE)
        seen.append(ctx.get(top.o_rdata))      # still the earlier EBR output
        await ctx.tick("tck")                  # output register follows
        await ctx.delay(SETTLE)
        seen.append(ctx.get(top.o_rdata))      # image[5]

        ctx.set(top.i_addr, 9)
        ctx.set(top.i_wdata, 0x2CAFE)
        ctx.set(top.i_we, 1)
        await ctx.tick("tck")                  # write 9 (address/wdata registered)
        await ctx.tick("tck")                  # EBR write + read of 9
        await ctx.tick("tck")
        await ctx.delay(SETTLE)
        seen.append(ctx.get(top.o_rdata))      # 0x2CAFE, not image[9]

    _run(h, tb, domain="tck")
    # No reset in this top: the input register is 0 at the start, so the EBR
    # reads word 0 first and its output shows `init[0]` in the second cycle.
    assert seen == [0, top.init[0], top.init[5], 0x2CAFE], \
        [hex(v) for v in seen]


def test_data_memory_top_preloads_a_full_nonzero_image():
    """The synth top must carry 1024 distinct words (DP16KD precondition)."""
    top = DataMemoryTop()
    assert len(top.init) == DEPTH
    assert len(set(top.init)) == DEPTH
    assert all(0 < w <= isa.MASK for w in top.init)
    assert len({w >> 17 for w in top.init}) == 1        # bit 17 always set
    assert top.ports[0] is top.clk
    assert set(name for name, value in vars(top).items()
               if isinstance(value, Signal)) == {
        "clk", "i_addr", "i_wdata", "i_we", "o_rdata"}
    Fragment.get(top, None)
