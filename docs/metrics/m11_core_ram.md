# m11_core_ram (seed 1, --ram-rf)

top: `risc18.tops:CoreTop`  
device: `LFE5U-25F-6CABGA256`  
constraint: 60.0 MHz

| resource | used | total |
|---|---:|---:|
| LUT4 | 1183 | 24288 |
| TRELLIS_FF | 407 | 24288 |
| DP16KD | 2 | 56 |
| MULT18X18D | 1 | 28 |

* fmax `$glbnet$clk$TRELLIS_IO_IN`: 73.48 MHz (constraint 60.0 MHz, met)

* critical path: logic 6.8 ns, routing 6.8 ns
