"""Combinational instruction decoder for the risc18 ISA (M0).

The normative behaviour lives in ``docs/ISA.md`` sections 2-4 and its executable
form is :func:`risc18.isa.decode`; this module is the RTL mirror of it.  Every
output is a pure function of ``i_instr`` — no clock, no reset.

Field extraction reproduces ``isa.decode()`` *including its defaults*: unless an
opcode overrides them, ``rd`` is the I-format field ``instr[13:10]``, ``rs1`` is
``instr[9:6]`` and ``rs2`` is the S/B-format field ``instr[13:10]``.  Those
defaults are observable outputs, so they must match bit for bit even for
instructions that do not use the operand (``rs1_used``/``rs2_used`` say which
operands the datapath actually reads).
"""

from amaranth import C, Cat, Elaboratable, Module, Signal

from . import isa

__all__ = [
    "DecodeOutputs",
    "Decoder",
    "PACK_LAYOUT",
    "PACK_WIDTH",
    "pack",
]


class DecodeOutputs:
    """Bundle of the 19 combinational decoder outputs (ISA section 4)."""

    def __init__(self):
        self.op = Signal(4)
        self.rd = Signal(4)
        self.rs1 = Signal(4)
        self.rs2 = Signal(4)
        self.imm = Signal(isa.WIDTH)
        self.alu_op = Signal(3)
        self.alu_src_imm = Signal(1)
        self.shift_amt = Signal(4)
        self.reg_write = Signal(1)
        self.mem_read = Signal(1)
        self.mem_write = Signal(1)
        self.mul = Signal(1)
        self.branch = Signal(1)
        self.branch_cond = Signal(2)
        self.jump = Signal(1)
        self.jump_reg = Signal(1)
        self.halt = Signal(1)
        self.rs1_used = Signal(1)
        self.rs2_used = Signal(1)


#: Packed output layout, ``(name, lsb_offset, width)``, LSB first.
#: 4+4+4+4+18+3+1+4+1+1+1+1+1+2+1+1+1+1+1 = 54 bits.
PACK_LAYOUT = (
    ("op", 0, 4),
    ("rd", 4, 4),
    ("rs1", 8, 4),
    ("rs2", 12, 4),
    ("imm", 16, 18),
    ("alu_op", 34, 3),
    ("alu_src_imm", 37, 1),
    ("shift_amt", 38, 4),
    ("reg_write", 42, 1),
    ("mem_read", 43, 1),
    ("mem_write", 44, 1),
    ("mul", 45, 1),
    ("branch", 46, 1),
    ("branch_cond", 47, 2),
    ("jump", 49, 1),
    ("jump_reg", 50, 1),
    ("halt", 51, 1),
    ("rs1_used", 52, 1),
    ("rs2_used", 53, 1),
)

PACK_WIDTH = sum(width for _, _, width in PACK_LAYOUT)


def pack(o: DecodeOutputs):
    """Concatenate a :class:`DecodeOutputs` bundle per :data:`PACK_LAYOUT`."""
    return Cat(*(getattr(o, name) for name, _, _ in PACK_LAYOUT))


class Decoder(Elaboratable):
    """Combinational 18-bit instruction decoder.

    ``i_instr`` is the raw instruction word; ``o`` is a bundle of decoded
    control signals whose values equal ``risc18.isa.decode(i_instr)``.
    """

    def __init__(self):
        self.i_instr = Signal(isa.WIDTH)
        self.o = DecodeOutputs()

    def elaborate(self, platform):
        m = Module()
        instr = self.i_instr
        o = self.o

        op = instr[14:18]
        fn = instr[12:14]
        imm6 = instr[0:6]
        imm10 = instr[0:10]

        # Extension forms used by the datapath (ISA section 2).
        sext6 = Cat(imm6, imm6[5].replicate(isa.WIDTH - 6))
        zext6 = Cat(imm6, C(0, isa.WIDTH - 6))
        sext10 = Cat(imm10, imm10[9].replicate(isa.WIDTH - 10))
        lui_imm = Cat(C(0, 8), imm10)  # (imm10 << 8) truncated to 18 bits

        # Neutral defaults, matching isa.decode()'s pre-dispatch state.
        m.d.comb += [
            o.op.eq(op),
            o.rd.eq(instr[10:14]),   # I/J/U-format rd
            o.rs1.eq(instr[6:10]),   # I/S/B-format rs1
            o.rs2.eq(instr[10:14]),  # S/B-format rs2
            o.imm.eq(0),
            o.alu_op.eq(isa.ALU_ADD),
            o.alu_src_imm.eq(0),
            o.shift_amt.eq(0),
            o.reg_write.eq(0),
            o.mem_read.eq(0),
            o.mem_write.eq(0),
            o.mul.eq(0),
            o.branch.eq(0),
            o.branch_cond.eq(isa.COND_EQ),
            o.jump.eq(0),
            o.jump_reg.eq(0),
            o.halt.eq(0),
            o.rs1_used.eq(0),
            o.rs2_used.eq(0),
        ]

        # R-format register fields, shared by ALU and MUL.
        r_fields = [
            o.rd.eq(instr[8:12]),
            o.rs1.eq(instr[4:8]),
            o.rs2.eq(instr[0:4]),
        ]

        with m.Switch(op):
            with m.Case(isa.OP_ALU):
                m.d.comb += r_fields
                m.d.comb += [
                    o.alu_op.eq(fn),
                    o.reg_write.eq(1),
                    o.rs1_used.eq(1),
                    o.rs2_used.eq(1),
                ]

            with m.Case(isa.OP_ADDI):
                m.d.comb += [
                    o.imm.eq(sext6),
                    o.alu_op.eq(isa.ALU_ADD),
                    o.alu_src_imm.eq(1),
                    o.reg_write.eq(1),
                    o.rs1_used.eq(1),
                ]

            with m.Case(isa.OP_ANDI):
                m.d.comb += [
                    o.imm.eq(zext6),
                    o.alu_op.eq(isa.ALU_AND),
                    o.alu_src_imm.eq(1),
                    o.reg_write.eq(1),
                    o.rs1_used.eq(1),
                ]

            with m.Case(isa.OP_ORI):
                m.d.comb += [
                    o.imm.eq(zext6),
                    o.alu_op.eq(isa.ALU_OR),
                    o.alu_src_imm.eq(1),
                    o.reg_write.eq(1),
                    o.rs1_used.eq(1),
                ]

            with m.Case(isa.OP_LD):
                m.d.comb += [
                    o.imm.eq(sext6),
                    o.alu_op.eq(isa.ALU_ADD),
                    o.alu_src_imm.eq(1),
                    o.reg_write.eq(1),
                    o.mem_read.eq(1),
                    o.rs1_used.eq(1),
                ]

            with m.Case(isa.OP_ST):
                m.d.comb += [
                    o.imm.eq(sext6),
                    o.alu_op.eq(isa.ALU_ADD),
                    o.alu_src_imm.eq(1),
                    o.mem_write.eq(1),
                    o.rs1_used.eq(1),
                    o.rs2_used.eq(1),
                ]

            for opcode, cond in ((isa.OP_BEQ, isa.COND_EQ),
                                 (isa.OP_BNE, isa.COND_NE),
                                 (isa.OP_BLT, isa.COND_LT),
                                 (isa.OP_BGE, isa.COND_GE)):
                with m.Case(opcode):
                    # rs1/rs2 keep their S/B-format defaults.
                    m.d.comb += [
                        o.imm.eq(sext6),
                        o.alu_op.eq(isa.ALU_SUB),
                        o.branch.eq(1),
                        o.branch_cond.eq(cond),
                        o.rs1_used.eq(1),
                        o.rs2_used.eq(1),
                    ]

            with m.Case(isa.OP_JAL):
                # No register operands: rd is the link register.
                m.d.comb += [
                    o.imm.eq(sext10),
                    o.reg_write.eq(1),
                    o.jump.eq(1),
                ]

            with m.Case(isa.OP_JALR):
                m.d.comb += [
                    o.imm.eq(sext6),
                    o.alu_op.eq(isa.ALU_ADD),
                    o.alu_src_imm.eq(1),
                    o.reg_write.eq(1),
                    o.jump.eq(1),
                    o.jump_reg.eq(1),
                    o.rs1_used.eq(1),
                ]

            with m.Case(isa.OP_LUI):
                m.d.comb += [
                    o.imm.eq(lui_imm),
                    o.reg_write.eq(1),
                ]

            with m.Case(isa.OP_MUL):
                m.d.comb += r_fields
                m.d.comb += [
                    o.mul.eq(1),
                    o.reg_write.eq(1),
                    o.rs1_used.eq(1),
                    o.rs2_used.eq(1),
                ]

            with m.Case(isa.OP_SHIFT):
                m.d.comb += [
                    # alu_op = 4 + kind, kind = imm6[5:4]
                    o.alu_op.eq(Cat(imm6[4:6], C(1, 1))),
                    o.shift_amt.eq(imm6[0:4]),
                    o.imm.eq(zext6),
                    o.reg_write.eq(1),
                    o.rs1_used.eq(1),
                ]

            with m.Case(isa.OP_SYSTEM):
                # Reserved imm6 values behave exactly like NOP.
                m.d.comb += [
                    o.imm.eq(zext6),
                    o.halt.eq(imm6 == isa.SYS_HALT),
                ]

            with m.Default():
                # The 4-bit opcode covers all 16 encodings; the default arm is
                # empty on purpose but kept so the switch is explicitly total.
                pass

        return m
