# Metrics conventions

`tools/synth.py` reports post-place-and-route numbers from nextpnr for the
Colorlight 5A-75B device (LFE5U-25F-6CABGA256, speed grade 6) at the stated
clock constraint. The `.md` files in this directory are copied verbatim from
`build/metrics/<name>.md`; if they disagree with a fresh run, the committed
file is wrong.

Caveats that apply to every unit milestone (M0–M4):

* Unit tops in `src/risc18/tops.py` are measurement wrappers. They register all
  inputs and outputs (so nextpnr has an interior register-to-register path) and
  are synthesis-only: the `sync` domain is reset-less and driven by the `clk`
  port, so the netlist has one clock port and no reset network.
* Because of that wrapping, part of the reported LUT/FF count belongs to the
  wrapper, not the unit. The `.md` numbers are therefore an upper bound for the
  unit and are not directly comparable to the M5+ `Core` metrics.
* M9 re-measures the full core with the PLL and the real reset network; treat
  that as the authoritative resource and timing table.
* `RegFileTop` specifically: 15 × 18 = 270 storage FFs (r0's storage is
  trimmed), 31 input pipeline FFs and 36 output FFs, i.e. 67 of the 337 FFs are
  the measurement shell. Two 16:1 read muxes dominate the LUT count; a binary
  tree over the index bits was measured at ~700 LUT4 with identical behaviour
  and is a candidate for M9, not an M1 defect.
