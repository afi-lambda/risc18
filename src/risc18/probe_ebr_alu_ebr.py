"""Probe: is `EBR -> ALU -> EBR` one clock on this part?

A measurement-only module, not part of the design.  Three tops, all built by the
same flow:

  ProbeTop    read EBR -> 18-bit add -> write EBR, plus a registered sum output
              (the path is reported to the flop, so the number is a lower bound
              on the EBR-input cost).
  ProbeW      same chain, no sum output: the reported path has to end at the
              EBR write data/address setup.
  ProbeWMux   ProbeW with the second operand coming through a 16:1 register-read
              mux -- the shape a real "load, add, store" instruction has.

Both memories are 1024 x 18 with registered ports and real contents (source
initialised, destination both written and read back), so neither is folded away.

    .venv/bin/python tools/synth.py --top risc18.probe_ebr_alu_ebr:ProbeW \
        --name probe_ebr_alu_ebr_w --freq 100
    .venv/bin/python tools/synth.py --top risc18.probe_ebr_alu_ebr:ProbeWMux \
        --name probe_ebr_alu_ebr_wmux --freq 100

No tests, no golden model: this file exists to be built and read.
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


class _Base(wiring.Component):
    """Shared plumbing: the two EBRs and their ports."""

    def _ports(self, m):
        src, dst = _memories(m)
        return src.read_port(domain="sync"), dst.write_port(domain="sync"), \
            dst.read_port(domain="sync")


class ProbeTop(_Base):
    def __init__(self):
        super().__init__({
            "clk": In(1),
            "i_addr": In(10),
            "i_op": In(WIDTH),
            "o_sum": Out(WIDTH),
            "o_q": Out(WIDTH),
            "o_rd": Out(WIDTH),
        })
        self.ports = [self.clk, self.i_addr, self.i_op, self.o_sum, self.o_q, self.o_rd]

    def elaborate(self, platform):
        m = Module()
        rp, wp, dp = self._ports(m)

        r_addr = Signal(10)
        r_op = Signal(signed(WIDTH))
        q = Signal(WIDTH)
        d_q = Signal(WIDTH)

        m.d.sync += [r_addr.eq(self.i_addr), r_op.eq(self.i_op)]

        sum = Signal(signed(WIDTH + 1))
        m.d.comb += sum.eq(q + r_op)
        m.d.comb += [
            rp.addr.eq(r_addr), rp.en.eq(1), q.eq(rp.data),
            wp.addr.eq(sum[:10]), wp.data.eq(sum[:WIDTH]), wp.en.eq(1),
            dp.addr.eq(r_addr), dp.en.eq(1), d_q.eq(dp.data),
            self.o_sum.eq(sum[:WIDTH]),
        ]
        m.d.sync += [self.o_q.eq(q), self.o_rd.eq(d_q)]
        return m


class ProbeW(_Base):
    """The add feeds only the write port: no output register on the sum."""

    def __init__(self):
        super().__init__({
            "clk": In(1),
            "i_addr": In(10),
            "i_op": In(WIDTH),
            "o_q": Out(WIDTH),
            "o_rd": Out(WIDTH),
        })
        self.ports = [self.clk, self.i_addr, self.i_op, self.o_q, self.o_rd]

    def elaborate(self, platform):
        m = Module()
        rp, wp, dp = self._ports(m)

        r_addr = Signal(10)
        r_op = Signal(signed(WIDTH))
        q = Signal(WIDTH)
        d_q = Signal(WIDTH)

        m.d.sync += [r_addr.eq(self.i_addr), r_op.eq(self.i_op)]

        sum = Signal(signed(WIDTH + 1))
        m.d.comb += sum.eq(q + r_op)
        m.d.comb += [
            rp.addr.eq(r_addr), rp.en.eq(1), q.eq(rp.data),
            wp.addr.eq(sum[:10]), wp.data.eq(sum[:WIDTH]), wp.en.eq(1),
            dp.addr.eq(r_addr), dp.en.eq(1), d_q.eq(dp.data),
        ]
        m.d.sync += [self.o_q.eq(q), self.o_rd.eq(d_q)]
        return m


class ProbeWMux(_Base):
    """`ProbeW` with the second operand behind a 16:1 register-read mux."""

    def __init__(self):
        super().__init__({
            "clk": In(1),
            "i_addr": In(10),
            "i_sel": In(4),
            "o_q": Out(WIDTH),
            "o_rd": Out(WIDTH),
        })
        self.ports = [self.clk, self.i_addr, self.i_sel, self.o_q, self.o_rd]

    def elaborate(self, platform):
        m = Module()
        rp, wp, dp = self._ports(m)

        regs = [Signal(signed(WIDTH), name=f"reg{i}") for i in range(16)]
        r_addr = Signal(10)
        r_sel = Signal(4)
        q = Signal(WIDTH)
        d_q = Signal(WIDTH)

        m.d.sync += [r_addr.eq(self.i_addr), r_sel.eq(self.i_sel)]
        m.d.sync += [r.eq(r + 1) for r in regs]

        b = Signal(signed(WIDTH))
        with m.Switch(r_sel):
            for i in range(16):
                with m.Case(i):
                    m.d.comb += b.eq(regs[i])

        sum = Signal(signed(WIDTH + 1))
        m.d.comb += sum.eq(q + b)
        m.d.comb += [
            rp.addr.eq(r_addr), rp.en.eq(1), q.eq(rp.data),
            wp.addr.eq(sum[:10]), wp.data.eq(sum[:WIDTH]), wp.en.eq(1),
            dp.addr.eq(r_addr), dp.en.eq(1), d_q.eq(dp.data),
        ]
        m.d.sync += [self.o_q.eq(q), self.o_rd.eq(d_q)]
        return m


class ProbeWDsp(_Base):
    """`ProbeW` with a full 18x18 multiply instead of the add.

    Models a streaming kernel: EBR operand -> MULT18X18D -> EBR write, one clock.
    """

    def __init__(self):
        super().__init__({
            "clk": In(1),
            "i_addr": In(10),
            "i_coef": In(WIDTH),
            "o_q": Out(WIDTH),
            "o_rd": Out(WIDTH),
        })
        self.ports = [self.clk, self.i_addr, self.i_coef, self.o_q, self.o_rd]

    def elaborate(self, platform):
        m = Module()
        rp, wp, dp = self._ports(m)

        r_addr = Signal(10)
        r_coef = Signal(signed(WIDTH))
        q = Signal(WIDTH)
        d_q = Signal(WIDTH)

        m.d.sync += [r_addr.eq(self.i_addr), r_coef.eq(self.i_coef)]

        prod = Signal(signed(2 * WIDTH))
        m.d.comb += prod.eq(q * r_coef)
        m.d.comb += [
            rp.addr.eq(r_addr), rp.en.eq(1), q.eq(rp.data),
            wp.addr.eq(r_addr), wp.data.eq(prod[:WIDTH]), wp.en.eq(1),
            dp.addr.eq(r_addr), dp.en.eq(1), d_q.eq(dp.data),
        ]
        m.d.sync += [self.o_q.eq(q), self.o_rd.eq(d_q)]
        return m
