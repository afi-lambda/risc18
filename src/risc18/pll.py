"""Explicit ECP5 ``EHXPLLL`` clock generator for the risc18 full tops.

The Colorlight 5A-75B feeds the FPGA a 25 MHz oscillator; the cores in this
repo are closed at 60 MHz (five-stage) and 120 MHz (six-stage).  Amaranth has
no ECP5 PLL primitive (no ``pll`` module the way ``amaranth.vendor`` has for
Xilinx/ICE40), and the open flow has no ``ecppll`` step, so the primitive is
instantiated by hand here, the same way :mod:`risc18.mul` instantiates
``MULT18X18D``.

Feedback from CLKOP: the frequency equations
--------------------------------------------
``FEEDBK_PATH = "CLKOP"`` feeds CLKOP back inside the block, so the feedback
divider counts **CLKOP**, not the VCO::

    f_PFD   = f_in / CLKI_DIV
    f_CLKOP = f_PFD * CLKFB_DIV        ( = f_in * CLKFB_DIV / CLKI_DIV )
    f_VCO   = f_CLKOP * CLKOP_DIV

``CLKOP_DIV`` therefore sets the **VCO** frequency, not the output.  This is
the vendor tool's own model, verbatim from the first lines of ``ecppll.cpp``
(prjtrellis -- the source of the installed ``/usr/bin/ecppll``)::

    f_pfd = f_in/refclk
    f_vco = f_pfd * feedback * output
    f_out = f_vco / output

An earlier version of this module divided by ``CLKOP_DIV`` a second time
(``f_out = f_in * CLKFB_DIV / (CLKI_DIV * CLKOP_DIV)``) and shipped
``CLKI_DIV=1, CLKFB_DIV=24, CLKOP_DIV=10`` as "60.000 MHz, 0 ppm".  Under
CLKOP feedback that set asks for 600 MHz of CLKOP and a 6 GHz VCO: nextpnr
printed ``Derived VCO frequency 6024.1 MHz ... out of legal range [400MHz,
800MHz]``, and a heartbeat design clocked by it produced nothing on the board
while the vendor instance streamed.  :class:`Pll` now rejects that set.

Configuration (``ecppll -i 25 -o 60 --f``, the instance measured on the
board)::

    CLKI_DIV     = 5     -> f_PFD  = 25 / 5   =  5.000 MHz
    CLKFB_DIV    = 12    -> f_CLKOP = 5 * 12  = 60.000 MHz
    CLKOP_DIV    = 10    -> f_VCO  = 60 * 10  = 600.000 MHz
    CLKOP_CPHASE = 4     -> static phase shift, must stay below CLKOP_DIV

Which witness settles each number:

* dividers and ``CPHASE``: ``ecppll -i 25 -o 60`` prints ``Refclk divisor: 5 /
  Feedback divisor: 12 / clkout0 divisor: 10 / VCO frequency: 600`` and emits
  ``.CLKOP_CPHASE(4)``.  ``ecppll -i 25 -o 120`` gives ``5 / 24 / 5`` with
  ``.CLKOP_CPHASE(2)`` -- the set :class:`risc18.tops.Full6Top` passes.
* the loop equation: ``ecppll.cpp``'s header (quoted above) and the board.
  ``HilTop`` -- which uses this class -- prints the same status line the
  ecppll-wired harness printed (``OK retires=000d cycles=0000001a pc=000f
  value=00000``, ``docs/metrics/hil5.md``), so the 60 MHz clock is real.
* output exactness: 25 * 12 / 5 = 60.000 MHz with integer dividers, i.e. 0 Hz
  error; 25 * 24 / 5 = 120.000 MHz for the six-stage top.
* VCO legality: 600 MHz is inside 400-800 MHz, the one band nextpnr-ecp5
  checks and the band ecppll searches over.
* wiring: ``CLKFB`` tied to the CLKOP net, ``ENCLKOP`` low, ``PHASEDIR`` /
  ``PHASESTEP`` / ``PHASELOADREG`` high, and the ``ICP_CURRENT`` /
  ``LPF_RESISTOR`` / ``MFG_*`` attributes -- all as ecppll's emitted Verilog
  has them.  Measured at stage 3: the same dividers with ``CLKFB`` tied low,
  ``CPHASE=0`` and ``ENCLKOP=1`` did **not** run on this board (the UART line
  came out stuck), while this wiring streams.  Dropping the ``MFG_*`` /
  ``ICP`` / ``LPF`` attributes made no difference (that build streamed too),
  so they are carried for parity with the vendor bitstream, not for
  correctness.

Bands, and why the phase detector at 5 MHz is legal
---------------------------------------------------
The legal ranges below are prjtrellis' ``ecppll.cpp`` constants -- the numbers
the vendor tool the board result was produced with actually enforces:
``INPUT_MIN/MAX 8/400`` (CLKI), ``PFD_MIN/MAX 3.125/400``, ``VCO_MIN/MAX
400/800``, ``OUTPUT_MIN/MAX 10/400`` (the output/feedback clock, checked
against ``f_VCO / CLKOP_DIV``).

The 10 MHz floor an earlier version of this module applied to the *PFD* was a
misreading: 10 MHz is the datasheet's lower bound for the output (and hence
feedback) clock, which here is CLKOP itself at 60 MHz.  Applying it to the
PFD is what made the vendor's own 25 -> 60 MHz answer -- PFD 5 MHz --
unrepresentable, and it is why that answer had to live in a second class
(``risc18.hil.HilPll``, now removed).

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

* ``FEEDBK_PATH = "CLKOP"`` -- CLKOP is fed back inside the block.  ``CLKFB``
  is still tied to the CLKOP net, as ``ecppll`` does: on this board that
  wiring is part of what makes the loop run (a build with ``CLKFB`` tied low
  did not).
* ``i_RST = 0`` and ``PLLRST_ENA = "DISABLED"`` -- the PLL free-starts with the
  FPGA and reports readiness on ``LOCK``; the top holds the core in reset until
  ``LOCK`` is high (see :class:`risc18.tops.BootReset`), so a second reset path
  into the PLL would only add a way to lose lock silently.
* ``ENCLKOP = 0``, ``ENCLKOS* = 0`` -- ecppll's value; only CLKOP is used and
  the other three outputs are left unconnected.
* ``INT_LOCK_STICKY = "ENABLED"`` -- once locked, a transient loss of lock does
  not latch the design off.
* ``i_STDBY = 0``/``STDBY_ENABLE = "DISABLED"``, ``DPHASE_SOURCE =
  "DISABLED"``, ``SYNC_ENABLE = "DISABLED"``, ``REFIN_RESET = "DISABLED"``,
  ``INTFB_WAKE = "DISABLED"`` -- all dynamic-phase/standby/refin features off:
  the tops only need a fixed 25 -> 60/120 MHz clock.  ``PHASESEL*``,
  ``PHASEDIR``, ``PHASESTEP`` and ``PHASELOADREG`` are held at ecppll's
  constants (0/0/1/1/1); they are inert with ``DPHASE_SOURCE`` disabled.
* ``CLKOP_CPHASE = 4`` for ``CLKOP_DIV = 10`` (ecppll; must be < CLKOP_DIV).
  ``CLKOP_FPHASE = 0`` and trim 0.

The netlist evidence for a metric run is ``build/synth/<name>/top.json``:
exactly one ``EHXPLLL`` cell, with the parameters above.
"""

from amaranth import Elaboratable, Instance, Module, Signal

__all__ = ["Pll", "F_IN_MHZ", "F_OUT_MHZ", "CLKI_DIV", "CLKFB_DIV",
           "CLKOP_DIV", "CLKOP_CPHASE"]

#: Reference clock on the Colorlight 5A-75B (the board's 25 MHz oscillator).
F_IN_MHZ = 25.0

#: The ``ecppll -i 25 -o 60`` divider set: PFD 5 MHz, CLKOP 60.000 MHz,
#: VCO 600 MHz (see the module docstring for the witnesses).
CLKI_DIV = 5
CLKFB_DIV = 12
CLKOP_DIV = 10

#: Static phase shift ecppll emits for ``CLKOP_DIV = 10``; must be < CLKOP_DIV.
CLKOP_CPHASE = 4

#: Output frequency of the default configuration, MHz:
#: ``f_CLKOP = f_in * CLKFB_DIV / CLKI_DIV`` (feedback from CLKOP).
F_OUT_MHZ = F_IN_MHZ * CLKFB_DIV / CLKI_DIV

#: VCO frequency of the default configuration, MHz: ``f_CLKOP * CLKOP_DIV``.
F_VCO_MHZ = F_OUT_MHZ * CLKOP_DIV

#: Legal ECP5 PLL ranges, as prjtrellis' ``ecppll.cpp`` enforces them (the
#: vendor tool named in the docstring); nextpnr-ecp5 checks the VCO band only.
INPUT_MIN_MHZ, INPUT_MAX_MHZ = 8.0, 400.0     # CLKI reference input
PFD_MIN_MHZ, PFD_MAX_MHZ = 3.125, 400.0      # phase-detector input
VCO_MIN_MHZ, VCO_MAX_MHZ = 400.0, 800.0      # VCO
OUT_MIN_MHZ, OUT_MAX_MHZ = 10.0, 400.0       # CLKOP output / feedback clock
DIV_MAX = 80
CLKOP_DIV_MAX = 128


class Pll(Elaboratable):
    """One ECP5 ``EHXPLLL``: ``i_clk`` (25 MHz) -> ``o_clk`` (60 MHz) + LOCK.

    With the shipped divider set (5, 12, 10) that is PFD 5 MHz, CLKOP
    60.000 MHz, VCO 600 MHz -- the ``ecppll -i 25 -o 60`` instance, measured
    running on the board through :class:`risc18.tops.HilTop`.

    ``o_clk`` is the CLKOP output and must drive a global clock net -- the
    enclosing top does that by wiring it to a clock domain's ``clk``, nextpnr
    then promotes it to a primary clock.  ``o_locked`` is the raw ``LOCK``
    output: high once the PLL has locked, and the reset sequencer in
    :class:`risc18.tops.FullTop` releases the core only while it is high.

    The unit is synthesis-only: ``EHXPLLL`` has no model in the Yosys ECP5
    library, and Amaranth's pure-Python simulator drives ``Instance`` outputs
    to 0, so ``o_locked`` reads 0 and ``o_clk`` does not toggle in simulation.
    The frequency arithmetic and the divider bands are checked here, at
    construction time, and the parameters are pinned in
    ``tests/test_pll.py``.
    """

    def __init__(self, *, clki_div=CLKI_DIV, clkfb_div=CLKFB_DIV,
                 clkop_div=CLKOP_DIV, clkop_cphase=CLKOP_CPHASE,
                 f_in_mhz=F_IN_MHZ):
        for name, value, limit in (("clki_div", clki_div, DIV_MAX),
                                   ("clkfb_div", clkfb_div, DIV_MAX),
                                   ("clkop_div", clkop_div, CLKOP_DIV_MAX)):
            if not isinstance(value, int) or not 1 <= value <= limit:
                raise ValueError(f"{name} must be an int in 1..{limit}, "
                                 f"got {value!r}")
        if not isinstance(clkop_cphase, int):
            raise ValueError(f"clkop_cphase must be an int, got {clkop_cphase!r}")
        if f_in_mhz <= 0:
            raise ValueError(f"f_in_mhz must be positive, got {f_in_mhz!r}")

        self.clki_div = clki_div
        self.clkfb_div = clkfb_div
        self.clkop_div = clkop_div
        self.clkop_cphase = clkop_cphase
        self.f_in_mhz = float(f_in_mhz)

        f_pfd = self.f_pfd_mhz
        f_vco = self.f_vco_mhz
        f_out = self.f_out_mhz
        if not INPUT_MIN_MHZ <= f_in_mhz <= INPUT_MAX_MHZ:
            raise ValueError(f"reference frequency {f_in_mhz} MHz outside "
                             f"{INPUT_MIN_MHZ}-{INPUT_MAX_MHZ} MHz")
        if not PFD_MIN_MHZ <= f_pfd <= PFD_MAX_MHZ:
            raise ValueError(f"phase-detector frequency {f_pfd} MHz outside "
                             f"{PFD_MIN_MHZ}-{PFD_MAX_MHZ} MHz")
        if not VCO_MIN_MHZ <= f_vco <= VCO_MAX_MHZ:
            raise ValueError(f"VCO frequency {f_vco} MHz outside "
                             f"{VCO_MIN_MHZ}-{VCO_MAX_MHZ} MHz")
        if not OUT_MIN_MHZ <= f_out <= OUT_MAX_MHZ:
            raise ValueError(f"CLKOP frequency {f_out} MHz outside "
                             f"{OUT_MIN_MHZ}-{OUT_MAX_MHZ} MHz")
        if not 0 <= clkop_cphase < clkop_div:
            raise ValueError(f"clkop_cphase {clkop_cphase} must be in "
                             f"0..{clkop_div - 1}")

        #: 25 MHz reference from the board oscillator (CLKI).
        self.i_clk = Signal()
        #: CLKOP: ``f_out_mhz``, ``clkop_cphase``/``CLKOP_DIV`` of a period
        #: shifted, 50 % duty.
        self.o_clk = Signal()
        #: LOCK: high once the loop has locked (INT_LOCK_STICKY, so it latches).
        self.o_locked = Signal()

    # -- computed frequencies (for the reports and the tests) --------------
    @property
    def f_pfd_mhz(self):
        """Phase-detector (PFD) frequency, MHz (``f_in / CLKI_DIV``)."""
        return self.f_in_mhz / self.clki_div

    @property
    def f_out_mhz(self):
        """CLKOP frequency, MHz (``f_in * CLKFB_DIV / CLKI_DIV``).

        CLKOP is the feedback signal, so it is the frequency the loop locks;
        ``CLKOP_DIV`` does not appear here.
        """
        return self.f_pfd_mhz * self.clkfb_div

    @property
    def f_vco_mhz(self):
        """VCO frequency, MHz (``f_out * CLKOP_DIV``)."""
        return self.f_out_mhz * self.clkop_div

    def error_hz(self, target_mhz=F_OUT_MHZ):
        """Absolute error of CLKOP against ``target_mhz`` (default 60 MHz), in Hz.

        A method rather than a property, because the target is not fixed for
        every legal divider set this class accepts.
        """
        return abs(self.f_out_mhz - float(target_mhz)) * 1e6

    # -- elaboration -------------------------------------------------------
    def elaborate(self, platform):
        m = Module()
        ports = dict(
            # Reference and feedback: CLKFB is tied to the CLKOP net, exactly
            # as ecppll emits it (and as the on-board measurement required).
            i_CLKI=self.i_clk,
            i_CLKFB=self.o_clk,
            # Dynamic phase control: off (DPHASE_SOURCE=DISABLED), inputs at
            # ecppll's constants so the emitted cell matches the vendor one.
            i_PHASESEL1=0, i_PHASESEL0=0, i_PHASEDIR=1, i_PHASESTEP=1,
            i_PHASELOADREG=1,
            # Standby: off (STDBY_ENABLE=DISABLED).
            i_STDBY=0, i_PLLWAKESYNC=0,
            # No reset into the PLL (PLLRST_ENA=DISABLED); LOCK is the only
            # handshake the top uses.
            i_RST=0,
            # Only CLKOP drives logic; ecppll leaves its enable low.
            i_ENCLKOP=0, i_ENCLKOS=0, i_ENCLKOS2=0, i_ENCLKOS3=0,
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
            # Static phase shift as ecppll computes it (0 <= CPHASE < DIV).
            p_CLKOP_CPHASE=self.clkop_cphase, p_CLKOS_CPHASE=0,
            p_CLKOS2_CPHASE=0, p_CLKOS3_CPHASE=0,
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
        # Charge-pump / loop-filter settings ecppll emits.  They made no
        # difference on the board (see the module docstring); carried so the
        # cell matches the vendor instance that was measured.
        ports.update(a_ICP_CURRENT="12", a_LPF_RESISTOR="8",
                     a_MFG_ENABLE_FILTEROPAMP="1", a_MFG_GMCREF_SEL="2")
        m.submodules.pll = Instance("EHXPLLL", **ports)
        return m
