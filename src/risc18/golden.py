"""Instruction-accurate golden model for the risc18 ISA (v1).

The model is the acceptance oracle for the RTL: tests run the same program on
both and compare the retired register-write trace (pc, rd, value) and the final
register/memory state.

Memory model: 16-bit word addresses, data memory decodes 0x0000..0x03FF.
Reads outside data memory return 0; writes outside are ignored.
"""

from .isa import (
    ALU_ADD, ALU_AND, ALU_OR, ALU_SUB, COND_EQ, COND_GE, COND_LT, COND_NE,
    MASK, MEM_WORDS, OP_ADDI, OP_ANDI, OP_ALU, OP_BEQ, OP_BGE, OP_BLT, OP_BNE,
    OP_JAL, OP_JALR, OP_LD, OP_LUI, OP_MUL, OP_ORI, OP_SHIFT, OP_ST, OP_SYSTEM,
    SHIFT_ROR, SHIFT_SLL, SHIFT_SRA, SHIFT_SRL, WIDTH, decode, to_signed,
)


def _alu(op: int, a: int, b: int) -> int:
    if op == ALU_ADD:
        return (a + b) & MASK
    if op == ALU_SUB:
        return (a - b) & MASK
    if op == ALU_AND:
        return a & b
    if op == ALU_OR:
        return a | b
    if op >= 4:  # SHIFT group: 4 SLL, 5 SRL, 6 SRA, 7 ROR
        kind = op - 4
        b &= 0xF  # amount is the 4-bit imm6[3:0] field (ISA section 4)
        if kind == SHIFT_SLL:
            return (a << b) & MASK
        if kind == SHIFT_SRL:
            return (a & MASK) >> b
        if kind == SHIFT_SRA:
            return (to_signed(a) >> b) & MASK
        if kind == SHIFT_ROR:
            return ((a >> b) | (a << (WIDTH - b))) & MASK if b else a & MASK
        raise ValueError(f"bad shift kind {kind}")
    raise ValueError(f"bad alu op {op}")


def _cond(cond: int, a: int, b: int) -> bool:
    if cond == COND_EQ:
        return a == b
    if cond == COND_NE:
        return a != b
    if cond == COND_LT:
        return to_signed(a) < to_signed(b)
    if cond == COND_GE:
        return to_signed(a) >= to_signed(b)
    raise ValueError(f"bad cond {cond}")


class Machine:
    """One risc18 hart, instruction accurate."""

    def __init__(self, program=(), mem_words: int = MEM_WORDS):
        self.program = [w & MASK for w in program]
        self.regs = [0] * 16
        self.mem = [0] * mem_words
        self.pc = 0
        self.halted = False
        self.retired = 0
        self.trace = []  # (pc, rd, value) for every architectural write, rd != 0

    # -- memory ---------------------------------------------------------
    def read_mem(self, addr: int) -> int:
        addr &= 0xFFFF
        return self.mem[addr] if addr < len(self.mem) else 0

    def write_mem(self, addr: int, value: int) -> None:
        addr &= 0xFFFF
        if addr < len(self.mem):
            self.mem[addr] = value & MASK

    def fetch(self, pc: int) -> int:
        return self.program[pc] if 0 <= pc < len(self.program) else 0

    # -- execution ------------------------------------------------------
    def step(self):
        """Execute one instruction, return the list of writes it performed."""
        if self.halted:
            return []
        pc = self.pc
        d = decode(self.fetch(pc))
        a = self.regs[d.rs1]
        b = self.regs[d.rs2]
        writes = []
        next_pc = (pc + 1) & 0xFFFF
        result = None

        if d.mul:
            result = (a * b) & MASK
        elif d.alu_op >= 4:  # SHIFT group
            result = _alu(d.alu_op, a, d.shift_amt)
        elif d.op in (OP_ADDI, OP_ANDI, OP_ORI, OP_LD, OP_ST, OP_JALR, OP_BEQ,
                      OP_BNE, OP_BLT, OP_BGE, OP_ALU):
            result = _alu(d.alu_op, a, d.imm if d.alu_src_imm else b)
        elif d.op == OP_LUI:
            result = d.imm

        if d.op in (OP_BEQ, OP_BNE, OP_BLT, OP_BGE):
            if _cond(d.branch_cond, a, b):
                next_pc = (pc + 1 + to_signed(d.imm)) & 0xFFFF
        elif d.op == OP_JAL:
            next_pc = (pc + 1 + to_signed(d.imm)) & 0xFFFF
            result = (pc + 1) & 0xFFFF
        elif d.op == OP_JALR:
            next_pc = result & 0xFFFF
            result = (pc + 1) & 0xFFFF

        if d.op == OP_LD:
            result = self.read_mem(result if result is not None else 0)
        elif d.op == OP_ST:
            self.write_mem(result, b)

        if d.halt:
            self.halted = True

        if d.reg_write and d.rd != 0:
            value = result & MASK if result is not None else 0
            self.regs[d.rd] = value
            writes.append((pc, d.rd, value))
            self.trace.append((pc, d.rd, value))

        self.pc = next_pc
        self.retired += 1
        return writes

    def run(self, max_instructions: int = 100_000):
        while not self.halted and self.retired < max_instructions:
            self.step()
        return self


def run_program(program, max_instructions: int = 100_000, mem_words: int = MEM_WORDS):
    return Machine(program, mem_words).run(max_instructions)
