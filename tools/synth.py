"""Synthesis + place-and-route metrics for a risc18 top.

    python tools/synth.py --top risc18.tops:DecoderTop --name m0_decoder

Elaborates the top with Amaranth, converts to Verilog, runs yosys
``synth_ecp5``, then nextpnr-ecp5 for the Colorlight 5A-75B
(LFE5U-25F, CABGA256, speed grade 6).  Writes ``build/metrics/<name>.json``
and ``build/metrics/<name>.md`` and prints a summary.

The top must expose a ``ports`` attribute listing its Signals (the same list
passed to ``verilog.convert``); the first port should be the clock.
"""

import argparse
import importlib
import json
import pathlib
import re
import subprocess
import sys
import warnings

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))
warnings.filterwarnings("ignore")

from amaranth.back import verilog  # noqa: E402

DEVICE = "--25k"
PACKAGE = "CABGA256"
SPEED = "6"


def load_top(spec: str):
    mod_name, _, attr = spec.partition(":")
    if not attr:
        raise SystemExit(f"--top must be module:attr, got {spec!r}")
    mod = importlib.import_module(mod_name)
    top = getattr(mod, attr)()
    ports = getattr(top, "ports", None)
    if ports is None:
        raise SystemExit(f"{spec} has no .ports attribute")
    return top, ports


def run(cmd, log: pathlib.Path):
    with open(log, "w") as fh:
        proc = subprocess.run([str(c) for c in cmd], stdout=fh,
                              stderr=subprocess.STDOUT, text=True)
    text = log.read_text()
    if proc.returncode != 0:
        print(text[-4000:])
        raise SystemExit(f"command failed ({proc.returncode}): {' '.join(map(str, cmd))}")
    return text


def parse_util(text: str):
    util = {}
    for name in ("LUT4", "TRELLIS_FF", "DP16KD", "MULT18X18D", "CCU2C"):
        m = (re.search(rf"Total {name}s:\s+(\d+)/\s*(\d+)", text)
             or re.search(rf"{name}:\s+(\d+)/\s*(\d+)", text))
        if m:
            util[name] = {"used": int(m.group(1)), "total": int(m.group(2))}
    return util


def _norm_clock(name: str) -> str:
    """Normalise a nextpnr clock name so pre/post-global-promotion reports match.

    nextpnr may report the placement estimate as ``clk`` and the routed result
    as ``$glbnet$clk$TRELLIS_IO_IN``; both are the same clock.
    """
    name = name.lstrip("\\")
    for prefix in ("$glbnet$",):
        if name.startswith(prefix):
            name = name[len(prefix):]
    for suffix in ("$TRELLIS_IO_IN",):
        if name.endswith(suffix):
            name = name[:-len(suffix)]
    return name


def parse_fmax(text: str):
    """Return one entry per clock, keeping the last (post-route) report.

    nextpnr prints a placement-time estimate and then the routed result; only
    the routed one is the milestone evidence.
    """
    found = {}
    order = []
    for name, mhz, verdict, target in re.findall(
            r"Max frequency for clock '([^']+)': ([\d.]+) MHz \((\w+) at ([\d.]+) MHz\)",
            text):
        key = _norm_clock(name)
        if key not in found:
            order.append(key)
        found[key] = {"clock": name, "fmax_mhz": float(mhz),
                      "constraint_mhz": float(target), "met": verdict == "PASS"}
    return [found[key] for key in order]


def parse_critical(text: str):
    m = re.search(r"Critical path report[^\n]*\n(.*?)(?=\nInfo: Critical path|\nInfo: Total|$)",
                  text, re.S)
    if not m:
        return None
    block = m.group(1)
    head = [ln.replace("Info:", "").strip()
            for ln in block.splitlines() if ln.strip().startswith("Info:")][:12]
    split = re.search(r"Info:\s+([\d.]+)\s+ns logic,\s+([\d.]+)\s+ns routing", block)
    return {
        "head": head,
        "logic_ns": float(split.group(1)) if split else None,
        "routing_ns": float(split.group(2)) if split else None,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--top", required=True, help="module:attr exposing .ports")
    ap.add_argument("--name", required=True, help="metrics file stem")
    ap.add_argument("--freq", type=float, default=60.0)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--no-pnr", action="store_true",
                    help="stop after yosys (unit tops without a clock)")
    ap.add_argument("--out", default="build")
    args = ap.parse_args()

    out = ROOT / args.out / "synth" / args.name
    out.mkdir(parents=True, exist_ok=True)
    metrics_dir = ROOT / args.out / "metrics"
    metrics_dir.mkdir(parents=True, exist_ok=True)

    top, ports = load_top(args.top)
    v = verilog.convert(top, name="top", ports=ports)
    (out / "top.v").write_text(v)

    run(["yosys", "-q", "-p",
         f"read_verilog {out/'top.v'}; synth_ecp5 -top top -json {out/'top.json'}"],
        out / "yosys.log")
    (out / "top.lpf").write_text(
        f"FREQUENCY NET \"clk\" {args.freq} MHz;\n")

    report = {
        "top": args.top,
        "name": args.name,
        "freq_constraint_mhz": args.freq,
        "device": f"LFE5U-25F-6{PACKAGE}",
        "verilog_bytes": len(v),
        "yosys_json_bytes": (out / "top.json").stat().st_size,
    }

    if args.no_pnr:
        pnr_log = run(["nextpnr-ecp5", "--version"], out / "nextpnr_version.log")
        report["pnr"] = None
        report["nextpnr"] = pnr_log.strip().splitlines()[0] if pnr_log else None
    else:
        text = run([
            "nextpnr-ecp5", "--json", out / "top.json", "--lpf", out / "top.lpf",
            DEVICE, "--package", PACKAGE, "--speed", SPEED,
            "--freq", args.freq, "--seed", args.seed, "--timing-allow-fail",
            "--lpf-allow-unconstrained",
            "--placer", "heap", "--router", "router2",
            "--report", out / "report.json", "--textcfg", out / "top.config",
        ], out / f"pnr_seed{args.seed}.log")
        run(["ecppack", "--bootaddr", "0", out / "top.config", out / "top.bit"],
            out / "ecppack.log")
        report["util"] = parse_util(text)
        report["fmax"] = parse_fmax(text)
        report["critical"] = parse_critical(text)
        report["bitstream"] = str((out / "top.bit").relative_to(ROOT))

    (metrics_dir / f"{args.name}.json").write_text(json.dumps(report, indent=2) + "\n")

    lines = [f"# {args.name}", "",
             f"top: `{args.top}`  ", f"device: `{report['device']}`  ",
             f"constraint: {args.freq} MHz", ""]
    if report.get("util"):
        lines += ["| resource | used | total |", "|---|---:|---:|"]
        for name, u in report["util"].items():
            lines.append(f"| {name} | {u['used']} | {u['total']} |")
        lines.append("")
    if report.get("fmax"):
        for f in report["fmax"]:
            lines.append(f"* fmax `{f['clock']}`: {f['fmax_mhz']} MHz "
                         f"(constraint {f['constraint_mhz']} MHz, "
                         f"{'met' if f['met'] else 'missed'})")
        lines.append("")
    if report.get("critical") and report["critical"]["logic_ns"] is not None:
        c = report["critical"]
        lines.append(f"* critical path: logic {c['logic_ns']} ns, "
                     f"routing {c['routing_ns']} ns")
    (metrics_dir / f"{args.name}.md").write_text("\n".join(lines) + "\n")

    print(f"[{args.name}] {args.top}")
    for name, u in report.get("util", {}).items():
        print(f"  {name:12s} {u['used']:6d}/{u['total']:6d}")
    for f in report.get("fmax", []):
        print(f"  fmax {f['clock']}: {f['fmax_mhz']} MHz "
              f"({'met' if f['met'] else 'missed'} {f['constraint_mhz']} MHz)")
    if report.get("critical") and report["critical"]["logic_ns"] is not None:
        print(f"  crit: {report['critical']['logic_ns']} ns logic, "
              f"{report['critical']['routing_ns']} ns routing")
    print(f"  metrics: {metrics_dir/args.name}.json")


if __name__ == "__main__":
    main()
