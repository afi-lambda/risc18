# m5_core

top: `risc18.tops:CoreTop`  
device: `LFE5U-25F-6CABGA256`  
constraint: 60.0 MHz

| resource | used | total |
|---|---:|---:|
| LUT4 | 1914 | 24288 |
| TRELLIS_FF | 521 | 24288 |
| DP16KD | 1 | 56 |
| MULT18X18D | 1 | 28 |

* fmax `$glbnet$clk$TRELLIS_IO_IN`: 53.89 MHz (constraint 60.0 MHz, missed)

* critical path: logic 3.7 ns, routing 14.9 ns
