# risc18 M9 — results

Timing closure of the core and the first full-chip top (25 MHz board clock →
PLL → 60 MHz core) on the Colorlight 5A-75B. Every number below is
post-place-and-route output from `tools/synth.py` (`yosys` → `nextpnr-ecp5` →
`ecppack`) or simulation output from `amaranth.sim`. No *frequency* is
silicon-measured: the only board evidence is the HIL status line in
`docs/metrics/hil5.md`, where the same core on the same PLL reports its
retire/cycle counts.

## Device and toolchain

| item | value |
|---|---|
| device | Lattice ECP5 `LFE5U-25F-6CABGA256`, speed grade 6 |
| board | Colorlight 5A-75B, 25 MHz oscillator, 24 288 LUT4 / 24 288 FF / 56 DP16KD / 28 MULT18X18D / 2 EHXPLLL |
| yosys | 0.52 (git sha1 `fee39a3284c90249e1d9684cf6944ffbbcbb8f90`), `synth_ecp5`, ABC9 |
| nextpnr | `nextpnr-ecp5` 0.7-1+b3, `--placer heap --router router2` |
| Amaranth | 0.5.10 (Python 3.11.15) |
| ecppack | Project Trellis 1.4-2+b5 |
| input clock | 25 MHz (`clk`), the only oscillator on the board |

`tools/synth.py` is the single entry point for both tops, so the flow is
identical apart from the clock constraint:

```
.venv/bin/python tools/synth.py --top risc18.tops:CoreTop --name m9_core --freq 60 --seed N
.venv/bin/python tools/synth.py --top risc18.tops:FullTop --name m9_full --freq 25 --seed N
```

## PLL: 25 MHz → 60.000 MHz, exactly

`src/risc18/pll.py` instantiates one `EHXPLLL` by hand (Amaranth has no ECP5 PLL
primitive). `FEEDBK_PATH = "CLKOP"` feeds CLKOP back *inside* the block, so the
feedback divider counts CLKOP, not the VCO:

```
f_PFD   = f_in / CLKI_DIV
f_CLKOP = f_PFD × CLKFB_DIV        ( = f_in × CLKFB_DIV / CLKI_DIV )
f_VCO   = f_CLKOP × CLKOP_DIV
```

`CLKOP_DIV` therefore sets the **VCO** frequency, not the output. The shipped
set is the vendor `ecppll -i 25 -o 60` answer:

| parameter | value | meaning |
|---|---:|---|
| `CLKI_DIV` | 5 | `f_PFD = 25 / 5 = 5.000 MHz` |
| `CLKFB_DIV` | 12 | `f_CLKOP = 5 × 12 = 60.000 MHz` |
| `CLKOP_DIV` | 10 | `f_VCO = 60 × 10 = 600.000 MHz` |
| `CLKOP_CPHASE` | 4 | static phase shift, `< CLKOP_DIV` |

* `f_out = f_in × CLKFB_DIV / CLKI_DIV = 25 × 12 / 5 = 60.000 MHz`, output
  frequency error **0 Hz** — the ratio 12/5 is exact.
* VCO 600 MHz in the middle of 400–800 MHz; PFD 5 MHz above the 3.125 MHz floor
  `ecppll` enforces; CLKOP 60 MHz inside the 10–400 MHz output/feedback band;
  counters and `CLKOP_CPHASE` inside their ranges. `Pll` validates each of those
  and raises instead of emitting a configuration the tools would reject.
* **The set this document used to quote, `(1, 24, 10)`, is wrong** and is now
  rejected at construction: under CLKOP feedback it asks for 600 MHz of CLKOP
  and a 6 GHz VCO. It looked like "60 MHz, 0 ppm" only because the old
  arithmetic divided by `CLKOP_DIV` a second time. nextpnr said so
  (`VCO 6024.1 MHz ... out of legal range`) and a design clocked by it produced
  nothing on the board (`docs/metrics/hil5.md`).
* Wiring is the vendor instance's, not a hand choice: `CLKFB` tied to the CLKOP
  net, `ENCLKOP` low, `PHASEDIR`/`PHASESTEP`/`PHASELOADREG` high,
  `CLKOP_CPHASE = 4`. Measured: the same dividers with `CLKFB` low,
  `CPHASE = 0` and `ENCLKOP` high produced no clock on this board; this wiring
  runs. The `ICP_CURRENT`/`LPF_RESISTOR`/`MFG_*` attributes made no difference
  and are carried for parity with the vendor bitstream.
* PLL reset is never asserted (`PLLRST_ENA="DISABLED"`, `RST` tied low), only
  CLKOP is used, and `INT_LOCK_STICKY="ENABLED"` keeps a transient unlock from
  latching the design off. `LOCK` is the sole handshake into the core's reset.
* Bitstream-level confirmation: the packed `top.bit` (`ecpunpack`) carries
  `CLKI_DIV 0000100` (4), `CLKFB_DIV 0001011` (11), `CLKOP_DIV 0001001` (9),
  `CLKOP_CPHASE 0000100` (4) — the encoder stores each divider minus one, i.e.
  5 / 12 / 10 / 4 — with `MODE EHXPLLL`, `CLKOP_ENABLE ENABLED`,
  `FEEDBK_PATH CLKOP`, `ICP_CURRENT 01100`, `LPF_RESISTOR 0001000`.

## Resources and fmax, seeds {1, 2, 3, 7}

Both tops run the same 16-word `CORE_TOP_PROGRAM` image on the same core; the
only differences are the PLL (`EHXPLLL`), the lock/watchdog sequencer and the
fact that FullTop's reset comes from a register instead of a pin.

The FullTop column was re-measured in stage 4 with the corrected PLL divider
set (`tools/synth.py --top risc18.tops:FullTop --name m9_full[_sN] --freq 25
--seed N`); the CoreTop column is the M9 head's number, which no PLL change can
move.

| | CoreTop | FullTop |
|---|---:|---:|
| LUT4 (nextpnr total) | 1813 | 1780 |
| ″ logic LUTs / carry LUTs | 1687 / 126 | 1628 / 152 |
| TRELLIS_FF | 605 | 670 |
| CCU2C (yosys netlist) | 63 | 76 |
| DP16KD | 2 | 2 |
| MULT18X18D | 1 | 1 |
| EHXPLLL | 0 | 1 |
| LUT4 utilisation | 7.5 % | 7.3 % |
| FF utilisation | 2.5 % | 2.8 % |
| DP16KD utilisation | 3.6 % | 3.6 % |

Per-seed fmax (MHz, routed):

| seed | CoreTop | FullTop |
|---|---:|---:|
| 1 | 65.82 | 73.67 |
| 2 | 67.91 | 71.80 |
| 3 | 70.01 | 72.26 |
| 7 | 73.89 | 69.42 |
| mean | 69.41 | 71.79 |
| min | 65.82 | 69.42 |

Worst-case path per seed (ns, logic + routing):

| seed | CoreTop | FullTop |
|---|---|---|
| 1 | 2.4 + 12.8 = 15.2 | 2.0 + 11.6 = 13.6 |
| 2 | 2.9 + 11.8 = 14.7 | 7.2 + 6.7 = 13.9 |
| 3 | 6.1 + 8.2 = 14.3 | 7.2 + 6.6 = 13.8 |
| 7 | 7.2 + 6.3 = 13.5 | 7.2 + 7.2 = 14.4 |

Both tops clear 60 MHz on all four seeds; the tightest result anywhere is
FullTop seed 7 at 69.42 MHz (15.7 % over the constraint, 2.27 ns of period
slack); CoreTop's worst is 65.82 MHz, 1.47 ns of slack. The spread is the
important caveat, not the ranking: 4.25 MHz across FullTop's seeds and 8.07 MHz
across CoreTop's, and small netlist or naming changes moved individual seeds by
up to 10 MHz during M9. Every number here is a sweep, never a single run. The
FullTop total is 33 LUT4 *below* CoreTop's, which is inside the ±100-cell ABC
remap variance `docs/metrics/NOTES.md` documents; do not read it as structure.

## Critical paths

Every reported path is register-to-register with no I/O path, but the shapes
differ between the tops, and the measured logs are the only acceptable source:

* **CoreTop, all four seeds** end by loading a D -> X operand register through
  the forward/hazard select: seed 1 `i_instr` -> `rd`/decoder -> `xm_we` cone ->
  `d_rs2` -> `dx_b` (2.4 + 12.8 = 15.2 ns); seed 2 `i_instr` -> `alu_op` ->
  `regfile.i_we` cone -> `d_rs2` -> `dx_b` (2.9 + 11.8 = 14.7 ns); seed 3
  `dx_a` -> `MULT18X18D` A -> P5 -> `o_target` adder -> `x_value` -> `d_rs2` ->
  `dx_b` (6.1 + 8.2 = 14.3 ns); seed 7 `dmem DOA` -> LUT -> `o_target` carry
  chain -> `d_rs1` -> `dx_a` (7.2 + 6.3 = 13.5 ns).
* **FullTop** (re-measured in stage 4, so these are not the M9 paths): seed 1
  is the operand-select chain again — `$signal$14` Q -> `dx_we` mux -> the
  `d_rs1`/`d_rs2` forward-select trees -> the `i_b` instruction cone
  (2.0 + 11.6 = 13.6 ns, with one 4.7 ns net `d_rs1 (15,7) -> (4,3)` doing most
  of the damage while the logic is only two LUT levels). Seeds 2, 3 and 7 all
  start at the instruction EBR read port instead: `fetch.imem.mem.0.0.DOA14`
  (5.6 ns of the 7.2 ns logic is the EBR clock-to-out) -> two LUT levels ->
  `$signal$14` (seed 2, 13.9 ns) / `$signal$11` (seed 3, 13.8 ns) /
  `$signal$14` (seed 7, 14.4 ns). No boot-sequencer register appears on any
  measurement path.

So on CoreTop the limiter is the operand select; on FullTop it is the operand
select on one seed and the instruction-EBR read path on the other three. The
branch-target adder (`o_target`) is on CoreTop seeds 3 and 7 but never the tail
of a path, and no path in either top ends at `ct_target`: registering the
control transfer removed that loop for good.

Nothing in either top crosses clock domains: the 25 MHz net only reaches the
PLL's `CLKI` pin, so nextpnr reports exactly one timed clock
(`$glbnet$boot_clk`, the promoted CLKOP net) and one cross-domain report,
`posedge $glbnet$boot_clk -> <async>`, measured at 4.4-5.7 ns to the output
pins across seeds 1/2/3/7 (worst seed 2 at 5.65 ns).

## CPI of the measurement programs

Measured in `amaranth.sim` on `Core` (simulation, not silicon), using the test
suite's convention: cycles from the first retire to the last retire, divided by
the number of retired instructions. The extra cycles are fully accounted for:

| program | retired | cycles | CPI | where the bubbles are |
|---|---:|---:|---:|---|
| `CORE_TOP_PROGRAM` (16 words, 3 squashed) | 13 | 21 | **1.615** | 2 taken transfers × 4 killed fetch slots = 8 |
| 5-iteration store/load loop (`test_core`) | 35 | 56 | **1.600** | 4 taken BNE × 4 killed slots = 16, plus 5 load-use bubbles |
| 5-iteration counted loop (`test_counted_loop_matches_golden`) | 19 | 35 | **1.842** | 4 taken BNE × 4 killed slots = 16 |

Both figures are the price of the M9a step-2 control-transfer policy (four
killed slots per taken branch/jump), which is what bought the ≥ 60 MHz
closure. Not-taken branches, distance-1 ALU dependencies (zero bubbles) and
independent loads (CPI 1.0) are unchanged; see `docs/metrics/NOTES.md`.

`docs/metrics/NOTES.md` quotes the same three pairs (1.615 / 1.600 / 1.842);
they are the counted loop, the store/load loop and `CORE_TOP_PROGRAM`
respectively, and all three reproduce on this head (19/35, 35/56, 13/21).

## What nextpnr does with the PLL-generated clock

nextpnr-ecp5 models `EHXPLLL` with the same loop equations as `ecppll`, and on
the corrected divider set its derivation agrees with the design. With the pad
constrained to its real frequency (LPF `FREQUENCY NET "clk"`, written from
`--freq 25`) the FullTop run prints:

```
Info:     Input frequency of PLL 'pll.pll' is constrained to 25.0 MHz
Info:     Derived frequency constraint of 60.0 MHz for net boot_clk
Info:     Derived frequency constraint of 75.0 MHz for net pll.$1
```

* No "out of legal range" line appears, and `boot_clk` — the promoted CLKOP net
  — is constrained at **60.0 MHz**, so the verdict in `docs/metrics/m9_full.md`
  is a real 60 MHz verdict and it reads `met`.
* The 75 MHz lines are the three disabled CLKOS outputs: `f_VCO / 8 = 600 / 8`.
  They are the second witness that nextpnr's VCO is 600 MHz, not 6 GHz — the
  same three lines used to read 6024.1/8.
* With the old, wrong divider set the same command printed `Derived VCO
  frequency 6024.1 MHz of PLL 'pll.pll' is out of legal range [400MHz,
  800MHz]` and constrained `boot_clk` at 600.2 MHz. Those two numbers are the
  loop equation of that set — 602.41 × 10 for the VCO, and 25.1 × 24 for the
  output — not a nextpnr mis-derivation, which is what the earlier version of
  this section claimed. The defect was in this repo's arithmetic and in nothing
  else: the argument that used to be here divided a rounded output frequency by
  the output divider and got a VCO 0.4 % away from the printed one, and
  concluded nextpnr had mis-scaled. The loop equation explains both numbers
  exactly, with no rounding argument needed.
* `--freq` only sets the LPF for the `clk` pad, so the value must be the board's
  25 MHz. A `--freq 60`/`--freq 120` run over-constrains the pad and nextpnr
  then derives a proportionally inflated constraint for `boot_clk`: measured on
  the 120 MHz top, `--freq 120` reports `Input frequency ... 120.0 MHz`,
  `Derived VCO frequency 2881.8 MHz ... out of legal range` and
  `Derived frequency constraint of 576.0 MHz for net boot_clk`
  (120 × 24/5). `build/synth/m12_ctrl_full6_120_s{1,2,3,7}` are those controls;
  with `--freq 25` the same top prints 120.0 MHz for `boot_clk` and no VCO
  warning (`m12_full6_s{1,2,3,7}`).
* The 60 MHz closure of the core itself is measured where there is no PLL in
  the way: `CoreTop` at `--freq 60`, 4/4 seeds met, worst 65.82 MHz.

## Measured vs estimated

Measured (tool output, reproducible with the commands above):

* LUT4/FF/CCU2C/DP16KD/MULT18X18D/EHXPLLL counts and per-seed fmax and critical
  paths for CoreTop and FullTop, post-place-and-route;
* PLL divider values in the packed bitstream: `ecpunpack` on
  `build/synth/m9_full/top.bit` gives `CLKFB_DIV 0001011` (11),
  `CLKI_DIV 0000100` (4), `CLKOP_CPHASE 0000100` (4), `CLKOP_DIV 0001001` (9),
  i.e. 12 / 5 / 4 / 10 after the encoder's minus-one, and the text config
  nextpnr hands to `ecppack` states the same numbers explicitly;
* CPI for the two measurement programs, in `amaranth.sim`;
* bitstream generation (`ecppack` accepts the `EHXPLLL` configuration, so the
  divider set, feedback path and output-enable encodings are at least legal);
* one board run of the same core on the same PLL through the HIL harness:
  `OK retires=000d cycles=0000001a pc=000f value=00000` (`docs/metrics/hil5.md`).

Not measured, and not claimed:

* **Silicon timing.** No *frequency* has been measured on the board; all fmax
  figures are static timing analysis on a routed netlist at the stated
  constraint. There is no oscilloscope, no on-chip counter-based frequency
  measurement and no temperature/voltage characterisation here. That the core
  runs at 60 MHz is an inference from PnR plus the HIL count match, not a
  period measurement.
* **HIL:** the board supplies exactly 25 MHz, so the 60 MHz core can only run
  on it through the PLL. That has now been done: `HilTop` (the same core, the
  same `Pll`, plus the UART harness) is loaded into SRAM and reports the
  simulated retire/cycle counts exactly. What is *not* proven is margin — a
  design that works at 25 °C on one board says nothing about the 69.42 MHz
  worst-seed model.
* **PLL lock time, jitter, duty cycle.** Derived from the datasheet (600 MHz
  VCO, `/10` output), not measured. `PLL_LOCK_DELAY` is left at the default 200.
* **The watchdog's 1.12 s period** is `2**26 / 60 MHz` arithmetic on the
  simulated counter, not a stopwatch measurement on the board.

## Risks

1. **`--freq` on a PLL top is a trap.** The value goes into the LPF for the
   *pad*, not for the generated clock: `--freq 120` on `Full6Top` constrains the
   25 MHz pad at 120 MHz and makes nextpnr derive 576 MHz for CLKOP. Always run
   these tops at `--freq 25` and read nextpnr's derived constraint, not the
   `--freq` value.
2. **No silicon evidence of timing margin at 60 MHz.** The PnR margins are
   5.82 MHz for `CoreTop` (65.82 vs 60) and 9.42 MHz for `FullTop` (69.42 vs 60)
   at the worst seed, and the board run shows the design *functions*, not that
   it has margin. Silicon at 25 °C is usually a little faster than the model and
   at temperature the reverse; nothing here bounds that.
3. **ABC's global remap inside `synth_ecp5` moves the LUT count by ~±100 on
   small design changes.** FullTop's core RTL never changed across this
   milestone, yet its netlist measured 1602, 1740 and 1729 LUT4 cells
   (nextpnr: 1754, 1892, 2013, and 1897 at this head) as the 30-FF reset sequencer was revised. Cross-top
   LUT comparisons below ~100 cells are not meaningful; quote the netlist, not
   the delta.
4. **The watchdog restart is not a cold reset.** It re-runs the program from a
   power-on core state but does not clear the data EBR (or the PLL), so a
   store-writing program restarts with whatever memory it had when it hung.
   Harmless for `CORE_TOP_PROGRAM` (no `ST`), misleading for anything else.
   The watchdog also restarts a hung program rather than reporting it. A design
   bug shows up as a repeating program, not as a stuck board — good for HIL
   safety, but a measurement window must be shorter than 1.12 s or it will see
   the restart. `o_halt` is sticky until the next reset.
5. **No reset input on `FullTop`.** Reset is `LOCK` + a fixed hold window; there
   is no external reset pin or button on this top, and the PLL is never reset.
