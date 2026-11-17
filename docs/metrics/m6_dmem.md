# m6_dmem

top: `risc18.tops:DataMemoryTop`  
device: `LFE5U-25F-6CABGA256`  
constraint: 60.0 MHz

| resource | used | total |
|---|---:|---:|
| LUT4 | 24 | 24288 |
| TRELLIS_FF | 84 | 24288 |
| DP16KD | 1 | 56 |
| MULT18X18D | 0 | 28 |

* fmax `$glbnet$clk$TRELLIS_IO_IN`: 261.03 MHz (constraint 60.0 MHz, met)

* critical path: logic 1.4 ns, routing 2.4 ns
