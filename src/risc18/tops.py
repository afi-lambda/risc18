"""Synthesis tops for risc18 unit milestones.

Only measurement wrappers live here: they register the unit's inputs and
outputs so nextpnr sees an interior register-to-register path (required for a
meaningful fmax, see docs/MILESTONES.md), and expose a ``.ports`` list whose
first entry is the clock.
"""

from amaranth import ClockDomain, Elaboratable, Module, Signal

from . import isa
from .decoder import PACK_WIDTH, Decoder, pack
from .regfile import RegFile

__all__ = ["DecoderTop", "RegFileTop"]


class DecoderTop(Elaboratable):
    """Registered-in / registered-out wrapper around :class:`Decoder`.

    Timing path: ``i_instr`` -> ``instr_q`` -> combinational decode ->
    ``o_pack``.  Two clock cycles of latency from port to port, one interior
    register-to-register path.  The top is synthesis-only: it declares a
    reset-less ``sync`` domain driven by ``self.clk``, so the netlist has
    exactly one clock port and no generated reset port.

    ``o_pack`` bit layout (LSB first, 54 bits total, ``risc18.decoder.
    PACK_LAYOUT`` is the single source of truth):

    ============ ==== =====================================
    field        bits
    ============ ==== =====================================
    op           [3:0]
    rd           [7:4]
    rs1          [11:8]
    rs2          [15:12]
    imm          [33:16]
    alu_op       [36:34]
    alu_src_imm  [37]
    shift_amt    [41:38]
    reg_write    [42]
    mem_read     [43]
    mem_write    [44]
    mul          [45]
    branch       [46]
    branch_cond  [48:47]
    jump         [49]
    jump_reg     [50]
    halt         [51]
    rs1_used     [52]
    rs2_used     [53]
    ============ ==== =====================================
    """

    def __init__(self):
        self.clk = Signal()
        self.i_instr = Signal(isa.WIDTH)
        self.o_pack = Signal(PACK_WIDTH)
        self.ports = [self.clk, self.i_instr, self.o_pack]

    def elaborate(self, platform):
        m = Module()
        cd = ClockDomain("sync", reset_less=True)
        m.domains += cd
        m.d.comb += cd.clk.eq(self.clk)
        m.submodules.decoder = decoder = Decoder()

        instr_q = Signal(isa.WIDTH)
        m.d.sync += instr_q.eq(self.i_instr)
        m.d.comb += decoder.i_instr.eq(instr_q)
        m.d.sync += self.o_pack.eq(pack(decoder.o))

        return m


class RegFileTop(Elaboratable):
    """Registered-in / registered-out wrapper around :class:`RegFile`.

    Timing paths measured by nextpnr:

    * ``i_rs1``/``i_rs2`` -> ``rs1_q``/``rs2_q`` (port to register);
    * ``rs1_q`` -> register file read mux -> ``o_rs1`` register
      (the interior register-to-register path that carries fmax);
    * ``i_rd``/``i_wdata``/``i_we`` -> ``*_q`` -> write decoder -> registers.

    Two cycles of port-to-port latency.  Synthesis-only: the reset-less
    ``sync`` domain driven by ``self.clk`` leaves exactly one clock port and no
    generated reset port in the netlist.
    """

    def __init__(self):
        self.clk = Signal()
        self.i_rs1 = Signal(4)
        self.i_rs2 = Signal(4)
        self.i_rd = Signal(4)
        self.i_wdata = Signal(isa.WIDTH)
        self.i_we = Signal(1)
        self.o_rs1 = Signal(isa.WIDTH)
        self.o_rs2 = Signal(isa.WIDTH)
        self.ports = [self.clk, self.i_rs1, self.i_rs2, self.i_rd, self.i_wdata,
                      self.i_we, self.o_rs1, self.o_rs2]

    def elaborate(self, platform):
        m = Module()
        cd = ClockDomain("sync", reset_less=True)
        m.domains += cd
        m.d.comb += cd.clk.eq(self.clk)
        m.submodules.regfile = regfile = RegFile()

        rs1_q = Signal(4)
        rs2_q = Signal(4)
        rd_q = Signal(4)
        wdata_q = Signal(isa.WIDTH)
        we_q = Signal(1)

        m.d.sync += [
            rs1_q.eq(self.i_rs1),
            rs2_q.eq(self.i_rs2),
            rd_q.eq(self.i_rd),
            wdata_q.eq(self.i_wdata),
            we_q.eq(self.i_we),
        ]
        m.d.comb += [
            regfile.i_rs1.eq(rs1_q),
            regfile.i_rs2.eq(rs2_q),
            regfile.i_rd.eq(rd_q),
            regfile.i_wdata.eq(wdata_q),
            regfile.i_we.eq(we_q),
        ]
        m.d.sync += [
            self.o_rs1.eq(regfile.o_rs1),
            self.o_rs2.eq(regfile.o_rs2),
        ]

        return m
