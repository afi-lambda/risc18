# Recipe for a fast, performant 18-bit CPU on an ECP5 — design review

I have built a dozen CPUs, most of them soft cores for the same board. I want the
distilled recipe — the ordered set of decisions — for a CPU in the 18-bit class that
is both fast (fmax) and performant (instructions retired per second), plus a ranking
of what to do next on the one that is currently furthest along. Answer in prose with
numbered sections matching my questions. **Do not write RTL, Verilog or code.** Where
you disagree with a premise, say so and give the measurement that settles it.

## Fixed platform (identical for every design below)

- Lattice ECP5 `LFE5U-25F-6CABGA256` on a Colorlight 5A-75B: 24 288 LUT4, 24 288 FF,
  56 `DP16KD` (18 Kbit each, so 1024x18 per block), 28 `MULT18X18D`, 2 `EHXPLLL`,
  one 25 MHz oscillator, speed grade -6.
- Flow: Amaranth HDL 0.5.10 -> Verilog -> yosys 0.52 `synth_ecp5` (ABC9) ->
  `nextpnr-ecp5` 0.7-1 (`--placer heap --router router2`) -> `ecppack`.
- All fmax numbers are **post-place-and-route**, from nextpnr's own report. Every
  design below was built with a different constraint and often a different
  measurement program, so the column is a fmax comparison, not a MIPS comparison.

## The evidence: what each attempt actually achieved

| design | ISA / width | microarchitecture | post-route fmax | other measured facts |
|---|---|---|---|---|
| `risc18` (current, M11) | 18-bit Harvard RISC, 16 regs, 16-bit address | F/D/X/M/W, control transfer resolved in X, redirect registered | 73.48 MHz core (60 MHz constraint, seed 1); 67.59 MHz full top with PLL | 1183 LUT4, 407 FF, 2 DP16KD, 1 MULT18X18D (distributed-RAM regfile). CPI 1.615 / 1.600 / 1.842 on three programs (sim). 4 killed fetch slots per taken transfer |
| `risc18` M9 (same core, flop regfile) | as above | as above | 65.82 / 67.91 / 70.01 / 73.89 MHz over seeds 1/2/3/7 | 1813 LUT4, 605 FF |
| `j1-cpu` | 16-bit stack machine (J1 style), separate data + return stacks | 2-stage, most ops 1 cycle | 52.36 MHz (50 MHz constraint) | 1145 FF, 8 DP16KD, integrated JTAG logic analyser |
| `pdp-8-cpu` | 12-bit PDP-8/I, multicycle | microcoded multicycle, flash-backed pack boot | 56.05 MHz (25 MHz constraint, probe build) | 48/56 DP16KD (85 % of EBR), 2367 FF, 3 MULT18X18D; boots OS/8 on hardware |
| `pdp-11-cpu` | 16-bit PDP-11 subset, multicycle | multicycle, one memory request at a time | 51.37 MHz (25 MHz constraint) | 32 DP16KD, 366 FF |
| `ecp5-fastcpu` | RV32I subset, 5-stage | in-order 5-stage, EBR imem/dmem | 39.15 MHz | no caches, no CSRs, no mul/div |
| `ecp5-vexriscv` (LiteX, SpinalHDL) | RV32IM, 5-stage + caches | VexRiscv_Full + Wishbone + SDRAM | ~70 MHz silicon-verified, 73.24 MHz static | 26 DP16KD, 2808 FF; 4 MiB SDRAM @ 70 MT/s, memtest OK. Its author-facing claim is "tops out at 82 MHz / ~70 MIPS" |
| `rosetta-cpu` | 32-bit stack bytecode CPU (Smalltalk) | stack machine, bytecode dispatch | 6.86 MHz | not timing-closed |

Things already tried and **measured**, so they should not be re-proposed as new:

- **Registering the control-transfer redirect** (resolving branches in X and applying
  the redirect one cycle later) is what took risc18 from a ~48 MHz ceiling to >= 60 MHz,
  at the cost of 4 killed fetch slots per taken transfer.
- **Regfile as distributed RAM (`TRELLIS_DPR16X4`) instead of 270 flops**: 1813 -> 1183
  LUT4 (-35 %) and 605 -> 407 FF, 73.48 MHz.
- **Collapsing the twin branch-target adders** (~1 % LUT4) does not move fmax: the adder
  is on two of four critical paths but never the tail.
- **265 "constant" LUT4s** in the netlist are a decomposition artefact, not reclaimable.
- **The PLL** is exact: `CLKI_DIV=5, CLKFB_DIV=12, CLKOP_DIV=10` with CLKOP fed
  back -> 60.000 MHz, 0 ppm, VCO 600 MHz (the `ecppll -i 25 -o 60` set).
- Current risc18 critical path (M11): 6.8 ns logic + 6.8 ns routing = 13.6 ns, i.e.
  consumer's source indices decoded from the instruction -> compare against the
  producer's registered rd/write-enable -> bypass select -> D->X operand register.
- The HIL harness (`HilTop`: the same core plus a UART) has been loaded on the
  board and reports the simulated retire/cycle counts; every *timing* number
  here is still static timing, and CPI is `amaranth.sim`
  (`docs/metrics/hil5.md`).

## Questions

1. **The recipe.** Distil the ordered decisions that produce a fast *and* performant
   CPU in this class on this part, in the order they must be made: ISA/width choices,
   pipeline shape and where control resolves, memory organisation, register file,
   arithmetic (DSP vs fabric), and the physical/tool step. For each, state the decision,
   the evidence from the table that supports it, and the failure mode if it is made the
   other way. Say which items are invariant across all these designs and which are
   specific to a 25K-LUT ECP5.

2. **18 bits specifically.** One DP16KD is 1024x18 and one MULT18X18D is 18x18, so an
   18-bit word is an exact fit for both. Where does the 18-bit width buy real speed or
   area over 32-bit, and where does it cost (byte access, immediate encoding, address
   reach, compiler/toolchain support, benchmark comparability)? Is 18 bits, in 2026, a
   defensible performance target or a self-imposed handicap? Be blunt.

3. **Rank the next levers on risc18** by expected MIPS gained per week of work, with the
   mechanism and the risk for each. My candidate list, not necessarily complete:
   (a) recover branch CPI with a 1-bit "backward taken" predictor, or by resolving in D;
   (b) a distance-1 bypass path straight out of `x_value` instead of an operand-register
   split; (c) a shorter fetch path (EBR read latency is what forces the 4-slot squash);
   (d) dual-issue or a 2-wide front end; (e) a real memory hierarchy (1024-word EBR, no
   cache today); (f) 18x18 DSP-based MAC or address generation; (g) push the clock past
   60 MHz by cutting the bypass cone; (h) drop to a 3-stage pipe and accept a slower
   clock for a much better CPI; (i) HIL bring-up first, before any of the above.

4. **Check my arithmetic, do not accept it.** At 73.48 MHz and CPI 1.615 the core retires
   about 45.5 MIPS. The 5-iteration store/load loop takes 56 cycles for 35 instructions,
   of which 16 cycles are 4 taken BNEs x 4 killed slots: removing all of them gives 40
   cycles, CPI 1.143, a 1.4x throughput gain — but a predictor is not free and a
   D-stage resolve re-opens the path that the X-stage policy closed. Is the 1.4x ceiling
   real, and what does each implementation actually collect? Also check: is a 4-slot
   squash on a 5-stage pipe with EBR fetch the *minimum* for this memory organisation,
   or is one of the four slots avoidable without touching the clock?

5. **The failure mode I have not listed.** Where does this class of design fall off a
   cliff on this part — the thing that will cap it regardless of how well the pipeline is
   tuned? Consider EBR port structure (one read port, one write port, no true dual port),
   the 85 %-full PDP-8 EBR budget as an address-space warning, the absence of byte lanes,
   the lack of a cache, and whatever else you consider the binding constraint.

6. **Honesty check.** Which of the levers in Q3 would be gaming a benchmark rather than
   making the machine faster — CPI measured on a 13-instruction program, a 16-word
   measurement image, a 1024-word memory that never misses? Name the defensible variant
   of each in exchange.

7. **What to measure first.** If I can only afford one week of instrumented work before
   committing to a plan, what is the single measurement that most changes the ranking in
   Q3, and what result would make you abandon the current microarchitecture?
