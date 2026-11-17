"""16 x 18 register file for the risc18 ISA (M1).

Interface and behaviour are frozen by ``docs/ISA.md`` section 5:

* two independent combinational read ports (``i_rs1``/``o_rs1`` and
  ``i_rs2``/``o_rs2``) — both may address any register in the same cycle;
* one synchronous write port (``i_rd``/``i_wdata``/``i_we``), sampled on the
  rising edge of the ``sync`` domain;
* ``r0`` reads as zero and writes to ``r0`` are discarded;
* write-through: if ``i_we`` is high and ``i_rd`` equals a port address, that
  port shows ``i_wdata`` in the *same* cycle, before the edge lands, so a
  writeback in W is visible to a read in D without a stall or a bypass mux
  elsewhere in the datapath;
* synchronous reset clears every register (the ``sync`` domain reset; all
  storage is assigned in ``m.d.sync`` so it resets to 0 automatically).

Storage is an array of 16 flip-flops rather than an EBR: the contract requires
the synchronous reset to clear all 16 entries in one cycle, and no RAM
primitive (DP16KD or distributed TRELLIS_DPR16X4) has a reset.  A distributed
RAM would otherwise fit the 2-read/1-write port pattern; it is only an option
if the reset contract is redefined.  The write enable is qualified with
``i_rd != 0``, so ``r0``'s storage is never written and synthesis removes those
18 flops; simulation still resets all 16 entries.
"""

from amaranth import Array, Elaboratable, Module, Signal, Mux

from . import isa

__all__ = ["RegFile"]


class RegFile(Elaboratable):
    """16 x 18 GPR file: 2 combinational reads, 1 synchronous write."""

    def __init__(self):
        self.i_rs1 = Signal(4)
        self.i_rs2 = Signal(4)
        self.o_rs1 = Signal(isa.WIDTH)
        self.o_rs2 = Signal(isa.WIDTH)
        self.i_rd = Signal(4)
        self.i_wdata = Signal(isa.WIDTH)
        self.i_we = Signal(1)

    def elaborate(self, platform):
        m = Module()

        regs = Array([Signal(isa.WIDTH, name=f"r{i}") for i in range(16)])

        # Write port: one synchronous write per cycle, r0 discarded.  Assigning
        # through a dynamic Array index synthesises a register file with a
        # single write decoder and per-register enable.
        with m.If(self.i_we & (self.i_rd != 0)):
            m.d.sync += regs[self.i_rd].eq(self.i_wdata)

        # Read ports: purely combinational, r0 hardwired to zero, write-through
        # bypass when the written register is the one being read.  The
        # ``!= 0`` terms are belt-and-braces: the output mux below is the
        # normative r0 guard, and this term is what lets synthesis delete r0's
        # storage.  Keep both.
        rs1_bypass = self.i_we & (self.i_rd == self.i_rs1) & (self.i_rs1 != 0)
        rs2_bypass = self.i_we & (self.i_rd == self.i_rs2) & (self.i_rs2 != 0)

        rs1_data = Signal(isa.WIDTH)
        rs2_data = Signal(isa.WIDTH)
        m.d.comb += [
            rs1_data.eq(Mux(rs1_bypass, self.i_wdata, regs[self.i_rs1])),
            rs2_data.eq(Mux(rs2_bypass, self.i_wdata, regs[self.i_rs2])),
            self.o_rs1.eq(Mux(self.i_rs1 == 0, 0, rs1_data)),
            self.o_rs2.eq(Mux(self.i_rs2 == 0, 0, rs2_data)),
        ]

        return m
