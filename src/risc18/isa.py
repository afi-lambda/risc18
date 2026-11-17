"""Frozen ISA contract for the risc18 18-bit RISC (v1).

Every RTL module, test, golden model and assembler in this repository uses the
encodings and constants defined here.  Do not change this file without a new
ISA revision documented in docs/ISA.md.
"""

from typing import NamedTuple

WIDTH = 18
ADDR_WIDTH = 16
REG_COUNT = 16
MASK = (1 << WIDTH) - 1

# --- opcodes (instruction bits [17:14]) -------------------------------------
OP_ALU = 0x0
OP_ADDI = 0x1
OP_ANDI = 0x2
OP_ORI = 0x3
OP_LD = 0x4
OP_ST = 0x5
OP_BEQ = 0x6
OP_BNE = 0x7
OP_BLT = 0x8
OP_BGE = 0x9
OP_JAL = 0xA
OP_JALR = 0xB
OP_LUI = 0xC
OP_MUL = 0xD
OP_SHIFT = 0xE
OP_SYSTEM = 0xF

# --- R-format function field (bits [13:12]) ---------------------------------
ALU_ADD = 0
ALU_SUB = 1
ALU_AND = 2
ALU_OR = 3

# --- shift kinds (imm6[5:4] of the SHIFT instruction) -----------------------
SHIFT_SLL = 0
SHIFT_SRL = 1
SHIFT_SRA = 2
SHIFT_ROR = 3

# --- branch conditions (opcode selects the condition) -----------------------
COND_EQ = 0
COND_NE = 1
COND_LT = 2
COND_GE = 3

# --- SYSTEM function codes (imm6) -------------------------------------------
SYS_NOP = 0
SYS_HALT = 1

# --- data memory (v1) -------------------------------------------------------
MEM_WORDS = 1024
MEM_MASK = MEM_WORDS - 1


def sext(value: int, bits: int) -> int:
    """Sign-extend *value* from *bits* to 18 bits, masked."""
    sign = 1 << (bits - 1)
    value &= (1 << bits) - 1
    if value & sign:
        value -= 1 << bits
    return value & MASK


def zext(value: int, bits: int) -> int:
    return value & ((1 << bits) - 1)


def to_signed(value: int, bits: int = WIDTH) -> int:
    value &= (1 << bits) - 1
    if value & (1 << (bits - 1)):
        value -= 1 << bits
    return value


# --- encoders ---------------------------------------------------------------

def r_type(op: int, fn: int, rd: int, rs1: int, rs2: int) -> int:
    return ((op & 0xF) << 14) | ((fn & 0x3) << 12) | ((rd & 0xF) << 8) \
        | ((rs1 & 0xF) << 4) | (rs2 & 0xF)


def i_type(op: int, rd: int, rs1: int, imm6: int) -> int:
    return ((op & 0xF) << 14) | ((rd & 0xF) << 10) | ((rs1 & 0xF) << 6) \
        | (imm6 & 0x3F)


def s_type(op: int, rs2: int, rs1: int, imm6: int) -> int:
    return ((op & 0xF) << 14) | ((rs2 & 0xF) << 10) | ((rs1 & 0xF) << 6) \
        | (imm6 & 0x3F)


b_type = s_type


def j_type(op: int, rd: int, imm10: int) -> int:
    return ((op & 0xF) << 14) | ((rd & 0xF) << 10) | (imm10 & 0x3FF)


u_type = j_type


# --- decoded instruction ----------------------------------------------------

class Decoded(NamedTuple):
    op: int
    rd: int
    rs1: int
    rs2: int
    imm: int          # 18-bit immediate already extended for the datapath
    alu_op: int       # one of ALU_*/SHIFT_* (SHIFT_* == 4..7)
    alu_src_imm: int  # operand B = imm instead of rs2
    shift_amt: int    # 0..15
    reg_write: int
    mem_read: int
    mem_write: int
    mul: int
    branch: int
    branch_cond: int
    jump: int
    jump_reg: int
    halt: int
    rs1_used: int
    rs2_used: int


def decode(instr: int) -> Decoded:
    """Reference decode of one 18-bit instruction word.

    This is the executable form of docs/ISA.md; the Amaranth decoder must match
    it bit for bit.
    """
    instr &= MASK
    op = (instr >> 14) & 0xF
    opcode = op
    fn = (instr >> 12) & 0x3

    # Field extraction differs per format; fill the fields the opcode uses.
    r_rd = (instr >> 8) & 0xF
    i_rd = (instr >> 10) & 0xF
    rs1_i = (instr >> 6) & 0xF
    rs2_s = (instr >> 10) & 0xF
    imm6 = instr & 0x3F
    imm10 = instr & 0x3FF
    rd = i_rd
    rs1 = rs1_i
    rs2 = rs2_s
    imm = 0
    alu_op = ALU_ADD
    alu_src_imm = 0
    shift_amt = 0
    reg_write = 0
    mem_read = 0
    mem_write = 0
    mul = 0
    branch = 0
    branch_cond = COND_EQ
    jump = 0
    jump_reg = 0
    halt = 0
    rs1_used = 0
    rs2_used = 0

    if opcode == OP_ALU:
        rd = r_rd
        rs1 = (instr >> 4) & 0xF
        rs2 = instr & 0xF
        alu_op = fn
        reg_write = 1
        rs1_used = 1
        rs2_used = 1
    elif opcode in (OP_ADDI, OP_ANDI, OP_ORI):
        imm = sext(imm6, 6) if opcode == OP_ADDI else zext(imm6, 6)
        alu_op = {OP_ADDI: ALU_ADD, OP_ANDI: ALU_AND, OP_ORI: ALU_OR}[opcode]
        alu_src_imm = 1
        reg_write = 1
        rs1_used = 1
    elif opcode == OP_LD:
        imm = sext(imm6, 6)
        alu_op = ALU_ADD
        alu_src_imm = 1
        reg_write = 1
        mem_read = 1
        rs1_used = 1
    elif opcode == OP_ST:
        imm = sext(imm6, 6)
        alu_op = ALU_ADD
        alu_src_imm = 1
        mem_write = 1
        rs1_used = 1
        rs2_used = 1
    elif opcode in (OP_BEQ, OP_BNE, OP_BLT, OP_BGE):
        imm = sext(imm6, 6)
        alu_op = ALU_SUB
        branch = 1
        branch_cond = opcode - OP_BEQ
        rs1_used = 1
        rs2_used = 1
    elif opcode == OP_JAL:
        imm = sext(imm10, 10)
        reg_write = 1
        jump = 1
    elif opcode == OP_JALR:
        imm = sext(imm6, 6)
        alu_op = ALU_ADD
        alu_src_imm = 1
        reg_write = 1
        jump = 1
        jump_reg = 1
        rs1_used = 1
    elif opcode == OP_LUI:
        imm = (imm10 << 8) & MASK
        reg_write = 1
    elif opcode == OP_MUL:
        rd = r_rd
        rs1 = (instr >> 4) & 0xF
        rs2 = instr & 0xF
        mul = 1
        reg_write = 1
        rs1_used = 1
        rs2_used = 1
    elif opcode == OP_SHIFT:
        alu_op = 4 + ((imm6 >> 4) & 0x3)
        shift_amt = imm6 & 0xF
        imm = zext(imm6, 6)
        reg_write = 1
        rs1_used = 1
    elif opcode == OP_SYSTEM:
        imm = zext(imm6, 6)
        halt = 1 if imm6 == SYS_HALT else 0

    return Decoded(opcode, rd, rs1, rs2, imm & MASK, alu_op, alu_src_imm,
                   shift_amt, reg_write, mem_read, mem_write, mul, branch,
                   branch_cond, jump, jump_reg, halt, rs1_used, rs2_used)


def is_shift_alu(alu_op: int) -> bool:
    return alu_op >= 4
