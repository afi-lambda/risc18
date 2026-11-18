# m10_full

top: `risc18.tops:FullTop`  
device: `LFE5U-25F-6CABGA256`  
constraint: 25.0 MHz

| resource | used | total |
|---|---:|---:|
| LUT4 | 1620 | 24288 |
| TRELLIS_FF | 670 | 24288 |
| DP16KD | 2 | 56 |
| MULT18X18D | 1 | 28 |

* fmax `$glbnet$boot_clk`: 73.45 MHz (missed against nextpnr's PLL-derived constraint) -- historical row: this build is of the M10-era FullTop RTL and cannot be re-rendered at this head, and the constraint nextpnr derived for it came from the PLL divider set that stage 4 replaced (see `docs/RESULTS.md`, "What nextpnr does with the PLL-generated clock"). The fmax number is the post-route delay; the verdict is not a verdict about this design.

* critical path: logic 5.8 ns, routing 7.8 ns
