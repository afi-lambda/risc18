# m11_full_s1

top: `risc18.tops:FullTop`  
device: `LFE5U-25F-6CABGA256`  
constraint: 25.0 MHz

| resource | used | total |
|---|---:|---:|
| LUT4 | 1295 | 24288 |
| TRELLIS_FF | 445 | 24288 |
| DP16KD | 2 | 56 |
| MULT18X18D | 1 | 28 |

* fmax `$glbnet$boot_clk`: 73.4 MHz (constraint 600.24 MHz, missed)

* critical path: logic 7.0 ns, routing 6.6 ns
