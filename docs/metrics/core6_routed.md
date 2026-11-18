# Core6 routed: `Core6Top` / `Full6Top` at a 120 MHz constraint

Stage 2 of the six-stage plan: the simulation-only `Core6` (`src/risc18/core6.py`,
M11) and the full top (`Full6Top`, PLL at exactly 120.000 MHz) built through
`tools/synth.py` -- Amaranth -> yosys `synth_ecp5` -> nextpnr-ecp5 (LFE5U-25F-6
CABGA256, `--placer heap --router router2 --timing-allow-fail`) -> ecppack.

Both tops use the **distributed-RAM register file** (`--ram-rf`,
`RegFileRam`), i.e. the shipping configuration of the five-stage core
(`m11_core_ram`, `m11_full_ram`), so the numbers are comparable. The flop-file
build was not routed.

Builds (8 measured, 2 controls), all at `TMPDIR=/home/alain/.hermes/cache/scratch`:

    python tools/synth.py --top risc18.tops:Core6Top --name m12_core6_s<N> --freq 120 --seed <N> --ram-rf
    python tools/synth.py --top risc18.tops:Full6Top --name m12_full6_s<N> --freq 120 --seed <N> --ram-rf
    # controls
    python tools/synth.py --top risc18.tops:CoreTop  --name m12_ctrl_core5_120_s1 --freq 120 --seed 1 --ram-rf
    python tools/synth.py --top risc18.tops:Full6Top --name m12_ctrl_full6_25_s1  --freq  25 --seed 1 --ram-rf

Metrics: `build/metrics/m12_*.{md,json}`. Evidence (routed `Critical path
report` block per build): `build/synth/m12_*/pnr_seed<N>.log`. Every build
reached a bitstream (`build/synth/m12_*/top.bit`, `ecppack.log`).

PLL divider set (`Full6Top`, asserted in `tests/test_tops6.py`):
`CLKI_DIV = 1`, `CLKFB_DIV = 24`, `CLKOP_DIV = 5` -> PFD 25.000 MHz, VCO
600.000 MHz, CLKOP **120.000 MHz**, error 0 Hz. `risc18.pll.Pll` accepts this
set unchanged (PFD 10-400, VCO 400-800, `CLKOP_DIV <= 128`, counters <= 80), so
`pll.py` was not edited.

## Per-seed results (120 MHz constraint, routed)

| top | seed | fmax | logic | routing | path hops | log |
|---|---:|---:|---:|---:|---:|---|
| `Core6Top` | 1 | **80.84 MHz** | 3.2 ns | 9.2 ns | 9 | `build/synth/m12_core6_s1/pnr_seed1.log` |
| `Core6Top` | 2 | **80.87 MHz** | 3.7 ns | 8.7 ns | 15 | `build/synth/m12_core6_s2/pnr_seed2.log` |
| `Core6Top` | 3 | **76.20 MHz** | 3.8 ns | 9.3 ns | 19 | `build/synth/m12_core6_s3/pnr_seed3.log` |
| `Core6Top` | 7 | **76.38 MHz** | 3.7 ns | 9.4 ns | 22 | `build/synth/m12_core6_s7/pnr_seed7.log` |
| `Full6Top` | 1 | **83.99 MHz** | 6.7 ns | 5.2 ns | 6 | `build/synth/m12_full6_s1/pnr_seed1.log` |
| `Full6Top` | 2 | **76.18 MHz** | 4.1 ns | 9.0 ns | 27 | `build/synth/m12_full6_s2/pnr_seed2.log` |
| `Full6Top` | 3 | **73.31 MHz** | 3.9 ns | 9.8 ns | 21 | `build/synth/m12_full6_s3/pnr_seed3.log` |
| `Full6Top` | 7 | **73.77 MHz** | 4.1 ns | 9.5 ns | 26 | `build/synth/m12_full6_s7/pnr_seed7.log` |

Means: `Core6Top` 78.57 MHz, `Full6Top` 76.81 MHz. Best 83.99, **worst 73.31**.

* **Worst seed: `Full6Top` seed 3, 73.31 MHz** (13.7 ns = 3.9 logic + 9.8
  routing). Against the five-stage core's 73.48 MHz this is a wash; against
  the 120.0 MHz gate it is 61 % of target.
* Gate `worst-seed Full6Top fmax >= 120.0 MHz`: **NOT MET**. 73.31 MHz is also
  below the 110 MHz stop-and-report line, so per the stage-2 brief no RTL was
  edited to chase it; this document reports the numbers.
* `--freq 120` on a top whose clock is a PLL output makes nextpnr print a
  meaningless PASS/FAIL column (see "Flow caveats"); the fmax figures above are
  the routed path delays (1000 / (logic + routing)), not the constraint check.

## (a) Core6Top vs Full6Top

| seed | `Core6Top` | `Full6Top` | delta |
|---|---:|---:|---:|
| 1 | 80.84 | 83.99 | +3.15 |
| 2 | 80.87 | 76.18 | -4.69 |
| 3 | 76.20 | 73.31 | -2.89 |
| 7 | 76.38 | 73.77 | -2.61 |

The PLL + boot sequencer shell changes fmax by a few MHz in both directions
(its own cost is +109 LUT4 / +30 FF, and `boot` adds a fourth clock domain on
the same net). Seed-to-seed spread (~7.7 MHz within a top) is larger than the
systematic Core6-vs-Full6 difference, so the shell is not a timing feature --
neither top reaches the target, and which one wins depends on the seed.

## (b) Worst path, logic vs routing

Routing, not logic, is what fails. On the four worst seeds the split is
3.9-4.1 ns logic against 9.0-9.8 ns routing, with 21-27 routing hops of
0.3-2.8 ns each; the paths run across 11-20 device rows/columns (e.g.
`Net core.dmem.mem_w_en (29,9) -> (26,25)` = 2.0 ns for 16 rows; `Net
core.dx_rs2[6] (18,11) -> (10,17)` = 2.8 ns). The one build that closed at
83.99 MHz has the shortest route (5.2 ns, 6 hops) and the *largest* logic share
(6.7 ns).

Two fixed costs are visible in that best build: 5.6 ns of the 6.7 ns logic is
the data EBR's clock-to-out (`Source core.dmem.mem.0.0.DOA0`, a `DP16KD`), and
2.6 ns of the 5.2 ns routing is the same EBR's distance to the logic cluster
(`Net core.m_value_LUT4_Z_D[1] (8,25) -> (4,4)`). The EBRs are placed in the
bottom row while the logic sits in rows 3-9.

## Critical path classes per seed (quoted from the logs)

| build | source cell | sink cell | class |
|---|---|---|---|
| `core6_s1` | `core.i_b$88_TRELLIS_FF_Q_1.Q` | `core.dmem.mem.0.0.ADA1` | X: operand reg -> ALU chain (`alu.py:58`) -> `core.dmem.mem_w_en` -> data-EBR address (`dmem.py:91`) |
| `core6_s2` | `core.i_a_TRELLIS_FF_Q_6.Q` | `core.fetch.o_instr_pc_TRELLIS_FF_Q_3.CE` | X: operand reg -> 16-bit compare (`branch.py:43`, `pc.py:47`) -> `core6.py:264` control -> `core.i_hold` -> fetch register CE |
| `core6_s3` | `core.i_a_TRELLIS_FF_Q_9.Q` | `core.fetch.pc.o_pc_TRELLIS_FF_Q.CE` | same class, `core.ct_valid` (`core6.py:264`) -> PC enable |
| `core6_s7` | `core.i_b$88_TRELLIS_FF_Q_1.Q` | `core.dmem.mem.0.0.ADA1` | X -> data-EBR address (`alu.py:64`) |
| `full6_s1` | `core.dmem.mem.0.0.DOA0` | `core.i_b$88_TRELLIS_FF_Q_2.M` (`core6.py:357`) | M: load word EBR DOA -> `core.m_value` -> forward mux -> ALU operand reg |
| `full6_s2` | `core.i_a_TRELLIS_FF_Q_17.Q` | `core.$signal$77_TRELLIS_FF_Q_1.M` | X: operand reg -> compare (`branch.py:43`, `pc.py:47`) -> `core.$signal$72` -> M reg |
| `full6_s3` | `core.i_b_TRELLIS_FF_Q_11.Q` | `core.i_instr_TRELLIS_FF_Q_6.DI` | X: `core.dx_rs2[6]` -> compare carry chain `core.sq1_*` (`branch.py:43`) -> `core.fetch.imem.in_range_q` -> fetch data |
| `full6_s7` | `core.i_b_TRELLIS_FF_Q_16.Q` | `core.fetch.imem.in_range_q_TRELLIS_FF_Q.CE` | X: compare (`branch.py:43`, `dmem.py:58`) -> `core.i_hold` -> imem CE |

Seven of the eight end in an X-stage cone: a 16-bit branch compare / ALU carry
chain fanning into control (`i_hold`, `ct_valid`, PC enable, EBR address/CE).
Yosys's LUT packing shares those nets aggressively, so the cell names on the
first and last hops are not always the signal names (`i_b$88`, `sq1`, `$signal$7x`,
`dx_mem_write_LUT4_C_Z_LUT4_A`); the `Defined in:` site quoted next to each hop
(`branch.py:43`, `pc.py:47`, `alu.py:58/64`, `dmem.py:91`/`58`, `core6.py:264`/`341`/`357`,
`imem.py:132`) is what identifies the stage.

## (c) X stage or fetch stage

**The X stage caps the clock, not the fetch.** In none of the eight 6-stage
builds does the critical path start at the instruction EBR (`core.fetch.imem.*.DOA*`)
or end at the fetch register `fq`; the classes above are X (7/8) and M (1/8).
The fetch stage does appear *inside* three of the `Full6Top` paths, as
`core.fq_valid_LUT4_C_Z...` fed by the compare chain `core.sq1_LUT4_D_1_Z[5]`
(6.9 ns at seed 2) before the path runs on to its register -- i.e. the X cone
drives the FD-capture qualifier, so the fetch stage is the victim of the X
stage here, not the source of the delay.
The five-stage control build is the counter-example -- and it is exactly the
class the fetch register was added to cut:

* `m12_ctrl_core5_120_s1`: `core.fetch.imem.mem.0.0.DOA14` ->
  `core.regfile._0__TRELLIS_FF_Q_17.DI` (`imem.py:105`, `memory.py:240`),
  13.6 ns = **73.48 MHz**, identical to the M11 five-stage build
  (`docs/metrics/m11_core_ram.md`).

So the cut did what it was designed to do: the EBR-output -> decode ->
register-file path that capped the five-stage core at 73.48 MHz is gone, and the
best 6-stage build reaches 83.99 MHz. The new limiter is the X stage, whose ALU
/ compare result feeds the data EBR address and the fetch-control registers in
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
  (8.69 ns at seed 1, 12.05 ns at seed 7), which do not set the clock period.

Conclusion: reset distribution is a *second* constraint in `Core6Top` (a
pad-limited net inside the store-enable cone) and absent from `Full6Top`. It is
not what holds either top at ~76 MHz.

## Flow caveats for the PLL top

* nextpnr derives the generated-clock constraint as `pad_freq * CLKFB_DIV`
  (24x), ignoring `CLKOP_DIV`: with `--freq 120` it prints `FAIL at 2881.84 MHz`,
  with `--freq 25` `FAIL at 600.24 MHz`. The PASS/FAIL column is therefore
  meaningless for `Full6Top`; only the measured fmax is used above. (The same
  artefact is visible in `docs/metrics/m11_full_ram.md`, which was built with
  `--freq 25`.)
* The constraint target does not change place-and-route here: `Full6Top` seed 1
  built at `--freq 25` and at `--freq 120` gives the same netlist size, the same
  utilisation and the same 83.99 MHz (`m12_ctrl_full6_25_s1` vs `m12_full6_s1`).
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
| `Core6Top` (all seeds) | 1215 | 460 | 2 | 1 |
| `Full6Top` (all seeds) | 1324 | 490 | 2 | 1 |

The six-stage cut costs **+32 LUT4 / +53 FF** (the fetch register `fq`, its
valid/PC bundle, the registered product and the extra control register); the
PLL + boot sequencer shell adds **+109 LUT4 / +30 FF** over `Core6Top` (the
`EHXPLLL` itself, the 26-bit watchdog and the sequencer). Both EBRs and the one
`MULT18X18D` are unchanged, so the DSP and memory budget is untouched.

## Verdict

* 120 MHz gate: **RED**. Worst `Full6Top` seed = **73.31 MHz** (seed 3),
  best = 83.99 MHz (seed 1). Below the 110 MHz stop line, so nothing was tuned
  and no RTL was touched.
* What the builds show: the six-stage cut removes the five-stage limiter
  (13.6 ns EBR-output -> file path, 73.48 MHz) and buys ~10-14 % (up to
  83.99 MHz), but the clock is now set by the X stage's ALU/compare -> control
  cones plus 8.7-9.8 ns of routing in a placement where the EBRs sit ~20 rows
  from the logic. The 120 MHz target needs those paths to fit in 8.33 ns; even
  with the routing of the best seed's 6-hop path, the X cones at 3.9-4.1 ns
  logic would leave ~2-4 ns of routing budget, which this flow did not deliver
  on any of the four seeds.
