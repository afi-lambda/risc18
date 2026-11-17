# m0_decoder

top: `risc18.tops:DecoderTop`  
device: `LFE5U-25F-6CABGA256`  
constraint: 60.0 MHz

| resource | used | total |
|---|---:|---:|
| LUT4 | 169 | 24288 |
| TRELLIS_FF | 72 | 24288 |
| DP16KD | 0 | 56 |
| MULT18X18D | 0 | 28 |

* fmax `$glbnet$clk$TRELLIS_IO_IN`: 176.58 MHz (constraint 60.0 MHz, met)

* critical path: logic 1.0 ns, routing 4.6 ns
