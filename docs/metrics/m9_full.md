# m9_full

top: `risc18.tops:FullTop`  
device: `LFE5U-25F-6CABGA256`  
constraint: 25.0 MHz

| resource | used | total |
|---|---:|---:|
| LUT4 | 1780 | 24288 |
| TRELLIS_FF | 670 | 24288 |
| DP16KD | 2 | 56 |
| MULT18X18D | 1 | 28 |

* fmax `$glbnet$boot_clk`: 73.67 MHz (constraint 60.0 MHz, met) -- nextpnr's derived constraint for the PLL-generated clock (computed from the PLL parameters, not from --freq); the PLL's CLKOP is 60.000 MHz

* critical path: logic 2.0 ns, routing 11.6 ns
