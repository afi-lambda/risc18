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

## Review: restart architecture (`prompt_restart_arch.md` -> `advice_restart_arch.md`)

openai/gpt-6.2-sol, 2026-11-19, effort high, **reasoning_tokens 3448** (unlike the
earlier recipe/deeper-pipeline runs which returned 0), 2432 prompt / 5719 completion,
$0.0633, 57.7 s. Effort was applied this time; the earlier caveat does not carry over.

### (a) What the brief got wrong

1. **Throughput arithmetic.** The brief said 120 MHz at CPI 1.5 is ~100 MIPS. It is 80
   (120/1.5). 100 MIPS needs 150 MHz at CPI 1.5 or 120 MHz at CPI 1.2. Settled by
   arithmetic; the plan doc's signoff line and P4 were corrected to 80 MIPS.
2. **"2 DP16KD per memory".** One DP16KD is 1024x18, and the current core uses **one per
   memory, two total** (`m11_full_ram`, `m12_full6_s7`: `DP16KD 2/56`). Settled by the
   routed utilisation tables.
3. **Framing the 145-150 MHz probe as a ceiling.** It is a read-stage result for this
   mapping and placement, not a silicon limit. The block floor (5.6 ns) is real; the rest
   is place-and-route-dependent.

### (b) What the answer could not know, and what we should not accept uncritically

- It ranks "constrain the actual board pins first". The repo flow has **no** pin
  constraints at all today and the board loads and runs, so this is a correctness-and-
  reproducibility argument, not a timing one; it is cheap, so it moves into P1 anyway.
- Its reset advice (release into the core clock domain, local valid gates the store) is
  already the `Full6Top` shape; the 8.18-9.94 ns reset path is a `Core6Top`-only artefact.
- It treats the 317-test suite as a correctness net and asks for cycle checks on top.
  Agreed, and that is the P0b slice: a bare read probe cannot prove a bypassed core closes.

### (c) Levers: executed, parked, killed

- **Killed: single-cycle EBR -> ALU -> EBR at 120 MHz.** 9.0 ns for an add alone, 5.6 ns
  of it the DP16KD clock-to-out. Also killed: `REGMODE=OUTREG` as the foundation (kept as
  a later experiment on one hot instruction).
- **Killed: blanket first-commit region constraint.** Replaced with endpoints-first
  (pins/PLL/global clock, anchor the data EBR, then a loose selective region tested
  against an unconstrained build over the same seeds). Its named failure mode — a legal
  placement with worse routes — is the reason a single good seed is not acceptance.
- **Killed: compare in D.** The review's argument is sound and beats mine: D already
  carries decode, distributed-RAM read and bypass select, and the freshest operand may
  still be produced in X, so an ALU-to-branch dependency would need a long X-to-D path or
  a stall. Replaced with: compare stays in X, **PC-relative target moves to D** (it needs
  PC + displacement, not forwarded operands), register-indirect target stays in X.
- **Parked: the 150 MHz stretch.** Block-limited at 145.10 MHz worst seed on a bare probe;
  120 MHz is the committed signoff.
- **Adopted: W-to-X bypass with a one-bubble adjacent load-use**, store payload forwarded
  to the write-sampling edge with an explicit ordering decision, and the branch budget
  `b*t <= (0.5 - s)/2` as the thing to measure rather than assume.

## Review round 2: the 5.6 ns EBR read cost (`prompt_ebr_round2.md`)

openai/gpt-6.2-sol, effort high requested. **`reasoning_tokens: 0` this run** (13.9 s,
1272 prompt / 1504 completion, $0.0182) — the endpoint accepted the flag and did not
honour it, so this is a fast completion, not a deliberation. Round 1 on the same model
returned 3448 reasoning tokens, so the behaviour is inconsistent run to run, not a
property of the model. Treat this answer as usable but not as evidence about what high
effort would have found.

What it settled, and where it corrected me:

1. **5.6 ns is plausible** for an unregistered (NOREG) read on a -6 ECP5, and it has no
   verified vendor number to replace it with. My "the datasheet may be materially
   better" hypothesis is unsupported as stated. The check that would move it is a
   **Diamond post-route report** for the same device, speed grade, 1024x18, NOREG mode,
   reading the launch arc separately from routing and destination setup.
2. **My 6.4 ns "if it were 3 ns" arithmetic is a sensitivity estimate, not a budget.**
   9.0 - 5.6 + 3.0 assumes the 3.0 replaces exactly that arc and nothing else; the 1.9 ns
   net already in the path leaves only 1.5 ns for the carry chain, the rest of the
   routing and setup. Correct: use the full path report, never a second sum of
   components. (Also corrected: "21 % above 120 MHz" is 1.44 ns of slack, not a
   frequency-only statement.)
3. **The OUTREG experiments prove nothing about OUTREG's speed**, only that nextpnr has
   no model for it. Already recorded above; this is the confirmation.

Its experiment for model-vs-silicon, if we want it: sweep the *functional* read path
(complementary EBR contents, alternating addresses so the outputs toggle every read),
same capture flop and route at every point, sticky error flag read over UART in a stable
domain, a checker control that bypasses the EBR, and legal PLL steps bracketed by
repeats. Near 150 MHz one 1 MHz step is ~44 ps of period — nominal, not accuracy. The
result is a pass/fail threshold for clock-to-DO + route + capture, not a tens-of-ps
measurement, and its failure mode is finding the launch/PLL/checker/write limit instead
of the read path. It explicitly rejects a ring oscillator or delayed replica as more
calibration for less decision value.

Decision criterion it gave for single-cycle vs split: close the **integrated** core
across representative placements, then compare CPI and resources; single-cycle at a real
6.4 ns leaves ~1.9 ns for integration at 120 MHz (plausible) and ~0.27 ns at 150 MHz
(not plausible). It also states the split is not yet a robust 150 MHz answer in our
seeds — which matches 145.10 MHz worst.

Ranked non-toolchain options: distributed RAM for a small critical file (combinational
read, costs LUTs and mux levels, replicate banks for independent read addresses and
write them all) > hot operands kept in fabric registers with bulk in EBR (helps hits
only) > 36-bit packing (no independent read address, does not remove the read stage) >
two-bank interleave (bandwidth, not latency; return mux can make it worse) > narrowing
(does not lower clock-to-DO).

New failure mode, and it is a good one: **paying for the second stage without getting
one operation per cycle.** A dependent instruction can launch its EBR read before the
previous writeback is visible; stalls then push dependent code to one op per two cycles,
while forwarding puts tag compares, priority muxes and writeback fanout on paths the
probe never contained. A late stall or redirect can also leave a stale read in flight
unless valid and destination travel with it. The test that settles it is a back-to-back
dependent instruction stream, not a static timing report.

### Same brief, re-issued: what high effort actually bought

The first run returned `reasoning_tokens: 0` in 13.9 s. The identical brief re-sent
returned **4520 reasoning tokens, 6246 completion, 123.9 s, $0.0656** — so the earlier
"effort not demonstrable" was endpoint flakiness, and the fast answer was a fast
completion. Same verdict on every headline question, better content in four places:

1. **Mechanism, not just plausibility.** NOREG bypasses the output register, not the
   memory-array read — the modelled clock-to-output arc still includes getting the
   addressed word to DOA, so it is not comparable to a fabric flop's clock-to-Q. That is
   *why* 5.6 ns is a believable budget rather than a suspicious one.
2. **A cheaper and sharper experiment than the clock sweep.** Fixed EBR launch clock,
   a second same-frequency PLL output shifted by fine phase into the capture flop; run
   it at 60 MHz because the point is the launch-to-capture interval, not running near
   failure. Fine phase is 1/8 VCO period = **0.21 ns at a 600 MHz VCO**. Alternate
   complementary words, count old-word and new-word captures at each phase in both
   transition directions, keep placement and routing fixed, and run a nearby fabric-flop
   reference with the same clock pair to expose clock insertion offset. It measures
   EBR clock-to-DO + data route + capture setup as a **transition band**, not a
   threshold, and can see a ~2.6 ns discrepancy if the offsets are controlled. Both
   runs reject a ring oscillator or self-timed counter.
3. **A genuinely new ranked option — issue the EBR read earlier when the address is
   already known.** It moves the read stage instead of speeding it up; costs address,
   tag and valid state, possibly a buffer, and a wasted read slot on redirect; applies
   to predictable fetches and table lookups, not to a late data-dependent address. The
   retry ranks it second, between distributed RAM and 36-bit packing, and drops the
   fast run's "hot operands in fabric registers".
4. **Numbers where the fast run had adjectives.** Distributed RAM: about one storage
   LUT per bit per 16-deep bank per read copy, so a 16x18 two-read one-write file is
   ~36 storage LUTs before overhead, writes broadcast to both copies, and replication
   does not buy extra write ports. 36-bit mode is 512x36 and pseudo-dual-port, not two
   independently addressed read ports; a half-word update can cost read-modify-write.

Both runs agree on the criterion (single-cycle only if the *integrated* core closes and
the shorter latency removes real stalls; a room-temperature measurement never buys
worst-case slack) and on the failure mode (a dependent stream that turns two-cycle
latency into a two-cycle issue interval, with forwarding on the EBR-to-split-flop path
spending the 1.44 ns of 120 MHz slack). The retry adds the hazard specifics: a consumer
reading a destination whose write is still in flight, including read and write
coinciding, and the requirement to route a short dependent ALU chain that exercises
same-address read/write before freezing the contract.
