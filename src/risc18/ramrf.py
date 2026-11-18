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
  16-cycle zeroing sequence on the write port for designs that need the frozen
  contract; it requires the reset to be held for 16 cycles.

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

        # The address is registered alongside the data so r0 can be forced to
        # zero on the *output* side, where it costs one 18-bit mux instead of a
        # 16-way decode in front of the RAM.
        rs1_q = Signal(4)
        rs2_q = Signal(4)
        m.d.sync += [rs1_q.eq(self.i_rs1), rs2_q.eq(self.i_rs2)]

        m.d.comb += [
            rp1.addr.eq(self.i_rs1),
            rp2.addr.eq(self.i_rs2),
            wp.addr.eq(self.i_rd),
            wp.data.eq(self.i_wdata),
            wp.en.eq(self.i_we & (self.i_rd != 0)),
            self.o_rs1.eq(Mux(rs1_q == 0, 0, rp1.data)),
            self.o_rs2.eq(Mux(rs2_q == 0, 0, rp2.data)),
        ]

        if self.clear:
            # Zeroing sequence: while ``clr_run`` is low after reset, walk the
            # write port through all 16 entries.  ``i_rst`` is the ``sync``
            # domain reset, so the counter starts at 0 on every reset.
            cnt = Signal(4)
            busy = Signal(1, init=1)
            with m.If(busy):
                m.d.sync += [
                    cnt.eq(cnt + 1),
                    wp.addr.eq(cnt),
                    wp.data.eq(0),
                    wp.en.eq(1),
                ]
                with m.If(cnt == 15):
                    m.d.sync += [busy.eq(0), cnt.eq(0)]
            with m.Else():
                m.d.sync += busy.eq(0)

        return m
