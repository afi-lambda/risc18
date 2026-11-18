"""Explicit ECP5 ``EHXPLLL`` clock generator for the risc18 full top (M9b).

The Colorlight 5A-75B feeds the FPGA a 25 MHz oscillator; the M9 core needs a
60 MHz clock to be exercised at the frequency it was closed at.  Amaranth has
no ECP5 PLL primitive (no ``pll`` module the way ``amaranth.vendor`` has for
Xilinx/ICE40), and the open flow has no ``ecppll`` step, so the primitive is
instantiated by hand here, the same way :mod:`risc18.mul` instantiates
``MULT18X18D``.

Configuration (25 MHz reference, ``f_out = f_in * CLKFB_DIV / (CLKI_DIV *
CLKOP_DIV)``, ``f_VCO = f_in * CLKFB_DIV / CLKI_DIV``)::

    CLKI_DIV   = 1     -> f_PFD = 25.000 MHz
    CLKFB_DIV  = 24    -> f_VCO = 600.000 MHz
    CLKOP_DIV  = 10    -> f_CLKOP = 60.000 MHz

60.0 MHz is reachable *exactly*: 25 * 24 / (1 * 10) = 60.000 MHz, so the output
frequency error is 0 Hz (0 ppm).  The search that produced these values, and
why the obvious smaller pairs do not work:

* ``CLKOP_DIV`` must be an integer, so the VCO must be an exact multiple of
  60 MHz; the legal ECP5 VCO band is 400-800 MHz, i.e. 420, 480, ..., 780 MHz.
* ``f_VCO = 25 * CLKFB_DIV / CLKI_DIV`` with integer dividers, and
  ``CLKFB_DIV <= 80``, so with ``CLKI_DIV = 1`` the smallest exact solution is
  ``f_VCO = 600 MHz`` (``CLKFB_DIV = 24``, ``CLKOP_DIV = 10``).  ``f_VCO = 300
  MHz`` (``CLKFB_DIV = 12``, ``CLKOP_DIV = 5``) would give 60 MHz too but sits
  below the VCO floor, and ``CLKFB_DIV = 96`` for 480 MHz exceeds the 80
  counter limit.
* ``CLKI_DIV = 2`` (``CLKFB_DIV = 48``, ``CLKOP_DIV = 10``) is an equally exact
  alternative; ``CLKI_DIV = 1`` is kept because it puts the phase detector at
  25 MHz, comfortably inside the 10-400 MHz PFD band, and leaves the widest
  VCO tuning margin on both sides of 600 MHz.

The module validates the resulting PFD/VCO/output frequencies against those
bands at elaboration time and raises :class:`ValueError` for a configuration
that trellis/ecppack would reject, so a bad ``Pll(...)`` fails in Python rather
than in the bitstream generator.

Port names, and how they were verified against the installed cell libraries
--------------------------------------------------------------------------------
``yosys``' ``synth_ecp5`` starts with::

    read_verilog -lib -specify +/ecp5/cells_sim.v +/ecp5/cells_bb.v

so the blackbox declaration is what the flow matches.  On this machine
(``yosys -V`` = 0.52, ``/usr/share/yosys``)::

    grep -n "module EHXPLLL" -A 40 /usr/share/yosys/ecp5/cells_bb.v

gives the port list used below, verbatim: ``CLKI, CLKFB, PHASESEL1,
PHASESEL0, PHASEDIR, PHASESTEP, PHASELOADREG, STDBY, PLLWAKESYNC, RST,
ENCLKOP, ENCLKOS, ENCLKOS2, ENCLKOS3`` in, ``CLKOP, CLKOS, CLKOS2, CLKOS3,
LOCK, INTLOCK, REFCLK, CLKINTFB`` out, plus the ``CLKI_DIV``/``CLKFB_DIV``/
``CLKOP_DIV``/``CLKOS*_DIV``/``*_CPHASE``/``*_FPHASE``/``*_ENABLE``/
``FEEDBK_PATH``/``*_TRIM_*``/``OUTDIVIDER_MUX*``/``PLL_LOCK_*``/``STDBY_ENABLE``/
``REFIN_RESET``/``SYNC_ENABLE``/``INT_LOCK_STICKY``/``DPHASE_SOURCE``/
``PLLRST_ENA``/``INTFB_WAKE`` parameters.  ``grep -n EHXPLLL
/usr/share/yosys/ecp5/cells_sim.v`` returns nothing: unlike the DSP, this
primitive has *no* behavioural model in the Yosys ECP5 library, so it cannot be
simulated -- the same reason the DSP tops are synthesis-only.  The legacy copy
``/usr/share/yosys/lattice/cells_bb_ecp5.v`` (line 1492) declares the same
ports and parameters, which is the cross-check that the two files agree.

Every port of this cell is one bit wide, so there is nothing to bit-blast here
(unlike ``MULT18X18D``'s 18-bit ``A``/``B`` buses, which :mod:`risc18.mul`
splits into ``i_A0``...).  Amaranth emits ``i_<PORT>``/``o_<PORT>`` for
single-bit connections and ``p_<PARAM>`` for parameters, i.e. exactly the names
the blackbox uses once the port direction prefix is stripped; the string
parameters are emitted as Verilog strings (``"ENABLED"``), which is what
ecppack parses when it writes the PLL configuration bits.

Wiring decisions and their reason:

* ``FEEDBK_PATH = "CLKOP"`` -- CLKOP is fed back inside the block, so the
  external ``CLKFB`` port is tied to 0 and no clock net has to leave and
  re-enter the primitive.
* ``i_RST = 0`` and ``PLLRST_ENA = "DISABLED"`` -- the PLL free-starts with the
  FPGA and reports readiness on ``LOCK``; the top holds the core in reset until
  ``LOCK`` is high (see :class:`risc18.tops.BootReset`), so a second reset path
  into the PLL would only add a way to lose lock silently.
* ``ENCLKOP = 1``, ``ENCLKOS* = 0`` -- only CLKOP is used; the other three
  outputs are disabled and left unconnected.
* ``INT_LOCK_STICKY = "ENABLED"`` -- once locked, a transient loss of lock does
  not latch the design off.
* ``i_STDBY = 0``/``STDBY_ENABLE = "DISABLED"``, ``DPHASE_SOURCE =
  "DISABLED"``, ``SYNC_ENABLE = "DISABLED"``, ``REFIN_RESET = "DISABLED"``,
  ``INTFB_WAKE = "DISABLED"`` -- all dynamic-phase/standby/refin features off:
  the top only needs a fixed 25 -> 60 MHz clock.
* Phase/duty trims are 0 and ``CLKOP_CPHASE = 0``: no static phase shift, and
  the PLL's fixed 50 % duty on CLKOP is what the core wants.

The netlist evidence for the metric run is ``build/synth/m9_full/top.json``:
exactly one ``EHXPLLL`` cell, with the parameters above.
"""

from amaranth import Elaboratable, Instance, Module, Signal

__all__ = ["Pll", "F_IN_MHZ", "CLKI_DIV", "CLKFB_DIV", "CLKOP_DIV",
           "F_OUT_MHZ"]

#: Reference clock on the Colorlight 5A-75B (the board's 25 MHz oscillator).
F_IN_MHZ = 25.0

#: Divider set that turns 25 MHz into 60.000 MHz exactly (see the module
#: docstring for the search and the rejected alternatives).
CLKI_DIV = 1
CLKFB_DIV = 24
CLKOP_DIV = 10

#: Output frequency of the default configuration, in MHz.
F_OUT_MHZ = F_IN_MHZ * CLKFB_DIV / (CLKI_DIV * CLKOP_DIV)

#: Legal ECP5 sysCLOCK PLL bands (datasheet), used to reject bad settings.
PFD_MIN_MHZ, PFD_MAX_MHZ = 10.0, 400.0
VCO_MIN_MHZ, VCO_MAX_MHZ = 400.0, 800.0
DIV_MAX = 80
CLKOP_DIV_MAX = 128


class Pll(Elaboratable):
    """One ECP5 ``EHXPLLL``: ``i_clk`` (25 MHz) -> ``o_clk`` (60 MHz) + LOCK.

    ``o_clk`` is the CLKOP output and must drive a global clock net -- the
    enclosing top does that by wiring it to a clock domain's ``clk``, nextpnr
    then promotes it to a primary clock.  ``o_locked`` is the raw ``LOCK``
    output: high once the PLL has locked, and the reset sequencer in
    :class:`risc18.tops.FullTop` releases the core only while it is high.

    The unit is synthesis-only: ``EHXPLLL`` has no model in the Yosys ECP5
    library, and Amaranth's pure-Python simulator drives ``Instance`` outputs
    to 0, so ``o_locked`` reads 0 and ``o_clk`` does not toggle in simulation.
    Nothing in the test suite simulates this class; the reset gating it feeds
    is simulated through :class:`risc18.tops.BootReset`, which is plain RTL.
    """

    def __init__(self, *, clki_div=CLKI_DIV, clkfb_div=CLKFB_DIV,
                 clkop_div=CLKOP_DIV, f_in_mhz=F_IN_MHZ):
        for name, value, limit in (("clki_div", clki_div, DIV_MAX),
                                   ("clkfb_div", clkfb_div, DIV_MAX),
                                   ("clkop_div", clkop_div, CLKOP_DIV_MAX)):
            if not isinstance(value, int) or not 1 <= value <= limit:
                raise ValueError(f"{name} must be an int in 1..{limit}, "
                                 f"got {value!r}")
        if f_in_mhz <= 0:
            raise ValueError(f"f_in_mhz must be positive, got {f_in_mhz!r}")

        self.clki_div = clki_div
        self.clkfb_div = clkfb_div
        self.clkop_div = clkop_div
        self.f_in_mhz = float(f_in_mhz)

        f_pfd = self.f_pfd_mhz
        f_vco = self.f_vco_mhz
        if not PFD_MIN_MHZ <= f_pfd <= PFD_MAX_MHZ:
            raise ValueError(f"phase-detector frequency {f_pfd} MHz outside "
                             f"{PFD_MIN_MHZ}-{PFD_MAX_MHZ} MHz")
        if not VCO_MIN_MHZ <= f_vco <= VCO_MAX_MHZ:
            raise ValueError(f"VCO frequency {f_vco} MHz outside "
                             f"{VCO_MIN_MHZ}-{VCO_MAX_MHZ} MHz")

        #: 25 MHz reference from the board oscillator (CLKI).
        self.i_clk = Signal()
        #: CLKOP: ``f_out_mhz``, 50 % duty, no phase shift.
        self.o_clk = Signal()
        #: LOCK: high once the loop has locked (INT_LOCK_STICKY, so it latches).
        self.o_locked = Signal()

    # -- computed frequencies (for the reports and the tests) --------------
    @property
    def f_pfd_mhz(self):
        """Phase-detector (PFD) frequency, MHz."""
        return self.f_in_mhz / self.clki_div

    @property
    def f_vco_mhz(self):
        """VCO frequency, MHz."""
        return self.f_in_mhz * self.clkfb_div / self.clki_div

    @property
    def f_out_mhz(self):
        """CLKOP frequency, MHz."""
        return self.f_vco_mhz / self.clkop_div

    @property
    def error_hz(self):
        """Absolute error of CLKOP against the requested 60.000 MHz, in Hz."""
        return abs(self.f_out_mhz - 60.0) * 1e6

    # -- elaboration -------------------------------------------------------
    def elaborate(self, platform):
        m = Module()
        ports = dict(
            # Reference and feedback.  CLKFB is unused with FEEDBK_PATH=CLKOP,
            # but the input must be connected: an undriven input on a blackbox
            # makes yosys/nextpnr warn about an undriven net.
            i_CLKI=self.i_clk,
            i_CLKFB=0,
            # Dynamic phase control: off (DPHASE_SOURCE=DISABLED).
            i_PHASESEL1=0, i_PHASESEL0=0, i_PHASEDIR=0, i_PHASESTEP=0,
            i_PHASELOADREG=0,
            # Standby: off (STDBY_ENABLE=DISABLED).
            i_STDBY=0, i_PLLWAKESYNC=0,
            # No reset into the PLL (PLLRST_ENA=DISABLED); LOCK is the only
            # handshake the top uses.
            i_RST=0,
            # Only CLKOP drives logic.
            i_ENCLKOP=1, i_ENCLKOS=0, i_ENCLKOS2=0, i_ENCLKOS3=0,
            o_CLKOP=self.o_clk,
            o_CLKOS=Signal(), o_CLKOS2=Signal(), o_CLKOS3=Signal(),
            o_LOCK=self.o_locked,
            o_INTLOCK=Signal(), o_REFCLK=Signal(), o_CLKINTFB=Signal(),
        )
        ports.update(
            p_CLKI_DIV=self.clki_div,
            p_CLKFB_DIV=self.clkfb_div,
            p_CLKOP_DIV=self.clkop_div,
            # Disabled outputs still need a legal divider value.
            p_CLKOS_DIV=8, p_CLKOS2_DIV=8, p_CLKOS3_DIV=8,
            p_CLKOP_ENABLE="ENABLED",
            p_CLKOS_ENABLE="DISABLED",
            p_CLKOS2_ENABLE="DISABLED",
            p_CLKOS3_ENABLE="DISABLED",
            # No static phase shift: CLKOP_CPHASE must stay below CLKOP_DIV.
            p_CLKOP_CPHASE=0, p_CLKOS_CPHASE=0, p_CLKOS2_CPHASE=0,
            p_CLKOS3_CPHASE=0,
            p_CLKOP_FPHASE=0, p_CLKOS_FPHASE=0, p_CLKOS2_FPHASE=0,
            p_CLKOS3_FPHASE=0,
            p_FEEDBK_PATH="CLKOP",
            p_CLKOP_TRIM_POL="RISING", p_CLKOP_TRIM_DELAY=0,
            p_CLKOS_TRIM_POL="RISING", p_CLKOS_TRIM_DELAY=0,
            p_OUTDIVIDER_MUXA="DIVA", p_OUTDIVIDER_MUXB="DIVB",
            p_OUTDIVIDER_MUXC="DIVC", p_OUTDIVIDER_MUXD="DIVD",
            p_PLL_LOCK_MODE=0, p_PLL_LOCK_DELAY=200,
            p_STDBY_ENABLE="DISABLED", p_REFIN_RESET="DISABLED",
            p_SYNC_ENABLE="DISABLED", p_INT_LOCK_STICKY="ENABLED",
            p_DPHASE_SOURCE="DISABLED", p_PLLRST_ENA="DISABLED",
            p_INTFB_WAKE="DISABLED",
        )
        m.submodules.pll = Instance("EHXPLLL", **ports)
        return m
