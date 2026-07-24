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


def print_header():
    print(
        f"{'TID':>7}  {'THREAD':<24} {'SCHEDULER':<15} {'RTPRIO':>6} "
        f"{'PRIO':>4} {'LAST_CPU':>8}  AFFINITY"
    )


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


def print_process(title, rows):
    if not rows:
        return

    print(f"\n=== {title} ===")
    print_header()
    for row in rows:
        print(
            f"{row['tid']:>7}  {row['thread']:<24.24} {row['scheduler']:<15.15} "
            f"{row['rtprio']:>6} {row['prio']:>4} {row['last_cpu']:>8}  {row['affinity']}"
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
    args = parser.parse_args()

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
            print_process(f"{group['name']} (PID {group['pid']})", group["threads"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
