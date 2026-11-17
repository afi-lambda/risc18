"""Data memory for the risc18 ISA (M6).

Interface and behaviour are frozen by ``docs/ISA.md`` sections 1 and 5:
``DataMemory(init=None)`` is a 1024 x 18 word-addressed RAM with

* ``i_addr`` (16), ``i_wdata`` (18), ``i_we`` (1), ``o_rdata`` (18);
* one Amaranth ``Memory`` with a synchronous write port and a synchronous
  (non-transparent) read port, so yosys maps it onto exactly one DP16KD.  The
  address is sampled on the clock edge: the word at that address is presented
  on ``o_rdata`` *after* the edge (one cycle of latency), never in the cycle
  the address is applied;
* word addresses ``>= 1024`` are outside the memory: reads return 0 and writes
  are ignored.  The EBR address is truncated to ten bits, so an out-of-range
  address aliases onto a real word; a registered in-range flag muxes the read
  data back to 0 in step with it (the same construction as
  :class:`risc18.imem.InstrMemory`), and the write enable is qualified with the
  *combinational* in-range flag so no out-of-range write can reach the array;
* ``init`` is a list of 18-bit words, zero-padded or truncated to exactly 1024
  entries (``docs/ISA.md`` section 5);
* reset does not clear the contents -- an EBR has no reset on its array, and
  ``docs/ISA.md`` says reset performs no memory writes.  Only the in-range
  output flag is reset (it is an ordinary flop on the ``sync`` domain), so the
  first cycle after reset reads 0 instead of stale data.

There is no read enable in the frozen interface: the read port is always
enabled and every cycle samples ``i_addr``.  Consumers qualify the data
themselves (the core only looks at ``o_rdata`` for a load in M).
"""

import warnings

from amaranth import Elaboratable, Module, Mux, Signal
from amaranth.hdl import Memory

from . import isa

__all__ = ["DataMemory", "DEPTH", "ADDR_BITS"]

ADDR_WIDTH = 16
DEPTH = 1024
ADDR_BITS = 10          # log2(DEPTH): the EBR address width
WORD_MASK = (1 << isa.WIDTH) - 1


def _words(init):
    """Normalise ``init`` to exactly ``DEPTH`` 18-bit words (pad/truncate)."""
    words = [w & WORD_MASK for w in list(init)[:DEPTH]]
    words += [0] * (DEPTH - len(words))
    return words


class DataMemory(Elaboratable):
    """1024 x 18 data EBR: synchronous write, synchronous read, 1-cycle read."""

    def __init__(self, init=None):
        self.init = list(init) if init is not None else []
        self.i_addr = Signal(ADDR_WIDTH)
        self.i_wdata = Signal(isa.WIDTH)
        self.i_we = Signal(1)
        self.o_rdata = Signal(isa.WIDTH)

    def elaborate(self, platform):
        m = Module()

        # Same deprecated-but-frozen `Memory` / `read_port(domain="sync")` form
        # as `imem.InstrMemory`: amaranth 0.5 prefers `amaranth.lib.memory`, but
        # this is the API the milestone contract names and the one yosys maps
        # onto one DP16KD.  The warning is a version note, not a design warning.
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            mem = Memory(width=isa.WIDTH, depth=DEPTH, init=_words(self.init))
        m.submodules.mem = mem

        # Synchronous (non-transparent) read port: address sampled on the edge,
        # data after it.  Always enabled -- the interface has no read enable.
        # Both ports share ``i_addr``, so a store cycle is a same-address
        # read+write; ``transparent=False`` makes that read see the old word
        # (read-before-write, what the DP16KD READBEFOREWRITE mode gives), and
        # no consumer looks at ``o_rdata`` for a store, so nothing depends on it.
        rp = mem.read_port(domain="sync", transparent=False)
        m.d.comb += [
            rp.addr.eq(self.i_addr[:ADDR_BITS]),
            rp.en.eq(1),
        ]

        # Addresses >= DEPTH are outside the memory: no write may reach the
        # array (the truncated address would alias onto a real word).
        in_range = Signal()
        m.d.comb += in_range.eq(self.i_addr[ADDR_BITS:] == 0)

        wp = mem.write_port()
        m.d.comb += [
            wp.addr.eq(self.i_addr[:ADDR_BITS]),
            wp.data.eq(self.i_wdata),
            wp.en.eq(self.i_we & in_range),
        ]

        # Read data: registered in-range flag, in step with the EBR output, so
        # an out-of-range read yields 0 and an in-range one the real word.
        in_range_q = Signal()
        m.d.sync += in_range_q.eq(in_range)

        m.d.comb += self.o_rdata.eq(Mux(in_range_q, rp.data, 0))

        return m
