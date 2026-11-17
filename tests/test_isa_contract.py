"""Contract tests for the frozen ISA reference (encoders, decode, assembler)."""

import random

import pytest

from risc18 import isa
from tools.asm import assemble


def test_r_format_roundtrip():
    w = isa.r_type(isa.OP_ALU, isa.ALU_SUB, 5, 6, 7)
    d = isa.decode(w)
    assert (d.op, d.rd, d.rs1, d.rs2) == (0, 5, 6, 7)
    assert d.alu_op == isa.ALU_SUB
    assert d.reg_write and d.rs1_used and d.rs2_used
    assert w >> 14 == 0


def test_immediate_extension():
    assert isa.decode(isa.i_type(isa.OP_ADDI, 1, 2, -1)).imm == isa.MASK
    assert isa.decode(isa.i_type(isa.OP_ADDI, 1, 2, 5)).imm == 5
    assert isa.decode(isa.i_type(isa.OP_ANDI, 1, 2, -1)).imm == 0x3F
    assert isa.decode(isa.i_type(isa.OP_ORI, 1, 2, 0x15)).imm == 0x15
    assert isa.decode(isa.j_type(isa.OP_LUI, 1, 0x155)).imm == (0x155 << 8) & isa.MASK
    assert isa.decode(isa.j_type(isa.OP_JAL, 1, -2)).imm == isa.MASK - 1


def test_branch_conditions():
    for opc, cond in ((isa.OP_BEQ, isa.COND_EQ), (isa.OP_BNE, isa.COND_NE),
                      (isa.OP_BLT, isa.COND_LT), (isa.OP_BGE, isa.COND_GE)):
        d = isa.decode(isa.b_type(opc, 3, 4, -7))
        assert d.branch == 1
        assert d.branch_cond == cond
        assert (d.rs1, d.rs2) == (4, 3)
        assert d.imm == isa.sext(-7, 6)
        assert d.reg_write == 0


def test_load_store_fields():
    ld = isa.decode(isa.i_type(isa.OP_LD, 3, 4, -2))
    assert (ld.rd, ld.rs1) == (3, 4)
    assert ld.mem_read and not ld.mem_write and ld.reg_write
    st = isa.decode(isa.s_type(isa.OP_ST, 5, 6, 9))
    assert (st.rs2, st.rs1) == (5, 6)
    assert st.mem_write and not st.reg_write


def test_jump_encodings():
    jal = isa.decode(isa.j_type(isa.OP_JAL, 9, -3))
    assert jal.jump and jal.reg_write and not jal.jump_reg
    assert jal.rd == 9 and jal.imm == isa.sext(-3, 10)
    jalr = isa.decode(isa.i_type(isa.OP_JALR, 9, 2, -1))
    assert jalr.jump and jalr.jump_reg and jalr.reg_write
    assert jalr.rs1_used and jalr.imm == isa.MASK


def test_shift_and_system():
    for kind, expected in enumerate((4, 5, 6, 7)):
        d = isa.decode(isa.i_type(isa.OP_SHIFT, 1, 2, (kind << 4) | 9))
        assert d.alu_op == expected and d.shift_amt == 9
    assert isa.decode(isa.i_type(isa.OP_SYSTEM, 0, 0, isa.SYS_HALT)).halt == 1
    assert isa.decode(isa.i_type(isa.OP_SYSTEM, 0, 0, isa.SYS_NOP)).halt == 0
    assert isa.decode(isa.i_type(isa.OP_SYSTEM, 0, 0, 5)).halt == 0


def test_mul_is_r_format():
    d = isa.decode(isa.r_type(isa.OP_MUL, 0, 2, 3, 4))
    assert d.mul and d.reg_write and (d.rd, d.rs1, d.rs2) == (2, 3, 4)


def test_assembler_labels_and_memory_operands():
    words = assemble("""
        addi r1, r0, 0
        addi r2, r0, 1
        addi r3, r0, 5
    loop:
        add  r1, r1, r2
        addi r2, r2, 1
        bne  r2, r3, loop
        halt
    """)
    assert len(words) == 7
    d = isa.decode(words[5])
    assert d.branch and d.branch_cond == isa.COND_NE
    # target = pc + 1 + imm == 3
    assert (5 + 1 + isa.to_signed(d.imm)) == 3

    words = assemble("ld r3, -2(r1)\nst r3, 4(r2)\njalr r5, r1, 0")
    ld, st, jalr = (isa.decode(w) for w in words)
    assert (ld.op, ld.rd, ld.rs1, ld.imm) == (isa.OP_LD, 3, 1, isa.MASK - 1)
    assert (st.op, st.rs2, st.rs1, st.imm) == (isa.OP_ST, 3, 2, 4)
    assert (jalr.op, jalr.rd, jalr.rs1) == (isa.OP_JALR, 5, 1)


def test_assembler_rejects_out_of_range():
    with pytest.raises(ValueError):
        assemble("addi r1, r0, 32")
    with pytest.raises(ValueError):
        assemble("beq r1, r0, 32")


def test_decode_is_total_and_deterministic():
    rng = random.Random(18)
    for _ in range(5000):
        w = rng.getrandbits(18)
        d = isa.decode(w)
        assert 0 <= d.op < 16 and 0 <= d.rd < 16 and 0 <= d.imm <= isa.MASK
        assert d.reg_write + d.mem_write <= 1
        assert isa.decode(w) == d
