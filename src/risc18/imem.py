"""Instruction memory and the fetch stage for the risc18 ISA (M4).

Interface and behaviour are frozen by ``docs/ISA.md`` sections 1 and 5 and by
the milestone M4 acceptance criteria in ``docs/MILESTONES.md``.

:class:`InstrMemory` is the 1024 x 18 instruction EBR.  It is a read-only
Amaranth ``Memory`` with a synchronous read port, so yosys maps it onto exactly
one DP16KD:

* address ``i_addr`` and enable ``i_en`` are sampled on the clock edge; the
  word at that address appears on ``o_data`` *after* the edge (one cycle of
  latency, no transparency);
* ``i_en`` low freezes the EBR's clock-enabled read path (its address register
  clock enable, ``CEA``; yosys reports ``REGMODE_A="NOREG"``), so ``o_data``
  holds its previous value;
* word addresses ``>= 1024`` are outside the memory and read as 0.  The EBR
  address is truncated to ten bits, so an out-of-range read aliases onto a real
  word; a registered in-range flag muxes the output back to 0 in step with the
  data, and the flag is enabled by the same ``i_en`` so the hold behaviour is
  identical for in-range and out-of-range reads.  No write port exists: the
  instruction memory is loaded only through ``init``.

:class:`Fetch` is the M5+ pipeline's F stage: a :class:`risc18.pc.PCUnit`
drives the :class:`InstrMemory` address, and the resulting instruction is
tagged with the PC that produced it.  ``o_pc`` is the PC register (the address
being fetched), ``o_instr`` the EBR output, and ``o_instr_pc`` a copy of
``o_pc`` registered in step with the EBR output.

Latency accounting (the numbers the tests pin down), counting clock edges from
the edge that makes a PC value current:

* edge 1 loads the PC (reset, or a redirect's target, or ``pc + 1``);
* edge 2 makes the word for that PC available on the EBR read path.

So an instruction appears one edge after its PC becomes the fetch address, i.e.
two edges counting the edge that loaded the PC.  The instruction for a redirect
takes exactly that long too: the redirect is asserted during the cycle it is
decoded, the target PC is current after the next edge, its instruction one edge
after that.

``i_hold`` freezes both the PC and the EBR output register (``i_en = ~i_hold``)
and, with it, ``o_instr_pc``, so a stalled fetch repeats the same tagged
instruction.  ``i_redirect`` takes priority over ``i_hold`` inside
:class:`PCUnit`; a redirect asserted while held moves the PC but keeps the
current ``o_instr``/``o_instr_pc`` until the hold is released.

Reset: the first cycle after the reset edge presents ``o_instr_pc = 0`` with
``o_instr = 0`` (the in-range flag is cleared, so the read path is masked) —
this is not word 0.  The real instruction for PC 0 appears one edge later, so
the M5 core must keep its first fetch slot empty for one cycle after reset and
must not treat the pair as a valid instruction before then.
"""

import warnings

from amaranth import Elaboratable, Module, Mux, Signal
from amaranth.hdl import Memory

from . import isa
from .pc import PCUnit

__all__ = ["InstrMemory", "Fetch"]

ADDR_WIDTH = 16
DEPTH = 1024
ADDR_BITS = 10          # log2(DEPTH): the EBR address width
WORD_MASK = (1 << isa.WIDTH) - 1


def _words(init):
    """Normalise ``init`` to exactly ``DEPTH`` 18-bit words.

    Shorter lists are zero-padded, longer ones truncated: the memory is
    ``1024 x 18`` whatever the caller hands over (docs/ISA.md section 1).
    """
    words = [w & WORD_MASK for w in list(init)[:DEPTH]]
    words += [0] * (DEPTH - len(words))
    return words


class InstrMemory(Elaboratable):
    """1024 x 18 read-only instruction EBR: sync read, 1-cycle latency."""

    def __init__(self, init=()):
        self.init = list(init)
        self.i_addr = Signal(ADDR_WIDTH)
        self.i_en = Signal(1)
        self.o_data = Signal(isa.WIDTH)

    def elaborate(self, platform):
        m = Module()

        # docs/ISA.md section 5 and the M4 scope name the `Memory` /
        # `read_port(domain="sync")` form, which amaranth 0.5 marks deprecated in
        # favour of `amaranth.lib.memory`; the deprecated-but-frozen API is kept
        # deliberately, and its warning is a version note, not a design warning.
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            mem = Memory(width=isa.WIDTH, depth=DEPTH, init=_words(self.init))
        m.submodules.mem = mem
        # Synchronous (non-transparent) read port: the address and enable are
        # sampled on the edge and data appears after it.  Yosys maps this onto
        # one DP16KD with REGMODE_A="NOREG": `en` drives the EBR's address
        # register clock enable (CEA), which is what freezes DOA while low.
        rp = mem.read_port(domain="sync")
        m.d.comb += [
            rp.addr.eq(self.i_addr[:ADDR_BITS]),
            rp.en.eq(self.i_en),
        ]

        # Addresses >= DEPTH read 0.  The flag is registered on the same edge
        # as the EBR read and enabled by the same `i_en`, so it holds together
        # with the data register.
        in_range = Signal()
        m.d.comb += in_range.eq(self.i_addr[ADDR_BITS:] == 0)
        in_range_q = Signal()
        with m.If(self.i_en):
            m.d.sync += in_range_q.eq(in_range)

        m.d.comb += self.o_data.eq(Mux(in_range_q, rp.data, 0))

        return m


class Fetch(Elaboratable):
    """F stage: PC register + instruction EBR, with the fetched PC tag."""

    def __init__(self, init=()):
        self.init = list(init)
        self.i_redirect = Signal(1)
        self.i_target = Signal(ADDR_WIDTH)
        self.i_hold = Signal(1)
        self.o_pc = Signal(ADDR_WIDTH)
        self.o_instr = Signal(isa.WIDTH)
        self.o_instr_pc = Signal(ADDR_WIDTH)

    def elaborate(self, platform):
        m = Module()

        m.submodules.pc = pc = PCUnit()
        m.submodules.imem = imem = InstrMemory(self.init)

        m.d.comb += [
            pc.i_redirect.eq(self.i_redirect),
            pc.i_target.eq(self.i_target),
            pc.i_hold.eq(self.i_hold),
        ]
        # The EBR fetches the PC that is current this cycle, and is disabled
        # while the fetch is held so the EBR read path holds its data.
        m.d.comb += [
            imem.i_addr.eq(pc.o_pc),
            imem.i_en.eq(~self.i_hold),
        ]

        m.d.comb += [
            self.o_pc.eq(pc.o_pc),
            self.o_instr.eq(imem.o_data),
        ]

        # Tag the instruction with the address sampled on the same edge that
        # produced it, and freeze the tag together with the EBR output.
        instr_pc = Signal(ADDR_WIDTH)
        with m.If(~self.i_hold):
            m.d.sync += instr_pc.eq(pc.o_pc)
        m.d.comb += self.o_instr_pc.eq(instr_pc)

        return m
