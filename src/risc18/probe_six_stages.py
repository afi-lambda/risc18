"""Probes for the 6-stage feasibility question: does each stage close at 120 MHz?

Three tops, each one *composed stage* of the proposed schedule, built at the
constraint the plan needs (8.33 ns). They are the PoC's stage-0: nothing here
changes the design, it only prices the three cones that the 6-stage cut would
put in separate stages.

  ProbeFetchIn   PC -> (+1 adder | redirect target) mux -> hold enable -> imem AD,
                 imem DOA -> capture FF.  Tests the *input* side of the EBR, which
                 the 6.7 ns read probe never measured.
  ProbeDstage    instruction FF -> decode -> regfile read-address select ->
                 distributed-RAM read -> FF.  This is the current 13.5 ns chain
                 minus the 5.6 ns EBR read, i.e. what the D stage becomes after
                 the cut.
  ProbeXstage    operand regs -> add and multiply in parallel -> result select,
                 plus branch compare and target adder -> FF.  The schedule puts
                 all of X in one stage; this is the cone that decides whether
                 120 MHz is reachable with a multiply in the machine.

    .venv/bin/python tools/synth.py --top risc18.probe_six_stages:ProbeFetchIn \
        --name probe_six_fetchin --freq 120
"""

from amaranth import Elaboratable, Memory, Module, Signal, signed
from amaranth.lib import wiring
from amaranth.lib.wiring import In, Out

WIDTH = 18
DEPTH = 1024
MASK = (1 << WIDTH) - 1
INIT = [(i * 7 + 1) & MASK for i in range(DEPTH)]


class ProbeFetchIn(wiring.Component):
    def __init__(self):
        super().__init__({
            "clk": In(1),
            "i_redirect": In(1),
            "i_target": In(16),
            "i_hold": In(1),
            "o_instr": Out(WIDTH),
            "o_pc": Out(16),
        })
        self.ports = [self.clk, self.i_redirect, self.i_target, self.i_hold,
                      self.o_instr, self.o_pc]

    def elaborate(self, platform):
        m = Module()
        mem = Memory(width=WIDTH, depth=DEPTH, init=INIT)
        m.submodules.mem = mem
        rp = mem.read_port(domain="sync")

        pc = Signal(16)
        nxt = Signal(16)
        # PC + 1 and the redirect target meet in a mux, then a hold gate, then
        # the EBR address: the exact shape the F1 stage has.
        m.d.comb += nxt.eq(pc + 1)
        with m.If(self.i_redirect):
            m.d.comb += rp.addr.eq(self.i_target[:10])
        with m.Else():
            m.d.comb += rp.addr.eq(nxt[:10])
        m.d.comb += rp.en.eq(~self.i_hold)

        m.d.sync += [self.o_instr.eq(rp.data), self.o_pc.eq(pc)]
        with m.If(~self.i_hold):
            with m.If(self.i_redirect):
                m.d.sync += pc.eq(self.i_target)
            with m.Else():
                m.d.sync += pc.eq(nxt)
        return m


class ProbeDstage(wiring.Component):
    """Instruction word -> decode -> regfile address -> LUT-RAM read -> FF."""

    def __init__(self):
        super().__init__({
            "clk": In(1),
            "i_word": In(WIDTH),
            "o_a": Out(WIDTH),
            "o_b": Out(WIDTH),
        })
        self.ports = [self.clk, self.i_word, self.o_a, self.o_b]

    def elaborate(self, platform):
        m = Module()
        rf = Memory(width=WIDTH, depth=16, init=[(i * 37 + 3) & MASK for i in range(16)])
        m.submodules.rf = rf
        ra = rf.read_port(domain="sync")
        rb = rf.read_port(domain="sync")
        rw = rf.write_port(domain="sync")

        word = Signal(WIDTH)
        m.d.sync += word.eq(self.i_word)
        # A live write port keeps the contents dynamic; without it yosys folds the
        # read of a constant memory into LUT constants and the probe measures nothing.
        m.d.comb += [rw.addr.eq(word[10:14]), rw.data.eq(word), rw.en.eq(1)]

        op = Signal(4)
        rd = Signal(4)
        rs1 = Signal(4)
        rs2 = Signal(4)
        is_store = Signal()
        m.d.comb += [
            op.eq(word[14:18]), rd.eq(word[10:14]),
            rs1.eq(word[6:10]), rs2.eq(word[10:14]),
            is_store.eq(op == 5),
        ]

        # Address select, the stall enable, and the write-through bypass are the
        # things that sit in front of a LUT-RAM read port in the real core.
        hold = Signal()
        m.d.sync += hold.eq(0)
        m.d.comb += [
            ra.addr.eq(rs1), ra.en.eq(~hold),
            rb.addr.eq(rs2), rb.en.eq(~hold),
            self.o_a.eq(ra.data), self.o_b.eq(rb.data),
        ]
        return m


class ProbeXstage(wiring.Component):
    """Everything the schedule puts in X: add, multiply, select, compare, target."""

    def __init__(self):
        super().__init__({
            "clk": In(1),
            "i_a": In(WIDTH),
            "i_b": In(WIDTH),
            "i_imm": In(16),
            "i_op": In(4),
            "o_val": Out(WIDTH),
            "o_tgt": Out(16),
            "o_eq": Out(1),
        })
        self.ports = [self.clk, self.i_a, self.i_b, self.i_imm, self.i_op,
                      self.o_val, self.o_tgt, self.o_eq]

    def elaborate(self, platform):
        m = Module()
        a = Signal(signed(WIDTH))
        b = Signal(signed(WIDTH))
        imm = Signal(signed(16))
        op = Signal(4)
        m.d.sync += [a.eq(self.i_a), b.eq(self.i_b),
                     imm.eq(self.i_imm), op.eq(self.i_op)]

        add = Signal(signed(WIDTH))
        mul = Signal(signed(2 * WIDTH))
        m.d.comb += [add.eq(a + b), mul.eq(a * b)]

        val = Signal(WIDTH)
        with m.Switch(op):
            with m.Case(0):
                m.d.comb += val.eq(add)
            with m.Case(1):
                m.d.comb += val.eq(mul[:WIDTH])
            with m.Case(2):
                m.d.comb += val.eq(a & b)
            with m.Case(3):
                m.d.comb += val.eq(a | b)
            with m.Default():
                m.d.comb += val.eq((a + imm)[:WIDTH])

        tgt = Signal(16)
        m.d.comb += tgt.eq((a + imm)[:16])

        m.d.sync += [self.o_val.eq(val), self.o_tgt.eq(tgt),
                     self.o_eq.eq(a == b)]
        return m


class ProbeXstageNoMul(wiring.Component):
    """X without the multiply: the same select tree, compare and target adder."""

    def __init__(self):
        super().__init__({
            "clk": In(1),
            "i_a": In(WIDTH),
            "i_b": In(WIDTH),
            "i_imm": In(16),
            "i_op": In(4),
            "o_val": Out(WIDTH),
            "o_tgt": Out(16),
            "o_eq": Out(1),
        })
        self.ports = [self.clk, self.i_a, self.i_b, self.i_imm, self.i_op,
                      self.o_val, self.o_tgt, self.o_eq]

    def elaborate(self, platform):
        m = Module()
        a = Signal(signed(WIDTH))
        b = Signal(signed(WIDTH))
        imm = Signal(signed(16))
        op = Signal(4)
        m.d.sync += [a.eq(self.i_a), b.eq(self.i_b),
                     imm.eq(self.i_imm), op.eq(self.i_op)]

        add = Signal(signed(WIDTH))
        m.d.comb += add.eq(a + b)

        val = Signal(WIDTH)
        with m.Switch(op):
            with m.Case(0):
                m.d.comb += val.eq(add)
            with m.Case(2):
                m.d.comb += val.eq(a & b)
            with m.Case(3):
                m.d.comb += val.eq(a | b)
            with m.Default():
                m.d.comb += val.eq((a + imm)[:WIDTH])

        tgt = Signal(16)
        m.d.comb += tgt.eq((a + imm)[:16])

        m.d.sync += [self.o_val.eq(val), self.o_tgt.eq(tgt), self.o_eq.eq(a == b)]
        return m


class ProbeXstageMulReg(ProbeXstage):
    """X with the multiply result registered before the select tree.

    The multiply gets its own latch (or its own completion edge), so the select
    tree and the branch target see a register instead of a 3.9 ns DSP arc.
    """

    def elaborate(self, platform):
        m = Module()
        a = Signal(signed(WIDTH))
        b = Signal(signed(WIDTH))
        imm = Signal(signed(16))
        op = Signal(4)
        m.d.sync += [a.eq(self.i_a), b.eq(self.i_b),
                     imm.eq(self.i_imm), op.eq(self.i_op)]

        add = Signal(signed(WIDTH))
        mul = Signal(signed(2 * WIDTH))
        mul_q = Signal(WIDTH)
        m.d.comb += [add.eq(a + b), mul.eq(a * b)]
        m.d.sync += mul_q.eq(mul[:WIDTH])

        val = Signal(WIDTH)
        with m.Switch(op):
            with m.Case(0):
                m.d.comb += val.eq(add)
            with m.Case(1):
                m.d.comb += val.eq(mul_q)
            with m.Case(2):
                m.d.comb += val.eq(a & b)
            with m.Case(3):
                m.d.comb += val.eq(a | b)
            with m.Default():
                m.d.comb += val.eq((a + imm)[:WIDTH])

        tgt = Signal(16)
        m.d.comb += tgt.eq((a + imm)[:16])

        m.d.sync += [self.o_val.eq(val), self.o_tgt.eq(tgt), self.o_eq.eq(a == b)]
        return m


class ProbeXstageRF(wiring.Component):
    """X with the *real* operand path: DPRAM register file + bypass + ALU + compare.

    The review's P0b requirement: the stage that matters is not the bare adder, it is
    read-operands-from-the-file, select the forward, add/multiply, compare, and form the
    target -- all in one cycle. A 16x18 two-read one-write file maps to
    TRELLIS_DPR16X4 (distributed RAM), so the read is address-to-data inside the cycle,
    exactly as the shipping core builds it.
    """

    with_target = True            # False: PC-relative target is formed in D instead

    def __init__(self):
        super().__init__({
            "clk": In(1),
            "i_ra": In(4),
            "i_rb": In(4),
            "i_waddr": In(4),
            "i_wdata": In(WIDTH),
            "i_we": In(1),
            "i_fwd": In(2),          # 0 none, 1 forward to a, 2 forward to b
            "i_imm": In(16),
            "i_op": In(4),
            "o_val": Out(WIDTH),
            "o_tgt": Out(16),
            "o_eq": Out(1),
        })
        self.ports = [self.clk, self.i_ra, self.i_rb, self.i_waddr, self.i_wdata,
                      self.i_we, self.i_fwd, self.i_imm, self.i_op,
                      self.o_val, self.o_tgt, self.o_eq]

    def elaborate(self, platform):
        m = Module()
        rf = Memory(width=WIDTH, depth=16,
                    init=[(i * 7919 + 13) & MASK for i in range(16)])
        m.submodules.rf = rf
        ra = rf.read_port(domain="sync")
        rb = rf.read_port(domain="sync")
        wr = rf.write_port(domain="sync")

        m.d.comb += [ra.addr.eq(self.i_ra), ra.en.eq(1),
                     rb.addr.eq(self.i_rb), rb.en.eq(1),
                     wr.addr.eq(self.i_waddr), wr.data.eq(self.i_wdata),
                     wr.en.eq(self.i_we)]

        a = Signal(signed(WIDTH))
        b = Signal(signed(WIDTH))
        with m.Switch(self.i_fwd):
            with m.Case(0):
                m.d.comb += [a.eq(ra.data), b.eq(rb.data)]
            with m.Case(1):
                m.d.comb += [a.eq(self.i_wdata), b.eq(rb.data)]
            with m.Case(2):
                m.d.comb += [a.eq(ra.data), b.eq(self.i_wdata)]
            with m.Default():
                m.d.comb += [a.eq(self.i_wdata), b.eq(self.i_wdata)]

        add = Signal(signed(WIDTH))
        mul = Signal(signed(2 * WIDTH))
        m.d.comb += [add.eq(a + b), mul.eq(a * b)]

        val = Signal(WIDTH)
        with m.Switch(self.i_op):
            with m.Case(0):
                m.d.comb += val.eq(add)
            with m.Case(1):
                m.d.comb += val.eq(mul[:WIDTH])
            with m.Case(2):
                m.d.comb += val.eq(a & b)
            with m.Case(3):
                m.d.comb += val.eq(a | b)
            with m.Default():
                m.d.comb += val.eq((ra.data + self.i_imm)[:WIDTH])

        tgt = Signal(16)
        if self.with_target:
            m.d.comb += tgt.eq((ra.data + self.i_imm)[:16])
        else:
            m.d.comb += tgt.eq(ra.data)
        m.d.sync += [self.o_val.eq(val), self.o_tgt.eq(tgt),
                     self.o_eq.eq(a == b)]
        return m


class ProbeXstageRFNoTgt(ProbeXstageRF):
    """The plan's actual X: the target adder lives in D, so X is file+bypass+ALU+compare."""

    with_target = False


class ProbeRstage(wiring.Component):
    """The new R stage: register-file read plus the W->R bypass select, nothing else.

    This is the stage the seven-stage contract adds. P0b showed the realistic X stage
    (file read + bypass + ALU + compare) closes only at 83.4 MHz; R takes the first two
    terms out of that cycle and leaves the ALU with latched operands.
    """

    with_fwd = True

    def __init__(self):
        super().__init__({
            "clk": In(1),
            "i_ra": In(4),
            "i_rb": In(4),
            "i_waddr": In(4),
            "i_wdata": In(WIDTH),
            "i_we": In(1),
            "i_fwd": In(2),
            "o_a": Out(WIDTH),
            "o_b": Out(WIDTH),
        })
        self.ports = [self.clk, self.i_ra, self.i_rb, self.i_waddr, self.i_wdata,
                      self.i_we, self.i_fwd, self.o_a, self.o_b]

    def elaborate(self, platform):
        m = Module()
        rf = Memory(width=WIDTH, depth=16,
                    init=[(i * 7919 + 13) & MASK for i in range(16)])
        m.submodules.rf = rf
        ra = rf.read_port(domain="sync")
        rb = rf.read_port(domain="sync")
        wr = rf.write_port(domain="sync")
        m.d.comb += [ra.addr.eq(self.i_ra), ra.en.eq(1),
                     rb.addr.eq(self.i_rb), rb.en.eq(1),
                     wr.addr.eq(self.i_waddr), wr.data.eq(self.i_wdata),
                     wr.en.eq(self.i_we)]

        a = Signal(WIDTH)
        b = Signal(WIDTH)
        if self.with_fwd:
            with m.Switch(self.i_fwd):
                with m.Case(0):
                    m.d.comb += [a.eq(ra.data), b.eq(rb.data)]
                with m.Case(1):
                    m.d.comb += [a.eq(self.i_wdata), b.eq(rb.data)]
                with m.Case(2):
                    m.d.comb += [a.eq(ra.data), b.eq(self.i_wdata)]
                with m.Default():
                    m.d.comb += [a.eq(self.i_wdata), b.eq(self.i_wdata)]
        else:
            m.d.comb += [a.eq(ra.data), b.eq(rb.data)]

        m.d.sync += [self.o_a.eq(a), self.o_b.eq(b)]
        return m


class ProbeRstageNoFwd(ProbeRstage):
    """R without the bypass mux: the cost of the forward alone."""

    with_fwd = False
