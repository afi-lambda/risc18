# Should the pipeline be cut at the EBR boundary? A 6-stage 18-bit core at 120-150 MHz

I have a working 18-bit RISC on an ECP5 that closes at 73.5 MHz and I now think that
clock is set by a bad stage boundary, not by the device. I want your judgement on
whether to re-cut the pipeline, and on the arithmetic I am using to justify it.
Prose, numbered sections matching my questions. **No RTL, no Verilog, no code.**

## Platform (identical for every number below)

Lattice `LFE5U-25F-6CABGA256` (Colorlight 5A-75B): 24 288 LUT4, 24 288 FF, 56 `DP16KD`
(18 Kbit each -> 1024x18 per block), 28 `MULT18X18D`, 2 `EHXPLLL`, one 25 MHz
oscillator, speed grade -6. Flow: Amaranth 0.5.10 -> yosys 0.52 `synth_ecp5` (ABC9)
-> `nextpnr-ecp5` 0.7-1 (`--placer heap --router router2`) -> `ecppack`. All fmax
numbers are post-place-and-route, seed 1 unless a sweep is stated.

## The machine as built

- 18-bit Harvard RISC, 16 registers (r0 hardwired), 16-bit word address, 1024 x 18
  instruction EBR and 1024 x 18 data EBR, one MULT18X18D inline.
- 5 stages: F (PC reg -> instruction EBR clock-enabled read -> FD reg), D (decode,
  register-file read, bypass), X (ALU/multiply/branch compare + target add, and the
  data EBR is addressed combinationally here), M (data EBR word returns), W (writeback).
- Control transfer resolves in X and the redirect is registered (applied one cycle
  later). That is deliberate: applying it on `take_x`'s own edge was the measured
  ~48 MHz ceiling. Price: 4 killed fetch slots per taken transfer.
- Register file: 16 x 18. Two builds exist. (a) Flops, two combinational reads,
  write-through: 1813 LUT4, 605 FF, fmax 65.82 / 67.91 / 70.01 / 73.89 over seeds
  1/2/3/7. (b) Distributed RAM (2 banks x 5 `TRELLIS_DPR16X4`, registered read,
  address issued in F): 1183 LUT4, 407 FF, 73.48 MHz seed 1, sweep 73.48 / 75.27 /
  68.24 / 73.44.
- CPI in `amaranth.sim`: 1.615 (16-word program), 1.600 (store/load loop),
  1.842 (counted loop). Bubbles: 4 slots per taken transfer, 1 per load-use.
- No timing number has been measured on the board. (The HIL harness added after
  this brief does run the same core on the board's PLL and reports the simulated
  retire/cycle counts: `docs/metrics/hil5.md`.)

Critical paths, both configurations (post-route segments):

- RAM regfile: 13.6 ns = 6.8 logic + 6.8 routing.
  `imem DOA clock-to-out 5.6 -> decode cone 2 LUT levels 2.0 -> regfile read
  address + stall mux 2.0 -> TRELLIS_DPR16X4 addr-to-out 1.9 -> read-data FF`.
  No arithmetic on it.
- Flop regfile: 14.1 ns = 5.8 + 8.3.
  `dx_a FF 0.5 -> MULT18X18D 3.9 -> branch target adder CCU2C 1.9 -> x_value
  select tree 4 levels 2.0 -> d_rs1 bypass 1.2 -> dx_a`.
- Second-worst path in both: reset distribution, 7.3-8.3 ns (fanout, not logic).
- Measured ablations of the RAM build (control 73.48 MHz): forwarding network
  removed 72.07, branch compare tied off 72.12, target adder removed 67.94,
  multiply forced constant 66.76. Removing datapath cones does not buy fmax in that
  configuration; the limiter is the fetch-side chain.

## Probe builds I ran to price pieces in isolation (bare tops, no decode/bypass/reset/fanout)

| probe | chain | period | fmax |
|---|---|---:|---:|
| read | EBR DOA -> fabric FF | 6.7 ns (5.6 + 1.1) | 149.97 MHz |
| read, 200 MHz constraint | same | 6.7 ns | 149.97 MHz (no change) |
| read loop | DOA -> next address, one dependent read per cycle | 6.6 ns | 150.78 MHz |
| read + decode | EBR DOA -> 4-to-12 decode + field pass-through -> FF | 6.7 ns | 150.08 MHz |
| add+write | EBR DOA -> 18-bit add -> EBR write data input | 9.0 ns (7.0 + 2.0) | 111.53 MHz |
| mux+add+write | 16:1 reg mux select -> add -> EBR write data | 10.4 ns (2.6 + 7.8) | 96.35 MHz |
| mul+write | EBR DOA -> 18x18 multiply -> EBR write data | 13.2 ns (9.5 + 3.7) | 75.76 MHz |

The decoder as a unit (19 fields, 54 bits of registered output) measures 169 LUT4
and 176.58 MHz. The EBR read data is registered in fabric (REGMODE = NOREG); the
block's own output register is not inferred by this flow.

The conclusion I drew from the table: the 5-stage core has two block reads in one
period (instruction EBR, then distributed-RAM register file), and that is what sets
13.5 ns / 73.5 MHz. A stage that contains only one EBR read plus a decode runs at
150 MHz.

## The proposal I want reviewed

Re-cut to 6 stages so that no stage contains two block reads:

  F1  PC -> instruction EBR read (registered out)
  F2  decode + register-file read address issue
  D   register-file data + bypass + hazard detect
  X   ALU / multiply / branch compare + target add, data EBR addressed
  M   data EBR word returns
  W   writeback

Target 120-150 MHz, accepting CPI ~2.0 (five killed slots per taken transfer
instead of four) and one extra cycle of load-use distance.

My arithmetic: 150 / 2.0 = 75 MIPS against 45.5 today (+65 %); break-even CPI at
150 MHz is 3.30; at a realistic 120 MHz, 60 MIPS (+32 %) with a break-even CPI of
2.64. The PLL can produce 150.000 MHz exactly (25 x 30 / 5 with `CLKOP_DIV = 4`,
VCO 600 MHz), just as it currently produces 60.000 MHz (25 x 12 / 5,
`CLKOP_DIV = 10`; CLKOP is the feedback signal, so `CLKOP_DIV` sets the VCO).

## Questions

1. **Is the stage boundary the right lever, and is this the right cut?** Compare
   (a) the 6-stage split above, (b) keeping 5 stages but making the register file
   flop-based and cutting the select cone, (c) staying at 73.5 MHz and attacking
   CPI, (d) a 7-stage schedule that also splits the X stage. Say which you would
   spend a week on and why, given that the whole design is 5 % of the device.

2. **Check my arithmetic and my squash derivation, do not accept them.** I claim one
   extra killed slot per taken transfer when going from 5 to 6 stages. Derive the
   number yourself for the schedule above (X-resolve, registered redirect, EBR fetch
   with its own pipeline register) — is it 5, 6, or does it depend on whether the
   redirect target can be presented to the EBR one edge earlier? Also check the MIPS
   and break-even figures, and what CPI the 6-stage machine would need to beat the
   current one at 120 and at 150 MHz.

3. **Per-stage worst case.** Using my measured pieces (5.6 ns EBR clock-to-out,
   1.9 ns distributed-RAM read, 1.9 ns carry add, 3.9 ns multiply, ~2.0 ns for a
   16:1 mux tree, ~1.2 ns for a bypass select, ~2.0 ns for the decode cone), give
   the worst-case period of each stage in the schedule above and identify the stage
   that caps the clock. Where would you move work to fix it? Is 150 MHz reachable at
   all with a multiply in the machine, or does the DSP pin you near 100?

4. **What breaks at 120-150 MHz that does not at 73.5?** I know of the 7.3-8.3 ns
   reset distribution as the next wall. Consider also: global clock buffer and PLL
   output quality, the EBR at 84 % of a 6.7 ns period, routing/placement with a heap
   placer on a 25K part, and whatever else you think bites first. Rank them by what
   you would fix first.

5. **The failure mode I have not listed.** Where does this plan fall off a cliff?
   Specifically: is there a reason a deeper pipeline is a bad idea for a machine with
   1024-word memories and a taken-branch-heavy workload, that is not captured by
   "CPI goes up a bit"?

6. **Honesty check.** Is chasing 150 MHz on an EBR-bound core a real performance
   improvement or a benchmark-shaped one? State what workload it helps, what it
   hurts, and the defensible way to report the result so it is not just a bigger MHz
   number.

7. **One measurement before committing.** If I can afford one week of instrumented
   work first, what should it be? Note that the previous review of this design ranked
   hardware bring-up first and argued that a 1.4x branch-CPI ceiling was really
   1.17-1.27x once exit mispredicts are counted.
