"""16 x 18 register file in ECP5 distributed RAM (``TRELLIS_DPR16X4``).

Why this exists
---------------
:class:`risc18.regfile.RegFile` is a flop array with two *combinational* read
ports.  On the 25K ECP5 that array plus its two 16:1 x 18-bit read muxes is the
largest single mux consumer in the core.  A 16 x 18 memory with one write port
and two read ports maps, in yosys' ``synth_ecp5`` flow, onto two replicated
banks of five ``TRELLIS_DPR16X4`` distributed-RAM cells (10 cells, no EBR, no
flops for the array).  It is *not* an EBR solution: one DP16KD would hold
16 x 18 bits in ~1 % of its capacity, and a dual-port EBR cannot serve two
reads plus one write per cycle without a third port.  The distributed-RAM
mapping is the one the hardware actually offers at this size.

Cost of that mapping
--------------------
Reads are registered: the address presented in cycle *C* appears as data in
cycle *C + 1*.  The core therefore issues the read in F (the decode fields are
available there since M10) and consumes the data in D.  Two consequences:

* there is no same-cycle write-through, so the core carries explicit W and
  W-1 bypasses into the operand select;
* the array cannot be cleared by the synchronous reset.  ``DPR16X4`` has no
  reset and the RAM contents come from the bitstream's initialisation, so a
  mid-run reset leaves the registers as they were.  ``clear=True`` adds a
  16-cycle zeroing walk of the write port that runs *after* the reset is
  released (one cycle of reset is enough; the registers read zero from the
  16th cycle after deassertion).  While the walk runs the write port belongs to
  it, so a software write issued in that window is dropped.

Interface
---------
``i_rs1``/``i_rs2`` are sampled on the rising edge of the ``sync`` domain;
``o_rs1``/``o_rs2`` carry the addressed register in the *next* cycle.  ``r0``
reads as zero.  The write port is the flop version's: one synchronous write,
``i_rd == 0`` discarded.
"""

from amaranth import Elaboratable, Module, Mux, Signal
from amaranth.lib.memory import Memory

from . import isa

__all__ = ["RegFileRam"]


class RegFileRam(Elaboratable):
    """16 x 18 GPR file in distributed RAM: registered reads, 1 write port."""

    def __init__(self, clear=False):
        self.clear = clear
        self.i_rs1 = Signal(4)
        self.i_rs2 = Signal(4)
        self.o_rs1 = Signal(isa.WIDTH)
        self.o_rs2 = Signal(isa.WIDTH)
        self.i_rd = Signal(4)
        self.i_wdata = Signal(isa.WIDTH)
        self.i_we = Signal(1)

    def elaborate(self, platform):
        m = Module()

        mem = Memory(shape=isa.WIDTH, depth=16, init=[0] * 16)
        m.submodules.mem = mem
        wp = mem.write_port()
        rp1 = mem.read_port()
        rp2 = mem.read_port()

        m.d.comb += [
            rp1.addr.eq(self.i_rs1),
            rp2.addr.eq(self.i_rs2),
            # ``r0`` is protected at the write port only: the array initialises
            # to zero from the bitstream, the write enable discards ``i_rd == 0``
            # and nothing else can write, so an output-side mask would be
            # untestable duplicate logic.
            self.o_rs1.eq(rp1.data),
            self.o_rs2.eq(rp2.data),
        ]

        if self.clear:
            # Zeroing sequence: a 16-cycle walk of the write port after reset.
            # The port is driven from *one* place (this comb block) so the two
            # sources cannot conflict; ``busy`` resets high, so the sequence
            # restarts on every synchronous reset.
            cnt = Signal(4)
            busy = Signal(1, init=1)
            with m.If(busy):
                m.d.sync += [cnt.eq(cnt + 1), busy.eq(cnt != 15)]
            m.d.comb += [
                wp.addr.eq(Mux(busy, cnt, self.i_rd)),
                wp.data.eq(Mux(busy, 0, self.i_wdata)),
                wp.en.eq(Mux(busy, 1, self.i_we & (self.i_rd != 0))),
            ]
        else:
            m.d.comb += [
                wp.addr.eq(self.i_rd),
                wp.data.eq(self.i_wdata),
                wp.en.eq(self.i_we & (self.i_rd != 0)),
            ]

        return m
