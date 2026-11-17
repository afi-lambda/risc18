"""Sanity tests for the golden model and assembler before RTL exists."""

from risc18.golden import run_program
from risc18 import isa
from tools.asm import assemble


def test_loop_sums_to_ten():
    m = run_program(assemble("""
        addi r1, r0, 0
        addi r2, r0, 1
        addi r3, r0, 5
    loop:
        add  r1, r1, r2
        addi r2, r2, 1
        bne  r2, r3, loop
        halt
    """))
    assert m.halted
    assert m.regs[1] == 10
    assert m.regs[2] == 5
    assert [(pc, rd) for pc, rd, _ in m.trace][:2] == [(0, 1), (1, 2)]


def test_load_store_memory():
    m = run_program(assemble("""
        addi r1, r0, 10
        addi r2, r0, 7
        st   r2, 0(r1)
        ld   r3, 0(r1)
        halt
    """))
    assert m.mem[10] == 7
    assert m.regs[3] == 7


def test_out_of_range_memory():
    program = [
        isa.i_type(isa.OP_LD, 1, 0, 0),  # ld r1, 0(r0)
        isa.i_type(isa.OP_ST, 0, 0, 0),  # st r0, 0(r0)
    ]
    m = run_program(program)
    assert m.regs[1] == 0


def test_jal_link_and_return():
    m = run_program(assemble("""
        jal  r1, sub
        addi r2, r0, 1
        halt
    sub:
        addi r3, r0, 9
        jalr r4, r1, 0
    """))
    assert m.regs[1] == 1
    assert m.regs[2] == 1
    assert m.regs[3] == 9
    assert m.regs[4] == 5


def test_shifts_and_signed_compare():
    m = run_program([
        isa.i_type(isa.OP_ADDI, 1, 0, -1),          # r1 = 0x3FFFF
        isa.i_type(isa.OP_SHIFT, 2, 1, (isa.SHIFT_SRA << 4) | 1),
        isa.i_type(isa.OP_SHIFT, 3, 1, (isa.SHIFT_SRL << 4) | 1),
        isa.i_type(isa.OP_ADDI, 4, 0, 1),           # r4 = 1
        isa.b_type(isa.OP_BLT, 4, 1, 1),            # -1 < 1 -> skip nop, halt
        isa.i_type(isa.OP_SYSTEM, 0, 0, 0),
        isa.i_type(isa.OP_SYSTEM, 0, 0, isa.SYS_HALT),
    ])
    assert m.regs[1] == isa.MASK
    assert m.regs[2] == isa.MASK
    assert m.regs[3] == 0x1FFFF
    assert m.halted
    assert m.retired == 6


def test_mul_low_bits():
    m = run_program([
        isa.i_type(isa.OP_ADDI, 1, 0, -1),
        isa.r_type(isa.OP_MUL, 0, 2, 1, 1),
        isa.i_type(isa.OP_SYSTEM, 0, 0, isa.SYS_HALT),
    ])
    assert m.regs[2] == 1
