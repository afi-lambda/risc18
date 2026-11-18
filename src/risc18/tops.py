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
from .core import Core
from .core6 import Core6
from .decoder import PACK_WIDTH, Decoder, pack
from .dmem import DEPTH as DMEM_DEPTH, DataMemory
from .imem import DEPTH as IMEM_DEPTH, Fetch, InstrMemory
from .mul import Multiplier
from .pc import BranchTarget, PCUnit
from .pll import Pll
from .regfile import RegFile

__all__ = ["AluTop", "BootReset", "BranchTargetTop", "BranchTop", "Core6Top",
           "CoreTop", "DataMemoryTop", "DecoderTop", "FetchTop", "Full6Top",
           "FullTop", "InstrMemoryTop", "MulRegTop", "MulTop", "PcTop",
           "RegFileTop", "CORE_TOP_PROGRAM", "CLKI6_DIV", "CLKFB6_DIV",
           "CLKOP6_DIV", "F_OUT6_MHZ"]


def _b(op, rs2, rs1, imm6):
    """B/S-format word (rs2 in [13:10], rs1 in [9:6], imm6 in [5:0])."""
    return ((op & 0xF) << 14) | ((rs2 & 0xF) << 10) | ((rs1 & 0xF) << 6) \
        | (imm6 & 0x3F)


#: Deterministic program for :class:`CoreTop`: ADDI, ALU add/sub/or, MUL,
#: SHIFT (SLL and ROR), LUI, a taken BEQ that skips two words, a JAL with a
#: link, and a HALT.  Hand-assembled from the ISA encoders so the top has no
#: dependency on ``tools/asm.py``.
CORE_TOP_PROGRAM = (
    isa.i_type(isa.OP_ADDI, 1, 0, 5),               # 0: r1 = 5
    isa.i_type(isa.OP_ADDI, 2, 0, 7),               # 1: r2 = 7
    isa.r_type(isa.OP_ALU, isa.ALU_ADD, 3, 1, 2),   # 2: r3 = r1 + r2
    isa.r_type(isa.OP_ALU, isa.ALU_SUB, 4, 2, 1),   # 3: r4 = r2 - r1
    isa.r_type(isa.OP_ALU, isa.ALU_OR, 5, 3, 4),    # 4: r5 = r3 | r4
    isa.r_type(isa.OP_MUL, 0, 6, 3, 2),             # 5: r6 = r3 * r2
    isa.i_type(isa.OP_SHIFT, 7, 6, (isa.SHIFT_SLL << 4) | 1),   # 6: r7 = r6 << 1
    isa.i_type(isa.OP_SHIFT, 8, 7, (isa.SHIFT_ROR << 4) | 3),   # 7: r8 = ror r7
    isa.u_type(isa.OP_LUI, 9, 0x155),               # 8: r9 = 0x155 << 8
    _b(isa.OP_BEQ, 1, 1, 2),                        # 9: taken -> 12
    isa.i_type(isa.OP_ADDI, 10, 0, 1),              # 10: squashed
    isa.i_type(isa.OP_ADDI, 10, 0, 2),              # 11: squashed
    isa.j_type(isa.OP_JAL, 11, 1),                  # 12: link 13 -> 14
    isa.i_type(isa.OP_ADDI, 12, 0, 3),              # 13: squashed
    isa.i_type(isa.OP_ADDI, 13, 1, 1),              # 14: r13 = r1 + 1
    isa.i_type(isa.OP_SYSTEM, 0, 0, isa.SYS_HALT),  # 15: HALT
)


def _program(depth=IMEM_DEPTH):
    """Deterministic non-trivial instruction pattern for the measurement tops.

    The EBR must not be filled with zeros: an all-zero image invites yosys to
    fold the memory away, and the milestone requires DP16KD >= 1 in the
    metrics.  Bit 17 is always set, so no word is zero, and the low ten bits
    are a bijection of the index (gcd(0x2D, 1024) == 1), so every address holds
    a different value.
    """
    return [0x20000 | ((i * 0x2D) & 0x03FF) | ((i & 0x3) << 10)
            | ((i & 0x3F) << 12)
            for i in range(depth)]


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


class MulTop(Elaboratable):
    """Registered-in / registered-out wrapper around the combinational MUL (M8).

    Elaborates :class:`risc18.mul.Multiplier` with ``registered=False`` and
    ``cell=True``, i.e. the explicit ECP5 ``MULT18X18D`` primitive, so the
    netlist shows exactly ``MULT18X18D = 1`` with every register parameter at
    ``NONE`` (``REG_INPUTA_CLK``/``REG_INPUTB_CLK``/``REG_PIPELINE_CLK``/
    ``REG_OUTPUT_CLK``) and no LUT-built multiplier.

    Timing path measured by nextpnr: ``i_a``/``i_b`` -> ``*_q`` (port to
    register), then ``*_q`` -> the DSP's combinational 18x18 array -> ``o_y``
    register, which is the interior register-to-register path that carries
    fmax.  Two cycles of port-to-port latency.  Synthesis-only, like the other
    unit tops (the simulator has no model for a vendor ``Instance``):
    reset-less ``sync`` domain driven by ``self.clk``.
    """

    def __init__(self):
        self.clk = Signal()
        self.i_a = Signal(isa.WIDTH)
        self.i_b = Signal(isa.WIDTH)
        self.o_y = Signal(isa.WIDTH)
        self.ports = [self.clk, self.i_a, self.i_b, self.o_y]

    def elaborate(self, platform):
        m = Module()
        cd = ClockDomain("sync", reset_less=True)
        m.domains += cd
        m.d.comb += cd.clk.eq(self.clk)
        m.submodules.mul = mul = Multiplier(registered=False, cell=True)

        a_q = Signal(isa.WIDTH)
        b_q = Signal(isa.WIDTH)

        m.d.sync += [
            a_q.eq(self.i_a),
            b_q.eq(self.i_b),
        ]
        m.d.comb += [
            mul.i_a.eq(a_q),
            mul.i_b.eq(b_q),
        ]
        m.d.sync += self.o_y.eq(mul.o_y)

        return m


class MulRegTop(Elaboratable):
    """Registered-in / registered-out wrapper around the pipelined MUL (M8).

    Elaborates :class:`risc18.mul.Multiplier` with ``registered=True`` and
    ``cell=True``: the product comes out of the DSP's *own* output register
    (``REG_OUTPUT_CLK = "CLK0"``, ``CLK0`` driven by the enclosing ``sync``
    clock, ``CE0`` high, ``RST0`` low) rather than from fabric flip-flops.
    That is the design's "MUL 1-2 cycles" option, and it is why this top has
    the same LUT/FF cost as :class:`MulTop` plus the one-cycle latency.

    Timing paths measured by nextpnr: ``i_a``/``i_b`` -> ``*_q``; ``*_q`` ->
    the DSP's combinational array -> the DSP's ``P`` register (inside the
    block, ``REG_OUTPUT_CLK``); ``P`` -> the ``o_y`` register the report sees
    as interior register-to-register.  Three cycles of port-to-port latency.
    Synthesis-only, like the other unit tops: reset-less ``sync`` domain
    driven by ``self.clk``.
    """

    def __init__(self):
        self.clk = Signal()
        self.i_a = Signal(isa.WIDTH)
        self.i_b = Signal(isa.WIDTH)
        self.o_y = Signal(isa.WIDTH)
        self.ports = [self.clk, self.i_a, self.i_b, self.o_y]

    def elaborate(self, platform):
        m = Module()
        cd = ClockDomain("sync", reset_less=True)
        m.domains += cd
        m.d.comb += cd.clk.eq(self.clk)
        m.submodules.mul = mul = Multiplier(registered=True, cell=True)

        a_q = Signal(isa.WIDTH)
        b_q = Signal(isa.WIDTH)

        m.d.sync += [
            a_q.eq(self.i_a),
            b_q.eq(self.i_b),
        ]
        m.d.comb += [
            mul.i_a.eq(a_q),
            mul.i_b.eq(b_q),
        ]
        m.d.sync += self.o_y.eq(mul.o_y)

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


class InstrMemoryTop(Elaboratable):
    """Registered-in / registered-out wrapper around :class:`InstrMemory`.

    Timing path measured by nextpnr: ``i_addr``/``i_en`` -> ``*_q`` (port to
    register), then ``addr_q`` -> the DP16KD address input -> DOA (the EBR's
    clock-enabled read path, ``CEA = en_q``, ``REGMODE_A="NOREG"``) -> the
    ``o_data`` register, which is the interior register-to-register path that
    carries fmax.  Three cycles of port-to-port latency (input register, EBR
    access, output register).  The EBR is preloaded with
    :func:`_program`'s deterministic 1024-word image so the milestone's
    ``DP16KD >= 1`` requirement is meaningful.  Synthesis-only, like the other
    unit tops: reset-less ``sync`` domain driven by ``self.clk``.
    """

    def __init__(self, init=None):
        self.init = _program() if init is None else list(init)
        self.clk = Signal()
        self.i_addr = Signal(16)
        self.i_en = Signal(1)
        self.o_data = Signal(isa.WIDTH)
        self.ports = [self.clk, self.i_addr, self.i_en, self.o_data]

    def elaborate(self, platform):
        m = Module()
        cd = ClockDomain("sync", reset_less=True)
        m.domains += cd
        m.d.comb += cd.clk.eq(self.clk)
        m.submodules.imem = imem = InstrMemory(self.init)

        addr_q = Signal(16)
        en_q = Signal(1)

        m.d.sync += [
            addr_q.eq(self.i_addr),
            en_q.eq(self.i_en),
        ]
        m.d.comb += [
            imem.i_addr.eq(addr_q),
            imem.i_en.eq(en_q),
        ]
        m.d.sync += self.o_data.eq(imem.o_data)

        return m


def _data_image(depth=DMEM_DEPTH):
    """Deterministic non-zero data image for :class:`DataMemoryTop`.

    Same construction as :func:`_program`: bit 17 is always set (no word is
    zero, so yosys cannot fold the array away) and the low ten bits are a
    bijection of the index (gcd(0x6B, 1024) == 1), so all 1024 words differ.
    """
    return [0x20000 | ((i * 0x6B) & 0x03FF) for i in range(depth)]


class DataMemoryTop(Elaboratable):
    """Registered-in / registered-out wrapper around :class:`DataMemory`.

    Timing paths measured by nextpnr: ``i_addr``/``i_wdata``/``i_we`` -> ``*_q``
    (port to register), then ``addr_q`` -> the DP16KD address input -> DOA ->
    the ``o_rdata`` register, which is the interior register-to-register path
    that carries fmax.  Three cycles of port-to-port latency (input register,
    EBR access, output register).  The EBR is preloaded with
    :func:`_data_image`'s deterministic 1024-word image so the milestone's
    ``DP16KD >= 1`` requirement is meaningful.  Synthesis-only, like the other
    unit tops: reset-less ``sync`` domain driven by ``self.clk``.
    """

    def __init__(self, init=None):
        self.init = _data_image() if init is None else list(init)
        self.clk = Signal()
        self.i_addr = Signal(16)
        self.i_wdata = Signal(isa.WIDTH)
        self.i_we = Signal(1)
        self.o_rdata = Signal(isa.WIDTH)
        self.ports = [self.clk, self.i_addr, self.i_wdata, self.i_we,
                      self.o_rdata]

    def elaborate(self, platform):
        m = Module()
        cd = ClockDomain("sync", reset_less=True)
        m.domains += cd
        m.d.comb += cd.clk.eq(self.clk)
        m.submodules.dmem = dmem = DataMemory(self.init)

        addr_q = Signal(16)
        wdata_q = Signal(isa.WIDTH)
        we_q = Signal(1)

        m.d.sync += [
            addr_q.eq(self.i_addr),
            wdata_q.eq(self.i_wdata),
            we_q.eq(self.i_we),
        ]
        m.d.comb += [
            dmem.i_addr.eq(addr_q),
            dmem.i_wdata.eq(wdata_q),
            dmem.i_we.eq(we_q),
        ]
        m.d.sync += self.o_rdata.eq(dmem.o_rdata)

        return m


class FetchTop(Elaboratable):
    """Registered-in / registered-out wrapper around :class:`Fetch`.

    Timing paths measured by nextpnr:

    * ``i_redirect``/``i_target``/``i_hold`` -> ``*_q`` (port to register);
    * ``*_q`` -> PC priority mux / incrementer -> the PC register (interior
      register-to-register, the path that usually carries fmax);
    * PC register -> DP16KD address/CE -> DOA -> ``o_instr`` register, and PC
      register -> ``instr_pc`` register -> ``o_instr_pc`` register.

    Four cycles of port-to-port latency (input register, PC register, EBR
    access, output register).  Preloaded with :func:`_program`'s
    image like :class:`InstrMemoryTop`.  Synthesis-only: reset-less ``sync``
    domain driven by ``self.clk``, so the netlist has exactly one clock port
    and no generated reset port.
    """

    def __init__(self, init=None):
        self.init = _program() if init is None else list(init)
        self.clk = Signal()
        self.i_redirect = Signal(1)
        self.i_target = Signal(16)
        self.i_hold = Signal(1)
        self.o_pc = Signal(16)
        self.o_instr = Signal(isa.WIDTH)
        self.o_instr_pc = Signal(16)
        self.ports = [self.clk, self.i_redirect, self.i_target, self.i_hold,
                      self.o_pc, self.o_instr, self.o_instr_pc]

    def elaborate(self, platform):
        m = Module()
        cd = ClockDomain("sync", reset_less=True)
        m.domains += cd
        m.d.comb += cd.clk.eq(self.clk)
        m.submodules.fetch = fetch = Fetch(self.init)

        redirect_q = Signal(1)
        target_q = Signal(16)
        hold_q = Signal(1)

        m.d.sync += [
            redirect_q.eq(self.i_redirect),
            target_q.eq(self.i_target),
            hold_q.eq(self.i_hold),
        ]
        m.d.comb += [
            fetch.i_redirect.eq(redirect_q),
            fetch.i_target.eq(target_q),
            fetch.i_hold.eq(hold_q),
        ]
        m.d.sync += [
            self.o_pc.eq(fetch.o_pc),
            self.o_instr.eq(fetch.o_instr),
            self.o_instr_pc.eq(fetch.o_instr_pc),
        ]

        return m


class CoreTop(Elaboratable):
    """Registered-output measurement shell around :class:`risc18.core.Core`.

    The core owns the reset-bearing ``sync`` domain it drives (``Core.cd``), so
    this top takes that domain's clock and reset signals as its own ``clk`` and
    ``i_rst`` ports instead of declaring a second clock domain: the netlist has
    exactly one clock net and no generated ``clk$``/``rst$`` port.  Yosys
    derives several reset aliases from ``i_rst`` (one of them don't-care-gated
    on the X bubble), so "one reset net" would be an overstatement.

    The shell registers sit in a ``measure`` domain of their own, clocked and
    reset by those same two ports.  A separate domain object is required
    because a submodule may not redefine the domain it finds in its child (and
    the core must own and reset its own), and it keeps the core free to be
    dropped into another top unchanged.  This top is therefore not reset-less
    like the unit tops -- the core it wraps is not, and a reset-less shell on a
    reset-bearing core would only complicate the clock/reset tree.

    Timing paths measured by nextpnr, on the preloaded :data:`CORE_TOP_PROGRAM`:

    * PC register -> EBR address/CE -> DOA -> ``fd_*`` registers -> ALU/branch
      logic -> ``dx_*`` registers -> ``w_*`` registers (the pipeline itself,
      the interior register-to-register path that carries fmax);
    * ``w_*`` registers -> RegFile read mux -> ALU -> ``dx_*`` registers (the
      hazard/read path, four bits wide on the address compare);
    * every core output -> its shell register -> output port.

    One cycle of port-to-port latency on the outputs (the shell registers),
    none on ``i_rst``: a reset input that was itself registered would delay the
    synchronous reset by a cycle for no benefit.
    """

    def __init__(self, program=None, ram_rf=False):
        self.program = list(CORE_TOP_PROGRAM if program is None else program)
        self.core = Core(self.program, ram_rf=ram_rf)
        # The core has no clock or reset ports of its own: the top's clk/rst
        # ports are the core's domain signals.
        self.clk = self.core.cd.clk
        self.i_rst = self.core.i_rst
        self.o_pc = Signal(16)
        self.o_retire = Signal(1)
        self.o_retire_pc = Signal(16)
        self.o_retire_we = Signal(1)
        self.o_retire_rd = Signal(4)
        self.o_retire_value = Signal(isa.WIDTH)
        self.o_halt = Signal(1)
        self.ports = [self.clk, self.i_rst, self.o_pc, self.o_retire,
                      self.o_retire_pc, self.o_retire_we, self.o_retire_rd,
                      self.o_retire_value, self.o_halt]

    def elaborate(self, platform):
        m = Module()
        # Shell registers: same clock, same reset, separate domain object (see
        # the class docstring).  Registered outputs, so nextpnr measures the
        # core's interior register-to-register paths rather than I/O paths.
        cd = ClockDomain("measure")
        m.domains += cd
        m.d.comb += [cd.clk.eq(self.clk), cd.rst.eq(self.i_rst)]
        m.submodules.core = self.core

        m.d.measure += [
            self.o_pc.eq(self.core.o_pc),
            self.o_retire.eq(self.core.o_retire),
            self.o_retire_pc.eq(self.core.o_retire_pc),
            self.o_retire_we.eq(self.core.o_retire_we),
            self.o_retire_rd.eq(self.core.o_retire_rd),
            self.o_retire_value.eq(self.core.o_retire_value),
            self.o_halt.eq(self.core.o_halt),
        ]

        return m


#: Boot clocks ``FullTop`` keeps the core in reset after ``LOCK`` rises, and
#: after every watchdog expiry.  Eight 60 MHz clocks (133 ns) is far more than
#: the core needs to leave its reset state, and it costs three FFs.
BOOT_HOLD = 8

#: Watchdog width: ``2**26`` core clocks is 1.12 s at 60 MHz -- long enough for
#: any measurement program (``CORE_TOP_PROGRAM`` halts after ~26 cycles) and
#: short enough that a hung core visibly restarts instead of freezing the board.
WDOG_BITS = 26


class BootReset(Elaboratable):
    """Reset sequencer and watchdog for the PLL-clocked core (M9b).

    Plain RTL in its own reset-less ``boot`` domain (clocked by the top from
    the PLL output), so it is simulatable even though the PLL it watches is
    not.  Three behaviours, in priority order:

    1. ``~i_locked`` -- ``o_rst`` is held high and the watchdog is cleared, so
       the core never sees a clock that is still slewing/undivided;
    2. ``i_locked`` and the hold counter is still running -- ``o_rst`` stays
       high for :data:`BOOT_HOLD` clocks after ``LOCK`` rises, which is the
       "release only when locked" part: lock can precede a stable output by a
       few cycles, and the core must start from a known reset state;
    3. released -- while ``i_halt`` is low the watchdog counts; on expiry it
       reloads the hold counter and clears itself, i.e. it re-resets the core
       and the program runs again.  ``i_halt`` high (the program finished)
       clears the watchdog, so a completed measurement is never restarted.

    ``o_rst`` is combinational (``~released``) so the reset is asserted in the
    same cycle the decision is taken, and it is a synchronous reset at the core
    -- one cycle is enough, the hold window is only for settling.

    Everything the sequencer stores powers up at 0 (Trellis FFs, no ``init``
    value and no ``zinit`` in this flow -- ``synth_ecp5`` is run without
    ``-dff``, so a ``Signal(init=...)`` on a flip-flop would *not* reach the
    bitstream).  The state is therefore inverted on purpose: ``released`` is the
    register, not ``hold``, so ``o_rst`` is already high in the power-up cycle,
    before this domain has seen a single clock edge.  The hold window counts up
    from 0 instead of down from ``hold`` for the same reason.
    """

    def __init__(self, *, hold=BOOT_HOLD, wdog_bits=WDOG_BITS):
        self.hold = int(hold)
        self.wdog_bits = int(wdog_bits)
        if self.hold < 1:
            raise ValueError(f"hold must be >= 1, got {hold!r}")
        if not 1 <= self.wdog_bits <= 32:
            raise ValueError(f"wdog_bits must be in 1..32, got {wdog_bits!r}")
        #: ``LOCK`` from the PLL (high once locked).
        self.i_locked = Signal()
        #: ``o_halt`` from the core: the program has completed.
        self.i_halt = Signal()
        #: Synchronous reset for the core (and for the output shell).
        self.o_rst = Signal()
        #: The domain the sequencer runs in; the top drives its ``clk`` with
        #: the PLL output (a submodule may not define the clock it is given).
        self.cd = ClockDomain("boot", reset_less=True)

    def elaborate(self, platform):
        m = Module()
        m.domains += self.cd

        released = Signal()
        hold_cnt = Signal(range(max(self.hold, 2)))
        wdog = Signal(self.wdog_bits)
        m.d.comb += self.o_rst.eq(~released)

        with m.If(~self.i_locked):
            # No lock: stay in reset, and never let the watchdog accumulate
            # cycles that belong to an unlocked (possibly stopped) clock.
            m.d.boot += [released.eq(0), hold_cnt.eq(0), wdog.eq(0)]
        with m.Elif(~released):
            with m.If(hold_cnt == self.hold - 1):
                # Hold window done: hand the core a running clock.
                m.d.boot += [released.eq(1), wdog.eq(0)]
            with m.Else():
                m.d.boot += hold_cnt.eq(hold_cnt + 1)
        with m.Else():
            with m.If(self.i_halt):
                m.d.boot += wdog.eq(0)
            with m.Elif(wdog == (1 << self.wdog_bits) - 1):
                # Watchdog expiry: back into reset for a full hold window.
                m.d.boot += [wdog.eq(0), released.eq(0), hold_cnt.eq(0)]
            with m.Else():
                m.d.boot += wdog.eq(wdog + 1)

        return m


class FullTop(Elaboratable):
    """Full-chip top: 25 MHz board clock -> :class:`Pll` -> 60 MHz core (M9b).

    This is the one top that is a whole chip rather than a measurement shell:
    the Colorlight 5A-75B supplies 25 MHz, the core was closed at 60 MHz, so
    the PLL is part of the design, not a lab fixture.  ``CoreTop`` (which takes
    its 60 MHz clock from the synthesis tool's constraint) stays the reference
    for the core's own resources and fmax; this top adds what the FPGA really
    needs and is measured through the same flow so the two are comparable.

    Structure::

        clk (25 MHz) -> EHXPLLL (CLKI_DIV=1, CLKFB_DIV=24, CLKOP_DIV=10)
                          |-> CLKOP  60.000 MHz -> core ``sync`` domain
                          |                     -> ``measure`` shell domain
                          |                     -> ``boot`` sequencer domain
                          `-> LOCK -> BootReset -> core i_rst

    Domains: the core owns its reset-bearing ``sync`` domain and the top only
    drives that domain's clock (``core.cd.clk``), exactly as ``CoreTop`` does
    -- a second ``sync`` domain in the top would be a conflict, and the core
    must stay droppable into another top unchanged.  ``BootReset`` runs in a
    reset-less ``boot`` domain and the output shell in a ``measure`` domain
    (same clock, ``rst = boot.o_rst``), so the three names all resolve to the
    PLL output net, which nextpnr promotes to one primary clock.

    Reset: nothing on this board resets the PLL (``PLLRST_ENA = "DISABLED"``);
    the core's reset comes from ``BootReset`` -- held while ``LOCK`` is low,
    held :data:`BOOT_HOLD` clocks more after it rises, then released.  If the
    core then stops retiring without halting, the watchdog re-resets it after
    ``2**WDOG_BITS`` clocks (1.12 s at 60 MHz), so a hang restarts the program
    instead of parking the board.

    Program: the same :data:`CORE_TOP_PROGRAM` image and the same 16-word
    instruction EBR as ``CoreTop``, so LUT/FF/EBR/DSP and CPI are directly
    comparable.  :func:`_program` (the 1024-word non-zero image the unit tops
    preload) is untouched; the core tops keep the short real program because
    they must actually run it.

    Outputs: the core's retire trace, PC and HALT are registered once more in
    the ``measure`` domain (one cycle of port-to-port latency, as in
    ``CoreTop``), so the ports are driven by FFs and nextpnr measures the
    core's interior paths.  ``o_locked`` is the raw LOCK bit, combinationally
    -- it is a status signal (pin/LED), not data, and delaying it by a clock
    would make "did the PLL ever lock" harder to observe.

    Synthesis only: the PLL instance has no simulator model, so this top is
    exercised through its parts -- :class:`BootReset` is simulated directly and
    the PLL parameters/instances are checked structurally in
    ``tests/test_full.py``.
    """

    def __init__(self, program=None, ram_rf=False):
        self.program = list(CORE_TOP_PROGRAM if program is None else program)
        self.pll = Pll()
        self.core = Core(self.program, ram_rf=ram_rf)
        self.boot = BootReset()
        #: 25 MHz board oscillator: the *first* port, and named ``clk`` because
        #: ``tools/synth.py`` constrains ``FREQUENCY NET "clk"``.
        self.clk = Signal()
        #: Named nets for the three inter-module connections that make this top
        #: work, so ``tests/test_full.py`` can pin them in the emitted Verilog:
        #: PLL output clock, PLL lock, and the sequencer's core reset.
        self.pll_clk = Signal()
        self.pll_locked = Signal()
        self.boot_rst = Signal()
        self.o_locked = Signal()
        self.o_pc = Signal(16)
        self.o_retire = Signal(1)
        self.o_retire_pc = Signal(16)
        self.o_retire_we = Signal(1)
        self.o_retire_rd = Signal(4)
        self.o_retire_value = Signal(isa.WIDTH)
        self.o_halt = Signal(1)
        self.ports = [self.clk, self.o_locked, self.o_pc, self.o_retire,
                      self.o_retire_pc, self.o_retire_we, self.o_retire_rd,
                      self.o_retire_value, self.o_halt]

    def elaborate(self, platform):
        m = Module()
        m.submodules.pll = pll = self.pll
        m.submodules.boot = boot = self.boot
        m.submodules.core = core = self.core

        m.d.comb += [
            # 25 MHz reference into the PLL, 60 MHz CLKOP out of it.  CLKOP
            # drives every registered domain in this design, so nextpnr sees
            # exactly one synchronous clock net (plus the pad net feeding
            # CLKI, which no FF is clocked by).
            pll.i_clk.eq(self.clk),
            self.pll_clk.eq(pll.o_clk),
            self.pll_locked.eq(pll.o_locked),
            self.boot_rst.eq(boot.o_rst),
            boot.cd.clk.eq(self.pll_clk),
            core.cd.clk.eq(self.pll_clk),
            # Lock gating: the only reset the core ever sees.
            boot.i_locked.eq(self.pll_locked),
            boot.i_halt.eq(core.o_halt),
            core.i_rst.eq(self.boot_rst),
            self.o_locked.eq(self.pll_locked),
        ]

        # Output shell: registered, same clock, reset from the sequencer (a
        # separate domain object because the core owns ``sync``).
        cd = ClockDomain("measure")
        m.domains += cd
        m.d.comb += [cd.clk.eq(self.pll_clk), cd.rst.eq(self.boot_rst)]

        m.d.measure += [
            self.o_pc.eq(core.o_pc),
            self.o_retire.eq(core.o_retire),
            self.o_retire_pc.eq(core.o_retire_pc),
            self.o_retire_we.eq(core.o_retire_we),
            self.o_retire_rd.eq(core.o_retire_rd),
            self.o_retire_value.eq(core.o_retire_value),
            self.o_halt.eq(core.o_halt),
        ]

        return m


# ---------------------------------------------------------------------------
# Six-stage (Core6) tops.  Additive: CoreTop/FullTop above are untouched, so
# the M9/M11 metrics stay comparable against the heads that produced them.
# ---------------------------------------------------------------------------

#: Divider set that turns the 25 MHz board clock into 120.000 MHz exactly:
#: ``f_out = 25 * 24 / (1 * 5) = 120.000 MHz``, VCO ``= 25 * 24 = 600 MHz``.
#: Every value is inside the bands :class:`risc18.pll.Pll` checks at
#: construction time (PFD 25 MHz in 10-400, VCO 600 MHz in 400-800,
#: ``CLKOP_DIV`` 5 <= 128, counters <= 80), so the wrapper accepts this set
#: unchanged -- no edit to ``pll.py`` was needed.
CLKI6_DIV = 1
CLKFB6_DIV = 24
CLKOP6_DIV = 5

#: Output frequency of that divider set, MHz.
F_OUT6_MHZ = 25.0 * CLKFB6_DIV / (CLKI6_DIV * CLKOP6_DIV)


class Core6Top(Elaboratable):
    """Registered-output measurement shell around :class:`risc18.core6.Core6`.

    The six-stage twin of :class:`CoreTop`: same ports, same list order, same
    ``measure`` shell, same clock/reset ownership (the core owns the
    reset-bearing ``sync`` domain, so the top's ``clk``/``i_rst`` *are* that
    domain's signals and the netlist has one clock net and no generated reset
    port).  The only difference is the core, which cuts the fetch path in two:
    ``F1`` is PC register -> EBR -> fetch register ``fq`` (no decode, no
    register-file read), and the decode plus the read-address select are what
    ``F2`` registers into FD.

    That is what makes this top measurable rather than merely available: at
    60 MHz the five-stage ``CoreTop`` was already the *fmax* reference, so a
    six-stage build at the same constraint only has to beat it, while
    :class:`Full6Top` is where the 120 MHz gate is taken.

    Synthesis only, like ``CoreTop``: no simulator model is needed here and
    ``tests/test_core6.py`` already covers the core behaviourally.
    """

    def __init__(self, program=None, ram_rf=False):
        self.program = list(CORE_TOP_PROGRAM if program is None else program)
        self.core = Core6(self.program, ram_rf=ram_rf)
        self.clk = self.core.cd.clk
        self.i_rst = self.core.i_rst
        self.o_pc = Signal(16)
        self.o_retire = Signal(1)
        self.o_retire_pc = Signal(16)
        self.o_retire_we = Signal(1)
        self.o_retire_rd = Signal(4)
        self.o_retire_value = Signal(isa.WIDTH)
        self.o_halt = Signal(1)
        self.ports = [self.clk, self.i_rst, self.o_pc, self.o_retire,
                      self.o_retire_pc, self.o_retire_we, self.o_retire_rd,
                      self.o_retire_value, self.o_halt]

    def elaborate(self, platform):
        m = Module()
        cd = ClockDomain("measure")
        m.domains += cd
        m.d.comb += [cd.clk.eq(self.clk), cd.rst.eq(self.i_rst)]
        m.submodules.core = self.core

        m.d.measure += [
            self.o_pc.eq(self.core.o_pc),
            self.o_retire.eq(self.core.o_retire),
            self.o_retire_pc.eq(self.core.o_retire_pc),
            self.o_retire_we.eq(self.core.o_retire_we),
            self.o_retire_rd.eq(self.core.o_retire_rd),
            self.o_retire_value.eq(self.core.o_retire_value),
            self.o_halt.eq(self.core.o_halt),
        ]

        return m


class Full6Top(Elaboratable):
    """Full-chip six-stage top: 25 MHz board clock -> PLL -> 120.000 MHz core.

    Structurally :class:`FullTop` with :class:`risc18.core6.Core6` inside and a
    PLL divider set changed from ``(1, 24, 10)`` to ``(1, 24, 5)``::

        clk (25 MHz) -> EHXPLLL (CLKI_DIV=1, CLKFB_DIV=24, CLKOP_DIV=5)
                          |-> CLKOP 120.000 MHz -> core ``sync`` domain
                          |                      -> ``measure`` shell domain
                          |                      -> ``boot`` sequencer domain
                          `-> LOCK -> BootReset -> core i_rst

    120.000 MHz is exact (0 Hz error, 0 ppm): the VCO stays at 600 MHz, which is
    the same VCO the 60 MHz top uses, so only the output divider changes and the
    lock behaviour of the loop is unchanged.  The set is legal for
    :class:`risc18.pll.Pll` as shipped -- PFD 25 MHz, VCO 600 MHz,
    ``CLKOP_DIV = 5 <= 128`` -- so the wrapper accepts it unchanged.

    Reset and watchdog are :class:`BootReset` exactly as in ``FullTop``
    (``BOOT_HOLD`` clocks after LOCK, then release; ``2**WDOG_BITS`` clocks of
    no-retire re-resets the core).  At 120 MHz those windows are half the
    duration they are at 60 MHz: 67 ns of hold and 559 ms of watchdog, both
    still far longer than the 24-cycle measurement program and short enough
    that a hang is visible.

    Synthesis only: ``EHXPLLL`` has no simulator model, so the PLL is checked
    structurally (``tests/test_tops6.py``) and the sequencer behaviourally
    through ``BootReset`` (``tests/test_full.py``).
    """

    def __init__(self, program=None, ram_rf=False):
        self.program = list(CORE_TOP_PROGRAM if program is None else program)
        self.pll = Pll(clki_div=CLKI6_DIV, clkfb_div=CLKFB6_DIV,
                       clkop_div=CLKOP6_DIV)
        self.core = Core6(self.program, ram_rf=ram_rf)
        self.boot = BootReset()
        #: 25 MHz board oscillator: the *first* port, named ``clk`` because
        #: ``tools/synth.py`` constrains ``FREQUENCY NET "clk"``.
        self.clk = Signal()
        self.pll_clk = Signal()
        self.pll_locked = Signal()
        self.boot_rst = Signal()
        self.o_locked = Signal()
        self.o_pc = Signal(16)
        self.o_retire = Signal(1)
        self.o_retire_pc = Signal(16)
        self.o_retire_we = Signal(1)
        self.o_retire_rd = Signal(4)
        self.o_retire_value = Signal(isa.WIDTH)
        self.o_halt = Signal(1)
        self.ports = [self.clk, self.o_locked, self.o_pc, self.o_retire,
                      self.o_retire_pc, self.o_retire_we, self.o_retire_rd,
                      self.o_retire_value, self.o_halt]

    def elaborate(self, platform):
        m = Module()
        m.submodules.pll = pll = self.pll
        m.submodules.boot = boot = self.boot
        m.submodules.core = core = self.core

        m.d.comb += [
            pll.i_clk.eq(self.clk),
            self.pll_clk.eq(pll.o_clk),
            self.pll_locked.eq(pll.o_locked),
            self.boot_rst.eq(boot.o_rst),
            boot.cd.clk.eq(self.pll_clk),
            core.cd.clk.eq(self.pll_clk),
            boot.i_locked.eq(self.pll_locked),
            boot.i_halt.eq(core.o_halt),
            core.i_rst.eq(self.boot_rst),
            self.o_locked.eq(self.pll_locked),
        ]

        cd = ClockDomain("measure")
        m.domains += cd
        m.d.comb += [cd.clk.eq(self.pll_clk), cd.rst.eq(self.boot_rst)]

        m.d.measure += [
            self.o_pc.eq(core.o_pc),
            self.o_retire.eq(core.o_retire),
            self.o_retire_pc.eq(core.o_retire_pc),
            self.o_retire_we.eq(core.o_retire_we),
            self.o_retire_rd.eq(core.o_retire_rd),
            self.o_retire_value.eq(core.o_retire_value),
            self.o_halt.eq(core.o_halt),
        ]

        return m
