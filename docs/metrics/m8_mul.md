# m8_mul

top: `risc18.tops:MulTop`  
device: `LFE5U-25F-6CABGA256`  
constraint: 60.0 MHz

| resource | used | total |
|---|---:|---:|
| LUT4 | 0 | 24288 |
| TRELLIS_FF | 54 | 24288 |
| DP16KD | 0 | 56 |
| MULT18X18D | 1 | 28 |

* fmax `$glbnet$clk$TRELLIS_IO_IN`: 141.4 MHz (constraint 60.0 MHz, met)

* critical path: logic 4.5 ns, routing 2.6 ns
