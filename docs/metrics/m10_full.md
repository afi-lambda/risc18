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

* fmax `$glbnet$boot_clk`: 73.45 MHz (constraint 600.24 MHz, missed) -- nextpnr's own derived constraint for the PLL-generated clock (it applies CLKFB_DIV without CLKOP_DIV), not the 25.0 MHz the design is constrained at

* critical path: logic 5.8 ns, routing 7.8 ns
