# Restart gate: memory-to-memory at 120 MHz, then a memory-centric 18-bit core

Status: proposal, not executed. Written 2026-11-19 against main `8dc6199`.

## 1. The gate, stated precisely

"We restart when `EBR -> ALU -> EBR` runs above 120 MHz" needs one disambiguation,
because the answer flips on it: **is that one clock or two?**

Measured on this part today (`docs/metrics/NOTES.md`, probes committed):

| chain | one cycle | equivalent | verdict at 120 MHz (8.33 ns) |
|---|---:|---:|---|
| EBR read -> fabric register | 6.7 ns | 149.97 MHz | PASS, 1.6 ns spare |
| EBR read -> add -> EBR write data | 9.0 ns | 111.53 MHz | FAIL by 0.67 ns |
| + 16:1 operand mux | 10.4 ns | 96.35 MHz | FAIL by 2.1 ns |
| + 18x18 multiply | 13.2 ns | 75.76 MHz | FAIL by 4.9 ns |

Of the 9.0 ns, **5.6 ns is the DP16KD clock-to-out alone** (`mem.0.0.DOA9` to the
first cell, REGMODE=NOREG in the routed report). In a single 8.33 ns cycle that
leaves 2.7 ns for the add, the second memory's address/data setup and all
interconnect. The add alone is 2.0 ns of logic and the write setup is not free, so

**a single-cycle `EBR -> ALU -> EBR` cannot reach 120 MHz on this fabric with the
current mapping. No amount of RTL tuning changes that; 5.6 ns of it is a hard
block.** The gate as literally written is unreachable, and the honest move is to
redefine it rather than to chase 0.67 ns of luck.

Two-cycle, by contrast, is already proven: read in cycle N (6.7 ns, 149.97 MHz),
consume in cycle N+1 (register -> add -> write setup: ~2 ns of logic plus setup,
comfortably over 200 MHz). So:

> **Restart gate (redefined): `EBR -> (register) -> ALU -> (register) -> EBR` with
> memory latency 2, at >= 150 MHz, worst seed, no misses.**
>
> This is met by measurement already for the read half and needs one new probe for
> the write half (P0 below). It is also the only form in which the chain is useful:
> a CPU that must finish a load-add-store inside one cycle at 120 MHz is not
> buildable here, and one that pipelines it is.

Rejected alternative, recorded so it is not re-litigated: instantiate `DP16KD`
directly with `REGMODE=OUTREG` and `IREG` so the block registers its own I/O
(clock-to-out drops to roughly 1.5 ns, which would put a single-cycle chain near
200 MHz). It is technically possible through a raw `Instance` and it is the only
route to a one-cycle memory-to-memory op. It is rejected for the restart because it
costs the portable `Memory` abstraction, ties the design to one vendor primitive,
and buys a capability (single-cycle RMW) that the ISA does not need. Keep it as a
later optimisation for a specific hot instruction, not as the foundation.

## 2. What the last two attempts taught, in one line each

- Fetch was the limiter at 73.48 MHz (`imem DOA -> decode -> regfile addr -> DPR16X4
  -> FF`, 13.6 ns = 6.8 logic + 6.8 routing). Cutting it bought 7 %.
- The limiter then moved to the X cone (`dx_b` select -> `o_target` carry chain ->
  `i_a` / dmem address) at 2.9-3.4 ns logic + 9.3-10.5 ns routing. **The pipeline is
  no longer the problem; placement is.**
- `Full6Top` 74.82-78.90 MHz, CPI 1.85/1.71/2.05 -> 42.6 MIPS, below the 5-stage
  core's 45.5 MIPS. More stages bought frequency and lost throughput.
- The PLL never ran at its requested rate until 2026-11-18; any pre-fix "60 MHz"
  board claim was wrong.

Consequence for the new design: **placement constraints are part of the
architecture, not a late optimisation.** A new pipeline that repeats the same
unconstrained flow will land in the same 75-80 MHz band.

## 3. Architecture

Signoff: **120 MHz at CPI <= 1.5 = 80 MIPS** (1.76x the 45.5 today). 100 MIPS needs
150 MHz at CPI 1.5 or 120 MHz at CPI 1.2. Memory latency 2 is absorbed, not fought.
(Section 6 of the review, and the arithmetic is mine to own: 120/1.5 = 80, not 100.)

```
  F     IQ    D      R      X      M      W
  |     |     |      |      |      |      |
 imem  imem  decode file   ALU    mem    mem
 addr  data  +file  data   +cmp   addr   data
             addr   +bypass             write
```

**Seven stages, not six** (P0b measurement, `docs/metrics/NOTES.md`): the realistic X
stage -- register-file read, bypass select, ALU, compare in one cycle -- measures
83.4-83.8 MHz at a 120 MHz constraint, identical across seeds, and moving the target
adder to D changes it by 0.4 MHz. The cost is the operand path (5.4 ns logic + 6.5 ns
routing), not the target adder. With operands already latched the same ALU cone is
181.79 MHz, so the fix is a register between the file data and the execute: stage R.

- **Harvard, both sides EBR, latencies declared in the ISA-visible timing model:**
  imem 2 cycles (F, IQ), dmem 2 cycles (M, W). Loads write back from W, so a
  load-use interlock costs exactly one bubble, same as today.
- **Early issue for known addresses** (review rank 2, and the one option I had not
  listed): for fetches and table lookups the address is known ahead, so launch the EBR
  read a stage earlier instead of making the read faster. It costs address/tag/valid
  state, possibly a small buffer, and a wasted read slot on a redirect. It does nothing
  for a late data-dependent address — that is what the latency-2 contract is for.
- **Register file stays in distributed RAM** (`DPR16X4`, ~1.9 ns access). Putting it
  in EBR would add a cycle to every instruction to save LUTs the design does not
  need (5 % utilisation today).
- **Memory contract by edges, not by the word "latency".** X forms the address so the
  EBR samples it at the X/M edge; the EBR output travels to a response register at the
  M/W edge; W exposes registered load data. That puts the memory off the ALU path.
  A store does not need a response stage.
- **Branches: compare stays in X; the PC-relative target moves to D.** The target needs
  PC and displacement, not forwarded operands, so compute and register it in D and let X
  select the redirect on the comparison result — a move that need not cost a bubble. A
  register-indirect target keeps its real operand dependency and stays in X. Static
  backward-taken prediction; mispredict costs 2 cycles, and that recovery must be shown
  on a cycle trace rather than assumed from stage labels. Budget: with ideal CPI 1 and
  2 lost cycles per taken branch, taken branches cap at 25 % of retired instructions;
  the general bound is `b*t <= (0.5 - s)/2`.
- **MUL: DSP with its output registered**, latency 2, one bubble on dependent use
  (measured: 9.85 ns in the select path vs 7.7 ns registered).
- **Placement: constrain endpoints before drawing a region.** Order: (1) real board pins,
  the PLL and the global clock route in the full shell; (2) anchor the data EBR at a legal
  site and check the X address drivers and the response registers around it; (3) only then
  a **loose, selective** region, tested over the same seeds against an anchored-but-
  unconstrained build. The failure mode is a *legal* placement with worse routes: a region
  constrains sites, not wires, and does not reserve area from unrelated logic — a tight one
  can crowd the EBR or split a path across its boundary. Never keep a region because it
  produced one good seed. Reset is fixed architecturally (release into the core clock
  domain, local valid gates the store), not by a pin LOC.
- **Width:** keep 18 bits. One DP16KD holds 1024x18, 18x18 is a native multiplier, and the
  ISA, assembler, golden model and 317 tests are already built around it.
- **Load/store schedule**, which follows from the edge contract: a load in X at N launches
  the EBR read at the X/M edge, its registered value is in W at the start of N+2, and a
  W-to-X bypass means **one bubble for an adjacent consumer, none with one independent
  instruction between**. W drives the register-file write and the bypass in the same cycle,
  from the W register; never rely on reading just-written distributed RAM. A store's base
  obeys the load-use rule, its payload is needed only at the write-sampling edge, so it can
  be forwarded late — but then a younger read must be blocked against an older write or
  handled by same-address forwarding, and that structural stall has to be counted.

## 4. Work order

P0 — prove the gate (1 session, measurement only, no RTL change):
  1. `Probe2C` (`src/risc18/probe_mem_pipe.py`): EBR read -> `q_r` (cycle 1) ->
     add -> EBR write address/data (cycle 2), built at 150 MHz.
  2. `Probe2CMux`: same with the second operand behind a 16:1 register-read mux.
  3. Gate: >= 150 MHz worst seed. If not, stop and report.

  **Result, seeds 1/2/3/7 (post-route, 150 MHz constraint):**

  | probe | period | routing | worst seed | at 120 | at 150 |
  |---|---:|---:|---:|---|---|
  | `Probe2C` | 6.7-6.9 ns (5.6 logic) | 1.1-1.3 ns | **145.10 MHz** | met, +21 % | missed by 3.3 % |
  | `Probe2CMux` | 9.4 ns (2.3 logic) | 7.1 ns | 106.51 MHz (seed 1) | **missed** | missed |

  Two conclusions, both architectural:

  - **The two-cycle gate is met at 120 MHz with margin, and 150 MHz is a
    block-limited stretch.** Splitting the chain at a register turns 9.0 ns into a
    6.7 ns read half plus a shorter write half. The 5.6 ns clock-to-out is paid
    alone in its cycle; it is 84 % of an 8.33 ns period and only `REGMODE=OUTREG`
    on the primitive would shrink it. Worst seed 145.10 MHz, spread 5 MHz (1.1 vs
    1.3 ns of routing).
  - **The 16:1 operand mux costs 7.1 ns of routing by itself.** The routed path is
    `r_sel` FF -> LUT (2.5 ns net) -> LUT (2.0 ns) -> LUT (0.9) -> LUT (0.9), i.e.
    a four-level LUT tree spread over four tiles — the same mechanism that capped
    the six-stage X stage. So the operand select must not be a wide mux in the ALU
    cycle: read operands from **distributed RAM** (`DPR16X4`, ~1.9 ns
    address-to-out) or from narrow, physically clustered muxes. This is a
    placement-level requirement, not a stylistic one.

  Caveat on the probe's routing: one consumer, so 1.1-1.3 ns is a lower bound. In a
  populated core the read stage also feeds the fetch register and decode; budget
  6.7-7.5 ns (133-150 MHz). Commit 120 MHz, chase 150.

P0b — prove the timing and the cycle contract *together* (a bare read probe cannot answer
  whether a bypassed core closes timing). Build one representative slice: distributed-RAM
  register file, bypass fan-out, X address generation, data EBR, W capture, plus cycle
  checks for ALU-to-branch, load-to-use, load-to-store-data and MUL. **Route a short
  dependent ALU chain that also exercises same-address read/write, and compare it with an
  independent stream**: the question is whether the split survives with the hazard
  machinery present (forwarding on the EBR-to-split-flop path spends the 1.44 ns of 120
  MHz slack) and still issues ~1/cycle. Gate: slice closes 120 MHz worst seed *and* the
  cycle counts match the contract on a trace.

  **Re-run on the seven-stage cone: every stage fits.** `ProbeRstage` (file read +
  bypass -> FF) is 3.8 ns / 264.34 / 263.44 MHz, and without the bypass mux 2.9 ns /
  343.05 / 338.98 MHz. With X at 7.7 ns (`ProbeXstageMulReg`, mul registered) the worst
  stage is X, 8 % inside the 120 MHz budget. P3 is unblocked; the remaining risk is
  integration (bypass fan-out, hazard logic, placement), not any single cone.

  **First result (six-stage X) was: gate NOT MET, and it changed the architecture.**
  in `docs/metrics/NOTES.md`; the short form is `ProbeXstageRF` 83.38 / 83.43 MHz
  (seeds 1/3, 12.0 ns = 5.4 logic + 6.6 routing) and `ProbeXstageRFNoTgt` 83.84 /
  83.76 MHz. Moving the target adder to D buys 0.4 MHz, so the operand path — file read
  + bypass select in the execute cycle — is the cost, and with operands latched the same
  ALU cone is 181.79 MHz. The contract therefore gains stage **R** between the file and
  the ALU (§3).

P0c — settle model vs silicon on the 5.6 ns read arc, cheaply. Fixed EBR launch clock,
  a second same-frequency PLL output shifted in fine phase (1/8 VCO period = 0.21 ns at a
  600 MHz VCO) into the capture flop; run at 60 MHz, since the point is the
  launch-to-capture interval. Complementary words, alternating addresses, count
  old-word vs new-word captures per phase in both directions, fixed placement, plus a
  nearby fabric-flop reference with the same clock pair to expose clock insertion offset.
  Report a transition band, not a threshold. Only a *qualified* 6.4 ns full-core path
  would reopen the single-cycle option; a fast board measurement alone does not.

P1 — placement feasibility (1 session, flow only), in the review's order: real board
  pins and the PLL/global clock in the full shell, then anchor the data EBR and inspect
  the X address drivers and response registers, then a loose selective region only if it
  beats the anchored-but-unconstrained build over seeds 1/2/3/7 at 120 MHz. Gate: worst
  seed >= 100 MHz and repeatable across seeds; a single good seed does not count.

  **First arm measured: knob-level placement is worth ~5 MHz.** `Full6Top` seed 1 at a
  120 MHz constraint: baseline 78.35 MHz, `--placer-heap-timingweight 50` -> **83.29**,
  `200` -> 75.83. So the placer's own timing weight is not the 120 MHz lever. Note also
  that nextpnr-ecp5's LPF supports `LOCATE`/`IOBUF`/`BLOCK` but **not** `REGION`, so a
  real region constraint has to be expressed either as per-cell `LOCATE` lines or as a
  `--pre-place` Python script calling `bindBel`, both fragile against yosys cell names.
  That is the remaining P1 work, and it is a day, not an afternoon.

P2 — ISA delta, not a new ISA: add only what the 6-stage timing model needs
  (explicit load latency in the docs, a branch-hint bit if P1 says mispredicts
  matter). Freeze it before RTL.

P3 — `Core7` written to the frozen ISA, simulation-first: golden model and the
  existing 317 tests must pass unchanged, plus new tests for the 2-cycle memory
  contract and the branch penalty. Gate: pytest 100 %.

  **Result: SIM green, PnR RED — and the restart is stopped here.** `Core7` is on main
  (`src/risc18/core7.py`): 355 tests pass, the retire trace equals `golden.Machine.trace`
  and `Core` on every program, and the bubble table of ISA 7.2 is met exactly (ALU and
  MUL free at distance 1 and 2, load-use one bubble at distance 1). But `Core7Top`
  routes at 64.49 / 68.20 / 70.56 MHz (seeds 3/1/2) against the 120 MHz gate, and
  `Core6Top` on the same flow is 77.92 MHz, so seven stages are 12 % *slower* than six
  and both are slower than the five-stage core's 73.48 MHz at 45.5 MIPS. The measured
  X path is 14.7 ns (4.4 logic + 10.3 routing) and ends in the data-EBR address: the
  bypass network, the tag compares and the EBR address fan-out that no probe contained
  are exactly the integration tax the review predicted. Dropping the M->X bypass and
  paying a bubble buys only 75.58 MHz, so the bypass mux is not the whole story either.

  Consequence for the program: stage-level probes at 120-150 MHz did not survive
  integration, and the memory-centric restart as specified does not reach 120 MHz on
  this flow. The remaining untested lever is placement (routing is 70-75 % of every
  critical path at ~1.5 kLUT and `tools/synth.py` still emits only `FREQUENCY NET`
  "clk"), and that decision belongs to the user.

P4 — routed and on the board: worst-seed fmax >= 120 MHz **and** >= 80 MIPS on the same
  counted loop as the baseline (45.5 MIPS), then `hil_status` on hardware with the
  measured MIPS on the line. Timing alone does not sign this off. **Not reached: P3's
  routed gate failed.**

## 5. Stop conditions

- P0 misses 150 MHz: the memory-centric premise is wrong; report, do not proceed.
- P0b misses 120 MHz: **fired on 2026-11-19** at 83.4-83.8 MHz. The response is a
  contract change, not a pass: stage R added (§3), and P0b must be re-run on the
  seven-stage cone before P3. Do not build Core7 to the six-stage contract.
- P1 misses 100 MHz: placement cannot buy the target; the honest fallback is to
  keep ~78 MHz and attack CPI, and that decision belongs to the user, not to a
  stage worker.
- No stage may raise its own gate to pass. A red gate is reported as red.
