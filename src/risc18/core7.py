"""Seven-stage in-order risc18 core: F/IQ/D/R/X/M/W (simulation model).

``Core`` resolves PC -> instruction EBR -> decode -> register-file read -> ALU in
fewer edges and pays for it in f_max.  ``Core6`` cut the EBR-output-to-decode
path in two.  ``Core7`` takes the next cut and *deepens* the pipeline, because
the contract (``docs/ISA.md`` section 7) is edge-explicit about the memories:

    F   PC register -> instruction EBR address
    IQ  instruction EBR output -> fetch register ``fq`` -> decode
    D   decode fields registered; register-file read address presented
    R   register-file data captured; bypass select; operands registered
    X   ALU / compare / multiply issue; effective address
    M   data EBR address presented (store data too); load launched
    W   register-file writeback; registered load word

Both memories are EBR with **latency 2 by edge contract**: the address register
is loaded on the closing edge of the presenting stage and the word is captured
one edge later.  So the instruction EBR's clock-to-out sits alone in the IQ
cycle (F presents the PC, IQ registers the word), and the data EBR's
clock-to-out sits alone in the M cycle (X presents the effective address, M
registers the word, W exposes it).

Pipeline registers, one per stage boundary (the same names the module uses):

    fq (IQ) -> fd (D) -> rx (R) -> xm (M) -> mw (W) -> retire outputs

F and IQ are the presenting and the capturing edge of one EBR access, exactly as
``Core6``'s ``F1`` is (PC -> EBR -> ``fq``); the five registers above are
therefore the same five boundaries ``Core6`` has, and an instruction whose PC
becomes the fetch address on cycle *c* retires on cycle *c + 6* in both cores.
What the R register changes is not the latency but *what sits in the X cycle*:
the file read and its bypass select happen in D and end at ``rx``, so the ALU
cycle starts at a flop and the data EBR's clock-to-out never reaches the ALU
input.  ``tests/test_core7.py`` pins the fill (word 0 retires on cycle 6 of the
driver's count).

Bubbles (section 7.2).  A register write commits on the closing edge of W and a
consumer latches its operands at the closing edge of D, so with the pipeline
this deep the dependency distances are:

* **ALU or MUL at distance 1 -> 0 bubbles.**  The producer is in X while the
  consumer reads its operands in D, so an ALU/link/LUI result is taken
  combinationally into ``rx``; a MUL's product is registered at the end of its X
  and is therefore picked up one cycle later by the **M -> X input bypass**
  (``fwd_value`` muxed onto the ALU input while the producer is in M).
* **ALU or MUL at distance >= 2 -> 0 bubbles.**  The producer is in M or later
  when the consumer reads its operands: ``m_value`` for distance 2, and the
  register file's same-cycle write-through for distance 3 and beyond (the
  producer's W cycle is the consumer's D cycle at distance 3, and the array
  holds the word from distance 4 on).
* **load at distance 1 -> exactly 1 bubble.**  The word is registered only at
  the end of the load's M, i.e. one cycle after the consumer's X opens, so the
  consumer is held in D for one cycle and then takes the word from ``m_value``
  while the load is in M (the EBR output is valid in M, and that mux feeds the
  ``rx`` latch, never the ALU input, so the EBR arc stays out of the X path).
* **load at distance >= 2 -> 0 bubbles.**

Control transfer (section 7.3): the PC-relative target is computed in **D**
(``BranchTarget`` on the registered ``fd`` fields) and travels down the pipe in
``rx_target``; the compare runs in **X** on the R-latched operands and its
decision is registered into a control-transfer register that the fetch applies
on the following edge.  The kill window is two cycles (``ct_valid`` and the edge
behind it), exactly like ``Core6``'s, so a taken transfer costs 5 retire slots --
the same 5 ``Core6`` pays, because the extra stage Core7 adds (R) sits on the
fetch side of X in place of Core6's D stage.  A not-taken branch costs nothing,
killed slots never enter X as valid, so none can retire, write a register, write
memory, link or halt.

HALT, reset and the retire trace are ``Core``'s: HALT decodes in D, freezes the
fetch there and squashes everything younger while it retires through W; reset
clears the pipeline, the register file and ``fq`` and performs no memory writes;
``o_retire`` pulses once per instruction leaving W with ``o_retire_pc`` /
``o_retire_we`` / ``o_retire_rd`` / ``o_retire_value`` valid with it.

Simulation only: this file is the behavioural contract the routed build is
measured against.  It reuses the frozen units and changes no ISA, golden-model
or existing-core behaviour.
"""

from amaranth import ClockDomain, Elaboratable, Module, Mux, Signal

from . import isa
from .alu import ALU
from .branch import BranchUnit
from .decoder import Decoder
from .dmem import DataMemory
from .imem import Fetch
from .pc import BranchTarget
from .regfile import RegFile

__all__ = ["Core7"]

PC_WIDTH = 16


class _DecodedFields:
    """Read-only view of the FD decode bundle (one ``Signal`` per field)."""

    def __init__(self, fields):
        self.__dict__.update(fields)


class Core7(Elaboratable):
    """F/IQ/D/R/X/M/W risc18 core: operands registered, memory latency 2."""

    def __init__(self, program=(), data=None, ram_rf=False, x_bypass=True):
        self.program = list(program)
        #: Optional 1024-word data-memory image (padded/truncated by DataMemory).
        self.data = list(data) if data is not None else []
        #: M -> X input bypass: distance-1 ALU and MUL producers cost nothing
        #: (section 7.2's first row).  ``False`` selects the section 7.2
        #: one-bubble fallback: no input mux in the X stage and one bubble for
        #: every distance-1 dependency, load included.  The default is measured
        #: against the fallback in ``docs/metrics/core7_sim.md``.
        self.x_bypass = bool(x_bypass)
        #: Accepted for interface parity with ``Core``/``Core6``; section 7.2's
        #: forward table is stated for the flop file, whose reads are
        #: combinational in D, and ``RegFileRam``'s registered read cannot be
        #: presented from D without an extra stage (section 7.4 forbids adding
        #: one).  Only ``ram_rf=False`` is implemented.
        if ram_rf:
            raise ValueError(
                "Core7 implements the flop register file only: RegFileRam's "
                "registered read cannot be presented from D in a seven-stage "
                "pipeline (docs/ISA.md 7.4)")
        self.ram_rf = False

        #: Synchronous active-high reset, wired to the ``sync`` domain's rst.
        self.i_rst = Signal(1)

        self.o_pc = Signal(PC_WIDTH)
        self.o_retire = Signal(1)
        self.o_retire_pc = Signal(PC_WIDTH)
        self.o_retire_we = Signal(1)
        self.o_retire_rd = Signal(4)
        self.o_retire_value = Signal(isa.WIDTH)
        self.o_halt = Signal(1)

        #: The ``sync`` domain this core owns (same shape as ``Core``).
        self.cd = ClockDomain("sync")

    # -- elaboration ----------------------------------------------------
    def elaborate(self, platform):
        m = Module()
        m.domains += self.cd
        m.d.comb += self.cd.rst.eq(self.i_rst)

        m.submodules.fetch = fetch = Fetch(self.program)
        m.submodules.decoder = decoder = Decoder()
        m.submodules.regfile = regfile = RegFile()
        m.submodules.alu = alu = ALU()
        m.submodules.branch = branch = BranchUnit()
        m.submodules.target = target = BranchTarget()
        m.submodules.dmem = dmem = DataMemory(self.data)

        fd_dec = {name: Signal(len(getattr(decoder.o, name)))
                  for name in vars(decoder.o)}
        o = _DecodedFields(fd_dec)
        #: White-box observability (same hooks as ``Core``/``Core6``).
        self.fd_valid = None
        self.fd_fields = fd_dec
        self.fq_valid = None
        self.rx_valid = None
        self.ct_valid = None

        # ------------------------------------------------------------ F ---
        fetch_alive = Signal()
        m.d.sync += fetch_alive.eq(1)      # 0 only in the post-reset cycle

        # ----------------------------------------------------------- IQ ---
        fq_valid = Signal()
        fq_pc = Signal(PC_WIDTH)
        fq_instr = Signal(isa.WIDTH)
        self.fq_valid = fq_valid

        m.d.comb += decoder.i_instr.eq(fq_instr)

        # ------------------------------------------------------------ D ---
        fd_valid = Signal()
        fd_pc = Signal(PC_WIDTH)
        self.fd_valid = fd_valid

        # ------------------------------------------------------------ R ---
        #: The R stage's registers: operands (already bypassed at the D -> R
        #: edge) plus everything X needs.
        rx_valid = Signal()
        rx_pc = Signal(PC_WIDTH)
        rx_we = Signal()
        rx_rd = Signal(4)
        rx_a = Signal(isa.WIDTH)
        rx_bv = Signal(isa.WIDTH)          # rs2 value (ALU B / store data / cmp)
        rx_imm = Signal(isa.WIDTH)
        rx_target = Signal(PC_WIDTH)       # PC-relative target, computed in D
        rx_alu_op = Signal(3)
        rx_alu_src_imm = Signal()
        rx_shift_amt = Signal(4)
        rx_mul = Signal()
        rx_lui = Signal()
        rx_link = Signal()
        rx_halt = Signal()
        rx_mem_read = Signal()
        rx_mem_write = Signal()
        rx_branch = Signal()
        rx_cond = Signal(2)
        rx_jump_reg = Signal()
        self.rx_valid = rx_valid

        # ---- X-stage results (declared here: D's forwarding reads them) ----
        #: X-stage result (ALU / link / LUI), forwarded into D one instruction
        #: ahead of its writeback.
        x_value = Signal(isa.WIDTH)
        #: Registered multiply product: written on the closing edge of the
        #: MUL's X, so it is what M carries (``m_value``) one cycle later.
        x_prod = Signal(isa.WIDTH)

        # ---- M-stage registers -------------------------------------------
        xm_valid = Signal()
        xm_pc = Signal(PC_WIDTH)
        xm_we = Signal()
        xm_rd = Signal(4)
        xm_value = Signal(isa.WIDTH)
        xm_mem_read = Signal()
        xm_mul = Signal()
        xm_halt = Signal()

        #: What M carries: the data EBR word for a load (valid during M, because
        #: X presented the address and the block sampled it on the X -> M edge),
        #: the registered product for a MUL, the X-stage result otherwise.
        m_value = Signal(isa.WIDTH)
        m.d.comb += m_value.eq(
            Mux(xm_mem_read, dmem.o_rdata, Mux(xm_mul, x_prod, xm_value))
        )

        #: The same value with the data EBR left out: registered producers only
        #: (``xm_value`` and ``x_prod`` are both flops).  This is what the X
        #: input bypass reads, so the EBR's clock-to-out never reaches the ALU
        #: or the compare -- that would put 5.6 ns of EBR arc inside the X
        #: cycle the contract budgets for the ALU.  A load producer is never
        #: bypassed into X: distance 1 stalls, and from distance 2 the word is
        #: registered in W and arrives through the R latch instead.
        fwd_value = Signal(isa.WIDTH)
        m.d.comb += fwd_value.eq(Mux(xm_mul, x_prod, xm_value))

        # ---- W-stage registers -------------------------------------------
        w_valid = Signal()
        w_pc = Signal(PC_WIDTH)
        w_we = Signal()
        w_rd = Signal(4)
        w_value = Signal(isa.WIDTH)
        w_halt = Signal()

        # ---- control-transfer register (applied to the fetch next cycle) ---
        ct_valid = Signal()
        ct_target = Signal(PC_WIDTH)
        self.ct_valid = ct_valid

        #: Two-cycle squash window: ``ct_valid`` and the edge behind it.  Both
        #: empty ``fq`` and FD so a word fetched behind a taken transfer is
        #: never decoded.
        sq1 = Signal(1)
        squash = Signal()
        m.d.comb += squash.eq(ct_valid | sq1)

        #: D-stage validity: the slot a registered control transfer has already
        #: killed is suppressed in D rather than at the capture mux.
        d_valid = Signal()
        m.d.comb += d_valid.eq(fd_valid & ~squash)

        # ---- operands: register-file read, X and M bypass ------------------
        #: D issues into R: valid, not stalled, not killed by an X transfer.
        #: Declared here (its equation is below, once ``stall`` and ``take_x``
        #: exist) because the registered X-input bypass select needs it.
        d_issue = Signal()

        d_need1 = Signal()
        d_need2 = Signal()
        m.d.comb += [
            d_need1.eq(d_valid & o.rs1_used & (o.rs1 != 0)),
            d_need2.eq(d_valid & o.rs2_used & (o.rs2 != 0)),
        ]

        # A producer in X whose value is already registered (not a load, not a
        # MUL) is forwarded from ``x_value``; one in M from ``m_value``.  The
        # register file covers distance 3 (same-cycle write-through: the W
        # stage is writing while this D stage reads) and beyond.
        r_hit1 = Signal()
        r_hit2 = Signal()
        m_hit1 = Signal()
        m_hit2 = Signal()
        d_rs1 = Signal(isa.WIDTH)
        d_rs2 = Signal(isa.WIDTH)
        m.d.comb += [
            r_hit1.eq(rx_valid & rx_we & ~rx_mem_read & ~rx_mul
                      & (rx_rd == o.rs1) & d_need1),
            r_hit2.eq(rx_valid & rx_we & ~rx_mem_read & ~rx_mul
                      & (rx_rd == o.rs2) & d_need2),
            m_hit1.eq(xm_valid & xm_we & (xm_rd == o.rs1) & d_need1),
            m_hit2.eq(xm_valid & xm_we & (xm_rd == o.rs2) & d_need2),
            d_rs1.eq(Mux(r_hit1, x_value,
                         Mux(m_hit1, m_value, regfile.o_rs1))),
            d_rs2.eq(Mux(r_hit2, x_value,
                         Mux(m_hit2, m_value, regfile.o_rs2))),
            regfile.i_rs1.eq(o.rs1),
            regfile.i_rs2.eq(o.rs2),
        ]

        # PC-relative target, computed in D and registered into R (section
        # 7.3: it needs only the PC and the displacement, not operands).
        m.d.comb += [
            target.i_pc.eq(fd_pc),
            target.i_imm.eq(o.imm),
            target.i_rs1.eq(0),
            target.i_jump_reg.eq(0),
        ]

        # ---- X input bypass: the M-stage producer (distance 1) -------------
        # A MUL's product is registered at the end of its X, so the R latch
        # could not have taken it; it is muxed onto the ALU input while the
        # producer sits in M.  The source is ``fwd_value``, not ``m_value``:
        # flop-to-mux-to-ALU, with the data EBR's clock-to-out left out of the
        # X cycle (a distance-1 load is handled by the one-bubble interlock).
        #
        # The select terms are computed one cycle early -- in the D cycle, from
        # the producer in X and the ``fd`` fields of the instruction that
        # becomes ``rx`` -- and registered, so the X cycle starts at a flop:
        # comparator, hit AND and validity all sit before the R edge instead of
        # inside the ALU's budget (section 7.2 warns the input mux has ~0.6 ns
        # of headroom).  The producer in X at cycle t is the one in M at t+1,
        # where ``fwd_value`` carries its registered result.
        x_hit1 = Signal()
        x_hit2 = Signal()
        x_a = Signal(isa.WIDTH)
        x_b2 = Signal(isa.WIDTH)
        if self.x_bypass:
            m.d.sync += [
                x_hit1.eq(d_issue & rx_valid & rx_we & ~rx_mem_read
                          & (rx_rd == o.rs1) & o.rs1_used),
                x_hit2.eq(d_issue & rx_valid & rx_we & ~rx_mem_read
                          & (rx_rd == o.rs2) & o.rs2_used),
            ]
            m.d.comb += [
                x_a.eq(Mux(x_hit1, fwd_value, rx_a)),
                x_b2.eq(Mux(x_hit2, fwd_value, rx_bv)),
            ]
        else:
            # One-bubble fallback (section 7.2): no input mux, the operands are
            # the R registers and every distance-1 producer is stalled for.
            m.d.comb += [
                x_a.eq(rx_a),
                x_b2.eq(rx_bv),
            ]

        # ALU operand B: shift amount, immediate, otherwise (bypassed) rs2.
        x_b = Signal(isa.WIDTH)
        with m.If(rx_alu_op >= 4):
            m.d.comb += x_b.eq(rx_shift_amt)
        with m.Elif(rx_alu_src_imm):
            m.d.comb += x_b.eq(rx_imm)
        with m.Else():
            m.d.comb += x_b.eq(x_b2)

        m.d.comb += [
            alu.i_a.eq(x_a),
            alu.i_b.eq(x_b),
            alu.i_op.eq(rx_alu_op),
            branch.i_a.eq(x_a),
            branch.i_b.eq(x_b2),
            branch.i_cond.eq(rx_cond),
        ]

        # ---- control transfer: resolved in X, redirect registered ----------
        take_x = Signal()
        m.d.comb += take_x.eq(
            rx_valid & ((rx_branch & branch.o_taken) | rx_link)
        )

        # JALR's target is rs1 + imm, which is exactly the ALU result for it;
        # branches and JAL take the D-computed, R-registered PC-relative target.
        # Registered with ``ct_valid``: the target must be the resolving
        # instruction's, not whatever the next cycle's R stage holds.
        x_target = Signal(PC_WIDTH)
        m.d.comb += x_target.eq(Mux(rx_jump_reg, alu.o_y, rx_target))

        # ---- hazard: a distance-1 producer whose value is not in R ----------
        # With the M -> X bypass a load is the only producer whose value does
        # not exist yet when the consumer's X opens (the word is registered at
        # the end of the load's M), so only load-use stalls.  In the one-bubble
        # fallback (``x_bypass=False``, section 7.2) there is no input mux, so
        # every distance-1 producer -- load, ALU or MUL -- waits one cycle and
        # then takes its value from ``m_value`` in the R latch.
        stall = Signal()
        freeze = Signal()
        halt_q = Signal()
        if self.x_bypass:
            m.d.comb += stall.eq(
                d_valid & rx_valid & rx_we & rx_mem_read
                & ((d_need1 & (o.rs1 == rx_rd)) | (d_need2 & (o.rs2 == rx_rd)))
            )
        else:
            m.d.comb += stall.eq(
                d_valid & rx_valid & rx_we
                & ((d_need1 & (o.rs1 == rx_rd)) | (d_need2 & (o.rs2 == rx_rd)))
            )

        d_halt = Signal()
        m.d.comb += d_halt.eq(d_valid & o.halt & ~take_x)

        # ---- control-transfer register: the redirect the fetch follows -----
        m.d.comb += freeze.eq(d_halt | halt_q)
        with m.If(freeze):
            m.d.sync += halt_q.eq(1)

        m.d.sync += [
            ct_valid.eq(take_x),
            ct_target.eq(x_target),
            sq1.eq(ct_valid),
        ]

        m.d.comb += [
            fetch.i_redirect.eq(ct_valid),
            fetch.i_target.eq(ct_target),
            fetch.i_hold.eq(stall | freeze),
            self.o_pc.eq(fetch.o_pc),
        ]

        # ---- F -> IQ: the fetch register ----------------------------------
        with m.If(stall):
            m.d.sync += [
                fq_valid.eq(fq_valid),
                fq_pc.eq(fq_pc),
                fq_instr.eq(fq_instr),
            ]
        with m.Elif(~squash & fetch_alive & ~freeze):
            m.d.sync += [
                fq_valid.eq(1),
                fq_pc.eq(fetch.o_instr_pc),
                fq_instr.eq(fetch.o_instr),
            ]
        with m.Else():
            m.d.sync += [
                fq_valid.eq(0),
                fq_pc.eq(0),
                fq_instr.eq(0),
            ]

        # ---- IQ -> D: the decode bundle ------------------------------------
        with m.If(stall):
            m.d.sync += [
                fd_valid.eq(fd_valid),
                fd_pc.eq(fd_pc),
            ] + [dst.eq(dst) for dst in fd_dec.values()]
        with m.Elif(~squash & fq_valid & ~freeze):
            m.d.sync += [
                fd_valid.eq(1),
                fd_pc.eq(fq_pc),
            ] + [dst.eq(getattr(decoder.o, name))
                 for name, dst in fd_dec.items()]
        with m.Else():
            # Killed or not-yet-valid slot: clear the payload too, so a stale
            # branch/HALT can never influence the control logic.
            m.d.sync += [
                fd_valid.eq(0),
                fd_pc.eq(0),
            ] + [dst.eq(0) for dst in fd_dec.values()]

        # ---- D -> R: decode bundle into operand registers -------------------
        d_we = Signal()
        m.d.comb += d_we.eq(o.reg_write & (o.rd != 0))

        d_lui = Signal()
        m.d.comb += d_lui.eq(o.op == isa.OP_LUI)

        m.d.comb += d_issue.eq(d_valid & ~stall & ~take_x)

        m.d.sync += [
            rx_valid.eq(d_issue),
            rx_pc.eq(fd_pc),
            rx_we.eq(d_issue & d_we),
            rx_rd.eq(o.rd),
            rx_a.eq(d_rs1),
            rx_bv.eq(d_rs2),
            rx_imm.eq(o.imm),
            rx_target.eq(target.o_target),
            rx_alu_op.eq(o.alu_op),
            rx_alu_src_imm.eq(o.alu_src_imm),
            rx_shift_amt.eq(o.shift_amt),
            rx_mul.eq(o.mul),
            rx_lui.eq(d_lui),
            rx_link.eq(o.jump),
            rx_halt.eq(d_issue & o.halt),
            rx_mem_read.eq(d_issue & o.mem_read),
            rx_mem_write.eq(d_issue & o.mem_write),
            rx_branch.eq(o.branch),
            rx_cond.eq(o.branch_cond),
            rx_jump_reg.eq(o.jump_reg),
        ]

        # ------------------------------------------------------------- X ---
        # The product is registered before any select tree, so the DSP arc ends
        # at ``x_prod`` and M (not X) selects it.
        with m.If(rx_valid & rx_mul):
            m.d.sync += x_prod.eq((x_a * x_b)[:isa.WIDTH])

        with m.If(rx_link):
            m.d.comb += x_value.eq(rx_pc + 1)
        with m.Elif(rx_lui):
            m.d.comb += x_value.eq(rx_imm)
        with m.Else():
            m.d.comb += x_value.eq(alu.o_y)

        # X presents the data address; the block samples it on the X -> M edge
        # and its word is registered into W at the end of M.  A store commits
        # on the X -> M edge, gated by reset.
        m.d.comb += [
            dmem.i_addr.eq(alu.o_y),
            dmem.i_wdata.eq(x_b2),
            dmem.i_we.eq(rx_valid & rx_mem_write & ~self.i_rst),
        ]

        # ------------------------------------------------------------- M ---
        m.d.sync += [
            xm_valid.eq(rx_valid),
            xm_pc.eq(rx_pc),
            xm_we.eq(rx_we),
            xm_rd.eq(rx_rd),
            xm_value.eq(x_value),
            xm_mem_read.eq(rx_mem_read),
            xm_mul.eq(rx_valid & rx_mul),
            xm_halt.eq(rx_halt),
        ]

        # ------------------------------------------------------------- W ---
        m.d.sync += [
            w_valid.eq(xm_valid),
            w_pc.eq(xm_pc),
            w_we.eq(xm_we),
            w_rd.eq(xm_rd),
            w_value.eq(m_value),
            w_halt.eq(xm_halt),
        ]

        m.d.comb += [
            regfile.i_rd.eq(w_rd),
            regfile.i_wdata.eq(w_value),
            regfile.i_we.eq(w_valid & w_we),
        ]

        m.d.comb += [
            self.o_retire.eq(w_valid),
            self.o_retire_pc.eq(w_pc),
            self.o_retire_we.eq(w_valid & w_we),
            self.o_retire_rd.eq(w_rd),
            self.o_retire_value.eq(w_value),
        ]

        # HALT retires like any other instruction, then the machine freezes.
        with m.If(w_valid & w_halt):
            m.d.sync += self.o_halt.eq(1)

        return m
