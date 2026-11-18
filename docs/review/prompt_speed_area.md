# risc18: how to raise fmax and cut LUT4 — design review

> Provenance: this is the brief as it was sent, and its tables are the numbers at
> that head. The FullTop (PLL) rows have since been re-measured with the corrected
> PLL divider set — 1780 LUT4 / 670 FF, 73.67 / 71.80 / 72.26 / 69.42 MHz over
> seeds 1/2/3/7 — see `docs/RESULTS.md`.

I built an 18-bit Harvard RISC in Amaranth for a Lattice ECP5 and it now meets its
target (60 MHz). I have two open goals from the original brief: **maximum clock
frequency** and **minimum LUT consumption**. I want your ranking of the levers,
not code.

Please answer in prose with numbered sections matching my questions. Do not write
RTL or Verilog.

## The machine

- Device: LFE5U-25F-6CABGA256 (Colorlight 5A-75B), speed grade -6, 24288 LUT4.
- Flow: Amaranth 0.5.10 -> Verilog -> yosys 0.52 `synth_ecp5` -> nextpnr-ecp5 0.7-1 ->
  ecppack. All numbers below are **post-place-and-route** from nextpnr's report.
- ISA: 18-bit data and instructions, 16 registers (r0 hardwired 0), 16-bit PC.
  8 ALU ops, 3 shifts, signed compares, LUI, JAL/JALR, conditional branches, LD/ST,
  MUL, HALT. Immediates: 6-bit (ALU/load/store), 8-bit offset (branch), 16-bit (LUI).
- Pipeline: F / D / X / M / W.
  - F: PC register -> 1024x18 instruction EBR (DP16KD, clock-enabled, 1-cycle) -> FD register.
  - D: decode (combinational), register-file read (combinational, 2 ports), forward select.
  - X: ALU, MUL (one MULT18X18D, inline `(a*b)[:18]`), link/LUI, LD/ST effective address,
    branch compare, branch/JALR target adder.
  - M: 1024x18 data EBR (DP16KD, `transparent=False` = read-before-write); store commits on
    the X->M edge; load word available in M.
  - W: register writeback (write-through, so a W write is visible to D in the same cycle).
- Register file: 16 x 18 flip-flops (270 FF), two **combinational** read ports,
  synchronous write, r0 tied to zero, reset clears. Not an EBR: it needs two async reads,
  write-through and reset.
- Forwarding: X > M > W priority. Only a distance-1 **load** costs a bubble (exactly one).
  Distance-1 ALU dependencies are free.
- Control transfer is resolved in **X**: the compare and the target add run on the X-stage
  operand registers, and the redirect is registered, so a taken transfer costs **four**
  killed fetch slots (CPI 1.615 on a 16-instruction measurement program, 1.600 on a
  5-iteration store/load loop, 1.842 on a 5-iteration counted loop).
- FullTop = core + one hand-instantiated EHXPLLL (25 -> 60.000 MHz, CLKI_DIV=5,
  CLKFB_DIV=12, CLKOP_DIV=10, CLKOP fed back) + a boot sequencer holding reset
  while LOCK is low.

## Measured area (CoreTop = core + registered output shell; post-route)

| item | CoreTop | FullTop (+PLL) |
|---|---|---|
| LUT4 tiles | 1813 | 1897 |
| -- of which general logic | 1561 | 1645 |
| -- of which carry-chain LUT4 (63 / 76 CCU2C x 2) | 126 | 152 |
| TRELLIS_FF | 605 | 635 |
| PFUMX (dedicated 2:1 mux cells) | 444 | 476 |
| L6MUX21 (wide-mux stitching) | 186 | 268 |
| DP16KD | 2 | 2 |
| MULT18X18D | 1 | 1 |
| EHXPLLL | 0 | 1 |
| LUT4 utilisation | 7.5 % | 7.8 % |

Inside the 1687 LUT4 cells (logic part only, CoreTop), by truth-table support:

| class | count |
|---|---|
| constant-output (INIT all-0 or all-1), every one feeds a PFUMX | 265 |
| inverter (1 real input) | 26 |
| 2-input | 254 |
| 3-input | 365 |
| 4-input | 777 |

Other measurements on CoreTop:

- **Forwarding ablation**: replacing `d_rs1`/`d_rs2` with the plain regfile reads
  (no forwarding) takes LUT4 1687 -> 1529 (-158, -9.4 %), PFUMX 444 -> 410,
  L6MUX21 186 -> 168.
- **Constant-ablation trap**: zeroing the compare/target operands (tying
  `branch.i_a/i_b` and `target.i_imm/i_rs1` to 0) *raised* LUT4 to 1840 and cut CCU2C
  63 -> 38. ABC remaps the whole cone; small ablations are not interpretable here.
- 1513 of the 1687 LUT4s drive other LUT4s, only 137 drive a flop directly: the logic is
  dominated by wide mux trees, not by arithmetic (the whole core has 63 carry cells).
- The two 16:1 x 18-bit regfile read ports are ~576 mux nodes; total mux cells in the core
  are 630 (444 PFUMX + 186 L6MUX21).

## Measured timing (60 MHz constraint = 16.667 ns period; four nextpnr seeds)

| top | seed 1 | seed 2 | seed 3 | seed 7 |
|---|---|---|---|---|
| CoreTop fmax (MHz) | 65.82 | 67.91 | 70.01 | 73.89 |
| CoreTop logic+routing (ns) | 2.4+12.8=15.19 | 2.9+11.8=14.72 | 6.1+8.2=14.28 | 7.2+6.3=13.53 |
| FullTop fmax (MHz) | 72.36 | 64.16 | 68.00 | 74.45 |
| FullTop logic+routing (ns) | 2.4+11.5=13.82 | 3.7+11.8=15.59 | 2.2+12.5=14.71 | 2.5+10.9=13.43 |

nextpnr delay model: clock-to-Q 0.52 ns, LUT4 0.24 ns, PFUMX 0.26 ns, L6MUX21 0.24 ns.
So 78-84 % of every critical path is interconnect.

### CoreTop seed 1 path, cell by cell (15.19 ns, 6 LUT levels)

```
0.52 clk-to-q   i_instr FF (F stage)
1.82 net        i_instr[17] -> decoder
0.24 LUT4       decoder.py:32 "rd"
2.42 net        rd[6] -> write-enable/hit cone
0.24 LUT4
1.11 net        -> operand select
0.24 LUT4
2.69 net        -> d_rs2 three-way forward select
0.40 PFUMX/LUT4
0.76 net  0.24 LUT4  0.61 net  0.24 LUT4
2.20 net        -> L6MUX21/PFUMX
0.26 LUT4
1.22 net        -> dx_b (D->X operand register) FF
```

Endpoint: the **D -> X operand register** (`dx_b`). Source: the **F-stage
instruction register**. That is the whole story on three of four seeds:
instruction -> decode -> hazard/write-enable -> three-way operand select -> operand
register. CoreTop seed 3 instead runs `dx_a -> MULT18X18D -> P5 -> o_target adder ->
x_value -> d_rs2 -> dx_b` (6.1 ns logic) and seed 7 runs `dmem DOA -> LUT ->
o_target carry chain -> d_rs1 -> dx_a`.

### FullTop seed 1 (13.82 ns) and seed 1 differs in shape

Same source and endpoint, but the middle is one **5.47 ns single net** from
`regfile.i_we` to the select cone (plus 1.10 and 1.65 ns hops), i.e. one bad placement
hop rather than four medium ones.

FullTop seed 2 is the only path that is not this loop: `i_a -> dx_a -> i_cond compare
carry chain -> fetch_alive -> freeze -> fetch.i_hold -> PC register clock enable`
(3.7 + 11.8 = 15.59 ns) — the squash/hold cone ending on a clock enable, not a D input.

## What I have already tried and measured

- **M8**: wrapping the multiply in an explicit `Multiplier` submodule cost **+159 LUT4 and
  -3.95 MHz** for cell-for-cell identical logic at `map_gates` (ABC9 remap sensitivity to
  the module boundary). The core keeps the inline `(a*b)[:18]`.
- **M8**: a registered DSP output (`REG_OUTPUT_CLK=CLK0`) needs one bubble per MUL and gave
  no resolvable core gain; rejected. Isolated DSP tops measure 140 / 185 MHz, so the
  multiply has >=13 ns of margin against a ~20 ns core path.
- **M9**: moving the branch compare and the target adder from D into X, with the redirect
  registered, took the core from 48.8 MHz to 65.8-73.9 MHz (and cost the two extra
  squash slots). Before that, registering the decision alone (compare still in D) only
  reached 53-58 MHz.
- Deleting a dead, already-trimmed 16-bit register moved individual seeds by up to
  10 MHz, so every number above is a four-seed sweep, never a single run.

## Constraints on any proposal

- The ISA is frozen in `docs/ISA.md`/`isa.py`, the Python golden model is the acceptance
  oracle, and every RTL unit is tested against it in `amaranth.sim`. An ISA or
  pipeline-contract change is allowed but must be justified and re-tested; a
  microarchitectural change that preserves the ISA is much cheaper for me.
- Initially one EBR per memory (1024 x 18), 56 DP16KD available, 28 DSP available.
- Single clock domain. No interrupts/exceptions yet. No board bring-up yet: all numbers
  are static timing, nothing has run on silicon.
- I care about both goals, but fmax matters more; the brief's target was "maximum
  frequency, minimum LUT", and the current 60 MHz was a floor, not a ceiling.

## Questions

1. **Rank the fmax levers** for this specific path shape (routing-dominated, 6 LUT levels,
   endpoint = the D->X operand register, source = the F instruction register): a further
   pipeline stage between D and X; registering the forward-select result; splitting the
   3-way select into two registered halves; physically constraining/floorplanning the
   decoder+regfile+operand cluster; restructuring decode to reduce the `rd`/write-enable
   cone; anything else. For each, say roughly what it buys and what it costs (LUT, CPI,
   test churn), and which one you would do first on an ECP5-25F with this netlist.
2. **Is 5.47 ns of a single net fixable by placement work alone** (nextpnr floorplanning,
   region constraints, `--placer-heap`/timing-driven options), or does that hop indicate a
   structural problem I should fix in RTL? Note that seed-to-seed the same logical path
   spreads over four nets of 1.2-2.7 ns instead.
3. **Area**: where would you cut LUT4 first? Candidates I see: the 16x18 two-read-port
   regfile (bank it? halve it to 8 registers? move it to an EBR with a registered read and
   a one-cycle hazard? use two LUT-RAM banks with a pre-decoded select?), the 265
   constant-output LUT4s that feed PFUMX leaves (zero-extension, immediate sign fill,
   in-range masks), the 630 mux cells, the four-slot squash window. Quantify what you
   expect each to save and what breaks.
4. **Does the four-slot squash deserve a redesign** now that the clock is met — e.g. a
   one-bit branch history or a backward-taken prediction to keep the four-slot cost off
   loops — or is 1.6-1.8 CPI the right trade at this size? What would you do about the
   taken-branch cost specifically if fmax stays at 65-75 MHz?
5. **Check my arithmetic and my premises**: 1687 LUT4 = 1561 general + 126 carry
   (63 CCU2C x 2); ~576 mux nodes for 2 x 18 bits x 16:1; "78-84 % of every critical path
   is interconnect"; the claim that ABC remap noise makes small ablations meaningless.
   Tell me which of these is wrong or mis-stated, and why.
6. **What am I not seeing?** Name the failure mode or the lever I have not listed — in
   particular anything that would let this core go materially faster (100 MHz class) on
   this device without turning it into a different machine, and anything in the current
   structure that will bite when the memories grow beyond one EBR each.
