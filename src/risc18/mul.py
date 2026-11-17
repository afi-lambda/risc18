"""Explicit ECP5 DSP multiplier for the risc18 MUL (M8).

``docs/ISA.md`` section 2 fixes the MUL result as ``rd = (rs1 * rs2)[17:0]``.
Only the low 18 bits of the 18x18 product are architecturally visible, and
those bits are the same whether the operands are read as unsigned or as signed
two's-complement values, so the DSP is instantiated unsigned
(``SIGNEDA = SIGNEDB = 0``) and just ``P[17:0]`` is used.

The unit has two elaborations of the same function, and the split is
deliberate:

``cell=False`` (default)
    Behavioural Amaranth: ``(i_a * i_b)[:18]``, with ``o_y`` registered when
    ``registered=True``.  This is the simulatable form -- Amaranth's
    pure-Python simulator has no model for a vendor ``Instance`` and drives its
    outputs as 0, so a hard cell cannot be exercised in ``amaranth.sim`` -- and
    it is what :class:`risc18.core.Core` uses, which keeps every core test
    simulatable.  ``synth_ecp5`` infers exactly one ``MULT18X18D`` from it
    (yosys ``$mul`` -> ``$__MUL18X18`` -> ``dsp_map.v``), which is why the
    ``m8_core`` metric carries ``MULT18X18D = 1`` with no LUT multiplier.

``cell=True``
    An explicit ``MULT18X18D`` primitive.  This is the form the
    :class:`risc18.tops.MulTop` / :class:`risc18.tops.MulRegTop` measurement
    tops elaborate, because only the primitive exposes the DSP's own output
    register: ``registered=True`` selects ``REG_OUTPUT_CLK = "CLK0"`` instead
    of ``"NONE"``, so the product register lives inside the DSP block rather
    than in 18 fabric flip-flops.

Ports are ``i_a`` (18), ``i_b`` (18) and ``o_y`` (18, low half of the product).
There is no clock port: the registered variant's DSP register is clocked from
the enclosing ``sync`` domain (``ClockSignal("sync")``), which every user of
this unit -- the core and both measurement tops -- declares.

Latency:

* ``registered=False``: ``o_y`` is valid in the cycle the operands are present
  (combinational, one instruction per cycle, nothing to stall);
* ``registered=True``: ``o_y`` is the product of the operands that were
  present on the *previous* clock edge -- one edge of latency -- so a consumer
  must hold the operators for one cycle (a one-cycle stall, the "one or two
  cycles" latitude of the design brief, not an ISA provision).

Netlist evidence, read from the synthesised ``build/synth/*/top.json``:

=================  =================  =================
parameter          registered=False   registered=True
=================  =================  =================
REG_INPUTA_CLK     ``NONE``           ``NONE``
REG_INPUTB_CLK     ``NONE``           ``NONE``
REG_PIPELINE_CLK   ``NONE``           ``NONE``
REG_OUTPUT_CLK     ``NONE``           ``CLK0``
CLK0 / CE0 / RST0  unconnected        ``sync`` clk, 1, 0
SIGNEDA / SIGNEDB  0 (unsigned)       0 (unsigned)
C0..C17            0                  0
=================  =================  =================

``P`` is 36 bits; all 36 are wired to a local bus but only ``P[17:0]`` is read, so the unused high half of
the product is dropped inside the block; ``DP16KD`` stays 0 for this unit (it
has no memory) and no ``CCU2C``/``LUT4`` multiplier appears besides the cell's
own fixed logic.
"""

from amaranth import ClockSignal, Elaboratable, Instance, Module, Signal

from . import isa

__all__ = ["Multiplier", "PRODUCT_WIDTH"]

#: Full 18x18 product width: the DSP's ``P`` port is 36 bits wide.
PRODUCT_WIDTH = 2 * isa.WIDTH


class Multiplier(Elaboratable):
    """18x18 ECP5 DSP multiply, combinational or one-edge registered."""

    def __init__(self, registered=False, *, cell=False):
        #: ``True``: the product comes from the DSP's output register and is
        #: therefore valid one clock edge after the operands.
        self.registered = bool(registered)
        #: ``True``: instantiate the ``MULT18X18D`` primitive (synthesis and
        #: netlist evidence).  ``False``: behavioural RTL (simulation).
        self.cell = bool(cell)
        self.i_a = Signal(isa.WIDTH)
        self.i_b = Signal(isa.WIDTH)
        self.o_y = Signal(isa.WIDTH)

    def elaborate(self, platform):
        m = Module()
        if self.cell:
            self._elaborate_cell(m)
        else:
            self._elaborate_behavioural(m)
        return m

    # -- behavioural model (simulation, DSP inferred in synthesis) ---------
    def _elaborate_behavioural(self, m):
        if self.registered:
            # One edge of latency: ``o_y`` holds the product of the operands
            # sampled at the previous edge until the next one.
            m.d.sync += self.o_y.eq((self.i_a * self.i_b)[:isa.WIDTH])
        else:
            m.d.comb += self.o_y.eq((self.i_a * self.i_b)[:isa.WIDTH])

    # -- explicit primitive (netlist evidence) -----------------------------
    def _elaborate_cell(self, m):
        p = Signal(PRODUCT_WIDTH)
        ports = {}
        for i in range(isa.WIDTH):
            ports[f"i_A{i}"] = self.i_a[i]
            ports[f"i_B{i}"] = self.i_b[i]
            ports[f"i_C{i}"] = 0          # no accumulate: P = A * B
        for i in range(PRODUCT_WIDTH):
            ports[f"o_P{i}"] = p[i]
        ports.update(i_SIGNEDA=0, i_SIGNEDB=0, i_SOURCEA=0, i_SOURCEB=0)
        ports.update(
            p_REG_INPUTA_CLK="NONE",
            p_REG_INPUTB_CLK="NONE",
            p_REG_INPUTC_CLK="NONE",
            p_REG_PIPELINE_CLK="NONE",
            p_REG_OUTPUT_CLK="CLK0" if self.registered else "NONE",
        )
        # CLK0/CE0/RST0 are always connected: if any REG_*_CLK is ever set the
        # register must not be clocked by an undriven net.
        # RST0 is 0, so the DSP's own product register is NOT cleared by the
        # enclosing reset whereas the behavioural form is: a registered consumer
        # must discard its first product after a reset (no user does today).
        ports.update(i_CLK0=ClockSignal("sync"), i_CE0=1, i_RST0=0)
        m.submodules.dsp = Instance("MULT18X18D", **ports)
        m.d.comb += self.o_y.eq(p[:isa.WIDTH])
