"""Amaranth simulation tests for the M0 instruction decoder.

Every test drives the RTL through ``amaranth.sim.Simulator`` — no test calls the
decoder's logic directly.  The conformance oracle is the frozen reference
``risc18.isa.decode()`` (docs/ISA.md section 4).
"""

import random

import pytest
from amaranth.sim import Simulator

from risc18 import isa
from risc18.decoder import PACK_LAYOUT, PACK_WIDTH, Decoder
from risc18.tops import DecoderTop

FIELDS = (
    "op", "rd", "rs1", "rs2", "imm", "alu_op", "alu_src_imm", "shift_amt",
    "reg_write", "mem_read", "mem_write", "mul", "branch", "branch_cond",
    "jump", "jump_reg", "halt", "rs1_used", "rs2_used",
)


# --- simulation helpers -----------------------------------------------------

def _run(words):
    """Return one tuple of decoded field values per word, in order."""
    dec = Decoder()
    sim = Simulator(dec)
    seen = []

    async def tb(ctx):
        for word in words:
            ctx.set(dec.i_instr, word & isa.MASK)
            await ctx.delay(1e-6)
            seen.append(tuple(ctx.get(getattr(dec.o, f)) for f in FIELDS))

    sim.add_testbench(tb)
    sim.run()
    return seen


def _one(word):
    (row,) = _run([word])
    return dict(zip(FIELDS, row))


def _expected(word):
    d = isa.decode(word & isa.MASK)
    return tuple(getattr(d, f) for f in FIELDS)


def _assert_isa_match(words, chunks=8):
    """Assert RTL == isa.decode() field by field for every word."""
    words = [w & isa.MASK for w in words]
    assert words
    seen = _run(words)
    assert len(seen) == len(words)
    bad = []
    for word, got in zip(words, seen):
        exp = _expected(word)
        if got != exp:
            diff = {f: {"rtl": g, "isa": e}
                    for f, g, e in zip(FIELDS, got, exp) if g != e}
            bad.append(f"0x{word:05X}: {diff}")
    assert not bad, (f"{len(bad)}/{len(words)} words differ\n"
                     + "\n".join(bad[:chunks]))


def _packed(decoded):
    value = 0
    for name, offset, width in PACK_LAYOUT:
        value |= (getattr(decoded, name) & ((1 << width) - 1)) << offset
    return value


# --- opcode / format coverage ----------------------------------------------

def test_alu_all_four_functions():
    words = []
    for fn in (isa.ALU_ADD, isa.ALU_SUB, isa.ALU_AND, isa.ALU_OR):
        word = isa.r_type(isa.OP_ALU, fn, rd=9, rs1=3, rs2=12)
        words.append(word)
        o = _one(word)
        assert (o["op"], o["rd"], o["rs1"], o["rs2"]) == (isa.OP_ALU, 9, 3, 12)
        assert o["alu_op"] == fn
        assert o["alu_src_imm"] == 0
        assert o["reg_write"] == 1
        assert (o["rs1_used"], o["rs2_used"]) == (1, 1)
        assert (o["mem_read"], o["mem_write"], o["mul"], o["branch"]) == (0, 0, 0, 0)
        assert (o["jump"], o["jump_reg"], o["halt"]) == (0, 0, 0)
        assert o["imm"] == 0
    _assert_isa_match(words)


def test_mul_uses_r_format_fields():
    word = isa.r_type(isa.OP_MUL, fn=0b10, rd=5, rs1=6, rs2=7)
    o = _one(word)
    assert o["mul"] == 1
    assert o["reg_write"] == 1
    assert (o["rd"], o["rs1"], o["rs2"]) == (5, 6, 7)
    assert o["alu_op"] == isa.ALU_ADD      # default; MUL uses the DSP path
    assert o["alu_src_imm"] == 0
    assert (o["mem_read"], o["mem_write"], o["branch"], o["jump"]) == (0, 0, 0, 0)
    _assert_isa_match([word, isa.r_type(isa.OP_MUL, 3, 15, 15, 15)])


def test_immediate_extension_cases():
    addi_neg = isa.i_type(isa.OP_ADDI, rd=1, rs1=2, imm6=-1)
    addi_pos = isa.i_type(isa.OP_ADDI, rd=1, rs1=2, imm6=5)
    andi_max = isa.i_type(isa.OP_ANDI, rd=3, rs1=4, imm6=0x3F)
    ori_small = isa.i_type(isa.OP_ORI, rd=3, rs1=4, imm6=0x15)
    lui_word = isa.j_type(isa.OP_LUI, rd=8, imm10=0x155)
    jal_neg = isa.j_type(isa.OP_JAL, rd=7, imm10=-2)

    assert _one(addi_neg)["imm"] == isa.MASK          # sext6(-1) = 0x3FFFF
    assert _one(addi_pos)["imm"] == 5
    assert _one(andi_max)["imm"] == 0x3F              # zero-extended
    assert _one(ori_small)["imm"] == 0x15
    assert _one(lui_word)["imm"] == (0x155 << 8) & isa.MASK
    assert _one(jal_neg)["imm"] == isa.MASK - 1       # sext10(-2)

    o = _one(andi_max)
    assert (o["alu_op"], o["alu_src_imm"], o["reg_write"]) == (isa.ALU_AND, 1, 1)
    assert o["rs1_used"] == 1 and o["rs2_used"] == 0
    o = _one(ori_small)
    assert (o["alu_op"], o["alu_src_imm"]) == (isa.ALU_OR, 1)

    _assert_isa_match([addi_neg, addi_pos, andi_max, ori_small, lui_word, jal_neg,
                       isa.i_type(isa.OP_ADDI, 15, 0, 31),
                       isa.i_type(isa.OP_ANDI, 0, 15, 32),
                       isa.j_type(isa.OP_LUI, 1, 0x3FF)])


def test_load_fields():
    word = isa.i_type(isa.OP_LD, rd=2, rs1=3, imm6=-4)
    o = _one(word)
    assert (o["rd"], o["rs1"]) == (2, 3)
    assert o["imm"] == isa.sext(-4, 6)
    assert (o["mem_read"], o["mem_write"]) == (1, 0)
    assert (o["alu_op"], o["alu_src_imm"], o["reg_write"]) == (isa.ALU_ADD, 1, 1)
    assert (o["rs1_used"], o["rs2_used"]) == (1, 0)
    assert (o["branch"], o["jump"], o["halt"], o["mul"]) == (0, 0, 0, 0)
    _assert_isa_match([word, isa.i_type(isa.OP_LD, 0, 15, 63)])


def test_store_fields():
    word = isa.s_type(isa.OP_ST, rs2=7, rs1=3, imm6=5)
    o = _one(word)
    assert (o["rs2"], o["rs1"]) == (7, 3)            # rs2 carries store data
    assert o["imm"] == 5
    assert (o["mem_read"], o["mem_write"]) == (0, 1)
    assert (o["alu_op"], o["alu_src_imm"], o["reg_write"]) == (isa.ALU_ADD, 1, 0)
    assert (o["rs1_used"], o["rs2_used"]) == (1, 1)
    _assert_isa_match([word, isa.s_type(isa.OP_ST, 1, 2, -8)])


def test_branch_conditions():
    pairs = ((isa.OP_BEQ, isa.COND_EQ), (isa.OP_BNE, isa.COND_NE),
             (isa.OP_BLT, isa.COND_LT), (isa.OP_BGE, isa.COND_GE))
    words = []
    for opcode, cond in pairs:
        word = isa.b_type(opcode, rs2=3, rs1=4, imm6=-7)
        words.append(word)
        o = _one(word)
        assert o["branch"] == 1
        assert o["branch_cond"] == cond
        assert (o["rs1"], o["rs2"]) == (4, 3)
        assert o["imm"] == isa.sext(-7, 6)
        assert o["alu_op"] == isa.ALU_SUB
        assert o["reg_write"] == 0
        assert (o["rs1_used"], o["rs2_used"]) == (1, 1)
        assert (o["jump"], o["jump_reg"], o["mem_read"], o["mem_write"]) == (0, 0, 0, 0)
    _assert_isa_match(words)


def test_jal_fields():
    word = isa.j_type(isa.OP_JAL, rd=11, imm10=-2)
    o = _one(word)
    assert o["rd"] == 11
    assert o["imm"] == isa.sext(-2, 10)
    assert (o["jump"], o["jump_reg"]) == (1, 0)
    assert o["reg_write"] == 1
    assert (o["rs1_used"], o["rs2_used"]) == (0, 0)   # JAL has no operands
    assert o["branch"] == 0
    _assert_isa_match([word, isa.j_type(isa.OP_JAL, 0, 0x1FF), isa.j_type(isa.OP_JAL, 15, 0)])


def test_jalr_fields():
    word = isa.i_type(isa.OP_JALR, rd=12, rs1=5, imm6=-6)
    o = _one(word)
    assert (o["rd"], o["rs1"]) == (12, 5)
    assert o["imm"] == isa.sext(-6, 6)               # no implicit +1
    assert (o["jump"], o["jump_reg"], o["reg_write"]) == (1, 1, 1)
    assert (o["alu_op"], o["alu_src_imm"]) == (isa.ALU_ADD, 1)
    assert (o["rs1_used"], o["rs2_used"]) == (1, 0)
    _assert_isa_match([word, isa.i_type(isa.OP_JALR, 0, 15, 63)])


def test_shift_kinds():
    words = []
    for kind, alu_op in ((isa.SHIFT_SLL, 4), (isa.SHIFT_SRL, 5),
                         (isa.SHIFT_SRA, 6), (isa.SHIFT_ROR, 7)):
        for amount in (0, 1, 15):
            imm6 = (kind << 4) | amount
            word = isa.i_type(isa.OP_SHIFT, rd=6, rs1=7, imm6=imm6)
            words.append(word)
            o = _one(word)
            assert o["alu_op"] == alu_op
            assert o["shift_amt"] == amount
            assert o["imm"] == imm6                  # zero-extended raw field
            assert o["alu_src_imm"] == 0
            assert (o["reg_write"], o["rs1_used"], o["rs2_used"]) == (1, 1, 0)
            assert (o["branch"], o["jump"], o["halt"]) == (0, 0, 0)
    _assert_isa_match(words)


def test_system_halt_nop_reserved():
    nop = isa.i_type(isa.OP_SYSTEM, 0, 0, isa.SYS_NOP)
    halt = isa.i_type(isa.OP_SYSTEM, 0, 0, isa.SYS_HALT)
    reserved = isa.i_type(isa.OP_SYSTEM, 3, 5, 2)
    o = _one(nop)
    assert o["halt"] == 0 and o["imm"] == 0 and o["reg_write"] == 0
    o = _one(halt)
    assert o["halt"] == 1 and o["imm"] == isa.SYS_HALT and o["reg_write"] == 0
    o = _one(reserved)
    assert o["halt"] == 0 and o["imm"] == 2          # reserved behaves as NOP
    assert (o["reg_write"], o["rs1_used"], o["rs2_used"]) == (0, 0, 0)
    _assert_isa_match([nop, halt, reserved,
                       isa.i_type(isa.OP_SYSTEM, 0, 0, 0x3F)])


# --- exhaustive-ish coverage and fuzz --------------------------------------

def test_every_opcode_sweep():
    rng = random.Random(0xC0FFEE)
    words = [(op << 14) | rng.getrandbits(14) for op in range(16) for _ in range(64)]
    assert {w >> 14 for w in words} == set(range(16))
    _assert_isa_match(words)


def test_seeded_fuzz_two_thousand_words():
    rng = random.Random(20261117)  # fixed seed: reproducible corpus
    words = [rng.getrandbits(isa.WIDTH) for _ in range(2000)]
    assert len(words) == 2000
    _assert_isa_match(words, chunks=10)


# --- synth wrapper ---------------------------------------------------------

def test_pack_layout_is_total_and_contiguous():
    assert PACK_WIDTH == 54
    assert [name for name, _, _ in PACK_LAYOUT] == list(FIELDS)
    offset = 0
    for name, off, width in PACK_LAYOUT:
        assert off == offset, f"{name} not contiguous"
        offset += width
    assert offset == PACK_WIDTH


def test_decoder_top_registers_input_and_pack():
    top = DecoderTop()
    assert top.ports[0] is top.clk
    assert top.ports == [top.clk, top.i_instr, top.o_pack]

    words = [isa.i_type(isa.OP_ADDI, 1, 2, -1),
             isa.r_type(isa.OP_ALU, isa.ALU_SUB, 4, 5, 6),
             isa.b_type(isa.OP_BLT, 3, 4, -7),
             isa.j_type(isa.OP_LUI, 9, 0x155),
             isa.i_type(isa.OP_SYSTEM, 0, 0, isa.SYS_HALT)]

    sim = Simulator(top)
    sim.add_clock(1e-6)
    seen = []

    async def tb(ctx):
        assert ctx.get(top.o_pack) == 0            # reset value
        prev = 0                                   # instruction in flight
        for word in words:
            ctx.set(top.i_instr, word)
            await ctx.tick()
            # input register captured; packed output still shows the old word
            assert ctx.get(top.o_pack) == _packed(isa.decode(prev))
            prev = word
            await ctx.tick()
            got = ctx.get(top.o_pack)
            assert got == _packed(isa.decode(word))
            seen.append(got)

    sim.add_testbench(tb)
    sim.run()
    assert seen == [_packed(isa.decode(w)) for w in words]
