# m2_alu

top: `risc18.tops:AluTop`  
device: `LFE5U-25F-6CABGA256`  
constraint: 60.0 MHz

| resource | used | total |
|---|---:|---:|
| LUT4 | 339 | 24288 |
| TRELLIS_FF | 57 | 24288 |
| DP16KD | 0 | 56 |
| MULT18X18D | 0 | 28 |

* fmax `$glbnet$clk$TRELLIS_IO_IN`: 101.95 MHz (constraint 60.0 MHz, met)

* critical path: logic 2.5 ns, routing 7.3 ns
