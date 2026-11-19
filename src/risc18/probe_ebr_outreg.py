"""Probe: DP16KD with the block's own output register (`REGMODE=OUTREG`) vs without.

RESULT (2026-11-19): this probe cannot produce a number in this flow. nextpnr-ecp5
has no timing arcs for a hand-instantiated DP16KD: the routed log says "No interior
timing paths found in design" for both REGMODE variants, i.e. the read path through
the cell is not modelled at all. Patching `REGMODE_A` in a post-synthesis netlist is
no better -- nextpnr then reports 346-442 MHz with the EBR absent from the timing log
entirely, which is a missing-model artefact, not a measurement. The file is kept as
the record of that negative result; do not quote a number from it.

What the flow actually builds, and what was measured (`probe_ebr_read.ProbeRd`,
`probe_mem_pipe.Probe2C`):

  REGMODE_A      = "NOREG"   (yosys `ecp5/brams_map.v` hardcodes it, lines 120-121
                              and 360-361, so no inferred memory can differ)
  address -> DOA = 5.6 ns    (block clock-to-out)
  DOA -> FF      = 6.7 ns    (149.97 MHz; 6.7-6.9 ns across seeds 1/2/3/7 in Probe2C)
  one clock EBR read -> add -> EBR write = 9.0 ns (111.53 MHz)

Both REGMODE variants present data in the cycle after the address is sampled, so an
internal output register would cost no additional latency -- it would only move the
register from fabric into the block. Whether that is faster on this part is unproven.

    .venv/bin/python tools/synth.py --top risc18.probe_ebr_outreg:RdOutreg \
        --name probe_outreg_rd --freq 200 --seed 1

Measurement only. Not part of the design.
"""

from amaranth import Elaboratable, Module, Signal
from amaranth.hdl import Instance

WIDTH = 18
ADDRBITS = 14          # DP16KD has 14 address inputs; 1024x18 uses the low 10
DEPTH = 1024


def _bits(name, sig, n):
    """Expand a Signal into named single-bit ports (ADA0..ADAn-1)."""
    return {f"{name}{i}": sig[i] for i in range(n)}


def _dp16kd(regmode, addr, di, we, do, clk):
    """One DP16KD, A side active; B side tied off and disabled."""
    zero_addr = Signal(ADDRBITS)
    zero_data = Signal(WIDTH)
    ports = {
        "i_CLKA": clk, "i_CEA": 1, "i_OCEA": 1, "i_WEA": we, "i_RSTA": 0,
        "i_CSA0": 0, "i_CSA1": 0, "i_CSA2": 0,
        "i_CLKB": clk, "i_CEB": 0, "i_OCEB": 1, "i_WEB": 0, "i_RSTB": 0,
        "i_CSB0": 0, "i_CSB1": 0, "i_CSB2": 0,
    }
    ports.update(_bits("i_ADA", addr, ADDRBITS))
    ports.update(_bits("i_ADB", zero_addr, ADDRBITS))
    ports.update(_bits("i_DIA", di, WIDTH))
    ports.update(_bits("i_DIB", zero_data, WIDTH))
    ports.update(_bits("o_DOA", do, WIDTH))
    ports.update(_bits("o_DOB", Signal(WIDTH), WIDTH))
    return Instance(
        "DP16KD",
        p_DATA_WIDTH_A=18, p_DATA_WIDTH_B=18,
        p_REGMODE_A=regmode, p_REGMODE_B="NOREG",
        p_CSDECODE_A="0b000", p_CSDECODE_B="0b000",
        p_CLKAMUX="CLKA", p_CLKBMUX="CLKB",
        p_RESETMODE="SYNC", p_ASYNC_RESET_RELEASE="SYNC",
        p_WRITEMODE_A="NORMAL", p_WRITEMODE_B="NORMAL", p_GSR="AUTO",
        **ports,
    )


class _Rd(Elaboratable):
    """One read: address register -> block -> fabric capture."""

    regmode = "NOREG"

    def __init__(self):
        self.clk = Signal()
        self.i_addr = Signal(10)
        self.o_q = Signal(WIDTH)
        self.ports = [self.clk, self.i_addr, self.o_q]

    def elaborate(self, platform):
        m = Module()
        a = Signal(ADDRBITS)
        d = Signal(WIDTH)
        m.d.sync += a.eq(self.i_addr)
        m.submodules.ebr = _dp16kd(self.regmode, a, Signal(WIDTH), 0, d, self.clk)
        m.d.sync += self.o_q.eq(d)
        return m


class RdNoreg(_Rd):
    regmode = "NOREG"


class RdOutreg(_Rd):
    regmode = "OUTREG"


class _Rw(Elaboratable):
    """Read one block, add, write another: the `EBR -> ALU -> EBR` chain, one clock."""

    regmode = "NOREG"

    def __init__(self):
        self.clk = Signal()
        self.i_addr = Signal(10)
        self.i_op = Signal(WIDTH)
        self.o_q = Signal(WIDTH)      # read-back of the destination block
        self.o_d = Signal(WIDTH)      # the sum, so the adder is not folded
        self.ports = [self.clk, self.i_addr, self.i_op, self.o_q, self.o_d]

    def elaborate(self, platform):
        m = Module()
        a = Signal(ADDRBITS)
        a2 = Signal(ADDRBITS)
        op = Signal(WIDTH)
        d_src = Signal(WIDTH)
        d_dst = Signal(WIDTH)
        s = Signal(WIDTH + 1)

        m.d.sync += [a.eq(self.i_addr), op.eq(self.i_op)]
        m.submodules.src = _dp16kd(self.regmode, a, Signal(WIDTH), 0, d_src, self.clk)
        m.submodules.dst = _dp16kd("NOREG", a2, s[:WIDTH], 1, d_dst, self.clk)
        m.d.comb += [s.eq(d_src + op), a2.eq(s[:ADDRBITS]), self.o_d.eq(s[:WIDTH])]
        m.d.sync += self.o_q.eq(d_dst)
        return m


class RwNoreg(_Rw):
    regmode = "NOREG"


class RwOutreg(_Rw):
    regmode = "OUTREG"
