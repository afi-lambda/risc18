# m11_full_ram

top: `risc18.tops:FullTop`  
device: `LFE5U-25F-6CABGA256`  
constraint: 25.0 MHz

| resource | used | total |
|---|---:|---:|
| LUT4 | 1425 | 24288 |
| TRELLIS_FF | 437 | 24288 |
| DP16KD | 2 | 56 |
| MULT18X18D | 1 | 28 |

* fmax `$glbnet$boot_clk`: 68.16 MHz (constraint 60.0 MHz, met) -- nextpnr's derived constraint for the PLL-generated clock (computed from the PLL parameters, not from --freq); the PLL's CLKOP is 60.000 MHz

* critical path: logic 7.3 ns, routing 7.4 ns
