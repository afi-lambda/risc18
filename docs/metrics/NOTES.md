# Metrics conventions

`tools/synth.py` reports post-place-and-route numbers from nextpnr for the
Colorlight 5A-75B device (LFE5U-25F-6CABGA256, speed grade 6) at the stated
clock constraint. The `.md` files in this directory are copied verbatim from
`build/metrics/<name>.md`; if they disagree with a fresh run, the committed
file is wrong.

Caveats that apply to every unit milestone (M0–M4):

* Unit tops in `src/risc18/tops.py` are measurement wrappers. They register all
  inputs and outputs (so nextpnr has an interior register-to-register path) and
  are synthesis-only: the `sync` domain is reset-less and driven by the `clk`
  port, so the netlist has one clock port and no reset network.
* Because of that wrapping, part of the reported LUT/FF count belongs to the
  wrapper, not the unit. The `.md` numbers are therefore an upper bound for the
  unit and are not directly comparable to the M5+ `Core` metrics.
* M9 re-measures the full core with the PLL and the real reset network; treat
  that as the authoritative resource and timing table.
* `RegFileTop` specifically: 15 × 18 = 270 storage FFs (r0's storage is
  trimmed), 31 input pipeline FFs and 36 output FFs, i.e. 67 of the 337 FFs are
  the measurement shell. Two 16:1 read muxes dominate the LUT count; a binary
  tree over the index bits was measured at ~700 LUT4 with identical behaviour
  and is a candidate for M9, not an M1 defect.
* M2 ALU: all 339 LUT4 are the unit (the wrapper adds only 57 FFs). The shift
  group is about two thirds of it. Informal single-run observations from the
  M2 review, not part of the milestone evidence (no committed variant source):
  a passthrough variant measured ~108 LUT4; a shared SRL/SRA right-shift
  network with a fill mux measured 363 LUT4 / 120 MHz. Re-measure through the
  full flow before using either as an M9 plan.
* M3 PC / branch target (FF convention: every register in the top, input and
  output, counts as wrapper): PcTop is 50 FFs = 34 wrapper (input + output) +
  16 PC; BranchTargetTop is 65 FFs = 49 wrapper (input + 16-bit output) + 0
  unit FFs, with `imm_q`/`rs1_q` 16 bits wide because the upper immediate bits
  are dead. Unit LUT figures here are pre-mapping yosys cells; nextpnr adds
  2 LUT4 per CCU2C (17 + 2×8 = 33, 16 + 2×16 = 48). The `hold` input costs no
  LUT: it becomes a native FF clock enable. BranchTarget builds two adders
  although only 16 result bits are used; collapsing to one adder with a muxed
  addend is an M5 option.
* M4 instruction memory / fetch: InstrMemoryTop is 5 LUT4 + 36 FF with one
  DP16KD; FetchTop is 37 LUT4 + 101 FF, one DP16KD. The unit's 5 LUT4 are a
  ~4-LUT4 in-range compare (LUT4 + PFUMX + L6MUX21) plus a 1-LUT4 clear that
  forces the output register to 0 through its LSR pin. nextpnr maps the read
  port to DP16KD with `REGMODE_A="NOREG"` and `CEA = en`, i.e. `en` freezes the
  EBR address register, not a fabric output register. The 5.6 ns critical path
  is EBR array access to DOA plus the destination setup — that is the F-stage
  floor the pipeline inherits. The measurement image varies 17 of 18 bits (bit
  17 is held high so no word is zero) and those 17 bits each drive a live DOA
  port; bit 17 folds to a constant that the in-range clear gates, so FF counts
  are still not reduced by constant folding.

## M5 core (`CoreTop`) — not a unit top

* 1914 LUT4, 521 FF, 1 DP16KD, 1 MULT18X18D, 53.89 MHz against the 60 MHz
  constraint (seeds 2 and 7 give 55.71 and 55.23 MHz, so the miss is not seed
  luck). The 16-word measurement image has instruction bit 3 constant, so
  `fd_instr[3]`/`dx_imm[3]` fold: 521 = 523 - 2 and the figure is
  program-dependent.
* Critical path (structural, not placement noise): FD register -> decoder ->
  operand mux -> branch compare -> target adder/PC mux -> redirect+squash select
  -> FD capture mux -> FD register, 3.7 ns logic and 14.9 ns routing. A branch
  resolves and redirects in one cycle, so closing it is M9's job. Candidate
  fixes, best first: collapse `BranchTarget`'s two adders into one muxed-addend
  adder (its 16-bit post-adder mux sits on this path); register the branch
  target result and redirect from X (one extra branch bubble); precompute
  `pc + 1 + imm` in F so D only selects; or add the fifth pipeline stage the
  design brief suggests.
* CPI: 1.00 for a dependency-free straight line, 1.74 for a counted loop, 1.83
  for a distance-1 dependent chain (exactly one bubble per dependency), 1.00 at
  distance 2 and beyond.
* The core owns its `sync` domain, so this is the one top that is not
  reset-less and not purely a measurement shell; FF counts include 57 shell FFs
  (11%).

## M6 data memory and LD/ST

* `DataMemoryTop`: 6 LUT4, 54 FF, 1 DP16KD, 147.78 MHz; the 5.6 ns critical
  path is the EBR clock-to-out to the output register, the same floor the
  instruction EBR has. The unit's own state is one flag; most of the 54 FFs are
  the wrapper's input/output registers and the registered read path.
* The read port is built with `transparent=False` (read-before-write) and both
  tops now map the data EBR to DP16KD `READBEFOREWRITE` with `REGMODE_A=NOREG`,
  which is what the simulation asserts. With the legacy `transparent=True`
  default the two tops mapped differently and neither matched the simulation
  contract: DataMemoryTop got a READBEFOREWRITE primitive plus a ~30-FF fabric
  bypass, CoreTop got `WRITEMODE_A=WRITETHROUGH`. The non-transparent port costs
  ~210 LUT4 (1883 -> 2096 on CoreTop, seed 1) and is kept so the contract does
  not depend on which of those yosys builds; nothing in the core consumes the
  collided word, so either choice is functionally correct today.
* `CoreTop` at M6: 2096 LUT4, 584 FF, **2** DP16KD (instruction + data), 1 DSP.
  fmax over seeds {1,2,3,7} = 48.56 / 51.28 / 52.19 / 51.54 MHz (mean ~50.9),
  against M5's 48.5-55.7 over the same seeds (means 50.89 vs 53.34): the mean
drop is ~2.4 MHz and any
  single-seed delta (the M6 PR quoted 49.12 vs 53.89) sits inside the ~7 MHz
  placement spread, so quote the sweep, not one seed. The worst path is still
  the D stage, now with the M-stage bypass *select chain* (`xm_we`/`xm_rd`/
  `d_need` -> `stall`/`m_hit`) ahead of the operand mux and the branch compare:
  FD register -> decoder -> bypass select + operand mux -> compare/redirect ->
  FD capture mux. M7's X-forwarding lengthens exactly that chain, which is the
  M9 risk to plan for (resolve the compare in X, precompute the branch
  condition in M, or register the load word and accept a second load-use
  bubble).
* Stores are addressed combinationally from the X stage and commit on the X->M
  edge, qualified by X-stage validity and by `~i_rst`: a store whose X cycle is
  the reset cycle does not commit. A store that already committed on the X->M
  edge stays written even if a reset kills it before W -- reset does not undo
  memory writes. `test_reset_on_the_stores_x_cycle_does_not_commit_it` pins the
  boundary and is red without the gate.
* CPI: load-use pair 1.167 (one bubble); 10 back-to-back loads 1.000; distance-2
  dependencies 0 bubbles (M bypass); distance-1 ALU dependencies still 1 bubble,
  which is M7's to remove.

## M7 X-stage forwarding

* `CoreTop` at M7: 2166 LUT4, 584 FF, 2 DP16KD, 1 DSP. fmax over seeds
  {1,2,3,7} = 48.81 / 47.68 / 47.24 / 45.96 MHz (mean 47.42), against M6's
  48.56 / 51.28 / 52.19 / 51.54 over the same seeds (mean 50.89): a ~3.5 MHz
  mean drop and the ranges overlap only at their extremes, so treat it as a
  small structural cost plus placement noise, and always quote the sweep.
  (Dropping the redundant `rd != 0` compares from the hit terms recovered
  ~2 MHz and ~100 LUT4 relative to the first M7 cut.)
* The LUT growth is mostly the select: a single-line ablation (`x_hit1/2 := 0`,
  functionally invalid, measurement only) removed 145 LUT4 and 79 PFUMX from
  the first cut, whose remaining growth was the narrowed stall plus ABC/netlist
  reshuffling. FF, EBR and DSP counts are unchanged; CCU2C goes 63 -> 62.
* CPI: a 20-instruction distance-1 ADDI chain (not in the suite) went 1.90
  (M6) -> 1.00 (M7); the 8-instruction program of
  tests/test_core.py::test_distance_one_alu_chain_is_gapless went 1.75 -> 1.00;
  a
  load-use chain stays at exactly one bubble per load; the `CORE_TOP_PROGRAM`
  image 1.62 -> 1.31; the five-iteration store/load loop of
  tests/test_core.py::test_memory_accumulation_loop 1.51 -> 1.37; independent
  loads remain 1.00.
* A JAL/JALR link value is never consumed at distance 1 by construction: the
  jump resolves in D and empties the two younger fetch slots, so the
  interesting case is the word after the target, which costs nothing.

## What M9 must actually do

Timing closure is **not** achievable by placement effort on the current D-stage
structure. Nine PnR runs put the best total path at 19.16 ns (M6 seed 3) and
20.5 ns (best M7, seed 1, 48.81 MHz) against the 16.67 ns budget of 60 MHz.
Per-seed M7 splits are 9.2/11.3, 7.9/13.1, 8.8/12.4 and 4.4/17.3 ns of
logic/routing; seed 7 is routing-bound (17.3 ns of routing alone, under 58 MHz
with zero logic) while seeds 1-3 route in 11.3-13.1 ns, so the miss is a mix of
logic depth and placement. A measurement-only ablation that deletes the X select
measures 48.47 / 53.29 / 54.20 MHz on seeds 1/3/7 at this head (50.93 MHz on the
superseded first cut), i.e. still ~6 MHz short at best. M9 therefore needs a
microarchitectural change,
in order of expected value: move the branch compare/resolve out of D (it is
the largest single block on the path: 18-bit CCU2C chain -> redirect -> FD
capture mux), register the operand-select inputs, or add the fifth stage the
design brief suggests. Use 19.1-23.6 ns as the observed starting range.

## M8 DSP multiplier

* `MulTop` (combinational) is 0 LUT4, 54 FF, one MULT18X18D at 141.4 MHz;
  `MulRegTop` (DSP output register, `REG_OUTPUT_CLK=CLK0`) is 0 LUT4, 54 FF, one
  MULT18X18D at 185.01 MHz. Both are pure DSP: no LUT/carry multiplier exists in
  either netlist, and `SIGNEDA/SIGNEDB=0` with `P[17:0]` read is correct because
  the low 18 bits do not depend on the operand signedness. This bounds the
  multiply path at ~7.1 ns port-to-port (~5.9 ns interior) against the core's
  ~20.5 ns critical path, i.e. >=13 ns of margin: the multiply is not, and
  cannot become, the timing limiter.
* The core keeps the multiply **inline** (`(dx_a * dx_b)[:18]`). Wrapping it in
  the `Multiplier` submodule was measured to cost +159 LUT4, +166 mux cells and
  -3.95 MHz for cell-for-cell identical logic at `map_gates` (abc9 remaps the
  same netlist differently when module names change) — a self-inflicted cost
  with a one-line fix, not something M9 has to inherit. `risc18.mul.Multiplier`
  stays as the unit that the two DSP measurement tops and the registered option
  use, and its port map is now pinned by tests.
* A registered-DSP core variant (one bubble per MUL) was built in an uncommitted
  scratch tree (no committed variant source, same caveat as the M2 note) and
  verified golden-clean on 20 seeded programs plus MUL edge cases. Its measured
  fmax (48.46 / 47.57 MHz) and the baseline's (44.86 / 46.08, before the inline
  revert brought the baseline back to 48.81) differ by 1.5-3.6 MHz — the same
  order as the mapping noise above, so two seeds cannot resolve it either way.
  What the numbers do support: both variants land in the 45-48 MHz band, the
  multiply is not the limiter (see the DSP tops), and the registered form costs
  a bubble per MUL. Decision: combinational, no bubble.
