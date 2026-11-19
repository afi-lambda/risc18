"""Probe: does `EBR -> ALU -> EBR` fit a 120/150 MHz clock when split over two cycles?

`probe_ebr_alu_ebr.ProbeW` measured the whole chain in one clock: 9.0 ns
(111.53 MHz), with `src.0.0.DOA2` -> add carry chain -> `dst` write data. This
module asks the follow-up question the architecture decision rests on: split the
same operation at a register, what do the two halves cost?

  Probe2C      cycle N:   read EBR -> q_r (fabric register)
               cycle N+1: q_r + op_r -> add -> write EBR (address and data)
  Probe2CMux   same, with the second operand behind a 16:1 register-read mux

The register `q_r` is the split point. It cannot be optimised away: removing it
changes the cycle in which the sum is written. The reported critical path is
therefore the worse of the two halves, and because nextpnr reports the maximum,
a run whose worst path is the read half also bounds the write half.

Build:

    .venv/bin/python tools/synth.py --top risc18.probe_mem_pipe:Probe2C \
        --name probe_mem_2c_s1 --freq 150 --seed 1
    .venv/bin/python tools/synth.py --top risc18.probe_mem_pipe:Probe2CMux \
        --name probe_mem_2cmux_s1 --freq 150 --seed 1

Measurement only; no tests, no golden model.
"""

from amaranth import Elaboratable, Memory, Module, Signal, signed
from amaranth.lib import wiring
from amaranth.lib.wiring import In, Out

DEPTH = 1024
WIDTH = 18
MASK = (1 << WIDTH) - 1
INIT = [(i * 7 + 1) & MASK for i in range(DEPTH)]


def _memories(m):
    src = Memory(width=WIDTH, depth=DEPTH, init=INIT)
    dst = Memory(width=WIDTH, depth=DEPTH, init=[])
    m.submodules.src = src
    m.submodules.dst = dst
    return src, dst


class Probe2C(Elaboratable):
    """Two-cycle EBR -> add -> EBR: the shape a load-add-store pipeline has."""

    def __init__(self):
        self.clk = Signal()
        self.i_addr = Signal(10)
        self.i_op = Signal(signed(WIDTH))
        self.o_qr = Signal(WIDTH)   # observation tap on the split register
        self.o_rd = Signal(WIDTH)   # read-back so `dst` is not folded away
        self.ports = [self.clk, self.i_addr, self.i_op, self.o_qr, self.o_rd]

    def elaborate(self, platform):
        m = Module()
        src, dst = _memories(m)
        rp = src.read_port(domain="sync")
        wp = dst.write_port(domain="sync")
        dp = dst.read_port(domain="sync")

        r_addr = Signal(10)
        r_op = Signal(signed(WIDTH))
        q_r = Signal(signed(WIDTH))
        d_q = Signal(WIDTH)

        m.d.sync += [r_addr.eq(self.i_addr), r_op.eq(self.i_op)]

        # cycle N: the EBR's registered address presents data on rp.data
        m.d.comb += [rp.addr.eq(r_addr), rp.en.eq(1)]

        # the split: capture the operand, add it in the NEXT cycle
        m.d.sync += q_r.eq(rp.data)

        s = Signal(signed(WIDTH + 1))
        m.d.comb += s.eq(q_r + r_op)
        m.d.comb += [
            wp.addr.eq(s[:10]), wp.data.eq(s[:WIDTH]), wp.en.eq(1),
            dp.addr.eq(r_addr), dp.en.eq(1), d_q.eq(dp.data),
        ]
        m.d.sync += [self.o_qr.eq(q_r), self.o_rd.eq(d_q)]
        return m


class Probe2CMux(Probe2C):
    """`Probe2C` with the second operand behind a 16:1 register-read mux."""

    def __init__(self):
        super().__init__()
        self.i_sel = Signal(4)
        self.ports = [self.clk, self.i_addr, self.i_op, self.i_sel, self.o_qr, self.o_rd]

    def elaborate(self, platform):
        m = Module()
        src, dst = _memories(m)
        rp = src.read_port(domain="sync")
        wp = dst.write_port(domain="sync")
        dp = dst.read_port(domain="sync")

        regs = [Signal(signed(WIDTH), name=f"reg{i}") for i in range(16)]
        r_addr = Signal(10)
        r_sel = Signal(4)
        q_r = Signal(signed(WIDTH))
        d_q = Signal(WIDTH)

        m.d.sync += [r_addr.eq(self.i_addr), r_sel.eq(self.i_sel)]
        m.d.sync += [r.eq(r + 1) for r in regs]
        m.d.comb += [rp.addr.eq(r_addr), rp.en.eq(1)]
        m.d.sync += q_r.eq(rp.data)

        b = Signal(signed(WIDTH))
        with m.Switch(r_sel):
            for i in range(16):
                with m.Case(i):
                    m.d.comb += b.eq(regs[i])

        s = Signal(signed(WIDTH + 1))
        m.d.comb += s.eq(q_r + b)
        m.d.comb += [
            wp.addr.eq(s[:10]), wp.data.eq(s[:WIDTH]), wp.en.eq(1),
            dp.addr.eq(r_addr), dp.en.eq(1), d_q.eq(dp.data),
        ]
        m.d.sync += [self.o_qr.eq(q_r), self.o_rd.eq(d_q)]
        return m
