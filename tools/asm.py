"""Minimal two-pass assembler for risc18 test programs.

Syntax (word addressed, labels are instruction indices):

    start:
        addi r1, r0, 5
        add  r2, r1, r1
        beq  r1, r0, done
        ld   r3, -2(r1)        ; base + signed 6-bit offset
        st   r3, 4(r2)
        jal  r4, start
        jalr r5, r1, 0
        lui  r6, 0x155         ; imm10, shifted left by 8
        mul  r7, r2, r3
        sll  r8, r1, 3
        nop
        halt
    done:
"""

import re

from risc18.isa import (
    ALU_ADD, ALU_AND, ALU_OR, ALU_SUB, OP_ADDI, OP_ALU, OP_ANDI, OP_BEQ,
    OP_BGE, OP_BLT, OP_BNE, OP_JAL, OP_JALR, OP_LD, OP_LUI, OP_MUL, OP_ORI,
    OP_SHIFT, OP_ST, OP_SYSTEM, SHIFT_ROR, SHIFT_SLL, SHIFT_SRA, SHIFT_SRL,
    SYS_HALT, SYS_NOP, b_type, i_type, j_type, r_type, s_type,
)

_R = re.compile(r"^([a-z]+)(?:\s+(.*))?$")
_MEM = re.compile(r"^([-\w]+)\s*\(\s*(r\d+)\s*\)$")
_REG = re.compile(r"^r(\d+)$")


def _reg(tok: str) -> int:
    m = _REG.match(tok.strip())
    if not m or not 0 <= int(m.group(1)) < 16:
        raise ValueError(f"bad register {tok!r}")
    return int(m.group(1))


def _imm(tok: str, labels: dict, idx: int, bits: int, relative: bool) -> int:
    tok = tok.strip()
    if tok in labels:
        value = labels[tok] - idx - 1 if relative else labels[tok]
    else:
        value = int(tok, 0)
    lo, hi = -(1 << (bits - 1)), (1 << (bits - 1)) - 1
    if not lo <= value <= hi:
        raise ValueError(f"immediate {value} out of range for {bits} bits")
    return value


def _split_args(text: str):
    return [a.strip() for a in text.split(",")]


def assemble(text: str):
    """Assemble source text, return a list of 18-bit words."""
    lines = []
    for raw in text.splitlines():
        line = raw.split(";")[0].split("#")[0].strip()
        if not line:
            continue
        if line.endswith(":"):
            lines.append(("label", line[:-1].strip(), None))
            continue
        m = _R.match(line)
        if not m:
            raise ValueError(f"cannot parse {raw!r}")
        lines.append(("insn", m.group(1).lower(), m.group(2)))

    labels = {}
    idx = 0
    for kind, name, args in lines:
        if kind == "label":
            labels[name] = idx
        else:
            idx += 1

    words = []
    idx = 0
    for kind, name, args in lines:
        if kind == "label":
            continue
        a = _split_args(args) if args else []
        op = name
        if op == "nop":
            w = i_type(OP_SYSTEM, 0, 0, SYS_NOP)
        elif op == "halt":
            w = i_type(OP_SYSTEM, 0, 0, SYS_HALT)
        elif op in ("add", "sub", "and", "or"):
            fn = {"add": ALU_ADD, "sub": ALU_SUB, "and": ALU_AND, "or": ALU_OR}[op]
            w = r_type(OP_ALU, fn, _reg(a[0]), _reg(a[1]), _reg(a[2]))
        elif op == "mul":
            w = r_type(OP_MUL, 0, _reg(a[0]), _reg(a[1]), _reg(a[2]))
        elif op in ("addi", "andi", "ori"):
            opc = {"addi": OP_ADDI, "andi": OP_ANDI, "ori": OP_ORI}[op]
            imm = _imm(a[2], labels, idx, 6, relative=False)
            w = i_type(opc, _reg(a[0]), _reg(a[1]), imm)
        elif op == "ld":
            m = _MEM.match(a[1])
            if not m:
                raise ValueError(f"bad ld operand {a[1]!r}")
            w = i_type(OP_LD, _reg(a[0]), _reg(m.group(2)),
                       _imm(m.group(1), labels, idx, 6, relative=False))
        elif op == "st":
            m = _MEM.match(a[1])
            if not m:
                raise ValueError(f"bad st operand {a[1]!r}")
            w = s_type(OP_ST, _reg(a[0]), _reg(m.group(2)),
                       _imm(m.group(1), labels, idx, 6, relative=False))
        elif op in ("beq", "bne", "blt", "bge"):
            opc = {"beq": OP_BEQ, "bne": OP_BNE, "blt": OP_BLT, "bge": OP_BGE}[op]
            imm = _imm(a[2], labels, idx, 6, relative=True)
            w = b_type(opc, _reg(a[1]), _reg(a[0]), imm)
        elif op == "jal":
            imm = _imm(a[1], labels, idx, 10, relative=True)
            w = j_type(OP_JAL, _reg(a[0]), imm)
        elif op == "jalr":
            imm = _imm(a[2], labels, idx, 6, relative=False)
            w = i_type(OP_JALR, _reg(a[0]), _reg(a[1]), imm)
        elif op == "lui":
            imm = _imm(a[1], labels, idx, 10, relative=False)
            w = j_type(OP_LUI, _reg(a[0]), imm)
        elif op in ("sll", "srl", "sra", "ror"):
            kind = {"sll": SHIFT_SLL, "srl": SHIFT_SRL,
                    "sra": SHIFT_SRA, "ror": SHIFT_ROR}[op]
            amt = _imm(a[2], labels, idx, 4, relative=False)
            w = i_type(OP_SHIFT, _reg(a[0]), _reg(a[1]), (kind << 4) | amt)
        else:
            raise ValueError(f"unknown mnemonic {op!r}")
        words.append(w)
        idx += 1
    return words


def assemble_file(path):
    with open(path) as fh:
        return assemble(fh.read())
