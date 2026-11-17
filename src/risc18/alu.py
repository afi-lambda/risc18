"""18-bit ALU for the risc18 ISA (M2).

Interface and behaviour are frozen by ``docs/ISA.md`` sections 4 and 5:

* ``i_a``/``i_b`` are 18-bit operands, ``i_op`` selects the function and
  ``o_y`` is the 18-bit result — every operation wraps modulo 2^18;
* the eight ``alu_op`` encodings are 0 ADD, 1 SUB, 2 AND, 3 OR, 4 SLL,
  5 SRL, 6 SRA, 7 ROR;
* the shift amount is ``i_b[3:0]``, which the datapath supplies from the
  decoded ``shift_amt`` field (SHIFT instructions) — the shift kinds never
  see a wider amount, so amounts are 0..15 and a shift by 0 is the identity;
* SRA sign-fills from bit 17, ROR rotates right inside the 18-bit word.

ADD and SUB use Amaranth's plain ``+``/``-`` operators.  Yosys maps exactly
that form onto the ECP5 carry chain (``CCU2C``); ``MULT18X18D`` cells come only
from the M8 multiply path, since ``synth_ecp5`` inserts DSPs from ``$mul``, not
from adder signedness or width.
"""

from amaranth import Elaboratable, Module, Signal

from . import isa

__all__ = ["ALU"]

#: ``i_op`` encodings, spelled out for readability at every use site.
OP_ADD = isa.ALU_ADD          # 0
OP_SUB = isa.ALU_SUB          # 1
OP_AND = isa.ALU_AND          # 2
OP_OR = isa.ALU_OR            # 3
OP_SLL = 4
OP_SRL = 5
OP_SRA = 6
OP_ROR = 7


class ALU(Elaboratable):
    """Combinational 18-bit ALU: 8 functions, shift amount ``i_b[3:0]``."""

    def __init__(self):
        self.i_a = Signal(isa.WIDTH)
        self.i_b = Signal(isa.WIDTH)
        self.i_op = Signal(3)
        self.o_y = Signal(isa.WIDTH)

    def elaborate(self, platform):
        m = Module()

        a = self.i_a
        b = self.i_b
        amt = b[:4]                 # shift amount, 0..15

        # ROR needs the complement of the amount to bring the wrapped-out low
        # bits back in at the top.  ``18 - amt`` is 5 bits wide (values 3..18);
        # a shift of 18 or more leaves nothing in an 18-bit result, so amt == 0
        # degenerates to ``a | 0`` and is the identity as required.
        rot_left = Signal(5)
        m.d.comb += rot_left.eq(isa.WIDTH - amt)

        with m.Switch(self.i_op):
            with m.Case(OP_ADD):
                m.d.comb += self.o_y.eq(a + b)
            with m.Case(OP_SUB):
                m.d.comb += self.o_y.eq(a - b)
            with m.Case(OP_AND):
                m.d.comb += self.o_y.eq(a & b)
            with m.Case(OP_OR):
                m.d.comb += self.o_y.eq(a | b)
            with m.Case(OP_SLL):
                m.d.comb += self.o_y.eq(a << amt)
            with m.Case(OP_SRL):
                m.d.comb += self.o_y.eq(a >> amt)
            with m.Case(OP_SRA):
                # Arithmetic shift: Amaranth sign-fills ``>>`` on signed values.
                m.d.comb += self.o_y.eq(a.as_signed() >> amt)
            with m.Case(OP_ROR):
                m.d.comb += self.o_y.eq((a >> amt) | (a << rot_left))
            with m.Default():
                # Unreachable with a 3-bit op, but every switch carries a
                # Default (project rule): fall back to ADD.
                m.d.comb += self.o_y.eq(a + b)

        return m
