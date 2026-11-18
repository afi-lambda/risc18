# Speed/area review — provenance and corrections

`prompt_speed_area.md` was sent to `openai/gpt-6.2-sol` with
`"reasoning": {"effort": "high"}` on 2026-11-18; the answer is
`advice_speed_area.md`. Usage: 3090 prompt + 7965 completion (4388 of them
reasoning) tokens, $0.087, about 2 minutes.

## Three things the brief got wrong (confirmed against the build)

1. **"1561 general logic + 126 carry LUT4" is not a decomposition of 1813.**
   The real accounting is 1687 `LUT4` cells (all general logic) plus 126 *sites*
   consumed by 63 `CCU2C` carry cells, i.e. 1687 + 2x63 = 1813. FullTop:
   1628 + 2x76 = 1780 (re-measured in stage 4; the M10-era netlist this note was
   written against read 1745 + 2x76 = 1897). `docs/RESULTS.md` states it that
   way ("logic LUTs / carry LUTs 1628 / 152"); the brief restated it wrongly.
2. **A 16:1 mux tree has 15 two-input nodes, not 16**, so the two 18-bit read
   ports are 2x18x15 = 540 nodes, not 576. 576 counted the data leaves.
3. **"78-84 % of every critical path is interconnect" is false.** Measured
   routing shares: CoreTop 84 / 80 / 57 / 47 %, FullTop 83 / 76 / 85 / 81 %.
   Only the decode-and-bypass family is routing-dominated; the DSP- and
   EBR-shaped seeds are not.

## Two things the answer could not know

- **The ECP5 has no distributed/LUT RAM.** `ecp5/cells_bb.v` contains no LUTRAM
  primitive and `synth_ecp5` has no `memory_lut` pass, so "one 16x18 copy per
  read port, mapped to ~36 RAM LUTs" is not implementable on this device. The
  storage would still be 270 FFs plus two read mux trees. The EBR form
  (one DP16KD, two synchronous read ports) is the version that applies here.
- **The PLL question was *not* settled — the answer was right to ask.** With
  `FEEDBK_PATH=CLKOP` the feedback divider counts CLKOP, so
  `CLKI_DIV=1 / CLKFB_DIV=24 / CLKOP_DIV=10` asks for 600 MHz of CLKOP and a
  6 GHz VCO: nextpnr's `VCO 6024.1 MHz ... out of legal range` was the loop
  equation, not a mislabel, and a design clocked by it produced nothing on the
  board. The bitstream decode below (CLKFB_DIV 0010111 = 23, CLKOP_DIV 0001001
  = 9, `FEEDBK_PATH CLKOP`) was and is correct — it proves what was loaded,
  not that the loaded set produces 60 MHz. The vendor set is
  `CLKI_DIV=5 / CLKFB_DIV=12 / CLKOP_DIV=10` with `CLKOP_CPHASE=4`, now what
  `risc18.pll.Pll` ships and validates (`docs/RESULTS.md`,
  `docs/metrics/hil5.md`).

## What the answer got right that changes the plan

- The D-stage loop is not "decode -> select" but "consumer's source indices
  decoded from the instruction -> compare against the producer's registered
  rd/write-enable -> select". So the lever is to take the *consumer's decoded
  field extraction* off the loop, not the producer metadata (which is already
  registered at D->X).
- Registering the bypass *data* is already done (`dx_a`/`dx_b`); the useful
  registered form is the bypass *grants*, and only if the grant describes where
  producers will be when the data is selected (a stale grant applied to the
  next cycle's buses is a classic bug).
- The 5.47 ns hop bounds the placement opportunity (~2.5-3 ns) rather than
  predicting 92 MHz.
- The 265 constant LUT4s are a decomposition artefact, not reclaimable area:
  budget tens, not 265.
- At 65-75 MHz, CPI 1.6-1.8 is 39-44 MIPS; a 1.8 -> 1.4 CPI change is worth
  about as much as 70 -> 90 MHz. The four-slot squash is a throughput lever
  first, an area lever second.

# Recipe review — what makes an 18-bit CPU fast and performant

`prompt_recipe_18bit.md` was sent to `openai/gpt-6.2-sol` with
`"reasoning": {"effort": "high"}` on 2026-11-18; the answer is
`advice_recipe_18bit.md`. Usage: 2205 prompt + 3702 completion tokens
(`reasoning_tokens` reported as 0 — the endpoint accepted `effort: high` but
the upstream reported no reasoning tokens, so this answer is not evidence that
high effort was honoured). $0.043, 30.2 s.

## One thing the brief got wrong

**"EBR has one read port and one write port" is a configuration, not a limit.**
The ECP5 `DP16KD` is a dual-port block (1Kx18 TDP is a legal mode), so both
ports can be 18-bit data ports. What is impossible in one block is 2 reads *and*
1 write: that needs replication or a different organisation. The brief's
phrasing invited the reviewer to spend a paragraph correcting it; the useful
version is "any 3-port structure costs a second block".

## One lever the answer proposed, checked against the source and closed

The answer suggested a slot of the four-kill window might be recoverable if the
EBR samples a stale PC for one extra edge, or if a valid target response is
suppressed by flush logic. Read `src/risc18/core.py` (M9a timing comment,
lines 20-50 and 480-530): each slot has a named mechanism — `P+1` killed at the
D->X validity, `P+2` by `squash`, `P+3`/`P+4` by the two FD capture edges while
`ct_valid` moves the PC. Applying the redirect on `take_x`'s own edge instead of
from the register is exactly the M5-M8 arrangement whose critical path
(compare -> redirect -> PC mux -> FD capture mux) was measured as a ~48 MHz
ceiling. So the slot is not free: recovering it costs the clock, which is the
trade already made once and measured. No build needed to settle this one.

## What it established that changes the CPI plan

- The 1.4x "remove the squash" figure is a *perfect, zero-cost predictor*
  ceiling, not an achievable gain. Concrete: static backward-taken = 44 cycles
  (1.27x), 48 cycles (1.17x) if each correct transfer still costs one cycle;
  last-outcome bit with a cold start = 48 cycles. The exit mispredict is the
  cost that stays.
- Resolving in D buys a 1.077x at best (52 of 56 cycles) and is erased by a
  clock loss above 7.1 %. The X-resolve decision stands.
- A 3-stage prototype must beat CPI 1.292 at 48 MHz just to tie 60 MHz at 1.615;
  compare routed full tops on branch-heavy *and* dependency-heavy code.
- 56 blocks = 57,344 18-bit words of total EBR, so a fully backed 64K-word space
  does not fit even before replication: the local-memory assumption is the
  structural cliff, not the pipeline.
- Removing ~11 % of elapsed cycles at 60 MHz is worth exactly 60 -> 67.59 MHz at
  unchanged CPI (67.59/60 = 1.1265). That is the exchange rate to use when
  ranking CPI work against timing work.

## What the answer could not know (repo/board-specific)

- Its fmax comparisons inherit the brief's mixed configurations: the j1
  (52.36 MHz at a 50 MHz constraint), PDP-8 (56.05 MHz, probe build at 25 MHz
  constraint, resources from a different `top.tim`) and VexRiscv (~70 MHz
  silicon-verified, 73.24 MHz static at a 69.44 MHz constraint) rows are not
  like-for-like, and no controlled width-only experiment exists in this repo.
- The VexRiscv "82 MHz / ~70 MIPS" figure is a claim in
  `ecp5-fastcpu/README.md`, not a number measured here.
- Nothing in the answer substitutes for HIL: every risc18 timing and CPI number
  is static or simulated.

# Deeper pipeline review — splitting the fetch stage

`prompt_deeper_pipeline.md` was sent to `openai/gpt-6.2-sol` twice on 2026-11-18,
once with `reasoning: {"effort": "high"}` (`advice_deeper_pipeline.md`) and once
with the top-level `reasoning_effort: "high"` plus `include_reasoning: true`
(`advice_deeper_pipeline_effortparam.md`). Both report `reasoning_tokens: 0` and
neither returns a `reasoning` field, while the model advertises `reasoning` and
`reasoning_effort` in its supported parameters. So the effort setting is accepted
and has no observable effect on this endpoint: do not describe these runs as
"high effort" without that caveat. Usage per run 2356 prompt + 2577/2656
completion tokens, $0.032/$0.027, 22/21 s. The two answers agree on every
substantive point, so the conclusion is stable across runs even though the
reasoning setting is not demonstrable.

## Endorsed

- Cut the pipeline at the instruction-EBR output: a register between the EBR data
  and the decode/register-file-address cone is the right lever. Target 120 MHz
  first; 150 MHz is a stretch, not a plan.
- After the cut the next limiters are the X-stage/execute-feedback paths and reset
  distribution (8.3 ns already uses the whole 120 MHz budget), then EBR capture
  placement and route variability.
- Bring up the existing RAM core at 60 MHz first, with retired-instruction, taken-
  redirect, lost-issue and dependency-stall counters plus an edge-level trace of
  one taken loop branch; keep the counters off the critical control paths.
- Arithmetic confirmed: 45.50 MIPS baseline; 60 / 75 MIPS at CPI 2.0 and 120 / 150
  MHz; break-even CPI 2.64 at 120 and 3.30 at 150. For the counted loop (CPI 1.842,
  39.89 MIPS) the thresholds are 3.01 and 3.76.

## Three things this brief got wrong

1. **"One extra killed slot when going 5 -> 6 stages" is not established.** With
   the registered redirect driving the EBR address directly, the target is sampled
   at R+1 and lands in the F1 output register at R+2, which is four younger slots,
   i.e. four lost X-issue cycles — not five. Five appears only if a separate PC
   register sits between the redirect register and the EBR address. Count from the
   actual address-sampling edges, not from the stage names.
2. **"Two block reads in one period" is wrong.** The register file is distributed
   RAM (`TRELLIS_DPR16X4`), not an EBR. The chain is one EBR read plus decode plus a
   1.9 ns LUT-RAM read, and the 1.9 ns figure is already the address-to-out number,
   not the block clock-to-out.
3. **The 150 MHz margin claim was too generous.** 5.6 ns is 84 % of a 6.67 ns
   period: the bare read probe's 6.7 ns *is* the period, so there is no demonstrated
   margin at 150. 120 MHz (8.33 ns) is the number the budget supports.

## Two things it could not know, and one it asked for

- The 6.7 ns figure is the read *output* path only. The EBR address-setup path
  (PC/redirect -> AD) was never measured, and it is a different path; F1 cannot be
  declared closed until it is.
- The 75.76 MHz EBR -> multiply -> EBR probe does not measure the DSP's standalone
  clock limit. A 3.9 ns multiply does not pin the core near 100 MHz if its result
  is registered before the result-select tree.
- It asked for the same thing the previous review did: board timing first. Nothing
  in this repo has run on hardware, so every projection here is static.
