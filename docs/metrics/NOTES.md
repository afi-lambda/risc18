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
* A JAL/JALR link value (pre-M9 note, restated for M9): a taken transfer empties
  four slots, so the word after the target costs nothing; since M9 the link is
  `x_value` (dx_pc + 1) forwarded like any ALU result rather than coming off the
  D-stage compare path, which is why a distance-1 link consumer still costs no
  bubble.

## M9 timing closure

Two structural steps were needed; the first alone was not enough.

* Baseline (M8, `2caf092`): 2166 LUT4, 584 FF, 2 DP16KD, 1 DSP, 48.81 / 47.68 /
  47.24 / 45.96 MHz (seeds 1/2/3/7), critical path 9.2-10.8 ns logic plus
  11.3-17.3 ns routing through the D-stage loop
  `FD reg -> decoder -> operand select -> 18-bit compare -> redirect -> PC mux ->
  FD capture mux -> FD reg`.
* Step 1 — register the control-transfer decision (compare still in D, redirect
  applied one edge later from a `ct_*` register): 2228 LUT4, 601 FF, 53.29 /
  55.34 / 58.44 / 57.71 MHz (uncommitted intermediate; measurement only, not
  reproducible from this head). The long loop is gone, but a new 17.7 ns path
  appears through `dx_a -> MULT18X18D -> x_value select -> operand mux ->
  compare -> take -> control register`; still short of 60 MHz.
* Step 2 — move the compare and the target adder into X (operands are already
  forwarded there), keeping the registered redirect (committed): 1813 LUT4, 605 FF,
  2 DP16KD, 1 DSP, **65.82 / 67.91 / 70.01 / 73.89 MHz** (seeds 1/2/3/7) —
  4/4 met at the 60 MHz constraint, worst seed 15.2 ns against a 16.67 ns
  budget. Every path is register-to-register with no fetch or I/O path, and in
  all four seeds the worst path ends by loading a D -> X operand register
  through the forward/hazard select: seed 1 `i_instr -> rd/decoder -> xm_we
  cone -> d_rs2 select -> dx_b` (2.4 ns logic + 12.8 ns routing), seed 2
  `i_instr -> alu_op -> regfile.i_we cone -> d_rs2 -> dx_b` (2.9 + 11.8),
  seed 3 `dx_a -> MULT18X18D A -> P5 -> o_target adder -> x_value ->
  d_rs2 -> dx_b` (6.1 + 8.2) and seed 7 `dmem DOA -> LUT -> the o_target
  carry chain -> d_rs1 -> dx_a` (7.2 + 6.3). So the limiter is the operand
  select itself; the branch-target adder (`o_target`) sits on two of the four
  paths but is never their tail, and no path ends at `ct_target`.
  (PnR spread: an intermediate head measured 76.40 / 65.81 / 73.36 / 74.26 MHz
  on the same structure. That head differed by the `dx_mem_*` gating change,
  which is netlist-visible (-16 LUT4); the dead `ct_pc` removal in the same
  commit is free because yosys already trimmed it — both heads measure 605 FF.
  Quote the sweep, never one run.)
* Cost: a taken branch or jump kills four fetch slots instead of two, so CPI
  goes 1.31 -> 1.615 on `CORE_TOP_PROGRAM`, 1.37 -> 1.60 on
  `test_memory_accumulation_loop_matches_golden`'s five-iteration store/load
  loop and 1.42 -> 1.84 on `test_counted_loop_matches_golden`'s counted loop
  (all three measured on this head and on M8's tree); not-taken branches, the load-use interlock
  (one bubble), distance-1 ALU chains (zero bubbles) and independent loads
  (CPI 1.0) are unchanged. That is the price of the clock, and it is the knob to
  revisit if a later design cares more about branch-heavy code than fmax.
* Residual risk: the tightest seed is 1 at 65.82 MHz, i.e. 5.8 MHz of margin
  (1.45 ns of a 16.67 ns budget, 8.7%), and the spread across seeds is 8.1 MHz.
  `docs/metrics/m9_core.md` records the seed-1 run (the tool writes `--seed` into
  the JSON only); the four-seed sweep above is the headline number.
  Every seed's limiter is the D -> X operand-register load through the
  forward/hazard select (`dx_b` on seeds 1/2/3, `dx_a` on seed 7).
  Levers, in order: a dedicated distance-1 bypass out of `x_value` (a plain D1/D2
  operand register split would break the zero-bubble distance-1 contract),
  reducing `dx_a`'s fanout into the DSP and the compare, or a full-top (PLL)
  re-measure. Collapsing `BranchTarget`'s twin adder is ~1% of the LUT4 budget
  and would not move fmax.

## M9 full top

`docs/metrics/m9_full.md` is `risc18.tops:FullTop`: 25 MHz board clock -> one
`EHXPLLL` (CLKI_DIV=1, CLKFB_DIV=24, CLKOP_DIV=10 -> 60.000 MHz exactly) -> the
same `CORE_TOP_PROGRAM` core as `CoreTop`, with a LOCK-gated reset sequencer and
a 2^26-clock watchdog (`risc18.tops.BootReset`). What this metric covers and
what it does not:

* Resources (2013 LUT4, 635 FF, 76 CCU2C, 2 DP16KD, 1 MULT18X18D, 1 EHXPLLL)
  are the *whole chip* for a real application: core + PLL + reset/watchdog
  logic. They are the honest number for "what this design costs on this board",
  and they are not comparable to the unit tops in this directory (those are
  measurement shells). Compare them with `m9_core.md`, which is the same core
  without the PLL, the boot FFs and the output shell reset path.
* `constraint: 25.0 MHz` in the `.md` is the *input* constraint
  (`tools/synth.py` writes its `--freq` value into `FREQUENCY NET "clk"`, and
  the board's oscillator is 25 MHz). It is not the core's clock.
* The `fmax` line reads `(constraint 600.24 MHz, missed)`. That constraint is
  nextpnr 0.7-1's own derived value for the PLL output and it is wrong by
  `CLKOP_DIV`: it derives `f_in * CLKFB_DIV / CLKI_DIV` (600.24 MHz) instead of
  60.0 MHz, and it flags its derived 6024.1 MHz VCO as out of the legal band.
  The fmax number is the real post-route result for the generated clock domain
  (73.88 / 76.88 / 73.84 / 65.88 MHz over seeds 1/2/3/7); the miss verdict means
  nothing. Fixing the verdict needs a
  `FREQUENCY NET` line on the CLKOP net, i.e. a `tools/synth.py` change.
* Run this top with `--freq 25`, not `--freq 60`: the value goes into the LPF for
  the 25 MHz pad. With `--freq 60` the pad is over-constrained and nextpnr
  derives 1440.92 MHz for the generated net (routed design unchanged, 71.68 MHz).
* Nothing about the PLL is simulated: `EHXPLLL` has no model in the Yosys ECP5
  library and none in `amaranth.sim`. Lock gating, the hold window and the
  watchdog are simulated through `BootReset` (plain RTL), and the PLL's divider
  parameters are checked structurally on the emitted Verilog and on the packed
  `top.config` PLL tile words. The `.bit` is produced but has never been loaded
  on a board: no silicon timing exists for this top.

