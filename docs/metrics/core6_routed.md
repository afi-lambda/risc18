# Core6 routed: `Core6Top` / `Full6Top` at a 120 MHz constraint

Stage 2 of the six-stage plan: the simulation-only `Core6` (`src/risc18/core6.py`,
M11) and the full top (`Full6Top`, PLL at exactly 120.000 MHz) built through
`tools/synth.py` -- Amaranth -> yosys `synth_ecp5` -> nextpnr-ecp5 (LFE5U-25F-6
CABGA256, `--placer heap --router router2 --timing-allow-fail`) -> ecppack.

**Stage 4 re-measured the `Full6Top` rows.** The stage-2 builds pinned the
`Full6Top` clock through a PLL divider set that was wrong (it asked nextpnr for a
2.88 GHz VCO), so its derived constraint was meaningless; the rows below marked
*stage 4* were rebuilt with the corrected set and `--freq 25` (the board's pad
frequency), which makes nextpnr derive exactly 120.0 MHz for `boot_clk`. The
`Core6Top` rows are the stage-2 builds: that top has no PLL, so nothing in this
change can move it.

Both tops use the **distributed-RAM register file** (`--ram-rf`,
`RegFileRam`), i.e. the shipping configuration of the five-stage core
(`m11_core_ram`, `m11_full_ram`), so the numbers are comparable. The flop-file
build was not routed.

Builds (8 measured, 2 controls), all at `TMPDIR=/home/alain/.hermes/cache/scratch`:

    python tools/synth.py --top risc18.tops:Core6Top --name m12_core6_s<N> --freq 120 --seed <N> --ram-rf   # stage 2
    python tools/synth.py --top risc18.tops:Full6Top --name m12_full6_s<N> --freq  25 --seed <N> --ram-rf   # stage 4
    # controls
    python tools/synth.py --top risc18.tops:CoreTop  --name m12_ctrl_core5_120_s1  --freq 120 --seed 1 --ram-rf
    python tools/synth.py --top risc18.tops:Full6Top --name m12_ctrl_full6_120_s<N> --freq 120 --seed <N> --ram-rf

Metrics: `build/metrics/m12_*.{md,json}`; the `Full6Top` rows are also
committed as `docs/metrics/m12_full6_s{1,2,3,7}.md`. Evidence (routed `Critical
path report` block per build): `build/synth/m12_*/pnr_seed<N>.log`. Every build
reached a bitstream (`build/synth/m12_*/top.bit`, `ecppack.log`).

PLL divider set (`Full6Top`, asserted in `tests/test_tops6.py` and
`tests/test_pll.py`): `CLKI_DIV = 5`, `CLKFB_DIV = 24`, `CLKOP_DIV = 5`,
`CLKOP_CPHASE = 2` -> PFD 5.000 MHz, CLKOP **120.000 MHz**, VCO 600.000 MHz,
error 0 Hz. CLKOP is the feedback signal, so `CLKOP_DIV` sets the VCO, not the
output; the set is `ecppll -i 25 -o 120` verbatim, and `risc18.pll.Pll`
validates it (input 25 in 8-400, PFD 5 in 3.125-400, VCO 600 in 400-800, CLKOP
120 in 10-400, counters <= 80). The stage-2 set `(1, 24, 5)` would have asked
for 600 MHz of CLKOP and a 3 GHz VCO and is now rejected at construction.

## Per-seed results (120 MHz constraint, routed)

| top | seed | fmax | logic | routing | path hops | log |
|---|---:|---:|---:|---:|---:|---|
| `Core6Top` | 1 | **80.84 MHz** | 3.2 ns | 9.2 ns | 9 | `build/synth/m12_core6_s1/pnr_seed1.log` |
| `Core6Top` | 2 | **80.87 MHz** | 3.7 ns | 8.7 ns | 15 | `build/synth/m12_core6_s2/pnr_seed2.log` |
| `Core6Top` | 3 | **76.20 MHz** | 3.8 ns | 9.3 ns | 19 | `build/synth/m12_core6_s3/pnr_seed3.log` |
| `Core6Top` | 7 | **76.38 MHz** | 3.7 ns | 9.4 ns | 22 | `build/synth/m12_core6_s7/pnr_seed7.log` |
| `Full6Top` | 1 | **78.35 MHz** (stage 4) | 3.0 ns | 9.8 ns | 10 | `build/synth/m12_full6_s1/pnr_seed1.log` |
| `Full6Top` | 2 | **74.83 MHz** (stage 4) | 3.3 ns | 10.1 ns | 16 | `build/synth/m12_full6_s2/pnr_seed2.log` |
| `Full6Top` | 3 | **78.90 MHz** (stage 4) | 3.4 ns | 9.3 ns | 17 | `build/synth/m12_full6_s3/pnr_seed3.log` |
| `Full6Top` | 7 | **74.82 MHz** (stage 4) | 2.9 ns | 10.5 ns | 8 | `build/synth/m12_full6_s7/pnr_seed7.log` |

Means: `Core6Top` 78.57 MHz, `Full6Top` 76.73 MHz. Best 78.90, **worst 74.82**.

* **Worst seed: `Full6Top` seed 7, 74.82 MHz** (13.4 ns = 2.9 logic + 10.5
  routing). Against the five-stage core's 73.48 MHz this is a wash; against
  the 120.0 MHz gate it is 62 % of target.
* Gate `worst-seed Full6Top fmax >= 120.0 MHz`: **NOT MET**. 74.82 MHz is also
  below the 110 MHz stop-and-report line, so per the stage-2 brief no RTL was
  edited to chase it; this document reports the numbers. The stage-4 rebuild
  moved individual seeds by up to 5.6 MHz (seed 1: 83.99 -> 78.35) because the
  netlist changed (the PLL's `CLKFB` now takes the CLKOP net and the parameters
  differ), which is the same seed-to-seed sensitivity the rest of this
  directory documents.
* The fmax figures are the routed path delays (1000 / (logic + routing)). The
  PASS/FAIL column next to them is now a real 120 MHz verdict — `missed` — not
  the artefact of the stage-2 constraint (see "Flow caveats").

## (a) Core6Top vs Full6Top

| seed | `Core6Top` | `Full6Top` (stage 4) | delta |
|---|---:|---:|---:|
| 1 | 80.84 | 78.35 | -2.49 |
| 2 | 80.87 | 74.83 | -6.04 |
| 3 | 76.20 | 78.90 | +2.70 |
| 7 | 76.38 | 74.82 | -1.56 |

The PLL + boot sequencer shell changes fmax by a few MHz in both directions
(its own cost is +5 LUT4 / +30 FF over `Core6Top` at this head, and `boot` adds
a fourth clock domain on the same net). The seed-to-seed spread is larger than
the systematic difference: 4.67 MHz within `Core6Top` (76.20-80.87) and
4.08 MHz within `Full6Top` (74.82-78.90), against a mean Core6-vs-Full6 delta of
-1.85 MHz. So the shell is not a timing feature -- neither top reaches the
target, and which one wins depends on the seed.

## (b) Worst path, logic vs routing

Routing, not logic, is what fails. Across the four stage-4 `Full6Top` seeds the
split is 2.9-3.4 ns logic against 9.3-10.5 ns routing, over 8-17 routing hops.
Every path starts in the instruction-register cone that feeds `dx_b`
(`core.i_b$88_*`), runs through `core.dx_b` into the `o_target` carry chain, and
ends at `core.i_a` (seeds 1/2) or at the data EBR address
(`core.dmem.mem.0.0.ADA0/1`, seeds 3/7).

One net is most of the answer: seed 1's `Net core.dx_b[0] (6,16) -> (7,17)` is
3.0 ns of its 9.8 ns of routing, and seed 2's `Net core.dx_b[5] (8,12) ->
(7,16)` is 4.1 ns of 10.1 ns -- the operand bus leaves one register-file column
and has to reach every ALU/compare/adder input. The best seed (3, 78.90 MHz) has
the shortest route (9.3 ns, 17 hops) and the largest logic share (3.4 ns); the
worst (7, 74.82 MHz) the opposite split (2.9 + 10.5 ns, 8 hops).

## Critical path classes per seed (quoted from the logs)

| build | source cell | sink cell | class |
|---|---|---|---|
| `core6_s1` | `core.i_b$88_TRELLIS_FF_Q_1.Q` | `core.dmem.mem.0.0.ADA1` | X: operand reg -> ALU chain (`alu.py:58`) -> `core.dmem.mem_w_en` -> data-EBR address (`dmem.py:91`) |
| `core6_s2` | `core.i_a_TRELLIS_FF_Q_6.Q` | `core.fetch.o_instr_pc_TRELLIS_FF_Q_3.CE` | X: operand reg -> 16-bit compare (`branch.py:43`, `pc.py:47`) -> `core6.py:264` control -> `core.i_hold` -> fetch register CE |
| `core6_s3` | `core.i_a_TRELLIS_FF_Q_9.Q` | `core.fetch.pc.o_pc_TRELLIS_FF_Q.CE` | same class, `core.ct_valid` (`core6.py:264`) -> PC enable |
| `core6_s7` | `core.i_b$88_TRELLIS_FF_Q_1.Q` | `core.dmem.mem.0.0.ADA1` | X -> data-EBR address (`alu.py:64`) |
| `full6_s1` (stage 4) | `core.i_b$88_TRELLIS_FF_Q_2.Q` | `core.i_a_TRELLIS_FF_Q.M` | X: instruction register -> `core.dx_b` operand select -> `core.o_target` carry chain (`branch.py`) -> operand register `i_a` |
| `full6_s2` (stage 4) | `core.i_b$88_TRELLIS_FF_Q_15.Q` | `core.i_a_TRELLIS_FF_Q_5.M` | same class: operand select -> target adder -> operand register |
| `full6_s3` (stage 4) | `core.i_b$88_TRELLIS_FF_Q_17.Q` | `core.dmem.mem.0.0.ADA0` | X: operand select -> target adder -> data-EBR address (`dmem.py`) |
| `full6_s7` (stage 4) | `core.i_b$88_TRELLIS_FF_Q.Q` | `core.dmem.mem.0.0.ADA1` | same class: operand select -> target adder -> data-EBR address |

All eight end in an X-stage cone: the branch-target adder / ALU carry chain
fanning into an operand register, a data-EBR address or the fetch control
(`i_hold`, `ct_valid`, PC enable, imem CE).
Yosys's LUT packing shares those nets aggressively, so the cell names on the
first and last hops are not always the signal names (`i_b$88`, `sq1`, `$signal$7x`,
`dx_mem_write_LUT4_C_Z_LUT4_A`); the `Defined in:` site quoted next to each hop
(`branch.py:43`, `pc.py:47`, `alu.py:58/64`, `dmem.py:91`/`58`, `core6.py:264`/`341`/`357`,
`imem.py:132`) is what identifies the stage.

## (c) X stage or fetch stage

**The X stage caps the clock, not the fetch.** In none of the eight 6-stage
builds does the critical path start at the instruction EBR (`core.fetch.imem.*.DOA*`)
or end at the fetch register `fq`; the classes above are X (7/8) and M (1/8).
In the four stage-4 `Full6Top` builds the pattern is tighter still: every path
starts in the X-stage operand cone (`core.i_b$88_*` -> `core.dx_b`), ends in an
X-stage register (`core.i_a`) or in the data EBR address
(`core.dmem.mem.0.0.ADA0/1`), and the branch-target adder `core.o_target` is
*on* all four -- the opposite of the five-stage top, where it is never the tail.
(The stage-2 `Full6Top` paths quoted in the table above for `full6_s1..s7` no
longer exist: that netlist's placement changed when the PLL parameters and the
`CLKFB` net changed. The rows are the stage-4 logs.)
The five-stage control build is the counter-example -- and it is exactly the
class the fetch register was added to cut:

* `m12_ctrl_core5_120_s1`: `core.fetch.imem.mem.0.0.DOA14` ->
  `core.regfile._0__TRELLIS_FF_Q_17.DI` (`imem.py:105`, `memory.py:240`),
  13.6 ns = **73.48 MHz**, identical to the M11 five-stage build
  (`docs/metrics/m11_core_ram.md`).

So the cut did what it was designed to do: the EBR-output -> decode ->
register-file path that capped the five-stage core at 73.48 MHz is gone, and the
best 6-stage build reaches 78.90 MHz. The new limiter is the X stage: the
operand bus fan-out into the branch-target adder and the data EBR address, in
the same cycle.

## (d) Reset distribution

* `Core6Top` has an asynchronous reset input pad, and the routed `<async>` ->
  `posedge clk` delay is **9.77 / 9.57 / 9.94 / 8.18 ns** (seeds 1/2/3/7) -- i.e.
  101-122 MHz. It is in the same range as the five-stage build's 8.91 ns and of
  the earlier 7.3-8.3 ns measurements, so the reset path does appear in the
  6-stage builds, but it is not the limiter (the 12.4-13.1 ns same-domain paths
  are 2.5-4.9 ns slower). The cost is placement: `i_rst$tr_io.O` lands at the die
  edge and the first hop is `Net i_rst$TRELLIS_IO_IN (72,5) -> (5,6)` = 5.6 ns,
  because `i_rst` is not only the domain reset but also gates the store
  (`core.dmem.mem_w_en`, `dmem.py:91`).
* `Full6Top` has **no** asynchronous reset path: the core's reset is
  `BootReset`'s flop in the `boot` domain, i.e. same clock as the core, and the
  PLL's `RST` is tied off. Its only pad-facing paths are FF -> output pad
  (4.79 / 5.44 / 4.84 / 4.99 ns routed over seeds 1/2/3/7, from
  `o_retire_pc`/`o_retire_value` to the pads), which do not set the clock
  period.

Conclusion: reset distribution is a *second* constraint in `Core6Top` (a
pad-limited net inside the store-enable cone) and absent from `Full6Top`. It is
not what holds either top at ~76 MHz.

## Flow caveats for the PLL top

* `--freq` is the **pad** frequency, not the core clock: `tools/synth.py` writes
  it into `FREQUENCY NET "clk"`. On `Full6Top` it must be 25 (the board's
  oscillator); nextpnr then derives `25 × CLKFB_DIV / CLKI_DIV = 120.0 MHz` for
  `boot_clk` and prints no VCO warning, so the PASS/FAIL column is a real
  120 MHz verdict. With `--freq 120` the 25 MHz pad is over-constrained to
  120 MHz and nextpnr derives 576.0 MHz (120 × 24/5) plus
  `Derived VCO frequency 2881.8 MHz ... out of legal range` — that is the
  `m12_ctrl_full6_120_s{1,2,3,7}` control, and it is why the stage-2 rows above
  carried a meaningless verdict.
* The constraint target does not change place-and-route here: `Full6Top` seed 1
  built at `--freq 25` and at `--freq 120` gives the same netlist size, the same
  utilisation and the same 78.35 MHz (`m12_ctrl_full6_120_s1` vs `m12_full6_s1`),
  and likewise for all four seeds.
  `CoreTop` at `--freq 120` reproduces the `--freq 60` M11 number exactly
  (73.48 MHz, 6.8 ns logic / 6.8 ns routing), so the comparison against the
  five-stage core is on equal terms.
* No pin/LOC constraints or placement regions are used anywhere in this flow
  (`tools/synth.py` writes only `FREQUENCY NET "clk"`), so the I/O pads and the
  `DP16KD` columns are the placer's choice -- that is where the 9 ns of routing
  comes from. Changing that is a flow change, not an RTL change, and it was not
  attempted here.

## (e) Resources against the five-stage core

| build | LUT4 | FF | DP16KD | MULT18X18D |
|---|---:|---:|---:|---:|
| `CoreTop` 5-stage (`m11_core_ram`, `m12_ctrl_core5_120_s1`) | 1183 | 407 | 2 | 1 |
| `Core6Top` (all seeds, stage 2) | 1215 | 460 | 2 | 1 |
| `Full6Top` (all seeds, stage 4) | 1220 | 490 | 2 | 1 |

The six-stage cut costs **+32 LUT4 / +53 FF** (the fetch register `fq`, its
valid/PC bundle, the registered product and the extra control register); the
PLL + boot sequencer shell adds **+5 LUT4 / +30 FF** over `Core6Top` at this
head (the `EHXPLLL` itself, the 26-bit watchdog and the sequencer; the stage-2
build measured +109 LUT4, well inside the ±100-cell ABC remap variance
`docs/metrics/NOTES.md` documents). Both EBRs and the one `MULT18X18D` are
unchanged, so the DSP and memory budget is untouched.

## Verdict

* 120 MHz gate: **RED**. Worst `Full6Top` seed = **74.82 MHz** (seed 7),
  best = 78.90 MHz (seed 3). Below the 110 MHz stop line, so nothing was tuned
  and no RTL was touched. (Stage 2 measured 73.31 / 83.99 MHz at the same
  target; the stage-4 rebuild moved individual seeds, not the verdict.)
* What the builds show: the six-stage cut removes the five-stage limiter
  (13.6 ns EBR-output -> file path, 73.48 MHz) and buys ~7 % (up to 78.90 MHz),
  but the clock is now set by the X stage's operand select into the
  branch-target adder and the data-EBR address, plus 9.3-10.5 ns of routing.
  The 120 MHz target needs those paths to fit in 8.33 ns; with 2.9-3.4 ns of
  logic that leaves ~5 ns of routing budget, and this flow delivered
  9.3-10.5 ns on every seed.
