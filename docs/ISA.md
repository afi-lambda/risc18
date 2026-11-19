# risc18 ISA v1 — frozen contract

18-bit word, 16-bit word address, Harvard, 16 registers, fixed 18-bit
instructions. Everything in this document is normative; `src/risc18/isa.py`
is its executable form and `src/risc18/golden.py` its instruction-accurate
model.

## 1. Machine state

| Item | Width | Notes |
|---|---:|---|
| GPRs | 16 × 18 | `r0` reads as 0, writes to `r0` are discarded |
| PC | 16 | word address, wraps modulo 2^16 |
| Instruction memory | 1024 × 18 | one EBR, word addressed |
| Data memory | 1024 × 18 | one EBR, word addressed |

Data memory occupies addresses `0x0000..0x03FF`. Loads outside that range
return 0; stores outside are ignored. Addresses are computed modulo 2^16.
All arithmetic is 18-bit and wraps; no overflow traps.

Reset: synchronous, active high; PC = 0, all registers 0, no memory writes.
(Exception: the optional RAM register file in section 5 cannot clear its array;
see "Optional RAM register file" below.)

## 2. Instruction formats

Instruction bits `[17:14]` are the opcode.

```
R:  [17:14] op | [13:12] fn | [11:8] rd | [7:4] rs1 | [3:0] rs2
I:  [17:14] op | [13:10] rd | [9:6] rs1  | [5:0] imm6
S:  [17:14] op | [13:10] rs2| [9:6] rs1  | [5:0] imm6
B:  [17:14] op | [13:10] rs2| [9:6] rs1  | [5:0] imm6
J:  [17:14] op | [13:10] rd | [9:0] imm10
U:  [17:14] op | [13:10] rd | [9:0] imm10
```

`S` and `B` share a layout; the opcode decides whether `rs2` is store data or
the second comparison operand.

Immediate extension used by the datapath:

| Format | Opcodes | `imm` value |
|---|---|---|
| I | ADDI, LD, ST, JALR | sign-extend `imm6` |
| I | ANDI, ORI | zero-extend `imm6` |
| I | SHIFT, SYSTEM | zero-extend `imm6` (raw function/amount field) |
| B | BEQ/BNE/BLT/BGE | sign-extend `imm6` |
| J | JAL | sign-extend `imm10` |
| U | LUI | `imm10 << 8` |

## 3. Opcodes

| Op | Mnemonic | Format | Operation |
|---|---|---|---|
| 0x0 | ALU | R | `rd = rs1 fn rs2`, fn: 00 ADD, 01 SUB, 10 AND, 11 OR |
| 0x1 | ADDI | I | `rd = rs1 + sext6(imm6)` |
| 0x2 | ANDI | I | `rd = rs1 & zext6(imm6)` |
| 0x3 | ORI | I | `rd = rs1 \| zext6(imm6)` |
| 0x4 | LD | I | `rd = mem[rs1 + sext6(imm6)]` |
| 0x5 | ST | S | `mem[rs1 + sext6(imm6)] = rs2` |
| 0x6 | BEQ | B | if `rs1 == rs2`: `pc = pc + 1 + sext6(imm6)` |
| 0x7 | BNE | B | if `rs1 != rs2`: branch |
| 0x8 | BLT | B | if signed `rs1 < rs2`: branch |
| 0x9 | BGE | B | if signed `rs1 >= rs2`: branch |
| 0xA | JAL | J | `rd = pc + 1; pc = pc + 1 + sext10(imm10)` |
| 0xB | JALR | I | `t = rs1 + sext6(imm6); rd = pc + 1; pc = t` |
| 0xC | LUI | U | `rd = imm10 << 8` |
| 0xD | MUL | R | `rd = (rs1 * rs2)[17:0]` |
| 0xE | SHIFT | I | `rd = rs1 shifted`, see below |
| 0xF | SYSTEM | I | `imm6` 0 = NOP, 1 = HALT, others reserved (NOP) |

Notes:

* Branch and jump targets are word addresses; `pc + 1` is the next
  instruction. Link values are `pc + 1` (the instruction after the jump).
* `JAL` has no register operands. `JALR` uses `rs1` plus a signed 6-bit
  offset; it does not add 1.
* `MUL` returns the low 18 bits of the 18×18 product; signedness of the
  operands does not affect the low bits.
* Reserved `SYSTEM` encodings behave exactly like `NOP`.

### SHIFT encoding

`imm6 = [5:4] kind, [3:0] amount`:

| kind | Operation |
|---|---|
| 00 | SLL: `rd = rs1 << amount` |
| 01 | SRL: logical right shift |
| 10 | SRA: arithmetic right shift (sign fill) |
| 11 | ROR: rotate right within 18 bits |

`amount` is 0..15. Shifts do not saturate; results are 18-bit.

## 4. Decoder port contract

`risc18.decoder.Decoder` is combinational. Interface:

```python
class Decoder(Elaboratable):
    i_instr: Signal(18)
    o: DecodeOutputs   # attribute names below, all combinational
```

| Output | Width | Meaning |
|---|---:|---|
| `op` | 4 | opcode |
| `rd`, `rs1`, `rs2` | 4 | register fields, extracted per format |
| `imm` | 18 | extended immediate per section 2 |
| `alu_op` | 3 | 0 ADD, 1 SUB, 2 AND, 3 OR, 4 SLL, 5 SRL, 6 SRA, 7 ROR |
| `alu_src_imm` | 1 | operand B of the ALU is `imm` |
| `shift_amt` | 4 | SHIFT amount |
| `reg_write` | 1 | writes `rd` |
| `mem_read` | 1 | LD |
| `mem_write` | 1 | ST |
| `mul` | 1 | MUL |
| `branch` | 1 | conditional branch |
| `branch_cond` | 2 | 0 EQ, 1 NE, 2 LT, 3 GE |
| `jump` | 1 | JAL or JALR |
| `jump_reg` | 1 | jump target is `rs1 + imm` (JALR) |
| `halt` | 1 | SYSTEM with `imm6 == 1` |
| `rs1_used`, `rs2_used` | 1 | operands read (hazard detection) |

`isa.decode()` must produce exactly these values for every 18-bit word.

## 5. Other frozen unit interfaces

```python
class RegFile(Elaboratable):
    # synchronous write, combinational read, write-through on same-cycle
    # read of the register being written; r0 reads 0, writes ignored
    i_rs1: Signal(4); i_rs2: Signal(4)
    o_rs1: Signal(18); o_rs2: Signal(18)
    i_rd: Signal(4); i_wdata: Signal(18); i_we: Signal(1)

class ALU(Elaboratable):
    i_a: Signal(18); i_b: Signal(18); i_op: Signal(3); o_y: Signal(18)
    # ops as in section 4; shift amount is i_b[3:0]

class BranchUnit(Elaboratable):
    i_a: Signal(18); i_b: Signal(18); i_cond: Signal(2); o_taken: Signal(1)
    # signed comparison for LT/GE

class InstrMemory(Elaboratable):
    InstrMemory(init=[])          # list of 18-bit words
    i_addr: Signal(16); i_en: Signal(1); o_data: Signal(18)
    # synchronous read, data valid the cycle after i_addr/i_en are sampled

class DataMemory(Elaboratable):
    DataMemory(init=None)         # 1024 x 18; init padded/truncated to 1024
    i_addr: Signal(16); i_wdata: Signal(18)
    i_we: Signal(1)
    o_rdata: Signal(18)           # synchronous read, 1-cycle latency
    # addr >= 0x400: reads 0, writes ignored (address truncated to ten bits, a
    # registered in-range flag masks the read data, the write enable is
    # qualified with the combinational in-range flag).  Both ports share
    # i_addr, so a store cycle is a same-address read+write; the read sees the
    # old word (read-before-write) and no consumer looks at it.
    # Reset performs no memory writes: contents survive a reset pulse.

class BranchTarget(Elaboratable):
    i_pc: Signal(16); i_imm: Signal(18); i_rs1: Signal(18)
    i_jump_reg: Signal(1); o_target: Signal(16)
    # jump_reg=0: (pc + 1 + signed(imm)) & 0xFFFF  (branch, JAL)
    # jump_reg=1: (rs1 + signed(imm)) & 0xFFFF    (JALR, no +1)

class PCUnit(Elaboratable):
    i_redirect: Signal(1); i_target: Signal(16); i_hold: Signal(1)
    o_pc: Signal(16)
    # redirect > hold > increment; wraps at 2^16; sync reset clears to 0

class Fetch(Elaboratable):
    Fetch(init=[...])             # words loaded into the instruction EBR
    i_redirect: Signal(1); i_target: Signal(16); i_hold: Signal(1)
    o_pc: Signal(16)              # PC register (address being fetched)
    o_instr: Signal(18)           # EBR output for o_instr_pc
    o_instr_pc: Signal(16)        # PC that produced o_instr
    # PCUnit drives InstrMemory; i_hold freezes both.  The instruction for a PC
    # value is present one clock edge after its PC register update (two edges
    # counting the edge that loaded the PC).  PC >= 0x400 reads 0: the address
    # is truncated to ten bits (so the EBR aliases) and a registered in-range
    # flag masks the data to 0.  init is padded/truncated to 1024 words.
    # The cycle after reset shows o_instr = 0 with o_instr_pc = 0, which is not
    # a fetched instruction: the core must not consume it.
```

## 6. Core (M5 onward)

```python
Core(program=[...], data=None)   # instruction words; optional 1024-word data image
  i_rst:            Signal(1)    # synchronous active-high, drives sync.rst
  o_pc:             Signal(16)   # current fetch address
  o_retire:         Signal(1)    # one cycle per instruction leaving W
  o_retire_pc:      Signal(16)   # valid with o_retire
  o_retire_we:      Signal(1)    # high only for a real rd != 0 write
  o_retire_rd:      Signal(4)
  o_retire_value:   Signal(18)
  o_halt:           Signal(1)    # sticky, set when HALT retires
  cd:               ClockDomain("sync")   # owned by Core
```

`Core` owns the `sync` domain it resets (`cd`), so an enclosing top must drive
`cd.clk`/`cd.rst` and may not declare a second `sync` domain of its own.

`o_retire` pulses once per instruction, in program order. The `o_retire_we=1`
rows match `golden.Machine.trace` for every program, loads included (M6).
`o_halt` asserts when the HALT instruction retires; fetching is already frozen
at that point and the PC holds its post-HALT value.

Pipeline (M10+): F (PC register, instruction EBR, decode, FD register -- the
decoder is combinational on the EBR output register and its 19 fields are
captured with the FD register, so D starts from registered state), D
(combinational regfile read, hazard check, operand select), X (ALU, multiply,
link value, LD/ST effective address, branch/jump resolution), M (data EBR, load
word), W (register writeback). The data EBR is addressed during X, so a store commits on
the X->M edge and a load's word is on the EBR output during M; D takes a
register written in M through an M-stage bypass (the ALU result or the load
word), so only a distance-1 load producer stalls. The EBR output lags the PC
register by one edge; decode runs on that registered word and its 19 fields are
captured into the FD register, so D sees decoded fields two edges after the PC
became the fetch address.

### Optional RAM register file

`Core(ram_rf=True)` replaces section 5's flop register file with
`risc18.ramrf.RegFileRam`, a 16 x 18 distributed-RAM array (`TRELLIS_DPR16X4`).
Two logical read ports, one write port, `r0` reads zero and write-then-read
ordering are all preserved, but three things differ: reads are registered (one
cycle of latency, so the core issues them in F), the write-through bypass moves
into the operand select, and the array is **not** cleared by the reset (it is
zero only after configuration, so the section 5 reset contract above does not
hold for this variant unless `RegFileRam(clear=True)` is used).
The default core uses the flop register file described above; the RAM variant is
an area/FF option, measured in `docs/metrics/NOTES.md` (`## M11`).

Control transfer (M9+): the branch compare and the target adder run in X, on
forwarded operands, and their result is registered as a control-transfer
decision that the fetch unit applies on the following edge. This costs two
extra slots relative to the M7 arrangement -- a taken branch or jump kills the
four fetch slots in flight (the two decode/fetch slots plus the two words the
EBR and FD registers still hold) -- and buys the pipeline's timing: the
compare -> redirect -> PC mux -> FD capture loop that limited the design to
~48 MHz no longer exists (65.8 / 67.9 / 70.0 / 73.9 MHz over PnR seeds 1/2/3/7
at the 60 MHz constraint, before the M10 decode retiming). Not-taken branches
and all non-control instructions cost nothing extra.

Hazard policy (M7+): D sees a register value in the same cycle through
forwarding -- X stage first (its ALU/MUL/link/LUI result), then M (its result
or load word), then the regfile's same-cycle write-through for W. Only a
load-use dependency stalls: X is a load and D needs its destination, because
the load word does not exist until M. That costs exactly one bubble;
distance-1 ALU dependencies and every dependency at distance 2 or more cost
nothing.

MUL (M8): `rd = (rs1 * rs2)[17:0]`, computed in X by one `MULT18X18D`
(unsigned; the low 18 bits are signedness-independent), written inline in the
core so yosys maps it without an extra module boundary. The DSP is the only
multiplier path; `risc18.mul.Multiplier(registered=True)` offers the DSP's own
output register, which costs one bubble per MUL and measured no resolvable fmax
gain, so it is a documented option rather than the default.

LD/ST (M6): `LD rd, imm(rs1)` writes `mem[(rs1 + sign_ext(imm6)) & 0xFFFF]`,
`ST rs2, imm(rs1)` writes the same address; addresses >= 0x400 read 0 and
ignore writes. A load followed immediately by a consumer costs exactly one
bubble; independent loads retire at one per cycle.

Reset: the core must forward its synchronous active-high reset to the `sync`
domain's `rst`. Unit instances (including `PCUnit` and `RegFile`) must be
elaborated under that reset-bearing domain; only the synthesis measurement
tops in `tops.py` use a reset-less domain, and they are not the core.

## 7. Core7 (seven-stage restart) — contract

`Core7` is a new core, not a change to `Core`. Encodings, machine state, the decoder
port contract and the retire interface of section 6 are unchanged; the golden model and
every existing test apply as they are. What differs is the timing contract, and this
section is normative for it.

### 7.1 Stages

```
F   PC register -> instruction EBR address
IQ  instruction EBR output -> FQ register -> decode fields
D   decode fields registered; register-file read address presented
R   register-file data captured; W->R bypass select; operands registered
X   ALU (add/sub/logic/shift), compare, multiply issue; effective address
M   data EBR address presented (store data too); load launched
W   register writeback; registered load word
```

The memories are EBR with **latency 2 by edge contract**: X presents the data address so
the block samples it at the X/M edge, its output is captured at the M/W edge, and W
exposes the registered word. The 5.6 ns DP16KD clock-to-out therefore sits alone in a
cycle. Measured: read half 6.7-6.9 ns (145.10-150.08 MHz), R stage 3.8 ns (263.44 MHz),
X with the product registered 7.7 ns (129.85 MHz), worst stage X.

### 7.2 Register writes and bypass

A register write commits at the closing edge of W, and consumers latch operands in R.
Because the pipeline is seven deep, a producer and its consumer are further apart in
cycles than in section 6, and that *reduces* the stall count rather than raising it:

| dependency | bubbles | mechanism |
|---|---:|---|
| ALU or MUL -> consumer at distance 1 | 0 | M->X input bypass: the producer's registered result exists in M while the consumer executes in X |
| ALU or MUL -> consumer at distance >= 2 | 0 | X->R bypass latched at the R edge |
| load -> consumer at distance 1 | 1 | the word is registered only at the end of the load's M, i.e. one cycle after the consumer's X opens |
| load -> consumer at distance >= 2 | 0 | the consumer's X arrives after the word is in W |

So the load-use rule stays at exactly one bubble, as in section 6, and every ALU/MUL
dependency is free at any distance.

The cost of the first row is timing, not cycles: the M->X bypass adds a select at the
ALU input, which is not in any probe. `ProbeXstageMulReg` measures the ALU with an
output select at 7.7 ns against an 8.33 ns budget, so the input mux spends most of the
remaining 0.6 ns. If the integrated X stage misses 120 MHz because of it, the fallback is
**one bubble at distance 1** rather than a wider pipeline -- measured, not assumed, and
reported either way.

### 7.3 Control transfer

The PC-relative target is computed in **D** and registered (it needs PC and displacement,
not forwarded operands). The compare runs in **X** on R-latched operands, and the
decision is applied at the X/M edge. Recovery is 2 cycles; not-taken costs nothing.
Prediction is static backward-taken until P1 measures a mispredict rate that justifies
otherwise. No branch-hint bit is added to the encoding: the frozen v1 formats stay
frozen, and a hint bit would be an ISA change for a policy that is not yet measured.

### 7.4 Constraints on the implementation

- Do not widen the pipeline further to hide a stage. If a stage misses 8.33 ns, report
  it, do not add a stage.
- Do not move the register-file read into X, and do not read the EBR in R: both were
  measured (83.4 MHz and 111.53 MHz respectively).
- Do not rely on reading just-written distributed RAM; the write-through and bypass are
  explicit selects.
- `o_retire` semantics are unchanged: one pulse per instruction in program order, with
  `o_retire_we` matching `golden.Machine.trace`.
