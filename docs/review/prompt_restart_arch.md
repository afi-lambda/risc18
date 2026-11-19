# Brief: restart architecture for an 18-bit CPU on an ECP5, given measured memory and routing costs

You are reviewing a re-architecture decision for a soft core. Answer in prose with
numbered sections matching the questions. **No RTL, no Verilog, no Python** — I want
reasoning and a ranked decision, not code.

## The part and the flow (all numbers below come from this exact configuration)

- Lattice ECP5 `LFE5U-25F-6CABGA256` (speed grade -6) on a Colorlight 5A-75B.
  24,288 LUT4, 24,288 FF, 56 DP16KD (1024x18 each), 28 MULT18X18D, 2 EHXPLLL,
  25 MHz oscillator.
- Amaranth 0.5.10 -> Verilog -> yosys 0.52 `synth_ecp5` (ABC9) -> nextpnr-ecp5 0.7
  (`--placer heap --router router2`) -> ecppack.
- **The flow writes exactly one constraint line: `FREQUENCY NET "clk"`. No LOC, no
  region, no pin constraints, no timing-driven placement weight.** This matters
  below.
- All figures are post-route, reported by nextpnr's critical path report.

## What exists today

18-bit Harvard RISC, 317 simulation tests, golden model, assembler.

- 5-stage core: 73.48 MHz (13.6 ns = 6.8 logic + 6.8 routing), CPI 1.615, 45.5 MIPS,
  1183 LUT4 / 407 FF / 2 DP16KD / 1 MULT.
  Critical path: `imem DOA 5.6 -> decode 2 LUTs 2.0 -> regfile address + stall mux
  2.0 -> TRELLIS_DPR16X4 1.9 -> FF`.
- 6-stage variant (`Core6`, one extra register `fq` between the instruction EBR output
  and decode; taken branch kills 5 slots; MUL latency 2; 1215 LUT4 / 460 FF):
  seeds 1/2/3/7 give `Core6Top` 80.84 / 80.87 / 76.20 / 76.38 MHz and `Full6Top`
  (PLL + boot shell) 78.35 / 74.83 / 78.90 / 74.82 MHz at a 120 MHz derived constraint.
  CPI 1.846 / 1.714 / 2.053. So the extra stage bought ~7 % of clock and lost throughput:
  78.9 MHz / 1.85 = 42.6 MIPS against 45.5 MIPS today.
- In all eight 6-stage builds the critical path is 2.9-3.4 ns of logic against 9.3-10.5 ns
  of routing over 8-17 hops. Every path starts in the X-stage operand cone
  (`core.i_b$88_*` -> `core.dx_b`) and ends at `core.i_a` or at the data EBR address
  (`core.dmem.mem.0.0.ADA0/1`); the branch-target adder `core.o_target` is on all four
  stage-4 paths. Single nets dominate: `dx_b[0]` is 3.0 ns of a 9.8 ns routing budget,
  `dx_b[5]` is 4.1 ns of 10.1 ns.
- Reset distribution is a second constraint in the core-only top (8.18-9.94 ns, because
  `i_rst` also gates the store enable) and absent in the PLL top (reset from a flop in
  the core clock domain).
- 1,220 LUT4 of 24,288 used (5 %). Nothing is congested. The clock is set by distance.

## Probe measurements (bare tops: registered I/O, one consumer per path, unconstrained

placement, `--freq` set to the number being probed. These are lower bounds on routing
because a probe has no fan-out; treat them as floors, not as what a populated core
will achieve.)

| chain | period | fmax |
|---|---:|---:|
| DP16KD read (REGMODE=NOREG) -> fabric FF | 6.7 ns (5.6 block + 1.1) | 149.97 MHz |
| EBR read -> 18-bit add -> EBR write data, **one cycle** | 9.0 ns (5.6 + 2.0 + setup/routing) | 111.53 MHz |
| same + 16:1 operand mux, one cycle | 10.4 ns | 96.35 MHz |
| same + 18x18 multiply, one cycle | 13.2 ns | 75.76 MHz |
| **two cycles**: read -> register; register + operand -> add -> EBR write | 6.7-6.9 ns per stage, seeds 1/2/3/7 | 150.08 / 149.93 / 145.18 / **145.10** MHz |
| same two-cycle split + 16:1 register-read mux in the ALU cycle | 9.4 ns (2.3 logic + 7.1 routing) | 106.51 MHz |
| X stage: add + 18x18 multiply in parallel + result select + compare + target adder | 9.85 ns | 101.62 MHz |
| same, multiply result registered before the select | 7.7 ns | 129.85 MHz |
| same, no multiply | 5.5 ns | 181.79 MHz |

The 16:1 mux path is `r_sel` FF -> LUT (2.5 ns net) -> LUT (2.0 ns) -> LUT (0.9 ns) ->
LUT (0.9 ns): a four-level LUT tree over four tiles. The two-cycle split's logic term is
exactly the 5.6 ns DP16KD clock-to-out in every seed (the reported path ends at the split
register); the write half is shorter in every seed.

## What is already ruled out, with the measurement that ruled it out

- Single-cycle `EBR -> ALU -> EBR` at 120 MHz: 9.0 ns for an add alone, of which 5.6 ns
  is the block's clock-to-out. The block's internal output register (`REGMODE=OUTREG`)
  is not inferred by this flow; forcing it would mean instantiating DP16KD directly.
- Fetch as the limiter: the 6-stage cut removed the 13.6 ns `imem DOA -> regfile` path,
  and no 6-stage path now starts at the instruction EBR.
- PLL: the shipped divider set was illegal (600 MHz from a 6 GHz VCO) and never produced
  the requested rate on hardware; fixed to the vendor set, and 120.000 MHz is now
  derived exactly.
- Constraint target: the same netlist built at `--freq 25` and `--freq 120` gives
  identical results, so the 74.82-78.90 MHz numbers are not constraint artefacts.
- MUL in the ALU select path: 101.62 MHz vs 129.85 MHz registered. MUL is latency 2.

## The proposed restart (what I want reviewed)

A 6-stage core written to a memory-latency-2 contract:

```
F      IQ     D      X      M      W
imem   imem   decode ALU    dmem   dmem
addr   data   rf addr branch addr   data
              (+1)   resolve read   write
```

- Both memories EBR with declared latency 2 (imem F/IQ, dmem M/W), so the 5.6 ns
  clock-to-out always sits alone in a cycle.
- Register file stays distributed RAM (`TRELLIS_DPR16X4`, 1.9 ns address-to-out).
  It is the only operand source fast enough to sit in a 6.7 ns cycle, and the 16:1 flop
  mux measured 7.1 ns of routing on its own.
- Branch condition computed in D, resolved in X; static backward-taken prediction.
- MUL registered, latency 2, one bubble on dependent use.
- Placement constraints from the first commit: a region holding regfile, ALU, branch
  adder and dmem, plus LOC for the two EBRs and the clock/reset pads.
- 18 bits kept (native 18x18 multiply, 2 DP16KD per memory, the ISA/assembler/golden
  model/317 tests carry over unchanged).
- Committed target 120 MHz, CPI <= 1.5, i.e. ~100 MIPS against 45.5 today; 150 MHz is
  a stretch that the probe says is block-limited.

## Questions

1. Check my arithmetic and the premise. Is "two-cycle memory with the 5.6 ns
   clock-to-out isolated in its own cycle" the right foundation for this part, or is
   there a better way to spend the 8.33 ns of a 120 MHz cycle that I have not
   considered (e.g. keeping one cycle and accepting a lower clock, or restructuring so
   the memory is not on the ALU path at all)?
2. Given the measured X cone (target adder + compare + store address in one cycle,
   9.85 ns with MUL), is "compare in D, resolve in X, MUL registered" the right split?
   Which of those three belongs in a different stage, and what does moving it cost in
   bubbles?
3. Routing is 78 % of the failed period and the flow has no placement constraints.
   What would you constrain first on this part, in what order, and what is the failure
   mode of region constraints in nextpnr-ecp5 that I have not listed (legality,
   placer rejection, congestion, seed sensitivity, anything else)?
4. Does memory latency 2 change the load/store schedule in a way I have not accounted
   for — specifically store data forwarding, load-use distance, and whether the W stage
   can write back and be bypassed in the same cycle?
5. At CPI 1.5 with a taken-branch penalty of 2 cycles, what branch density makes the
   target unreachable? Is static backward-taken the right policy here, or is a 1-cycle
   penalty worth a stage?
6. What is the failure mode you have not seen me list — the thing that most often makes
   a latency-2-memory 6-stage core end up *slower in MIPS* than a 5-stage core at 73.5
   MHz, even when the clock goes up?
7. Rank what you would do first, second, third, and name what you would kill.

Constraints on the answer: prose, no code. If you think a number in this brief is wrong,
say which and why before answering the numbered questions.
