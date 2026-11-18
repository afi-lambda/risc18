"""Five-stage in-order risc18 core with data memory and forwarding (M7).

The pipeline is F/D/X/M/W, Harvard, word addressed::

    F: Fetch        PCUnit drives the instruction EBR; the EBR output lags
                    the PC register by one edge
    D: Decode       instruction from the FD register -> Decoder, combinational
                    RegFile reads (write-through), X and M forwarding,
                    immediate, load-use hazard check
    X: Execute      ALU (a = rs1, b = imm/rs2/shift amount), 18x18 multiply,
                    the JAL/JALR link value, the LD/ST effective address and
                    the branch/jump resolution (BranchUnit + BranchTarget).
                    The ALU drives the data EBR's address and write data here,
                    and ``x_value`` is what D forwards from
    M: Memory       the data EBR has latched the X-stage address, so a load's
                    word is on ``o_rdata``: it is captured into W and is also
                    the value the D-stage bypass presents for a consumer that
                    follows the load one instruction behind
    W: Writeback    single write port into the RegFile, retire pulse

Timing (M9a), counting edges from the one that makes a PC current: the FD
register adds the second edge to the EBR's one, so an instruction is decoded two
edges after its PC became the fetch address.  The control transfer is resolved
in X, from the operand registers D already filled (``dx_a``/``dx_rs2`` for the
compare, ``dx_pc``/``dx_imm``/``dx_a`` for the target, ``dx_link`` for
JAL/JALR): ``take_x`` is registered into a one-cycle control register and the
redirect is applied from that register on the following cycle, while the
transfer sits in M.  Two loops the M5-M8 metrics measured as the critical path
are gone with it -- compare -> redirect -> PC mux -> FD capture mux (step 1) and
X-stage result -> forward select -> compare (step 2, which is why the compare
moved out of D rather than the operand select moving into a register: a
registered operand select cannot serve a distance-1 dependency, and those cost
zero bubbles by contract).  The price is four killed slots per taken transfer
instead of two, so a taken branch or jump fetches its target two cycles later
than the M7/M8 core did.

The four slots a taken transfer ``P`` kills, and which mechanism kills each:

* ``P+1``, the word the FD register captures on the edge the transfer enters X.
  That capture is *allowed* (making it conditional would put the compare back on
  the FD register's enable) and the slot is stopped at the D -> X boundary
  instead: ``dx_valid`` is ``d_valid & ~stall & ~take_x``, so it never reaches X
  and can neither write memory, retire, stall, nor -- being invisible to D --
  halt;
* ``P+2``, captured into D one edge later, which is the edge ``ct_valid``
  appears on: ``d_valid = fd_valid & ~squash`` kills it in D;
* ``P+3`` and ``P+4``, the two words the EBR latches from the old PC while the
  redirect moves it: ``squash = ct_valid | sq1`` empties the FD register for
  both capture edges.  ``sq1`` is one cycle behind ``ct_valid``.

Fetching resumes with the target's word, which the FD register captures on the
edge after that.  Nothing killed is ever latched into X as valid, so a stale
instruction can never retire and can never write memory (only an instruction in
X writes the data EBR).  Nothing can register a redirect from a stalled or
squashed cycle either: ``take_x`` requires ``dx_valid``, a stalled instruction
never enters X, and reset clears the register.

Memory (M6, ``docs/ISA.md`` sections 1, 5 and 6): ``DataMemory`` is a
1024 x 18 EBR with a one-cycle synchronous read.  The data EBR is addressed
*combinationally during X* with the ALU result, i.e. the effective address the
X stage just computed, and the write data is the rs2 value the D stage
captured.  One cycle later the addressed word is on ``o_rdata`` while the
instruction is in M, which is what makes the load-use cost a single bubble:

* a store commits at the X -> M edge (``i_we = dx_valid & dx_mem_write &
  ~i_rst``), so
  the store in front of a load is already in the array when the load addresses
  it one cycle later;
* a load's word is on ``o_rdata`` during M, so the M stage carries the value
  and W latches it for writeback -- and the D-stage bypass can hand the same
  word to a consumer that is immediately behind the load, so the stall is
  exactly one cycle.

Addressing during X rather than M is deliberate: with the port addressed by a
registered M address the word would only appear in W, and a two-instruction-
apart consumer would need a second stall (or an M -> X forward) to see it.

Hazards (M7: X > M > W forwarding; only load-use stalls): a register written
by X -- the ALU/MUL/link/LUI result of the instruction currently executing --
is forwarded straight into D from ``x_value``, so a distance-1 RAW costs
nothing.  The single exception is a load: its word only appears on the data EBR
output a cycle later, in M, so D stalls exactly once when it needs the register
a load in X writes (``dx_mem_read``).  D takes a register written by M through
the M-stage bypass (the ALU/MUL/link/LUI result, or the data EBR word for a
load), and the instruction in W writes through the RegFile's combinational read
port in the same cycle D reads it, so a producer two or three instructions
ahead costs nothing either.  Forwarding feeds ``d_rs1``/``d_rs2`` themselves,
so the ALU operands, the branch compare, the BranchTarget/JALR base and the
store data all see the forwarded value.  A stall holds ``Fetch`` (``i_hold``)
and the FD register and injects a bubble into X, so the instruction in D is
executed exactly once.  A store writes no register (``reg_write = 0``), so
nothing ever stalls on it; a store's operands are ordinary reads and are
forwarded or stalled for on their behalf like any other.

Control: HALT (``SYSTEM`` with ``imm6 == SYS_HALT``) is decoded in D, freezes
``Fetch`` there and squashes everything younger; the HALT itself still flows
down the pipe, retires through W exactly like golden counts it, and sets
``o_halt`` sticky.  Nothing retires after it.  A HALT younger than a control
transfer in X -- i.e. one that decodes on the very edge ``take_x`` resolves --
is killed by it (``d_halt`` is qualified with ``~take_x``), so it cannot freeze
the fetch of the slot the redirect is about to fill, and a redirect registered
from a stalled or squashed cycle can never exist at all, so none can fire after
the freeze starts.

MUL is a single-cycle combinational 18x18 multiply masked to 18 bits, written
inline so yosys maps it to one DSP without adding a module boundary (M8
measured a +159 LUT4 / -3.95 MHz ABC remap for the equivalent submodule form).
:class:`risc18.mul.Multiplier` exists for the DSP measurement tops and the
registered option; the product is available in X, so MUL retires at CPI 1.0
and the X-stage forward path covers it like any other ALU result.  The unit's ``registered=True`` mode
(one edge of latency, the DSP's own output register) needs a one-cycle X hold
and was measured against this baseline in M8; the core keeps the
zero-bubble form.

Reset: the core owns the ``sync`` domain it drives (``i_rst`` is wired to that
domain's ``rst``), so every unit elaborated underneath -- ``PCUnit``,
``RegFile``, ``DataMemory``, the FD/DX/XM/W registers -- resets synchronously
from one port.  No reset-less domain is used here.  Reset clears the pipeline,
the register file and the data memory's in-range flag, but not the data EBR's
contents (``docs/ISA.md`` section 1: reset performs no memory writes).  After
reset the first fetch slot is empty for one cycle (the instruction EBR's masked
output at PC 0 is not word 0), which the ``fetch_alive`` register below encodes.

Retire trace: ``o_retire`` is high for one cycle per instruction leaving W;
``o_retire_pc``/``o_retire_we``/``o_retire_rd``/``o_retire_value`` are valid
with it.  ``o_retire_we`` is high only for a real write to a register other
than ``r0`` -- ALU/MUL/LUI/link results and loads alike -- so ``o_retire``
counts every instruction (branches, stores, NOPs and HALT included) while the
``we`` rows are exactly the architectural write trace the golden model records.
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

__all__ = ["Core"]

PC_WIDTH = 16


class Core(Elaboratable):
    """F/D/X/M/W risc18 core with a data EBR and the load-use interlock (M6)."""

    def __init__(self, program=(), data=None):
        self.program = list(program)
        #: Optional 1024-word data-memory image (padded/truncated by DataMemory).
        self.data = list(data) if data is not None else []

        #: Synchronous active-high reset, wired to the ``sync`` domain's rst.
        self.i_rst = Signal(1)

        self.o_pc = Signal(PC_WIDTH)
        self.o_retire = Signal(1)
        self.o_retire_pc = Signal(PC_WIDTH)
        self.o_retire_we = Signal(1)
        self.o_retire_rd = Signal(4)
        self.o_retire_value = Signal(isa.WIDTH)
        self.o_halt = Signal(1)

        #: The ``sync`` domain this core owns, exposed so the measurement tops
        #: in ``tops.py`` can drive its clock from their ``clk`` port (a core
        #: has no clock port of its own; simulation drives the domain directly).
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

        o = decoder.o

        # ------------------------------------------------------------- F ---
        # Fetch control is combinational (`fetch.i_*` below).  A redirected
        # fetch needs no special case inside Fetch: the PC moves on the edge
        # and the EBR presents the target's word one edge later, which is what
        # the four-slot squash window (d_valid, ~take_x, ct_valid, sq1) covers.
        fetch_alive = Signal()
        m.d.sync += fetch_alive.eq(1)      # 0 only in the post-reset cycle

        # ------------------------------------------------------------- D ---
        fd_valid = Signal()
        fd_pc = Signal(PC_WIDTH)
        fd_instr = Signal(isa.WIDTH)

        m.d.comb += decoder.i_instr.eq(fd_instr)

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
        #: Control-transfer fields carried into X (M9a step 2): the compare and
        #: the target are resolved there, from these registers, so neither the
        #: X-stage forward path nor the ALU/DSP output reaches the decoder's
        #: compare or the fetch mux in the same cycle.
        dx_branch = Signal()
        dx_cond = Signal(2)
        dx_jump_reg = Signal()

        #: X-stage result (ALU/MUL/link/LUI), driven combinationally below and
        #: forwarded into D one instruction ahead of its writeback.
        x_value = Signal(isa.WIDTH)

        # ---- control-transfer register (M9a) -------------------------------
        # The decision, not the compare, drives the fetch: the control transfer
        # resolves in X (step 2) and the redirect is applied from this register
        # one cycle later, while it sits in M.  Only the target is needed: the
        # pc of the entry would be dead in the datapath, so it is not stored.
        ct_valid = Signal()
        ct_target = Signal(PC_WIDTH)

        #: Registered squash window: ``ct_valid`` and the edge behind it, i.e.
        #: the two FD-capture edges that must be emptied while the redirect
        #: moves the PC.  Both are registers, so the FD capture mux never sees
        #: the compare.
        sq1 = Signal(1)
        squash = Signal()
        m.d.comb += squash.eq(ct_valid | sq1)

        #: D-stage validity: the slot a registered control transfer has already
        #: killed must be suppressed in D rather than at the capture mux, which
        #: would put the compare back on the FD register's enable.
        d_valid = Signal()
        m.d.comb += d_valid.eq(fd_valid & ~squash)

        # ---- operands: RegFile read, X and M forwarding ---------------------
        # Three producers can still be in flight in front of D:
        #
        #   * the RegFile read port carries the W-stage write-through (the
        #     instruction three or more ahead), so nothing is needed there;
        #   * M is bypassed (ALU/MUL/link/LUI result, or the data EBR word for
        #     a load) because its RegFile write lands a cycle later;
        #   * X is forwarded (M7): the instruction in X has already produced
        #     its result combinationally, so D takes it straight from
        #     ``x_value`` instead of stalling.
        #
        # Priority is newest first: X > M > RegFile.  The one X-stage writer
        # whose result is *not* available is a load -- its word only appears on
        # the data EBR output during M -- so that, and only that, still costs D
        # a bubble (``stall`` below).
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
        xm_halt = Signal()

        m_value = Signal(isa.WIDTH)
        m.d.comb += m_value.eq(Mux(xm_mem_read, dmem.o_rdata, xm_value))

        m_hit1 = Signal()
        m_hit2 = Signal()
        m.d.comb += [
            # ``xm_we`` already implies rd != 0 (it is ``d_we`` registered), so
            # no separate rd-zero compare is needed here.
            m_hit1.eq(xm_valid & xm_we & (xm_rd == o.rs1) & d_need1),
            m_hit2.eq(xm_valid & xm_we & (xm_rd == o.rs2) & d_need2),
        ]

        # X-stage forwarding (M7).  ``x_value`` -- the ALU result, the MUL
        # product, the JAL/JALR link value or the LUI immediate -- is
        # combinational while the producer sits in X, so a consumer one
        # instruction behind takes it directly.  A load is excluded: its
        # ``x_value`` is the effective address, not the word, and the word
        # only exists in M (that case stalls below and reads the M bypass).
        x_hit1 = Signal()
        x_hit2 = Signal()
        m.d.comb += [
            # ``dx_we`` already implies rd != 0.  ``~dx_mem_read`` is
            # redundant with the load-use stall below -- both use the same
            # operand-match predicate -- and is kept only so an X-stage load
            # never presents its effective address as data; change the two
            # together.
            x_hit1.eq(dx_valid & dx_we & ~dx_mem_read & (dx_rd == o.rs1)
                      & d_need1),
            x_hit2.eq(dx_valid & dx_we & ~dx_mem_read & (dx_rd == o.rs2)
                      & d_need2),
        ]

        # rs1/rs2 as the D stage sees them: X (newest) > M > W write-through.
        d_rs1 = Signal(isa.WIDTH)
        d_rs2 = Signal(isa.WIDTH)
        m.d.comb += [
            d_rs1.eq(Mux(x_hit1, x_value,
                         Mux(m_hit1, m_value, regfile.o_rs1))),
            d_rs2.eq(Mux(x_hit2, x_value,
                         Mux(m_hit2, m_value, regfile.o_rs2))),
            regfile.i_rs1.eq(o.rs1),
            regfile.i_rs2.eq(o.rs2),
        ]
        # Branch compare and target: driven from the X-stage registers (M9a
        # step 2).  ``dx_a`` is rs1, ``dx_rs2`` is rs2 (``dx_b`` is the ALU's B
        # operand, which may be the immediate), ``dx_imm``/``dx_pc`` the target
        # operands, and ``dx_link`` marks a JAL/JALR (it is also the link mux
        # select, so no separate jump register is needed).
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

        # ---- control transfer: resolved in X (M9a step 2) -------------------
        # The branch compare and the target adder run on the *X-stage* operand
        # registers.  They used to sit in D behind the operand-select mux, which
        # put "X-stage result -> forward select -> compare -> redirect -> PC mux
        # -> FD capture" on one path (~20.5 ns at M7, ~17.5 ns with only the
        # redirect registered).  Resolving one stage later costs one more killed
        # slot per taken transfer and takes the whole forward mux and the DSP
        # out of the control path: what is left is register -> 18-bit compare
        # -> register.
        #
        # ``take_x`` is combinational: it reaches the control register's input,
        # the D -> X validity and the HALT kill only.  Its one combinational
        # path into F is ``d_halt -> freeze -> fetch.i_hold`` (a single LUT into
        # an enable); the redirect, the target and the FD capture muxes are all
        # registered, which is what removed the ~48 MHz loop.
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

        # ALU operand B: the 4-bit shift amount for the SHIFT group, the
        # immediate when the decoder asks for it (LD/ST included: their
        # effective address is rs1 + imm), otherwise rs2.
        d_b = Signal(isa.WIDTH)
        with m.If(o.alu_op >= 4):
            m.d.comb += d_b.eq(o.shift_amt)
        with m.Elif(o.alu_src_imm):
            m.d.comb += d_b.eq(o.imm)
        with m.Else():
            m.d.comb += d_b.eq(d_rs2)

        # ---- hazard: D needs a register only a load in X can still produce --
        # A load's word does not exist until M (the data EBR output), so a
        # consumer one instruction behind it is the one remaining stall.  Every
        # other X-stage writer is forwarded above, so it costs no bubble.
        stall = Signal()
        m.d.comb += stall.eq(
            dx_valid & dx_we & dx_mem_read
            & ((d_need1 & (o.rs1 == dx_rd)) | (d_need2 & (o.rs2 == dx_rd)))
        )

        # ---- control-transfer register: the redirect the fetch follows ------
        # Sticky freeze once HALT is in D; o_halt itself follows the retire.
        # A HALT younger than a control transfer in X is killed by it, so it
        # must not freeze the fetch: ``take_x`` suppresses the freeze
        # (``d_halt``), and a redirect registered from a stalled or squashed
        # cycle can never exist (``take_x`` requires ``dx_valid``, and a
        # stalled instruction never enters X).
        halt_q = Signal()
        freeze = Signal()
        m.d.comb += freeze.eq(d_halt | halt_q)
        with m.If(freeze):
            m.d.sync += halt_q.eq(1)

        # The one-cycle control register: written in X, read in M.
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

        with m.If(stall):
            m.d.sync += [
                fd_valid.eq(fd_valid),
                fd_pc.eq(fd_pc),
                fd_instr.eq(fd_instr),
            ]
        with m.Elif(~squash & fetch_alive & ~freeze):
            m.d.sync += [
                fd_valid.eq(1),
                fd_pc.eq(fetch.o_instr_pc),
                fd_instr.eq(fetch.o_instr),
            ]
        with m.Else():
            # Killed or not-yet-valid slot: clear the payload too, so a stale
            # branch/HALT can never influence the control logic.
            m.d.sync += [
                fd_valid.eq(0),
                fd_pc.eq(0),
                fd_instr.eq(0),
            ]

        # ------------------------------------------------------------- X ---
        # Every validity that leaves D is qualified by `d_valid`: the slot a
        # registered redirect killed must not reach X as valid, and its
        # halt/write bits must not leak into the retire path either.  `~take_x`
        # is the last kill: the word that was captured into D on the very edge
        # the resolving control transfer entered X (the transfer's own fall-
        # through) has no registered squash behind it yet, so it is stopped
        # here, at the D -> X boundary, instead.
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

        # X-stage result mux: the value the writeback path carries and the
        # value D forwards from (M7).  Driven here, declared above.
        with m.If(dx_mul):
            # Single-cycle 18x18 product, low 18 bits from the explicit
            # multiplier unit (M8): signedness of the operands does not affect
            # the low bits of the product.
            m.d.comb += x_value.eq((dx_a * dx_b)[:isa.WIDTH])
        with m.Elif(dx_link):
            # JAL/JALR link: pc + 1, the instruction after the jump.
            m.d.comb += x_value.eq(dx_pc + 1)
        with m.Elif(dx_lui):
            m.d.comb += x_value.eq(dx_imm)
        with m.Else():
            m.d.comb += x_value.eq(alu.o_y)

        # The data EBR is addressed during X: the ALU result is the LD/ST
        # effective address, the store data is the rs2 value captured in D.
        # A store commits on the X -> M edge; a load's word is on `o_rdata`
        # during M, one edge later.
        # The reset term makes the boundary explicit: a store whose X cycle is
        # the cycle the reset is asserted does not commit.  A store that has
        # already committed on the X -> M edge stays written even if a reset
        # then kills it before W: reset does not undo memory writes.
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
            xm_halt.eq(dx_halt),
        ]

        # ------------------------------------------------------------- W ---
        w_valid = Signal()
        w_pc = Signal(PC_WIDTH)
        w_we = Signal()
        w_rd = Signal(4)
        w_value = Signal(isa.WIDTH)
        w_halt = Signal()

        m.d.sync += [
            w_valid.eq(xm_valid),
            w_pc.eq(xm_pc),
            w_we.eq(xm_we),
            w_rd.eq(xm_rd),
            # A load's word arrives on the EBR output during M; W captures it
            # (and that same word is the M-stage bypass value above).
            w_value.eq(Mux(xm_mem_read, dmem.o_rdata, xm_value)),
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
