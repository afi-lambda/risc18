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

* `CoreTop` at M7: 2265 LUT4 (+169), 584 FF, 2 DP16KD, 1 DSP, 46.55 MHz on seed
  1 (M6: 48.56). The extra 18-bit X-vs-M-vs-regfile select on both operand
  paths is the whole LUT growth and the 2 MHz loss; the critical path is the
  same D-stage chain with one more select level. Quote the seed sweep before
  drawing conclusions from the delta.
* CPI: a 20-instruction distance-1 ALU chain went 1.85 -> 1.00; a load-use chain
  stays at exactly one bubble per load; the CoreTop image 1.62 -> 1.31 (the
  remainder is the two-edge squash on taken branches); a memory loop
  1.51 -> 1.37. Independent loads remain 1.00.
* A JAL/JALR link value is never consumed at distance 1 by construction: the
  jump resolves in D and empties the two younger fetch slots, so the
  interesting case is the word after the target, which costs nothing.
