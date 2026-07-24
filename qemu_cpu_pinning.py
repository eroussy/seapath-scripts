#!/usr/bin/env python3
# Copyright (C) 2026 Savoir-faire Linux Inc.
# SPDX-License-Identifier: Apache-2.0
"""Report QEMU/KVM thread affinity and scheduler state from procfs."""

import argparse
import json
import os
import re
import sys
from pathlib import Path


PROC = Path("/proc")

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
    except (FileNotFoundError, PermissionError, ProcessLookupError):
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
    if len(fields) < 37:
        return None

    # proc(5): nice=field 19, processor=39. Array starts at field 3.
    return {"nice": fields[16], "cpu": fields[36]}


def scheduler(pid, tid):
    try:
        policy = os.sched_getscheduler(tid)
        rt_priority = os.sched_getparam(tid).sched_priority
    except (AttributeError, OSError):
        return "unknown", "?"
    return POLICIES.get(policy, f"unknown({policy})"), str(rt_priority)


def ps_priority(policy, rt_priority, nice):
    """Return Linux ps-style priority: normal 0-39, RT 41-139."""
    if policy in ("SCHED_FIFO", "SCHED_RR"):
        return str(40 + int(rt_priority))
    return str(19 + int(nice))


def vm_name(cmdline):
    arguments = cmdline.split("\0")
    for index, argument in enumerate(arguments[:-1]):
        if argument == "-name":
            name = arguments[index + 1]
            # libvirt normally emits guest=<name>,debug-threads=on.
            for option in name.split(","):
                if option.startswith("guest="):
                    return option.removeprefix("guest=")
            return name.split(",", 1)[0]
    return "<unnamed>"


def is_qemu(pid, comm):
    cmdline = read_text(PROC / str(pid) / "cmdline") or ""
    executable = os.path.basename(cmdline.split("\0", 1)[0])
    return executable.startswith("qemu") or comm.startswith("qemu")


def process_ids():
    for entry in PROC.iterdir():
        if entry.name.isdecimal():
            yield int(entry.name)


def print_header(colored):
    header = (
        f"{'TID':>7}  {'THREAD':<24} {'SCHEDULER':<15} {'RTPRIO':>6} "
        f"{'PRIO':>4} {'LAST_CPU':>8}  AFFINITY"
    )
    print(colorize(header, "bold") if colored else header)


def process_rows(pid):
    task_dir = PROC / str(pid) / "task"
    try:
        tids = sorted(int(entry.name) for entry in task_dir.iterdir() if entry.name.isdecimal())
    except (FileNotFoundError, PermissionError, ProcessLookupError):
        return []

    rows = []
    for tid in tids:
        status = read_status(pid, tid)
        stat = read_stat(pid, tid)
        if status is None or stat is None:
            continue
        policy, rt_priority = scheduler(pid, tid)
        priority = ps_priority(policy, rt_priority, stat["nice"])
        rows.append(
            {
                "tid": tid,
                "thread": status.get("Name", "?"),
                "scheduler": policy,
                "rtprio": rt_priority,
                "prio": priority,
                "last_cpu": int(stat["cpu"]),
                "affinity": status.get("Cpus_allowed_list", "?"),
            }
        )
    return rows


def print_process(title, rows, colored):
    if not rows:
        return

    heading = f"\n=== {title} ==="
    print(colorize(heading, "cyan", "bold") if colored else heading)
    print_header(colored)
    for row in rows:
        tid = f"{row['tid']:>7}"
        scheduler = f"{row['scheduler']:<15.15}"
        rtprio = f"{row['rtprio']:>6}"
        last_cpu = f"{row['last_cpu']:>8}"
        affinity = row["affinity"]
        if colored:
            tid = colorize(tid, "yellow")
            scheduler = colorize(scheduler, *scheduler_colors(row["scheduler"]))
            if row["scheduler"] in ("SCHED_FIFO", "SCHED_RR"):
                rtprio = colorize(rtprio, "yellow", "bold")
            last_cpu = colorize(last_cpu, "yellow")
            affinity = colorize(affinity, "cyan")
        print(
            f"{tid}  {row['thread']:<24.24} {scheduler} {rtprio} "
            f"{row['prio']:>4} {last_cpu}  {affinity}"
        )


def main():
    parser = argparse.ArgumentParser(
        description="Show QEMU VM and host KVM task CPU affinity and scheduler state."
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="also show QEMU/KVM task groups with no detectable VM name",
    )
    parser.add_argument(
        "vm_name",
        nargs="?",
        help="show only this VM and its associated kvm-pit task",
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

    qemu_processes = []
    other_processes = []
    kvm_pit_processes = {}
    qemu_names = {}
    other_pattern = re.compile(r"^(?:qemu|kvm|vhost)", re.IGNORECASE)

    for pid in process_ids():
        comm = read_text(PROC / str(pid) / "comm")
        if comm is None:
            continue
        if is_qemu(pid, comm):
            qemu_processes.append(pid)
            cmdline = read_text(PROC / str(pid) / "cmdline") or ""
            qemu_names[pid] = vm_name(cmdline)
        elif other_pattern.match(comm):
            match = re.fullmatch(r"kvm-pit/(\d+)", comm)
            if match:
                qemu_pid = int(match.group(1))
                kvm_pit_processes.setdefault(qemu_pid, []).append(pid)
            else:
                other_processes.append(pid)

    groups = []

    def add_group(pid, title):
        rows = process_rows(pid)
        groups.append({"pid": pid, "name": title, "threads": rows})

    for pid in sorted(qemu_processes):
        name = qemu_names[pid]
        if args.vm_name is not None and name != args.vm_name:
            continue
        if name != "<unnamed>" or args.all:
            add_group(pid, f"VM: {name}")
            for pit_pid in kvm_pit_processes.pop(pid, []):
                add_group(pit_pid, f"KVM PIT for VM: {name} (QEMU PID {pid})")

    if args.vm_name is None:
        for pid in sorted(other_processes):
            comm = read_text(PROC / str(pid) / "comm") or "?"
            add_group(pid, f"Other QEMU/KVM task: {comm}")

        for qemu_pid in sorted(kvm_pit_processes):
            for pit_pid in kvm_pit_processes[qemu_pid]:
                add_group(
                    pit_pid,
                    f"KVM PIT: kvm-pit/{qemu_pid} (QEMU PID {qemu_pid}, VM not found)",
                )

    if not groups:
        if args.vm_name is None:
            print("No QEMU VM or KVM task found.", file=sys.stderr)
        else:
            print(f"VM not found: {args.vm_name}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps({"groups": groups}, indent=2, sort_keys=True))
    else:
        for group in groups:
            print_process(f"{group['name']} (PID {group['pid']})", group["threads"], colored)
    return 0


if __name__ == "__main__":
    sys.exit(main())
