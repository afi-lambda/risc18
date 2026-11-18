# Speed/area review — provenance and corrections

`prompt_speed_area.md` was sent to `openai/gpt-6.2-sol` with
`"reasoning": {"effort": "high"}` on 2026-11-18; the answer is
`advice_speed_area.md`. Usage: 3090 prompt + 7965 completion (4388 of them
reasoning) tokens, $0.087, about 2 minutes.

## Three things the brief got wrong (confirmed against the build)

1. **"1561 general logic + 126 carry LUT4" is not a decomposition of 1813.**
   The real accounting is 1687 `LUT4` cells (all general logic) plus 126 *sites*
   consumed by 63 `CCU2C` carry cells, i.e. 1687 + 2x63 = 1813. FullTop:
   1745 + 2x76 = 1897. `docs/RESULTS.md` already states it that way
   ("logic LUTs / carry LUTs 1687 / 126"); the brief restated it wrongly.
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
- **The PLL question is already settled.** `FEEDBK_PATH=CLKOP` with
  `CLKI_DIV=1 / CLKFB_DIV=24 / CLKOP_DIV=10` gives VCO 600 MHz and CLKOP
  60.000 MHz; nextpnr's own derived 600.24 MHz is the VCO figure it
  mislabels as the output. The packed bitstream was decoded word by word
  (CLKFB_DIV 0010111 = 23, CLKOP_DIV 0001001 = 9, FEEDBK_PATH CLKOP).

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
