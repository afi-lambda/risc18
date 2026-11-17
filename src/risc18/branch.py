"""Branch condition evaluation for the risc18 ISA (M2).

Interface and behaviour are frozen by ``docs/ISA.md`` sections 4 and 5:

* ``i_cond`` selects the predicate: 0 EQ, 1 NE, 2 LT, 3 GE;
* ``o_taken`` is the single-bit result, purely combinational;
* LT and GE are *signed* comparisons over the full 18-bit word (two's
  complement), so ``0x3FFFF`` (-1) is less than ``0`` and ``0x20000``
  (-2^17) is the smallest value.

The datapath feeds ``rs1``/``rs2`` straight in; the branch offset, PC and
flush logic belong to the PC unit (M3), not here.
"""

from amaranth import Elaboratable, Module, Signal

from . import isa

__all__ = ["BranchUnit"]


class BranchUnit(Elaboratable):
    """Combinational branch predicate: EQ/NE equality, LT/GE signed."""

    def __init__(self):
        self.i_a = Signal(isa.WIDTH)
        self.i_b = Signal(isa.WIDTH)
        self.i_cond = Signal(2)
        self.o_taken = Signal(1)

    def elaborate(self, platform):
        m = Module()

        a = self.i_a
        b = self.i_b
        a_s = a.as_signed()
        b_s = b.as_signed()

        lt = Signal()
        eq = Signal()
        m.d.comb += [
            eq.eq(a == b),
            lt.eq(a_s < b_s),
        ]

        with m.Switch(self.i_cond):
            with m.Case(isa.COND_EQ):
                m.d.comb += self.o_taken.eq(eq)
            with m.Case(isa.COND_NE):
                m.d.comb += self.o_taken.eq(~eq)
            with m.Case(isa.COND_LT):
                m.d.comb += self.o_taken.eq(lt)
            with m.Case(isa.COND_GE):
                m.d.comb += self.o_taken.eq(~lt)
            with m.Default():
                # Unreachable with a 2-bit condition, kept for the project's
                # Default rule: behave like EQ.
                m.d.comb += self.o_taken.eq(eq)

        return m
