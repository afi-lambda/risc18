"""Program counter and branch/jump target generation for the risc18 ISA (M3).

Interface and behaviour are frozen by ``docs/ISA.md`` sections 3 and 5 and by
the milestone M3 acceptance criteria in ``docs/MILESTONES.md``.

:class:`BranchTarget` is purely combinational.  It resolves the three control
transfers of the ISA from the current word address:

* conditional branches (BEQ/BNE/BLT/BGE) and JAL use
  ``(pc + 1 + signed(imm)) & 0xFFFF`` — ``pc + 1`` is the next instruction and
  the offset is relative to *it*;
* JALR (``i_jump_reg`` high) uses ``(rs1 + signed(imm)) & 0xFFFF`` — the link
  register already holds the return address, so there is no ``+ 1``.

``i_imm`` is the 18-bit immediate the decoder already produced
(``docs/ISA.md`` section 2): sign-extended for B and J formats, sign-extended
``imm6`` for JALR.  It is interpreted here as two's complement, so
``0x3FFFE`` is ``-2``.

:class:`PCUnit` is the architectural PC register: a 16-bit synchronous counter
with a redirect (branch/jump) input that takes priority over the sequential
increment, and a hold input that freezes the PC for stalled or halted fetch.
Both the increment and the redirect wrap modulo 2^16.  All storage lives in
``m.d.sync``, so the synchronous ``sync`` domain reset clears the PC to 0
without extra logic.

Flush/squash of in-flight instructions belongs to the pipeline (M5+), not
here: this milestone is the PC register plus the next-PC mux.
"""

from amaranth import Elaboratable, Module, Mux, Signal, signed

from . import isa

__all__ = ["BranchTarget", "PCUnit"]

PC_WIDTH = 16
PC_MASK = (1 << PC_WIDTH) - 1


class BranchTarget(Elaboratable):
    """Combinational branch/JAL/JALR target: ``i_pc``/``i_rs1`` + signed imm."""

    def __init__(self):
        self.i_pc = Signal(PC_WIDTH)
        self.i_imm = Signal(isa.WIDTH)
        self.i_rs1 = Signal(isa.WIDTH)
        self.i_jump_reg = Signal(1)
        self.o_target = Signal(PC_WIDTH)

    def elaborate(self, platform):
        m = Module()

        imm_s = self.i_imm.as_signed()

        # pc + 1 in 17 bits (0xFFFF + 1 = 0x10000), sign-extended into a 19-bit
        # signed sum.  Reading pc_next = 0x10000 as -65536 is immaterial
        # because only the low 16 bits of the sum are consumed and 2^17 is a
        # multiple of 2^16.
        pc_next = Signal(PC_WIDTH + 1)
        m.d.comb += pc_next.eq(self.i_pc + 1)
        rel_sum = Signal(signed(isa.WIDTH + 1))
        m.d.comb += rel_sum.eq(pc_next.as_signed() + imm_s)

        # JALR: rs1 (18-bit architectural value) plus the signed immediate.
        # Both are 18 bits unsigned, so a 19-bit accumulator cannot overflow.
        abs_sum = Signal(isa.WIDTH + 1)
        m.d.comb += abs_sum.eq(self.i_rs1 + self.i_imm)

        m.d.comb += self.o_target.eq(
            Mux(self.i_jump_reg, abs_sum[:PC_WIDTH], rel_sum[:PC_WIDTH])
        )

        return m


class PCUnit(Elaboratable):
    """16-bit synchronous PC: redirect > hold > increment, wraps at 2^16."""

    def __init__(self):
        self.i_redirect = Signal(1)
        self.i_target = Signal(PC_WIDTH)
        self.i_hold = Signal(1)
        self.o_pc = Signal(PC_WIDTH)

    def elaborate(self, platform):
        m = Module()

        pc = Signal(PC_WIDTH)
        m.d.comb += self.o_pc.eq(pc)

        with m.If(self.i_redirect):
            m.d.sync += pc.eq(self.i_target)
        with m.Elif(self.i_hold):
            m.d.sync += pc.eq(pc)
        with m.Else():
            m.d.sync += pc.eq(pc + 1)

        return m
