"""Synthesis tops for risc18 unit milestones.

Only measurement wrappers live here: they register the unit's inputs and
outputs so nextpnr sees an interior register-to-register path (required for a
meaningful fmax, see docs/MILESTONES.md), and expose a ``.ports`` list whose
first entry is the clock.
"""

from amaranth import ClockDomain, Elaboratable, Module, Signal

from . import isa
from .alu import ALU
from .branch import BranchUnit
from .decoder import PACK_WIDTH, Decoder, pack
from .pc import BranchTarget, PCUnit
from .regfile import RegFile

__all__ = ["AluTop", "BranchTargetTop", "BranchTop", "DecoderTop", "PcTop",
           "RegFileTop"]


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


class AluTop(Elaboratable):
    """Registered-in / registered-out wrapper around :class:`ALU`.

    Timing path measured by nextpnr: ``i_a``/``i_b``/``i_op`` -> ``*_q``
    (port to register), then ``*_q`` -> ALU (carry chain / barrel shifter) ->
    ``o_y`` register, which is the interior register-to-register path that
    carries fmax.  Two cycles of port-to-port latency.  Synthesis-only: the
    reset-less ``sync`` domain driven by ``self.clk`` leaves exactly one clock
    port and no generated reset port in the netlist.
    """

    def __init__(self):
        self.clk = Signal()
        self.i_a = Signal(isa.WIDTH)
        self.i_b = Signal(isa.WIDTH)
        self.i_op = Signal(3)
        self.o_y = Signal(isa.WIDTH)
        self.ports = [self.clk, self.i_a, self.i_b, self.i_op, self.o_y]

    def elaborate(self, platform):
        m = Module()
        cd = ClockDomain("sync", reset_less=True)
        m.domains += cd
        m.d.comb += cd.clk.eq(self.clk)
        m.submodules.alu = alu = ALU()

        a_q = Signal(isa.WIDTH)
        b_q = Signal(isa.WIDTH)
        op_q = Signal(3)

        m.d.sync += [
            a_q.eq(self.i_a),
            b_q.eq(self.i_b),
            op_q.eq(self.i_op),
        ]
        m.d.comb += [
            alu.i_a.eq(a_q),
            alu.i_b.eq(b_q),
            alu.i_op.eq(op_q),
        ]
        m.d.sync += self.o_y.eq(alu.o_y)

        return m


class BranchTop(Elaboratable):
    """Registered-in / registered-out wrapper around :class:`BranchUnit`.

    Timing path measured by nextpnr: ``i_a``/``i_b`` -> ``*_q``, then
    ``*_q`` -> signed comparator / condition mux -> ``o_taken`` register.
    Two cycles of port-to-port latency, one interior register-to-register
    path.  Synthesis-only, like the other unit tops: reset-less ``sync``
    domain driven by ``self.clk``.
    """

    def __init__(self):
        self.clk = Signal()
        self.i_a = Signal(isa.WIDTH)
        self.i_b = Signal(isa.WIDTH)
        self.i_cond = Signal(2)
        self.o_taken = Signal(1)
        self.ports = [self.clk, self.i_a, self.i_b, self.i_cond, self.o_taken]

    def elaborate(self, platform):
        m = Module()
        cd = ClockDomain("sync", reset_less=True)
        m.domains += cd
        m.d.comb += cd.clk.eq(self.clk)
        m.submodules.branch = branch = BranchUnit()

        a_q = Signal(isa.WIDTH)
        b_q = Signal(isa.WIDTH)
        cond_q = Signal(2)

        m.d.sync += [
            a_q.eq(self.i_a),
            b_q.eq(self.i_b),
            cond_q.eq(self.i_cond),
        ]
        m.d.comb += [
            branch.i_a.eq(a_q),
            branch.i_b.eq(b_q),
            branch.i_cond.eq(cond_q),
        ]
        m.d.sync += self.o_taken.eq(branch.o_taken)

        return m


class BranchTargetTop(Elaboratable):
    """Registered-in / registered-out wrapper around :class:`BranchTarget`.

    Timing path measured by nextpnr: ``i_pc``/``i_imm``/``i_rs1``/``i_jump_reg``
    -> ``*_q``, then ``*_q`` -> signed add / target mux -> ``o_target``
    register, which is the interior register-to-register path that carries
    fmax.  Two cycles of port-to-port latency.  Synthesis-only: reset-less
    ``sync`` domain driven by ``self.clk``, so the netlist has exactly one
    clock port and no generated reset port.
    """

    def __init__(self):
        self.clk = Signal()
        self.i_pc = Signal(16)
        self.i_imm = Signal(isa.WIDTH)
        self.i_rs1 = Signal(isa.WIDTH)
        self.i_jump_reg = Signal(1)
        self.o_target = Signal(16)
        self.ports = [self.clk, self.i_pc, self.i_imm, self.i_rs1,
                      self.i_jump_reg, self.o_target]

    def elaborate(self, platform):
        m = Module()
        cd = ClockDomain("sync", reset_less=True)
        m.domains += cd
        m.d.comb += cd.clk.eq(self.clk)
        m.submodules.branch_target = target = BranchTarget()

        pc_q = Signal(16)
        imm_q = Signal(isa.WIDTH)
        rs1_q = Signal(isa.WIDTH)
        jump_reg_q = Signal(1)

        m.d.sync += [
            pc_q.eq(self.i_pc),
            imm_q.eq(self.i_imm),
            rs1_q.eq(self.i_rs1),
            jump_reg_q.eq(self.i_jump_reg),
        ]
        m.d.comb += [
            target.i_pc.eq(pc_q),
            target.i_imm.eq(imm_q),
            target.i_rs1.eq(rs1_q),
            target.i_jump_reg.eq(jump_reg_q),
        ]
        m.d.sync += self.o_target.eq(target.o_target)

        return m


class PcTop(Elaboratable):
    """Registered-in / registered-out wrapper around :class:`PCUnit`.

    Timing path measured by nextpnr: ``i_redirect``/``i_target``/``i_hold`` ->
    ``*_q``, then ``*_q`` -> PC priority mux / incrementer -> PC register ->
    ``o_pc`` register, which is the interior register-to-register path that
    carries fmax.  Three cycles of port-to-port latency (input register, PC
    register, output register).  Synthesis-only: reset-less ``sync`` domain
    driven by ``self.clk``, so the netlist has exactly one clock port and no
    generated reset port.
    """

    def __init__(self):
        self.clk = Signal()
        self.i_redirect = Signal(1)
        self.i_target = Signal(16)
        self.i_hold = Signal(1)
        self.o_pc = Signal(16)
        self.ports = [self.clk, self.i_redirect, self.i_target, self.i_hold,
                      self.o_pc]

    def elaborate(self, platform):
        m = Module()
        cd = ClockDomain("sync", reset_less=True)
        m.domains += cd
        m.d.comb += cd.clk.eq(self.clk)
        m.submodules.pc = pc_unit = PCUnit()

        redirect_q = Signal(1)
        target_q = Signal(16)
        hold_q = Signal(1)

        m.d.sync += [
            redirect_q.eq(self.i_redirect),
            target_q.eq(self.i_target),
            hold_q.eq(self.i_hold),
        ]
        m.d.comb += [
            pc_unit.i_redirect.eq(redirect_q),
            pc_unit.i_target.eq(target_q),
            pc_unit.i_hold.eq(hold_q),
        ]
        m.d.sync += self.o_pc.eq(pc_unit.o_pc)

        return m
