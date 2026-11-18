# m9_full

top: `risc18.tops:FullTop`  
device: `LFE5U-25F-6CABGA256`  
constraint: 25.0 MHz

| resource | used | total |
|---|---:|---:|
| LUT4 | 2013 | 24288 |
| TRELLIS_FF | 635 | 24288 |
| DP16KD | 2 | 56 |
| MULT18X18D | 1 | 28 |

* fmax `$glbnet$boot_clk`: 73.88 MHz (constraint 600.24 MHz, missed)

* critical path: logic 6.1 ns, routing 7.5 ns
