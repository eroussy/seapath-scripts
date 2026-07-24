#!/usr/bin/env python3
# Copyright (C) 2026 Savoir-faire Linux Inc.
# SPDX-License-Identifier: Apache-2.0
"""Report network-device IRQ affinity and threaded IRQ scheduler state."""

import argparse
import json
import os
import re
import sys
from pathlib import Path


PROC = Path("/proc")
SYS_NET = Path("/sys/class/net")

POLICIES = {
    0: "SCHED_OTHER",
    1: "SCHED_FIFO",
    2: "SCHED_RR",
    3: "SCHED_BATCH",
    5: "SCHED_IDLE",
    6: "SCHED_DEADLINE",
}

COLORS = {
    "reset": "\033[0m",
    "bold": "\033[1m",
    "cyan": "\033[36m",
    "yellow": "\033[33m",
    "red": "\033[31m",
}


def colorize(value, *colors):
    if not colors:
        return value
    return "".join(COLORS[color] for color in colors) + value + COLORS["reset"]


def scheduler_colors(scheduler):
    if scheduler == "SCHED_FIFO":
        return "red", "bold"
    if scheduler == "SCHED_RR":
        return "yellow", "bold"
    return ()


def read_text(path):
    try:
        return path.read_text().strip()
    except (FileNotFoundError, PermissionError, ProcessLookupError, OSError):
        return None


def read_status(pid, tid):
    text = read_text(PROC / str(pid) / "task" / str(tid) / "status")
    if text is None:
        return None

    fields = {}
    for line in text.splitlines():
        key, separator, value = line.partition(":")
        if separator:
            fields[key] = value.strip()
    return fields


def read_stat(pid, tid):
    text = read_text(PROC / str(pid) / "task" / str(tid) / "stat")
    if text is None:
        return None

    # comm may contain spaces and parentheses. Fields after final ')' are fixed.
    closing_paren = text.rfind(")")
    if closing_paren < 0:
        return None
    fields = text[closing_paren + 2 :].split()
    # proc(5): nice=field 19, processor=field 39. Array starts at field 3.
    if len(fields) <= 36:
        return None
    return {"nice": fields[16], "last_cpu": fields[36]}


def scheduler(tid):
    try:
        policy = os.sched_getscheduler(tid)
        rt_priority = os.sched_getparam(tid).sched_priority
    except (AttributeError, OSError):
        return "unknown", None
    return POLICIES.get(policy, f"unknown({policy})"), rt_priority


def ps_priority(policy, rt_priority, nice):
    """Return Linux ps-style priority: normal 0-39, RT 41-139."""
    if policy in ("SCHED_FIFO", "SCHED_RR") and rt_priority is not None:
        return 40 + rt_priority
    try:
        return 19 + int(nice)
    except ValueError:
        return None


def interrupt_lines():
    text = read_text(PROC / "interrupts")
    if text is None:
        return {}, 0

    lines = text.splitlines()
    if not lines:
        return {}, 0
    cpu_count = len(lines[0].split())
    interrupts = {}
    for line in lines[1:]:
        match = re.match(r"\s*(\d+):\s*(.*)", line)
        if match is None:
            continue
        fields = match.group(2).split()
        counts = fields[:cpu_count]
        if len(counts) != cpu_count or not all(value.isdecimal() for value in counts):
            continue
        interrupts[int(match.group(1))] = {
            "per_cpu_counts": {str(cpu): int(value) for cpu, value in enumerate(counts)},
            "description": " ".join(fields[cpu_count:]),
        }
    return interrupts, cpu_count


def device_name(device):
    try:
        return str(device.resolve().relative_to(Path("/sys/devices")))
    except (FileNotFoundError, ValueError, OSError):
        return str(device)


def add_irqs_from_directory(irqs, source, directory):
    try:
        entries = directory.iterdir()
    except (FileNotFoundError, PermissionError, OSError):
        return
    for entry in entries:
        if entry.name.isdecimal():
            irqs.setdefault(int(entry.name), set()).add(source)


def network_devices(interrupts):
    """Return physical network devices and IRQs discovered from sysfs/procfs."""
    devices = {}
    try:
        interfaces = sorted(SYS_NET.iterdir(), key=lambda path: path.name)
    except (FileNotFoundError, PermissionError, OSError):
        return devices

    for interface in interfaces:
        device_link = interface / "device"
        if not device_link.exists():
            continue
        try:
            device = device_link.resolve()
        except (FileNotFoundError, OSError):
            continue

        key = str(device)
        card = devices.setdefault(
            key,
            {
                "device": device_name(device),
                "interfaces": [],
                "irqs": {},
            },
        )
        card["interfaces"].append(interface.name)
        add_irqs_from_directory(card["irqs"], "msi_irqs", device / "msi_irqs")

        legacy_irq = read_text(device / "irq")
        if legacy_irq is not None and legacy_irq.isdecimal():
            card["irqs"].setdefault(int(legacy_irq), set()).add("device_irq")

        # Some non-PCI drivers expose no per-device IRQ directory. Names from
        # /proc/interrupts provide a best-effort fallback for those drivers.
        for irq, details in interrupts.items():
            if interface.name in details["description"]:
                card["irqs"].setdefault(irq, set()).add("proc_interrupts_name")
    return devices


def irq_threads(irqs):
    """Find schedulable IRQ handler threads for requested IRQ numbers."""
    requested = set(irqs)
    threads = {}
    try:
        process_entries = PROC.iterdir()
    except PermissionError:
        return threads

    for process in process_entries:
        if not process.name.isdecimal():
            continue
        pid = int(process.name)
        task_dir = process / "task"
        try:
            task_entries = task_dir.iterdir()
        except (FileNotFoundError, PermissionError, ProcessLookupError, OSError):
            continue
        for task in task_entries:
            if not task.name.isdecimal():
                continue
            tid = int(task.name)
            name = read_text(task / "comm")
            match = re.match(r"irq/(\d+)-", name or "")
            if match is None or int(match.group(1)) not in requested:
                continue

            status = read_status(pid, tid)
            stat = read_stat(pid, tid)
            if status is None or stat is None:
                continue
            policy, rt_priority = scheduler(tid)
            irq = int(match.group(1))
            threads.setdefault(irq, []).append(
                {
                    "pid": pid,
                    "tid": tid,
                    "name": name,
                    "policy": policy,
                    "rt_priority": rt_priority,
                    "priority": ps_priority(policy, rt_priority, stat["nice"]),
                    "last_cpu": int(stat["last_cpu"]),
                    "effective_affinity": status.get("Cpus_allowed_list"),
                }
            )
    return threads


def irq_report(irq, sources, interrupts, threads):
    irq_dir = PROC / "irq" / str(irq)
    interrupt = interrupts.get(irq, {})
    return {
        "irq": irq,
        "sources": sorted(sources),
        "description": interrupt.get("description"),
        "per_cpu_counts": interrupt.get("per_cpu_counts"),
        "configured_affinity": read_text(irq_dir / "smp_affinity_list"),
        "effective_affinity": read_text(irq_dir / "effective_affinity_list"),
        "threads": threads.get(irq, []),
    }


def display(value):
    return "?" if value is None else str(value)


def table_row(irq):
    thread = irq["threads"][0] if irq["threads"] else {}
    return {
        "irq": irq["irq"],
        "tid": display(thread.get("tid")),
        "irq_name": display(irq["description"]),
        "scheduler": display(thread.get("policy")),
        "rtprio": display(thread.get("rt_priority")),
        "prio": display(thread.get("priority")),
        "last_cpu": display(thread.get("last_cpu")),
        "affinity": display(irq["effective_affinity"]),
    }


def build_report(interface_name=None):
    interrupts, cpu_count = interrupt_lines()
    devices = network_devices(interrupts)
    all_irqs = {irq for device in devices.values() for irq in device["irqs"]}
    threads = irq_threads(all_irqs)
    cards = []
    for device in sorted(devices.values(), key=lambda item: (item["device"], item["interfaces"])):
        interfaces = sorted(device["interfaces"])
        if interface_name is not None:
            interfaces = [interface for interface in interfaces if interface == interface_name]
            if not interfaces:
                continue
        irqs = [
            irq_report(irq, sources, interrupts, threads)
            for irq, sources in sorted(device["irqs"].items())
        ]
        for interface in interfaces:
            cards.append(
                {
                    "interface": interface,
                    "device": device["device"],
                    "irqs": [table_row(irq) for irq in irqs],
                }
            )
    return {"interfaces": cards}


def print_interface(interface, colored):
    heading = f"\n=== {interface['interface']} ({interface['device']}) ==="
    print(colorize(heading, "cyan", "bold") if colored else heading)
    header = (
        f"{'IRQ':>6} {'TID':>7}  {'IRQ_NAME':<42} {'SCHEDULER':<15} {'RTPRIO':>6} "
        f"{'PRIO':>4} {'LAST_CPU':>8}  AFFINITY"
    )
    print(colorize(header, "bold") if colored else header)
    if not interface["irqs"]:
        return

    for irq in interface["irqs"]:
        irq_number = f"{irq['irq']:>6}"
        tid = f"{irq['tid']:>7}"
        scheduler = f"{irq['scheduler']:<15.15}"
        rtprio = f"{irq['rtprio']:>6}"
        last_cpu = f"{irq['last_cpu']:>8}"
        affinity = irq["affinity"]
        if colored:
            irq_number = colorize(irq_number, "yellow")
            tid = colorize(tid, "yellow") if irq["tid"] != "?" else tid
            scheduler = colorize(scheduler, *scheduler_colors(irq["scheduler"]))
            if irq["scheduler"] in ("SCHED_FIFO", "SCHED_RR"):
                rtprio = colorize(rtprio, "yellow", "bold")
            last_cpu = colorize(last_cpu, "yellow") if irq["last_cpu"] != "?" else last_cpu
            affinity = colorize(affinity, "cyan") if affinity != "?" else affinity
        print(
            f"{irq_number} {tid}  {irq['irq_name']:<42.42} {scheduler} {rtprio} "
            f"{irq['prio']:>4} {last_cpu}  {affinity}"
        )


def print_report(report, colored):
    interfaces = report["interfaces"]
    if not interfaces:
        print("No physical network device found.")
        return

    for interface in interfaces:
        print_interface(interface, colored)


def main():
    parser = argparse.ArgumentParser(
        description="Show each physical network device IRQ affinity and threaded IRQ scheduler state."
    )
    parser.add_argument(
        "interface",
        nargs="?",
        help="show only this physical network interface",
    )
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    parser.add_argument(
        "--color",
        choices=("auto", "always", "never"),
        default="auto",
        help="color human output (default: auto)",
    )
    args = parser.parse_args()
    colored = args.color == "always" or (
        args.color == "auto" and sys.stdout.isatty() and "NO_COLOR" not in os.environ
    )

    report = build_report(args.interface)
    if args.interface is not None and not report["interfaces"]:
        print(f"Physical network interface not found: {args.interface}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print_report(report, colored)
    return 0


if __name__ == "__main__":
    sys.exit(main())
