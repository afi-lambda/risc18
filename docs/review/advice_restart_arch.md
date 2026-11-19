<!-- openai/gpt-6.2-sol, 2026-11-19 09:31, effort high, usage {'prompt_tokens': 2432, 'completion_tokens': 5719, 'total_tokens': 8151, 'cost': 0.0632685, 'is_byok': False, 'prompt_tokens_details': {'cached_tokens': 0, 'cache_write_tokens': 2429, 'audio_tokens': 0, 'video_tokens': 0}, 'cost_details': {'upstream_inference_cost': 0.0632685, 'upstream_inference_prompt_cost': 0.0060785, 'upstream_inference_completions_cost': 0.05719}, 'completion_tokens_details': {'reasoning_tokens': 3448, 'image_tokens': 0, 'audio_tokens': 0}} -->

The throughput target is off: **120 MHz at CPI 1.5 is 80 MIPS, not 100 MIPS.** That is still a 76% gain over 45.5 MIPS. Getting 100 MIPS requires CPI 1.2 at 120 MHz or 150 MHz at CPI 1.5. The existing throughput calculations check out. Also, 18-bit width does not by itself require two DP16KDs per memory: one block holds 1024×18. A second block needs a depth, port, or replication requirement. Finally, the 145–150 MHz probe is a read-stage result for this mapping and placement, not a hard silicon limit.

### 1. Memory and the 120 MHz premise

**Use a registered EBR response as the foundation, but make 120 MHz a signoff target, not an assumption.** At 120 MHz the period is 8.33 ns. The isolated 6.7–6.9 ns read stage leaves 1.4–1.6 ns over the probe for additional routing and other implementation costs. The 9.0 ns read–add–write probe already fails before you add a realistic operand network. The split is the right way to buy timing margin without giving up one request per cycle.

Define the contract by edges, not by calling a RAM “latency 2.” Let X form the address in time for the EBR to sample it at the X/M edge. Let the EBR output travel to a nearby response register at the M/W edge. The memory is then **off the ALU path**: X drives memory address setup, and W exposes registered load data. Another fabric address register before the EBR would change that schedule. A store does not inherently need a read-response stage.

Keep distributed RAM for the register file. The probes rule out *that measured 16:1 mux implementation* on the critical path, not every possible register-file design, but there is no reason to replace the known-good structure now. Treat explicit EBR `OUTREG` as a later experiment: establish its actual edge-to-edge latency and primitive behavior before treating it as a drop-in way to shorten the period. Keep a lower-clock, lower-CPI five-stage implementation as the comparison. For example, 100 MHz at CPI 1.25 ties the proposed 80 MIPS.

### 2. Split the X cone by operand readiness

**Register MUL, keep effective-address generation and branch comparison in X, and move direct branch-target generation earlier.** The 9.85 ns probe is the combined cone *with* the unregistered multiply result. Registering that result gets the probe to 7.7 ns, only 0.63 ns inside the 120 MHz budget. That makes the cut worthwhile, but it does not sign off the populated X stage.

For a PC-relative branch, the target depends on the PC and displacement, not on forwarded register operands. Compute it in D if it fits there, register it, and let X select the redirect using the comparison result. That move need not add a bubble. A register-indirect target is different: it has a real operand dependency. Keep the data-memory address in X; moving it later normally adds a cycle to load-use distance unless you recover a boundary elsewhere.

I would **not** put the general branch compare in D. That asks D to do decode, distributed-RAM read, bypass selection, and compare, precisely when the freshest operand may still be produced in X. An adjacent ALU-to-branch dependency then needs either a long X-to-D combinational path or a stall. A load-dependent branch likewise needs its value a stage earlier than an X-stage consumer would. Comparing in D but still redirecting in X pays that cost without necessarily saving a recovery cycle. For MUL, count one dependent-use bubble only once the registered result and its bypass are shown to arrive on the required edge; measure multiplier issue rate and writeback conflicts separately.

### 3. Constrain endpoints before drawing a region

First, constrain the **actual board pins** and verify the PLL, global clock route, and derived clock in the full shell. If there is an external reset, constrain its real pin, but fix the reset distribution architecturally: bring release into the core clock domain and use local registered valid/control to inhibit stores. A reset pin LOC does not solve a fabric reset fan-out path.

Second, anchor the **data EBR** at a legal site with suitable nearby fabric, then check the placement of the X address drivers and memory response registers. Keep the critical launch flops, operand selection, and first consumers close; the measured `dx_b` nets tell you where to look. If branch and memory consumers pull the operand bus in different directions, try local registered copies and verify that synthesis preserves them. Only then constrain a DSP or the instruction EBR if the timing reports show a reason. The instruction EBR is not currently the limiter.

Third, try a **loose, selective region** against an anchored but otherwise unconstrained build, over the same seeds. Verify the region against the actual packed cells and inspect the placed BELs; a source-level hierarchy name is not proof that the intended cells were constrained. Leave room for legal carry-chain and LUTRAM packing and for placement around the fixed hard blocks.

The subtle failure mode is a **legal placement with worse routes**. A region constrains cell sites, not the wires between them, and does not by itself reserve the area against unrelated logic. A tight region can crowd access to the EBR, force detours, or split a critical path across the region boundary. Low whole-chip utilization does not prevent that. Compare path endpoints, dominant nets, and worst-seed timing in the full top; do not keep a region just because it produces one good seed.

### 4. Load-use, store data, and writeback

With the edge contract above, a load in X in cycle N launches the EBR read at the end of N. It is in M during N+1, and its **registered** value is available from W at the start of N+2. A W-to-X bypass therefore permits a dependent instruction in X in N+2: **one bubble for an adjacent consumer, none with one independent instruction in between.** If instead W is the cycle in which the raw EBR output is still arriving, you cannot count that same schedule without putting EBR clock-to-out and consumer logic back on one path.

W can drive the register-file write and the bypass in the same cycle. Both should use the W register; the register file commits at the closing edge of W. Do not rely on reading the just-written distributed RAM at the right instant. Give the bypass explicit priority for the youngest valid matching producer, and time it as part of X.

A store has two distinct dependencies. Its **base** is needed in X and obeys the ordinary load-use rule. Its **payload** is needed only by the write sampling edge. If you deliberately put that edge later, an adjacent load-to-store payload dependency can avoid a bubble through late forwarding, provided you carry the store address, source identity, and valid state to that point. The cost is a port and ordering decision: can a younger read launch while the older store writes? If not, count the structural stall. If so, define same-address store-to-load forwarding or interlock it rather than relying on EBR read-during-write behavior.

### 5. Branch budget and prediction

With ideal CPI 1 and two lost cycles for every taken branch, taken branches can account for at most **25% of retired instructions**. If branch density is \(b\) and taken fraction is \(t\), the branch contribution is \(2bt\). At 60% taken, the branch-density limit is 41.7% with no other stalls. With \(s\) CPI already spent on other stalls, the limit is \(bt \le (0.5-s)/2\).

With backward-taken prediction, substitute the **misprediction fraction** for the taken fraction only if correct predictions really cost zero cycles. Predicting taken after the instruction reaches D does not by itself get the target to the fetch address in time. Count late-target bubbles separately, and verify the claimed two-cycle recovery on a cycle trace: X resolution and the labels F/IQ/D do not, by themselves, establish how many issue slots are lost.

Backward-taken is a reasonable first policy, not a reason to add a stage. If cutting recovery from two cycles to one saves 0.10 CPI on the workload, going from 1.50 to 1.40 can tolerate only about a 6.7% clock loss before the gain disappears. Measure the actual redirect cost before buying an earlier compare, an earlier target mechanism, or another stage.

### 6. The likely MIPS failure

The first thing I would look for is **extra dependency stalls hidden by the stage diagram**. A load may have the advertised memory latency while its consumer still waits for register-file writeback because the W-to-X bypass is missing or cannot meet timing. A conservative scoreboard can also stall independent instructions behind loads or MULs, and a store-data interlock can impose an X-stage stall for a value that is not needed until later. These costs stack with redirect bubbles on real instruction streams.

At 80 MHz, the core needs CPI below about **1.76** to beat 45.5 MIPS. At a genuine 120 MHz, the break-even CPI is about **2.64**. So the most plausible failure is a partial clock gain coupled with the full latency cost. Keep the 317 correctness tests, but add cycle checks for adjacent dependencies and measure retired instructions and lost cycles by cause on the same binaries as the baseline.

### 7. Ranked decision

**First, prove the timing and cycle contract together.** Build a representative slice with the real distributed RF, bypass fan-out, X address generation, data EBR, and W capture. Include ALU-to-branch, load-to-use, load-to-store-data, and MUL dependencies in the cycle checks. A bare read probe cannot answer whether the bypassed core closes timing.

**Second, close placement incrementally in the full shell.** Start with correct board and clock constraints, then anchor the data-memory path, then add a local region only if it improves repeatable timing. Keep the five-stage baseline in the same throughput comparison.

**Third, spend changes on the measured CPI losses.** Implement the simple predictor, measure target delivery and recovery, and improve whichever cost actually dominates. Hold the restart to **80 MIPS at the stated signoff point**, not just a 120 MHz timing report.

**Kill** the 120 MHz single-cycle EBR–ALU–EBR path, the unregistered multiply in the result select, and the blanket first-commit region. Do not commit to a D-stage compare or another pipeline stage until it wins in measured MIPS.
