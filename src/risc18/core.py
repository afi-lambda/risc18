"""Five-stage in-order risc18 core with data memory (M6).

The pipeline is F/D/X/M/W, Harvard, word addressed::

    F: Fetch        PCUnit drives the instruction EBR; the EBR output lags
                    the PC register by one edge
    D: Decode       instruction from the FD register -> Decoder, combinational
                    RegFile reads (write-through), M-stage bypass, immediate,
                    branch/jump resolution (BranchUnit + BranchTarget), hazard
                    check
    X: Execute      ALU (a = rs1, b = imm/rs2/shift amount), 18x18 multiply,
                    the JAL/JALR link value, and the LD/ST effective address.
                    The ALU drives the data EBR's address and write data here
    M: Memory       the data EBR has latched the X-stage address, so a load's
                    word is on ``o_rdata``: it is captured into W and is also
                    the value the D-stage bypass presents for a consumer that
                    follows the load one instruction behind
    W: Writeback    single write port into the RegFile, retire pulse

Timing, counting edges from the one that makes a PC current: the FD register
adds the second edge to the EBR's one, so an instruction is decoded two edges
after its PC became the fetch address.  A taken branch or a jump therefore has
two younger slots in flight when it resolves in D -- the word sitting on the
instruction EBR's output (the fall-through the FD register was about to
capture) and the word the EBR latches from the old PC on that same edge.  Both
are squashed: the FD register is forced empty for two consecutive edges while
the redirect moves the PC, and fetching resumes with the target's word.
Nothing killed is ever latched into X as valid, so a stale instruction can
never retire and can never write memory (only an instruction in X writes the
data EBR).

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

Hazards (no forwarding yet; M7 adds X/W forwarding): D stalls only when it
needs a register that X is about to write (``rs1``/``rs2`` of the D instruction
equal ``dx_rd`` with ``dx_we`` and ``rd != 0``), because a register written by
X -- a load included -- is not in the RegFile yet.  The instruction in W writes
through the RegFile's combinational read port in the same cycle D reads it, so
a producer three or more instructions ahead costs nothing, and the instruction
in M is bypassed into D (its ALU/MUL/link/LUI result, or the data EBR word for
a load), so a producer two instructions ahead costs nothing either.  A stall
holds ``Fetch`` (``i_hold``) and the FD register and injects a bubble into X,
so the instruction in D is executed exactly once.  A store writes no register
(``reg_write = 0``), so nothing ever stalls on it; a store's operands are
ordinary reads and are stalled for on their behalf like any other.

Control: HALT (``SYSTEM`` with ``imm6 == SYS_HALT``) is decoded in D, freezes
``Fetch`` there and squashes everything younger; the HALT itself still flows
down the pipe, retires through W exactly like golden counts it, and sets
``o_halt`` sticky.  Nothing retires after it.

MUL is a single-cycle combinational 18x18 multiply masked to 18 bits; it
already maps to one MULT18X18D, so M8 owns operand edge cases and result
handling rather than cell inference.

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
        # the two-edge squash window in D accounts for.
        fetch_alive = Signal()
        m.d.sync += fetch_alive.eq(1)      # 0 only in the post-reset cycle

        # ------------------------------------------------------------- D ---
        fd_valid = Signal()
        fd_pc = Signal(PC_WIDTH)
        fd_instr = Signal(isa.WIDTH)

        m.d.comb += decoder.i_instr.eq(fd_instr)

        # ---- operands: RegFile read, M-stage bypass ------------------------
        # The RegFile read port already carries the W-stage write-through.  A
        # register written by the instruction in M is not in the RegFile yet
        # (its write lands a cycle later), so D takes it from the M stage: the
        # ALU/MUL/link/LUI result it carries, or the data EBR word when that
        # instruction was a load.  A register written by X stalls D instead
        # (below) -- that is the load-use interlock and the distance-1 RAW.
        d_need1 = Signal()
        d_need2 = Signal()
        m.d.comb += [
            d_need1.eq(fd_valid & o.rs1_used & (o.rs1 != 0)),
            d_need2.eq(fd_valid & o.rs2_used & (o.rs2 != 0)),
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
            m_hit1.eq(xm_valid & xm_we & (xm_rd != 0) & (xm_rd == o.rs1)
                      & d_need1),
            m_hit2.eq(xm_valid & xm_we & (xm_rd != 0) & (xm_rd == o.rs2)
                      & d_need2),
        ]

        d_rs1 = Signal(isa.WIDTH)          # rs1 as the D stage sees it
        d_rs2 = Signal(isa.WIDTH)          # rs2 as the D stage sees it
        m.d.comb += [
            d_rs1.eq(Mux(m_hit1, m_value, regfile.o_rs1)),
            d_rs2.eq(Mux(m_hit2, m_value, regfile.o_rs2)),
            regfile.i_rs1.eq(o.rs1),
            regfile.i_rs2.eq(o.rs2),
        ]
        m.d.comb += [
            branch.i_a.eq(d_rs1),
            branch.i_b.eq(d_rs2),
            branch.i_cond.eq(o.branch_cond),
        ]
        m.d.comb += [
            target.i_pc.eq(fd_pc),
            target.i_imm.eq(o.imm),
            target.i_rs1.eq(d_rs1),
            target.i_jump_reg.eq(o.jump_reg),
        ]

        # LD writes rd (reg_write = 1, mem_read = 1); ST writes no register.
        d_we = Signal()
        m.d.comb += d_we.eq(o.reg_write & (o.rd != 0))

        d_lui = Signal()
        m.d.comb += d_lui.eq(o.op == isa.OP_LUI)

        d_halt = Signal()
        m.d.comb += d_halt.eq(fd_valid & o.halt)

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

        # ---- hazard: D needs a register that X writes ----------------------
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

        stall = Signal()
        m.d.comb += stall.eq(
            dx_valid & dx_we
            & ((d_need1 & (o.rs1 == dx_rd)) | (d_need2 & (o.rs2 == dx_rd)))
        )

        # ---- control transfer ---------------------------------------------
        # A branch resolves here; JAL/JALR always redirect.  A stalled D
        # instruction must not redirect yet: its operands are not final.
        redirect = Signal()
        m.d.comb += redirect.eq(
            fd_valid & ~stall & ((o.branch & branch.o_taken) | o.jump)
        )

        # Sticky freeze once HALT is in D; o_halt itself follows the retire.
        halt_q = Signal()
        freeze = Signal()
        m.d.comb += freeze.eq(d_halt | halt_q)
        with m.If(freeze):
            m.d.sync += halt_q.eq(1)

        # Two-edge squash window: the edge that would capture the fall-through
        # and the one that would capture the word latched from the old PC.
        sq = Signal(1)
        with m.If(redirect):
            m.d.sync += sq.eq(1)
        with m.Elif(sq):
            m.d.sync += sq.eq(0)

        squash = Signal()
        m.d.comb += squash.eq(redirect | sq)
        m.d.comb += [
            fetch.i_redirect.eq(redirect),
            fetch.i_target.eq(target.o_target),
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
        m.d.sync += [
            dx_valid.eq(fd_valid & ~stall),
            dx_pc.eq(fd_pc),
            dx_we.eq(d_we & ~stall),
            dx_rd.eq(o.rd),
            dx_a.eq(d_rs1),
            dx_b.eq(d_b),
            dx_rs2.eq(d_rs2),
            dx_alu_op.eq(o.alu_op),
            dx_imm.eq(o.imm),
            dx_mul.eq(o.mul),
            dx_lui.eq(d_lui),
            dx_link.eq(o.jump),
            dx_halt.eq(o.halt & ~stall),
            dx_mem_read.eq(o.mem_read),
            dx_mem_write.eq(o.mem_write),
        ]

        m.d.comb += [
            alu.i_a.eq(dx_a),
            alu.i_b.eq(dx_b),
            alu.i_op.eq(dx_alu_op),
        ]

        x_value = Signal(isa.WIDTH)
        with m.If(dx_mul):
            # Single-cycle 18x18 product, low 18 bits: signedness of the
            # operands does not affect the low bits of the product.
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
