"""Parser tests for tools/synth.py (metrics must be unambiguous)."""

from tools.synth import _norm_clock, parse_critical, parse_fmax

SAMPLE = """
Info: Device utilisation:
Info: Max frequency for clock 'clk': 139.18 MHz (PASS at 60.00 MHz)
Info: Max frequency for clock '$glbnet$clk$TRELLIS_IO_IN': 176.58 MHz (PASS at 60.00 MHz)
Info: Critical path report for clock 'clk' (posedge -> posedge):
Info:  curr total
Info:   1.0  1.0  Source instr_q_TRELLIS_FF_Q.Q
Info:   4.6  5.6  Net rs2
Info:   1.0 ns logic, 4.6 ns routing
Info: Total 5.6 ns
"""


def test_norm_clock_strips_promotion():
    assert _norm_clock("clk") == "clk"
    assert _norm_clock("$glbnet$clk$TRELLIS_IO_IN") == "clk"
    assert _norm_clock("\\$glbnet$clk$TRELLIS_IO_IN") == "clk"


def test_fmax_keeps_only_post_route_report():
    got = parse_fmax(SAMPLE)
    assert len(got) == 1
    assert got[0]["fmax_mhz"] == 176.58
    assert got[0]["clock"] == "$glbnet$clk$TRELLIS_IO_IN"


def test_fmax_keeps_distinct_clocks():
    text = SAMPLE + (
        "Info: Max frequency for clock 'clk2': 90.00 MHz (PASS at 60.00 MHz)\n")
    got = parse_fmax(text)
    assert [f["clock"] for f in got] == ["$glbnet$clk$TRELLIS_IO_IN", "clk2"]
    assert [f["fmax_mhz"] for f in got] == [176.58, 90.0]


def test_critical_path_split_parses_ns_units():
    crit = parse_critical(SAMPLE)
    assert crit is not None
    assert (crit["logic_ns"], crit["routing_ns"]) == (1.0, 4.6)
