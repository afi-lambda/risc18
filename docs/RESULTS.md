# risc18 M9 — results

Timing closure of the core and the first full-chip top (25 MHz board clock →
PLL → 60 MHz core) on the Colorlight 5A-75B. Every number below is
post-place-and-route output from `tools/synth.py` (`yosys` → `nextpnr-ecp5` →
`ecppack`) or simulation output from `amaranth.sim`. Nothing here is
silicon-measured.

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
primitive). The divider set was chosen so the output is exact:

| parameter | value | meaning |
|---|---:|---|
| `CLKI_DIV` | 1 | `f_PFD = 25.000 MHz` |
| `CLKFB_DIV` | 24 | `f_VCO = 25 × 24 / 1 = 600.000 MHz` |
| `CLKOP_DIV` | 10 | `f_CLKOP = 600 / 10 = 60.000 MHz` |

* `f_out = f_in × CLKFB_DIV / (CLKI_DIV × CLKOP_DIV) = 25 × 24 / 10 = 60.000 MHz`
* **output frequency error: 0 Hz (0 ppm)** — 60.0 MHz is reachable exactly, so
  no rounding is involved anywhere in the chain.
* VCO 600 MHz sits in the middle of the 400–800 MHz band; the PFD at 25 MHz is
  inside 10–400 MHz; all three counters are inside their legal ranges
  (`CLKI_DIV`/`CLKFB_DIV` ≤ 80, `CLKOP_DIV` ≤ 128). `Pll` validates all of that
  and raises instead of emitting a configuration trellis would reject.
* Rejected alternatives: `CLKFB_DIV=12, CLKOP_DIV=5` is also exactly 60 MHz but
  needs a 300 MHz VCO (below the band); a 480 MHz VCO would need
  `CLKFB_DIV=96` (above the 80 counter limit); `CLKI_DIV=2, CLKFB_DIV=48,
  CLKOP_DIV=10` is equally exact and is kept as a tested alternative.
* Feedback is internal (`FEEDBK_PATH="CLKOP"`, `CLKFB` tied low), only CLKOP is
  enabled, the PLL is never reset (`PLLRST_ENA="DISABLED"`, `RST` tied low) and
  `INT_LOCK_STICKY="ENABLED"` keeps a transient unlock from latching the design
  off. `LOCK` is the sole handshake into the core's reset.
* Bitstream-level confirmation: the packed `top.config` PLL tile words decode to
  `CLKFB_DIV=23`, `CLKOP_DIV=9`, `CLKOS*_DIV=7` — the trellis encoder stores each
  divider minus one, i.e. 1 / 24 / 10 as intended — with `MODE EHXPLLL`,
  `CLKOP_ENABLE ENABLED`, `FEEDBK_PATH CLKOP`.

## Resources and fmax, seeds {1, 2, 3, 7}

Both tops run the same 16-word `CORE_TOP_PROGRAM` image on the same core; the
only differences are the PLL (`EHXPLLL`), the lock/watchdog sequencer (30 FFs)
and the fact that FullTop's reset comes from a register instead of a pin.

| | CoreTop | FullTop |
|---|---:|---:|
| LUT4 (nextpnr total) | 1813 | 1897 |
| ″ logic LUTs / carry LUTs | 1687 / 126 | 1745 / 152 |
| TRELLIS_FF | 605 | 635 |
| CCU2C (yosys netlist) | 63 | 76 |
| DP16KD | 2 | 2 |
| MULT18X18D | 1 | 1 |
| EHXPLLL | 0 | 1 |
| LUT4 utilisation | 7.5 % | 7.8 % |
| FF utilisation | 2.5 % | 2.6 % |
| DP16KD utilisation | 3.6 % | 3.6 % |

Per-seed fmax (MHz, routed):

| seed | CoreTop | FullTop |
|---|---:|---:|
| 1 | 65.82 | 72.36 |
| 2 | 67.91 | 64.16 |
| 3 | 70.01 | 68.00 |
| 7 | 73.89 | 74.45 |
| mean | 69.91 | 69.74 |
| min | 65.82 | 64.16 |

Worst-case path per seed (ns, logic + routing):

| seed | CoreTop | FullTop |
|---|---|---|
| 1 | 2.4 + 12.8 = 15.2 | 2.4 + 11.5 = 13.8 |
| 2 | 2.9 + 11.8 = 14.7 | 3.7 + 11.8 = 15.6 |
| 3 | 6.1 + 8.2 = 14.3 | 2.2 + 12.5 = 14.7 |
| 7 | 7.2 + 6.3 = 13.5 | 2.5 + 10.9 = 13.4 |

Both tops clear 60 MHz on all four seeds; the tightest result anywhere is
64.16 MHz (FullTop seed 2, 6.9 % over the constraint, 0.95 ns of period slack);
CoreTop's worst is 65.82 MHz, 1.47 ns of slack. The two tops trade places seed
by seed with an 8-10 MHz spread, so the cross-top difference carries no
structural meaning: it is the same core plus a PLL that never sits on the logic
path. The spread itself is the important caveat — small netlist or naming
changes moved individual seeds by up to 10 MHz during M9, so every number here
is a sweep, never a single run.

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
* **FullTop** repeats the operand-register load on seeds 1, 3 and 7 (seed 1
  `d_rs2` -> `dx_b` at 13.8 ns, seed 3 `d_b`/`d_rs2` -> `dx_b` at 14.7 ns,
  seed 7 `dx_we` -> `d_rs2` -> `dx_b` at 13.4 ns), and seed 2 is different: it
  is the fetch hold cone, `freeze` -> `fetch.i_hold` -> the PC register's clock
  enable (3.7 + 11.8 = 15.6 ns), i.e. the squash/hazard path rather than the
  operand path. No boot-sequencer register appears on any measurement path.

So on CoreTop the limiter is the operand select; on FullTop it is the operand
select on three seeds and the fetch-hold cone on one. The branch-target adder
(`o_target`) is on CoreTop seeds 3 and 7 but never the tail of a path, and no
path in either top ends at `ct_target`: registering the control transfer removed
that loop for good.

Nothing in either top crosses clock domains: the 25 MHz net only reaches the
PLL's `CLKI` pin, so nextpnr reports exactly one timed clock
(`$glbnet$boot_clk`, the promoted CLKOP net) and one cross-domain report,
`posedge $glbnet$boot_clk -> <async>`, measured at 3.9-4.7 ns to the output
pins across seeds 1/2/3/7.

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

This is the part of the milestone that does not work as one would hope, stated
as measured:

* nextpnr-ecp5 **does** model `EHXPLLL` and tries to constrain the generated
  domain. With the input net constrained (LPF `FREQUENCY NET "clk"`) it prints:

  ```
  Info:     Input frequency of PLL 'pll.pll' is constrained to 25.0 MHz
  Info:     Derived VCO frequency 6024.1 MHz of PLL 'pll.pll' is out of legal range [400MHz, 800MHz]
  Info:     Derived frequency constraint of 600.2 MHz for net boot_clk
  ```

  i.e. it derives **600.24 MHz** for a PLL that produces 60.000 MHz, and flags
  its own VCO figure as illegal. What the log proves is that `CLKOP_DIV` is not
  applied to the output: a probe with `CLKOP_DIV` changed 10 -> 12 keeps the
  derived output at 600.2 MHz while the derived VCO moves 6024.1 -> 7246.4 MHz,
  so the VCO line is a further scaled variant of that same expression rather
  than `output × CLKOP_DIV` (600.24 × 10 = 6002.4, not 6024.1). Nothing in
  `tools/synth.py` can fix this: the generated net needs its own `FREQUENCY NET`
  constraint, which is a tool change outside this PR.
* Consequence: the generated clock **cannot** be timed against a 60 MHz
  constraint by this flow. nextpnr's STA number for the domain is real
  (73.88 MHz seed 1) — the constraint it compares against is not.
  `docs/metrics/m9_full.md` therefore reads
  `fmax $glbnet$boot_clk: 73.88 MHz (constraint 600.24 MHz, missed)`, and the
  "missed" verdict is an artifact of the derived constraint, not a 60 MHz miss.
* Fixing it needs an explicit `FREQUENCY NET <CLKOP net> 60 MHz;` line in the
  LPF (or a `--freq` that nextpnr applied to the generated net), i.e. a change
  to `tools/synth.py`, which is out of scope for M9b.
* `--freq` itself only sets the default for clocks with no constraint. The one
  invocation detail that matters: the run must be given **`--freq 25`**, because
  `synth.py` writes that value into the LPF for the `clk` net and the board's
  oscillator is 25 MHz. With the nominal `--freq 60` the input pad is
  over-constrained to 60 MHz and nextpnr then derives 1440.92 MHz for the
  generated domain (`--name m9_full_f60_input`); the routed design is otherwise
  identical (73.88 MHz, same resources), and both bitstreams pack.
* The 60 MHz closure of the core itself is measured where there is no PLL in
  the way: `CoreTop` at `--freq 60`, 4/4 seeds met, worst 65.82 MHz.

## Measured vs estimated

Measured (tool output, reproducible with the commands above):

* LUT4/FF/CCU2C/DP16KD/MULT18X18D/EHXPLLL counts and per-seed fmax and critical
  paths for CoreTop and FullTop, post-place-and-route;
* PLL divider values in the packed bitstream (`top.config` PLL tile words);
* CPI for the two measurement programs, in `amaranth.sim`;
* bitstream generation (`ecppack` accepts the `EHXPLLL` configuration, so the
  divider set, feedback path and output-enable encodings are at least legal).
  `CLKI_DIV=0` and `FEEDBK_PATH=CLKOP` are the all-zero/default encodings, so
  the unpacked bitstream shows them only implicitly; the text config nextpnr
  hands to `ecppack` states them explicitly, and the two agree on every
  non-default field.

Not measured, and not claimed:

* **Silicon timing.** No frequency has been measured on the board; all fmax
  figures are static timing analysis on a routed netlist at the stated
  constraint. There is no oscilloscope, no on-chip counter-based frequency
  measurement and no temperature/voltage characterisation here. Actual
  operation at 60 MHz is an inference from PnR, nothing more.
* **HIL of the 60 MHz core.** The board supplies exactly 25 MHz, so a 60 MHz
  core cannot be exercised on it without the PLL — which is precisely why
  `FullTop` exists. Even with the PLL, what has been run on hardware is nothing:
  no bitstream from this branch has been loaded. The only end-to-end statement
  available is "the flow produces a bitstream and PnR says the core closes at
  60 MHz with ≥ 5.8 MHz of margin at the worst seed".
* **PLL lock time, jitter, duty cycle.** Derived from the datasheet (600 MHz
  VCO, `/10` output), not measured. `PLL_LOCK_DELAY` is left at the default 200.
* **The watchdog's 1.12 s period** is `2**26 / 60 MHz` arithmetic on the
  simulated counter, not a stopwatch measurement on the board.

## Risks

1. **nextpnr's PLL model is wrong by `CLKOP_DIV`** (see above). Anyone reading
   the committed `docs/metrics/m9_full.md` must know that the "missed" verdict
   compares against 600.24 MHz. The routed fmax number is usable; the verdict is
   not. Upstream nextpnr may have fixed this in a later release; the installed
   one is 0.7-1+b3.
2. **No silicon evidence at 60 MHz.** The PnR margins are 5.81 MHz for
   `CoreTop` (65.82 vs 60) and 5.88 MHz for `FullTop` (65.88 vs 60) at the worst
   seed. Silicon at 25 °C is usually a little faster than the model and at
   temperature the reverse; nothing here bounds that.
3. **ABC's global remap inside `synth_ecp5` moves the LUT count by ~±100 on
   small design changes.** FullTop's core RTL never changed across this
   milestone, yet its netlist measured 1602, 1740 and 1729 LUT4 cells
   (nextpnr: 1754, 1892, 2013) as the 30-FF reset sequencer was revised. Cross-top
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
