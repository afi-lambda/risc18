"""PLL divider semantics: the regression that would have caught the 60 MHz bug.

With ``FEEDBK_PATH="CLKOP"`` the feedback divider counts CLKOP, so the loop
locks ``f_out = f_in * CLKFB_DIV / CLKI_DIV`` and ``f_VCO = f_out * CLKOP_DIV``
(``CLKOP_DIV`` sets the VCO, not the output).  The first version of
:mod:`risc18.pll` divided by ``CLKOP_DIV`` twice and shipped ``(1, 24, 10)`` as
"60 MHz"; that set actually asks for 600 MHz of CLKOP and a 6 GHz VCO, which
nextpnr rejected as out of band and which produced no clock on the board.

The tests below pin the corrected arithmetic, the vendor instance's wiring,
and the rejection of the old set -- the last one is the regression test.
"""

import re

import pytest
from amaranth import Elaboratable, Module
from amaranth.back import verilog

from risc18 import pll
from risc18.pll import (CLKFB_DIV, CLKI_DIV, CLKOP_CPHASE, CLKOP_DIV,
                        F_IN_MHZ, F_OUT_MHZ, F_VCO_MHZ, Pll,
                        VCO_MAX_MHZ, VCO_MIN_MHZ)
from risc18.tops import (CLKFB6_DIV, CLKI6_DIV, CLKOP6_CPHASE, CLKOP6_DIV,
                         F_OUT6_MHZ, Full6Top, FullTop)

#: What ``ecppll -i 25 -o 60 --f out.v`` prints and emits: "Refclk divisor: 5 /
#: Feedback divisor: 12 / clkout0 divisor: 10 / VCO frequency: 600", with
#: ``.CLKOP_CPHASE(4)``, ``.CLKFB(clkout0)`` and ``.ENCLKOP(1'b0)``.
VENDOR_60 = {"clki_div": 5, "clkfb_div": 12, "clkop_div": 10,
             "clkop_cphase": 4}

#: ``ecppll -i 25 -o 120 --f out.v``: same VCO, twice the feedback divider and
#: half the output divider, ``.CLKOP_CPHASE(2)``.
VENDOR_120 = {"clki_div": 5, "clkfb_div": 24, "clkop_div": 5,
              "clkop_cphase": 2}


# --- the arithmetic ---------------------------------------------------------

def test_shipped_set_is_exactly_60_mhz():
    p = Pll()
    assert (p.clki_div, p.clkfb_div, p.clkop_div, p.clkop_cphase) == (5, 12, 10, 4)
    assert (CLKI_DIV, CLKFB_DIV, CLKOP_DIV, CLKOP_CPHASE) == (5, 12, 10, 4)
    assert p.f_in_mhz == 25.0
    assert p.f_pfd_mhz == 5.0          # 25 / 5
    assert p.f_out_mhz == 60.0         # 5 * 12
    assert p.f_vco_mhz == 600.0        # 60 * 10
    assert p.error_hz() == 0.0
    assert F_OUT_MHZ == 60.0 and F_VCO_MHZ == 600.0


def test_the_output_follows_clkfb_over_clki_and_clkop_div_sets_the_vco():
    # Output: f_in * CLKFB / CLKI.  CLKOP_DIV only scales the VCO up from it.
    default = Pll()
    fast = Pll(**VENDOR_120)
    assert fast.f_out_mhz == 120.0
    assert fast.f_out_mhz == 2 * default.f_out_mhz
    assert fast.f_vco_mhz == default.f_vco_mhz == 600.0
    assert fast.f_vco_mhz == fast.f_out_mhz * fast.clkop_div
    assert default.f_vco_mhz == default.f_out_mhz * default.clkop_div


def test_vco_is_legal_under_the_corrected_formula():
    # The band nextpnr-ecp5 enforces and ecppll searches over.
    assert VCO_MIN_MHZ == 400.0 and VCO_MAX_MHZ == 800.0
    for kwargs in (VENDOR_60, VENDOR_120):
        p = Pll(**kwargs)
        assert VCO_MIN_MHZ <= p.f_vco_mhz <= VCO_MAX_MHZ
    assert VCO_MIN_MHZ <= F_VCO_MHZ <= VCO_MAX_MHZ
    # The formula, not the constant, is what the check uses: the old set's
    # 25 * 24 / 1 = 600 MHz of CLKOP would put the VCO at 6 GHz.
    assert F_IN_MHZ * 24 / 1 * 10 == 6000.0


def test_old_divider_set_is_rejected():
    """The regression: (1, 24, 10) was shipped as "60 MHz, 0 ppm".

    Under CLKOP feedback it asks for 600 MHz of CLKOP and a 6 GHz VCO, so the
    class must refuse to build it.  Restoring the old double division (or the
    old set) fails this test.
    """
    with pytest.raises(ValueError):
        Pll(clki_div=1, clkfb_div=24, clkop_div=10)
    # ... and specifically because of the VCO, not some unrelated band.
    with pytest.raises(ValueError, match="VCO"):
        Pll(clki_div=1, clkfb_div=24, clkop_div=10)
    # Sanity: the same numbers under the *old* formula looked like 60 MHz.
    assert 25 * 24 / (1 * 10) == 60.0


def test_vendor_120_mhz_set_is_legal():
    p = Pll(**VENDOR_120)
    assert (p.clki_div, p.clkfb_div, p.clkop_div, p.clkop_cphase) == (5, 24, 5, 2)
    assert p.f_out_mhz == 120.0 and p.f_vco_mhz == 600.0
    assert p.error_hz(120.0) == 0.0


# --- the wiring the vendor instance has -------------------------------------

def _pll_verilog():
    """The Verilog the flow hands to yosys for one bare ``Pll``."""

    class Wrapper(Elaboratable):
        def __init__(self):
            self.pll = Pll()

        def elaborate(self, platform):
            m = Module()
            m.submodules.pll = self.pll
            return m

    w = Wrapper()
    return w.pll, verilog.convert(w, name="p", ports=[w.pll.i_clk])


def test_cphase_and_feedback_match_the_vendor_instance():
    p, v = _pll_verilog()
    assert f".CLKOP_CPHASE(32'd{CLKOP_CPHASE})" in v
    assert '."FEEDBK_PATH"."CLKOP"' in v or '.FEEDBK_PATH("CLKOP")' in v
    # CLKFB is tied to the CLKOP net, as ecppll emits it (and as the board
    # measurement required); CLKOP and CLKFB are the same wire.
    body = v.split("EHXPLLL #(")[1]
    clkop = re.search(r"\.CLKOP\(([^)]+)\)", body).group(1).strip()
    clkfb = re.search(r"\.CLKFB\(([^)]+)\)", body).group(1).strip()
    assert clkop == clkfb, (clkop, clkfb)
    assert ".ENCLKOP(1'h0)" in body          # ecppll's value, not 1
    assert ".PHASEDIR(1'h1)" in body
    assert ".PHASESTEP(1'h1)" in body
    assert ".PHASELOADREG(1'h1)" in body
    # CPHASE must stay inside one output period.
    assert 0 <= p.clkop_cphase < p.clkop_div


def test_default_wiring_is_the_vendor_parameter_pack():
    _, v = _pll_verilog()
    for param, value in (("CLKI_DIV", "32'd5"), ("CLKFB_DIV", "32'd12"),
                         ("CLKOP_DIV", "32'd10"), ("CLKOP_CPHASE", "32'd4")):
        assert f".{param}({value})" in v, param


# --- the tops pass the set the class validates ------------------------------

def test_full_top_passes_exactly_the_set_the_class_ships():
    top = FullTop()
    assert (top.pll.clki_div, top.pll.clkfb_div, top.pll.clkop_div,
            top.pll.clkop_cphase) == (CLKI_DIV, CLKFB_DIV, CLKOP_DIV,
                                      CLKOP_CPHASE)
    assert top.pll.f_out_mhz == 60.0
    # Constructing the class *is* the validation; the same call the top makes.
    Pll(clki_div=top.pll.clki_div, clkfb_div=top.pll.clkfb_div,
        clkop_div=top.pll.clkop_div, clkop_cphase=top.pll.clkop_cphase)


def test_full6_top_passes_exactly_the_set_the_class_validates():
    top = Full6Top()
    assert (top.pll.clki_div, top.pll.clkfb_div, top.pll.clkop_div,
            top.pll.clkop_cphase) == (CLKI6_DIV, CLKFB6_DIV, CLKOP6_DIV,
                                      CLKOP6_CPHASE)
    assert F_OUT6_MHZ == 120.0 and top.pll.f_out_mhz == F_OUT6_MHZ
    assert VCO_MIN_MHZ <= top.pll.f_vco_mhz <= VCO_MAX_MHZ
    Pll(clki_div=top.pll.clki_div, clkfb_div=top.pll.clkfb_div,
        clkop_div=top.pll.clkop_div, clkop_cphase=top.pll.clkop_cphase)


def test_pll_is_the_only_clock_generator_left():
    # HilPll (the private ecppll copy) is gone: every top uses Pll.
    assert not hasattr(pll, "HilPll")
    import risc18.hil as hil
    assert not hasattr(hil, "HilPll")
    from risc18.tops import HilTop
    assert isinstance(HilTop().pll, Pll)
