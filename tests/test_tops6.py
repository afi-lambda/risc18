"""Elaboration and PLL-parameter tests for the six-stage tops.

``Core6Top`` and ``Full6Top`` are synthesis tops: they exist to be routed, so
what is testable without a bitstream is that they *elaborate* (every port is
driven, the domain wiring is legal -- a duplicated or undriven clock domain
fails here) and that the PLL they instantiate is the exact 120 MHz divider set.

The divider set is asserted twice on purpose: once on the values the top passes
to :class:`risc18.pll.Pll` (``top.pll.clki_div`` ...) and once on the parameters
that reach the netlist (the ``EHXPLLL`` instance in the emitted Verilog).  A
comment saying "120 MHz" cannot satisfy either.
"""

import pytest
from amaranth.hdl import Fragment

from risc18.pll import Pll
from risc18.tops import (
    CLKFB6_DIV,
    CLKI6_DIV,
    CLKOP6_DIV,
    F_OUT6_MHZ,
    Core6Top,
    CoreTop,
    Full6Top,
    FullTop,
)


# --- elaboration ------------------------------------------------------------

def _domains(frag):
    """Every domain name declared anywhere in an elaborated fragment tree.

    ``Fragment.get`` keeps a top's own domains on the top fragment and each
    submodule's on its subfragment, so the check has to walk the tree.
    """
    names = set(frag.domains)
    for sub, _name, _loc in frag.subfragments:
        names |= _domains(sub)
    return names


@pytest.mark.parametrize("factory", [Core6Top, Full6Top,
                                     lambda: Core6Top(ram_rf=True),
                                     lambda: Full6Top(ram_rf=True)])
def test_six_stage_tops_elaborate(factory):
    top = factory()
    frag = Fragment.get(top, None)
    # One clock domain per registered domain in the design: the core's own
    # ``sync``, the ``measure`` shell and -- in Full6Top -- the ``boot``
    # sequencer.  Elaboration is where a second definition of ``sync`` or an
    # undriven domain clock would surface.
    domains = _domains(frag)
    assert "sync" in domains
    assert "measure" in domains
    assert top.ports[0] is top.clk
    assert top.clk.name == "clk"          # tools/synth.py constrains NET "clk"


def test_full6_top_has_the_boot_sequencer_domain():
    assert "boot" in _domains(Fragment.get(Full6Top(), None))


def test_core6_top_ports_match_core_top():
    # Same shell, same interface: the two are meant to be interchangeable in
    # the metrics table and in anything that drives a CoreTop.
    assert ([p.name for p in Core6Top().ports]
            == [p.name for p in CoreTop().ports])
    assert ([p.name for p in Full6Top().ports]
            == [p.name for p in FullTop().ports])


def test_six_stage_tops_run_the_same_program_as_the_five_stage_ones():
    assert Core6Top().program == CoreTop().program
    assert Full6Top().program == FullTop().program


# --- PLL at exactly 120.000 MHz --------------------------------------------

def test_full6_pll_divider_constants_are_the_exact_120_mhz_set():
    # f_out = 25 * CLKFB_DIV / (CLKI_DIV * CLKOP_DIV), VCO = 25 * CLKFB_DIV.
    assert (CLKI6_DIV, CLKFB6_DIV, CLKOP6_DIV) == (1, 24, 5)
    assert F_OUT6_MHZ == 120.0


def test_full6_top_passes_the_exact_120_mhz_set_to_the_pll():
    top = Full6Top()
    assert (top.pll.clki_div, top.pll.clkfb_div, top.pll.clkop_div) == \
        (CLKI6_DIV, CLKFB6_DIV, CLKOP6_DIV) == (1, 24, 5)
    assert top.pll.f_in_mhz == 25.0
    assert top.pll.f_pfd_mhz == 25.0
    assert top.pll.f_vco_mhz == 600.0      # same VCO as the 60 MHz top
    assert top.pll.f_out_mhz == 120.0
    # Exact: no frequency error at all, so the target is a real clock and not
    # a rounded one.
    assert top.pll.error_hz(120.0) == 0.0


def test_the_120_mhz_set_is_legal_for_the_shipped_pll_wrapper():
    # Pll validates PFD/VCO/dividers in __init__; constructing it is the check.
    # If it ever raises, the divider set (not the wrapper) is what to revisit.
    pll = Pll(clki_div=1, clkfb_div=24, clkop_div=5)
    assert pll.f_out_mhz == 120.0


def test_full6_netlist_carries_the_120_mhz_pll_parameters():
    from amaranth.back import verilog

    top = Full6Top()
    v = verilog.convert(top, name="top", ports=top.ports)
    assert v.count("EHXPLLL") == 1
    for param, value in (("CLKI_DIV", "32'd1"),
                         ("CLKFB_DIV", "32'd24"),
                         ("CLKOP_DIV", "32'd5")):
        assert f".{param}({value})" in v, param
    assert '."FEEDBK_PATH"."CLKOP"' in v or '"CLKOP"' in v
