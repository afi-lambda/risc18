"""Probe: what fmax does an EBR read port run at, registered in and out?

Measurement-only, like `probe_ebr_alu_ebr`.  Two tops:

  ProbeRd      10-bit counter FF -> read address, registered read data -> FF.
               The reported path is whichever is longer: address-setup or
               clock-to-out into the output register.
  ProbeRdLoop  the read data feeds the next address (a dependent read chain, i.e.
               one read per cycle).  This is the throughput limit of the block
               itself: clock-to-out + routing + address setup.

    .venv/bin/python tools/synth.py --top risc18.probe_ebr_read:ProbeRd \
        --name probe_ebr_read --freq 150
    .venv/bin/python tools/synth.py --top risc18.probe_ebr_read:ProbeRdLoop \
        --name probe_ebr_read_loop --freq 150
"""

from amaranth import Memory, Module, Signal
from amaranth.lib import wiring
from amaranth.lib.wiring import In, Out

DEPTH = 1024
WIDTH = 18
MASK = (1 << WIDTH) - 1
INIT = [(i * 7 + 1) & MASK for i in range(DEPTH)]


class ProbeRd(wiring.Component):
    def __init__(self):
        super().__init__({
            "clk": In(1),
            "o_q": Out(WIDTH),
            "o_a": Out(10),
        })
        self.ports = [self.clk, self.o_q, self.o_a]

    def elaborate(self, platform):
        m = Module()
        mem = Memory(width=WIDTH, depth=DEPTH, init=INIT)
        m.submodules.mem = mem
        rp = mem.read_port(domain="sync")

        addr = Signal(10)
        q = Signal(WIDTH)
        a_q = Signal(10)

        m.d.sync += [addr.eq(addr + 1), q.eq(rp.data), a_q.eq(addr)]
        m.d.comb += [rp.addr.eq(addr), rp.en.eq(1),
                     self.o_q.eq(q), self.o_a.eq(a_q)]
        return m


class ProbeRdLoop(wiring.Component):
    def __init__(self):
        super().__init__({
            "clk": In(1),
            "o_q": Out(WIDTH),
            "o_a": Out(10),
        })
        self.ports = [self.clk, self.o_q, self.o_a]

    def elaborate(self, platform):
        m = Module()
        mem = Memory(width=WIDTH, depth=DEPTH, init=INIT)
        m.submodules.mem = mem
        rp = mem.read_port(domain="sync")

        # The read data *is* the next address: a dependent chain through the block.
        addr = Signal(10)
        q = Signal(WIDTH)

        m.d.comb += [rp.addr.eq(addr), rp.en.eq(1), q.eq(rp.data)]
        m.d.sync += [addr.eq(q[:10]), self.o_q.eq(q), self.o_a.eq(addr)]
        return m


class ProbeRdDec(wiring.Component):
    """EBR read -> combinational decode of the word just read -> FF.

    This is the "consumer in the same stage" case: the decode is not overhead
    bolted onto the read, it is the instruction's decode of the word the block
    returned, so it sits on the same path by construction.
    """

    def __init__(self):
        super().__init__({
            "clk": In(1),
            "o_ctrl": Out(12),
            "o_rd": Out(4),
            "o_rs1": Out(4),
            "o_imm": Out(6),
        })
        self.ports = [self.clk, self.o_ctrl, self.o_rd, self.o_rs1, self.o_imm]

    def elaborate(self, platform):
        m = Module()
        mem = Memory(width=WIDTH, depth=DEPTH, init=INIT)
        m.submodules.mem = mem
        rp = mem.read_port(domain="sync")

        addr = Signal(10)
        q = Signal(WIDTH)
        ctrl = Signal(12)
        rd = Signal(4)
        rs1 = Signal(4)
        imm = Signal(6)

        m.d.comb += [rp.addr.eq(addr), rp.en.eq(1), q.eq(rp.data)]
        with m.Switch(q[14:18]):
            with m.Case(0):
                m.d.comb += ctrl.eq(0x001)
            with m.Case(1):
                m.d.comb += ctrl.eq(0x003)
            with m.Case(2):
                m.d.comb += ctrl.eq(0x007)
            with m.Case(3):
                m.d.comb += ctrl.eq(0x00F)
            with m.Case(4):
                m.d.comb += ctrl.eq(0x01F)
            with m.Case(5):
                m.d.comb += ctrl.eq(0x03F)
            with m.Case(6):
                m.d.comb += ctrl.eq(0x07F)
            with m.Case(7):
                m.d.comb += ctrl.eq(0x0FF)
            with m.Default():
                m.d.comb += ctrl.eq(0x1FF)

        m.d.sync += [addr.eq(addr + 1), self.o_ctrl.eq(ctrl),
                     self.o_rd.eq(q[10:14]), self.o_rs1.eq(q[6:10]),
                     self.o_imm.eq(q[0:6])]
        return m
