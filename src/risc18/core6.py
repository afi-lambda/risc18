"""Six-stage in-order risc18 core: F1/F2/D/X/M/W (simulation model).

The shipping five-stage :class:`risc18.core.Core` resolves a fetch that starts at
the PC register and ends at the FD register in *one* clock: the instruction EBR's
output register, the combinational decode, the register-file read-address select
and (for the distributed-RAM register file) the read itself all sit on that path.
Measured post-route at 13.6 ns / 73.48 MHz.  This core cuts that path in two by
putting a register between the EBR output and the decode: the *fetch register*
``fq``.  One stage becomes two:

    F1  PC register -> imem EBR (clock-enabled read) -> fetch register ``fq``
    F2  decode (combinational, from ``fq``) + register-file read-address select,
        registered into FD (and, for the RAM file, into the file's own address
        register)
    D   register-file data + forward select + load-use detect
    X   ALU / registered multiply / branch compare + target add; dmem addressed
    M   dmem word returns
    W   register-file writeback

Everything downstream of FD keeps the five-stage core's shape, its forwarding
priority (X > M > W, and W-1 for the RAM file), its hazard rules and its memory
schedule: the data EBR is addressed combinationally in X from the ALU result and
a load's word returns in M, so a distance-1 load-use still costs exactly one
bubble, and a store still commits on the X -> M edge gated by reset.

``fq`` is *not* a pass-through register.  It is where a killed slot dies: the
fetch keeps running while a control transfer resolves, and the words it fetched
behind the transfer are cleared out of ``fq`` by the squash window instead of
being decoded.  ``fq_valid`` is the qualifier that stops a stale word -- one the
squash window has already emptied around -- from being decoded into FD, which is
what the five-stage core got for free by decoding straight off the EBR output.

Timing of one instruction, counting edges from the one that makes its PC the
fetch address: the EBR latches that address on edge 1, its word is in ``fq`` on
edge 2, decoded into FD on edge 3, executed (X) on edge 5, and its writeback
register is loaded on edge 6 -- one edge later than ``Core`` at the decode and
at every stage after it.  ``tests/test_core6.py`` pins the depth: word 0 retires
on the sixth cycle after reset.

Control transfer: unchanged policy from M9a.  The compare and target adder run
in X on the operand registers D filled, ``take_x`` is registered into a one-cycle
control register, and the redirect is applied from that register on the next
cycle.  The extra fetch register costs one more killed slot per taken transfer --
**five** instead of four: the fall-through the FD register captured on the
resolving edge, the slot in D one edge later, the slot sitting in ``fq`` behind
it, and the two words the EBR latches from the old PC while the redirect moves
it.  Nothing killed is ever latched into X as valid, so a killed instruction can
neither retire, write the register file, nor write memory: only an instruction
in X addresses the data EBR, and ``dx_valid`` is 0 for every killed slot.  A
killed HALT never reaches D, so it cannot freeze the fetch; a redirect is never
registered from a stalled or squashed cycle, because ``take_x`` requires
``dx_valid`` and a stalled instruction never enters X.

Multiply: the 18x18 product is registered in X (``x_prod``) and only enters a
select tree in M, so the DSP arc is not in the X-stage result-select path.  That
makes MUL latency 2: the product of the instruction in X appears one cycle later,
when the instruction is in M, and is bypassed to D from there.  A consumer at
distance 1 therefore costs one bubble -- the same interlock shape as load-use --
while a consumer at distance 2 or more costs nothing.  Every other producer
(ALU, ADDI, SHIFT, LUI, JAL/JALR link) is still forwarded combinationally out of
``x_value`` at distance 1 with no bubble.

HALT, reset and the retire trace are the five-stage core's: HALT decodes in D,
freezes the fetch there, squashes everything younger and still retires through W;
reset clears the pipeline, the register file and ``fq`` but performs no memory
writes; ``o_retire`` pulses once per instruction leaving W and
``o_retire_pc``/``o_retire_we``/``o_retire_rd``/``o_retire_value`` are valid with
it.

Simulation only: this file is the behavioural contract stage 2 builds and routes.
It reuses the frozen units and changes no ISA, golden-model or five-stage
behaviour.
"""

from amaranth import ClockDomain, Elaboratable, Module, Mux, Signal

from . import isa
from .alu import ALU
from .branch import BranchUnit
from .decoder import Decoder
from .dmem import DataMemory
from .imem import Fetch
from .pc import BranchTarget
from .ramrf import RegFileRam
from .regfile import RegFile

__all__ = ["Core6"]

PC_WIDTH = 16


class _DecodedFields:
    """Read-only view of the FD decode bundle (one ``Signal`` per field)."""

    def __init__(self, fields):
        self.__dict__.update(fields)


class Core6(Elaboratable):
    """F1/F2/D/X/M/W risc18 core: extra fetch register, multiply registered."""

    def __init__(self, program=(), data=None, ram_rf=False):
        self.program = list(program)
        #: Optional 1024-word data-memory image (padded/truncated by DataMemory).
        self.data = list(data) if data is not None else []
        #: Use :class:`risc18.ramrf.RegFileRam` (distributed RAM, registered
        #: reads) instead of the flop :class:`RegFile`.
        self.ram_rf = ram_rf

        #: Synchronous active-high reset, wired to the ``sync`` domain's rst.
        self.i_rst = Signal(1)

        self.o_pc = Signal(PC_WIDTH)
        self.o_retire = Signal(1)
        self.o_retire_pc = Signal(PC_WIDTH)
        self.o_retire_we = Signal(1)
        self.o_retire_rd = Signal(4)
        self.o_retire_value = Signal(isa.WIDTH)
        self.o_halt = Signal(1)

        #: The ``sync`` domain this core owns (same shape as ``Core``: no clock
        #: port of its own; simulation drives the domain).
        self.cd = ClockDomain("sync")

    # -- elaboration ----------------------------------------------------
    def elaborate(self, platform):
        m = Module()
        m.domains += self.cd
        m.d.comb += self.cd.rst.eq(self.i_rst)

        m.submodules.fetch = fetch = Fetch(self.program)
        m.submodules.decoder = decoder = Decoder()
        if self.ram_rf:
            m.submodules.regfile = regfile = RegFileRam()
        else:
            m.submodules.regfile = regfile = RegFile()
        m.submodules.alu = alu = ALU()
        m.submodules.branch = branch = BranchUnit()
        m.submodules.target = target = BranchTarget()
        m.submodules.dmem = dmem = DataMemory(self.data)

        # Decode runs in F2, on the instruction the fetch register ``fq`` holds,
        # and its outputs are captured into the FD bundle.  D then starts every
        # path from flops, exactly as in the five-stage core -- the difference is
        # that the EBR output register is no longer the input of that cone.
        fd_dec = {name: Signal(len(getattr(decoder.o, name)))
                  for name in vars(decoder.o)}
        o = _DecodedFields(fd_dec)
        #: White-box observability (same hooks as ``Core``): the FD validity and
        #: the captured field bundle are otherwise local to this module.
        self.fd_valid = None
        self.fd_fields = fd_dec
        #: The fetch register's validity, for the same kind of white-box test.
        self.fq_valid = None

        # ------------------------------------------------------------ F1 ---
        # PC register -> EBR -> fetch register.  A redirected fetch needs no
        # special case inside Fetch: the PC moves on the edge and the EBR
        # presents the target's word one edge later.
        fetch_alive = Signal()
        m.d.sync += fetch_alive.eq(1)      # 0 only in the post-reset cycle

        fq_valid = Signal()
        fq_pc = Signal(PC_WIDTH)
        fq_instr = Signal(isa.WIDTH)
        self.fq_valid = fq_valid

        # ------------------------------------------------------------ F2 ---
        # The decode cone's only input is registered state, which is the point
        # of the cut: EBR output register -> fq -> decode, one EBR read per edge.
        m.d.comb += decoder.i_instr.eq(fq_instr)

        fd_valid = Signal()
        fd_pc = Signal(PC_WIDTH)
        self.fd_valid = fd_valid

        # ---- X stage registers (declared here: D's forwarding reads them) ---
        dx_valid = Signal()
        dx_pc = Signal(PC_WIDTH)
        dx_we = Signal()
        dx_rd = Signal(4)
        dx_a = Signal(isa.WIDTH)
        dx_b = Signal(isa.WIDTH)
        dx_rs2 = Signal(isa.WIDTH)         # store data, carried to the EBR
        dx_alu_op = Signal(3)
        dx_imm = Signal(isa.WIDTH)
        dx_mul = Signal()
        dx_lui = Signal()
        dx_link = Signal()
        dx_halt = Signal()
        dx_mem_read = Signal()
        dx_mem_write = Signal()
        dx_branch = Signal()
        dx_cond = Signal(2)
        dx_jump_reg = Signal()

        #: X-stage result (ALU/MUL-free: ALU/link/LUI), forwarded into D one
        #: instruction ahead of its writeback.
        x_value = Signal(isa.WIDTH)

        #: Registered multiply product.  Written on the X -> M edge of a MUL, so
        #: it holds that instruction's product while the instruction is in M;
        #: the DSP arc therefore ends at a flop and never reaches the X-stage
        #: result-select tree.
        x_prod = Signal(isa.WIDTH)

        # ---- control-transfer register (M9a, unchanged) ---------------------
        ct_valid = Signal()
        ct_target = Signal(PC_WIDTH)

        #: Registered squash window: ``ct_valid`` and the edge behind it.  Both
        #: empty ``fq`` and FD, so the decode cone never sees a killed word.
        sq1 = Signal(1)
        squash = Signal()
        m.d.comb += squash.eq(ct_valid | sq1)

        #: D-stage validity: the slot a registered control transfer has already
        #: killed is suppressed in D rather than at the capture mux.
        d_valid = Signal()
        m.d.comb += d_valid.eq(fd_valid & ~squash)

        # ---- operands: RegFile read, X and M forwarding ---------------------
        d_need1 = Signal()
        d_need2 = Signal()
        m.d.comb += [
            d_need1.eq(d_valid & o.rs1_used & (o.rs1 != 0)),
            d_need2.eq(d_valid & o.rs2_used & (o.rs2 != 0)),
        ]

        xm_valid = Signal()
        xm_pc = Signal(PC_WIDTH)
        xm_we = Signal()
        xm_rd = Signal(4)
        xm_value = Signal(isa.WIDTH)
        xm_mem_read = Signal()
        xm_mul = Signal()
        xm_halt = Signal()

        #: What M carries: the data EBR word for a load, the registered product
        #: for a MUL, the X-stage result otherwise.  This is also the value the
        #: D-stage bypass hands a distance-1 consumer.
        m_value = Signal(isa.WIDTH)
        m.d.comb += m_value.eq(
            Mux(xm_mem_read, dmem.o_rdata, Mux(xm_mul, x_prod, xm_value))
        )

        m_hit1 = Signal()
        m_hit2 = Signal()
        m.d.comb += [
            m_hit1.eq(xm_valid & xm_we & (xm_rd == o.rs1) & d_need1),
            m_hit2.eq(xm_valid & xm_we & (xm_rd == o.rs2) & d_need2),
        ]

        w_valid = Signal()
        w_pc = Signal(PC_WIDTH)
        w_we = Signal()
        w_rd = Signal(4)
        w_value = Signal(isa.WIDTH)
        w_halt = Signal()

        # ``stall``/``freeze`` and ``halt_q`` are declared here because the RAM
        # register file's read address depends on ``stall``.
        stall = Signal()
        freeze = Signal()
        halt_q = Signal()

        x_hit1 = Signal()
        x_hit2 = Signal()
        m.d.comb += [
            # A load's word does not exist yet and a MUL's product is not
            # combinationally available in X: neither may be forwarded from
            # ``x_value``.  Both stall instead (``stall`` below).  ``dx_we``
            # already implies rd != 0.
            x_hit1.eq(dx_valid & dx_we & ~dx_mem_read & ~dx_mul
                      & (dx_rd == o.rs1) & d_need1),
            x_hit2.eq(dx_valid & dx_we & ~dx_mem_read & ~dx_mul
                      & (dx_rd == o.rs2) & d_need2),
        ]

        # rs1/rs2 as the D stage sees them: X (newest) > M > W > W-1.
        d_rs1 = Signal(isa.WIDTH)
        d_rs2 = Signal(isa.WIDTH)
        if self.ram_rf:
            # The RAM file samples its read addresses one cycle before the
            # operands are needed, so D races both the W write that lands on the
            # capturing edge (W-1: a producer four instructions ahead) and the W
            # write that lands at the end of this cycle (W: three ahead).  Both
            # are bypassed explicitly; the flop file needs only the same-cycle
            # write-through, which the RAM cannot do.
            w_hit1 = Signal()
            w_hit2 = Signal()
            wq_hit1 = Signal()
            wq_hit2 = Signal()
            wq_valid = Signal()
            wq_we = Signal()
            wq_rd = Signal(4)
            wq_value = Signal(isa.WIDTH)
            m.d.comb += [
                d_rs1.eq(Mux(x_hit1, x_value,
                             Mux(m_hit1, m_value,
                                 Mux(w_hit1, w_value,
                                     Mux(wq_hit1, wq_value,
                                         regfile.o_rs1))))),
                d_rs2.eq(Mux(x_hit2, x_value,
                             Mux(m_hit2, m_value,
                                 Mux(w_hit2, w_value,
                                     Mux(wq_hit2, wq_value,
                                         regfile.o_rs2))))),
                # Addresses come from the F2 decode: the word decoded now is the
                # one that reads its operands in D next cycle.  While D is
                # stalled it does not move, so the address must be D's own: the
                # data sampled on the stall cycle is what the instruction in D
                # needs on the cycle the stall lifts.
                regfile.i_rs1.eq(Mux(stall, o.rs1, decoder.o.rs1)),
                regfile.i_rs2.eq(Mux(stall, o.rs2, decoder.o.rs2)),
            ]
        else:
            m.d.comb += [
                d_rs1.eq(Mux(x_hit1, x_value,
                             Mux(m_hit1, m_value, regfile.o_rs1))),
                d_rs2.eq(Mux(x_hit2, x_value,
                             Mux(m_hit2, m_value, regfile.o_rs2))),
                regfile.i_rs1.eq(o.rs1),
                regfile.i_rs2.eq(o.rs2),
            ]

        # Branch compare and target: driven from the X-stage registers.
        m.d.comb += [
            branch.i_a.eq(dx_a),
            branch.i_b.eq(dx_rs2),
            branch.i_cond.eq(dx_cond),
        ]
        m.d.comb += [
            target.i_pc.eq(dx_pc),
            target.i_imm.eq(dx_imm),
            target.i_rs1.eq(dx_a),
            target.i_jump_reg.eq(dx_jump_reg),
        ]

        # ---- control transfer: resolved in X, redirect registered ----------
        take_x = Signal()
        m.d.comb += take_x.eq(
            dx_valid & ((dx_branch & branch.o_taken) | dx_link)
        )

        # LD writes rd (reg_write = 1, mem_read = 1); ST writes no register.
        d_we = Signal()
        m.d.comb += d_we.eq(o.reg_write & (o.rd != 0))

        d_lui = Signal()
        m.d.comb += d_lui.eq(o.op == isa.OP_LUI)

        d_halt = Signal()
        m.d.comb += d_halt.eq(d_valid & o.halt & ~take_x)

        # ALU operand B: shift amount, immediate, otherwise rs2.
        d_b = Signal(isa.WIDTH)
        with m.If(o.alu_op >= 4):
            m.d.comb += d_b.eq(o.shift_amt)
        with m.Elif(o.alu_src_imm):
            m.d.comb += d_b.eq(o.imm)
        with m.Else():
            m.d.comb += d_b.eq(d_rs2)

        # ---- hazard: a producer in X whose value is not available yet -------
        # Two producers are late, and both are served out of M one cycle later:
        # a load (its word is the data EBR's output during M) and a MUL (its
        # product is registered on the X -> M edge).  Every other X-stage writer
        # is forwarded from ``x_value`` and costs no bubble.
        m.d.comb += stall.eq(
            dx_valid & dx_we & (dx_mem_read | dx_mul)
            & ((d_need1 & (o.rs1 == dx_rd)) | (d_need2 & (o.rs2 == dx_rd)))
        )

        # ---- control-transfer register: the redirect the fetch follows ------
        m.d.comb += freeze.eq(d_halt | halt_q)
        with m.If(freeze):
            m.d.sync += halt_q.eq(1)

        m.d.sync += [
            ct_valid.eq(take_x),
            ct_target.eq(target.o_target),
            sq1.eq(ct_valid),
        ]

        m.d.comb += [
            fetch.i_redirect.eq(ct_valid),
            fetch.i_target.eq(ct_target),
            fetch.i_hold.eq(stall | freeze),
            self.o_pc.eq(fetch.o_pc),
        ]

        # ---- F1 -> F2: the fetch register -----------------------------------
        # Captured unconditionally while the fetch is alive; emptied by either
        # edge of the squash window, so a word fetched behind a taken transfer
        # is never decoded.  A stall must hold it: the fetch's EBR read path is
        # disabled during the stall, so its output still carries the word *after*
        # the one in ``fq`` and re-capturing would overwrite the instruction the
        # stall is holding D for.
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

        # ---- F2 -> D: the decode bundle -------------------------------------
        # ``fq_valid`` is the last kill: the register that emptied ``fq`` may be
        # followed by a cycle in which the squash window has already closed but
        # the word in ``fq`` was the killed one, so decoding is qualified by the
        # register's own validity and not by the window alone.
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

        # ------------------------------------------------------------- X ---
        d_issue = Signal()
        m.d.comb += d_issue.eq(d_valid & ~stall & ~take_x)
        m.d.sync += [
            dx_valid.eq(d_issue),
            dx_pc.eq(fd_pc),
            dx_we.eq(d_issue & d_we),
            dx_rd.eq(o.rd),
            dx_a.eq(d_rs1),
            dx_b.eq(d_b),
            dx_rs2.eq(d_rs2),
            dx_alu_op.eq(o.alu_op),
            dx_imm.eq(o.imm),
            dx_mul.eq(o.mul),
            dx_lui.eq(d_lui),
            dx_link.eq(o.jump),
            dx_branch.eq(o.branch),
            dx_cond.eq(o.branch_cond),
            dx_jump_reg.eq(o.jump_reg),
            dx_halt.eq(d_issue & o.halt),
            dx_mem_read.eq(d_issue & o.mem_read),
            dx_mem_write.eq(d_issue & o.mem_write),
        ]

        m.d.comb += [
            alu.i_a.eq(dx_a),
            alu.i_b.eq(dx_b),
            alu.i_op.eq(dx_alu_op),
        ]

        # The product is registered here, before any select tree: the DSP arc
        # ends at ``x_prod`` and the M stage selects it.  MUL is latency 2.
        with m.If(dx_valid & dx_mul):
            m.d.sync += x_prod.eq((dx_a * dx_b)[:isa.WIDTH])

        # X-stage result mux: ALU / JAL-JALR link / LUI.  A MUL is deliberately
        # absent -- its product comes from ``x_prod`` one cycle later.
        with m.If(dx_link):
            m.d.comb += x_value.eq(dx_pc + 1)
        with m.Elif(dx_lui):
            m.d.comb += x_value.eq(dx_imm)
        with m.Else():
            m.d.comb += x_value.eq(alu.o_y)

        # The data EBR is addressed during X (LD/ST effective address); a store
        # commits on the X -> M edge, gated by reset so a store whose X cycle is
        # a reset cycle does not commit.
        m.d.comb += [
            dmem.i_addr.eq(alu.o_y),
            dmem.i_wdata.eq(dx_rs2),
            dmem.i_we.eq(dx_valid & dx_mem_write & ~self.i_rst),
        ]

        # ------------------------------------------------------------- M ---
        m.d.sync += [
            xm_valid.eq(dx_valid),
            xm_pc.eq(dx_pc),
            xm_we.eq(dx_we),
            xm_rd.eq(dx_rd),
            xm_value.eq(x_value),
            xm_mem_read.eq(dx_mem_read),
            xm_mul.eq(dx_valid & dx_mul),
            xm_halt.eq(dx_halt),
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

        if self.ram_rf:
            # W and W-1 bypasses for the one-cycle-late RAM operands.
            m.d.comb += [
                w_hit1.eq(w_valid & w_we & (w_rd == o.rs1) & d_need1),
                w_hit2.eq(w_valid & w_we & (w_rd == o.rs2) & d_need2),
                wq_hit1.eq(wq_valid & wq_we & (wq_rd == o.rs1) & d_need1),
                wq_hit2.eq(wq_valid & wq_we & (wq_rd == o.rs2) & d_need2),
            ]
            m.d.sync += [
                wq_valid.eq(w_valid),
                wq_we.eq(w_we),
                wq_rd.eq(w_rd),
                wq_value.eq(w_value),
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
