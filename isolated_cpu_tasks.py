#!/usr/bin/env python3
# Copyright (C) 2026 Savoir-faire Linux Inc.
# SPDX-License-Identifier: Apache-2.0
"""Report tasks last scheduled on, or allowed to run on, selected CPUs."""

import argparse
import json
import os
import sys
from pathlib import Path


PROC = Path("/proc")
SYS_CPU = Path("/sys/devices/system/cpu")

COLORS = {
    "reset": "\033[0m",
    "bold": "\033[1m",
    "cyan": "\033[36m",
    "yellow": "\033[33m",
}


def colorize(value, *colors):
    return "".join(COLORS[color] for color in colors) + value + COLORS["reset"]


def read_text(path):
    try:
        return path.read_text().strip()
    except (FileNotFoundError, PermissionError, ProcessLookupError):
        return None


def parse_cpu_list(value):
    """Parse Linux CPU-list format, such as 0-3,8,10-11."""
    cpus = set()
    for part in value.split(","):
        if not part:
            raise ValueError("empty CPU-list element")
        start, separator, end = part.partition("-")
        if not start.isdecimal() or (separator and not end.isdecimal()):
            raise ValueError(f"invalid CPU-list element: {part!r}")
        first = int(start)
        last = int(end) if separator else first
        if last < first:
            raise ValueError(f"CPU range ends before it starts: {part!r}")
        cpus.update(range(first, last + 1))
    return cpus


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


def read_last_cpu(pid, tid):
    text = read_text(PROC / str(pid) / "task" / str(tid) / "stat")
    if text is None:
        return None

    # comm may contain spaces and parentheses. Fields after final ')' are fixed.
    closing_paren = text.rfind(")")
    if closing_paren < 0:
        return None
    fields = text[closing_paren + 2 :].split()
    # proc(5): processor is field 39. Array starts at field 3.
    if len(fields) <= 36:
        return None
    try:
        return int(fields[36])
    except ValueError:
        return None


def process_ids():
    try:
        entries = PROC.iterdir()
        for entry in entries:
            if entry.name.isdecimal():
                yield int(entry.name)
    except PermissionError:
        return


def task_rows(selected_cpus):
    running = []
    allowed = []
    skipped = 0

    for pid in process_ids():
        process_name = read_text(PROC / str(pid) / "comm")
        task_dir = PROC / str(pid) / "task"
        try:
            tids = sorted(int(entry.name) for entry in task_dir.iterdir() if entry.name.isdecimal())
        except (FileNotFoundError, PermissionError, ProcessLookupError):
            skipped += 1
            continue

        for tid in tids:
            status = read_status(pid, tid)
            last_cpu = read_last_cpu(pid, tid)
            if status is None or last_cpu is None:
                skipped += 1
                continue

            affinity_text = status.get("Cpus_allowed_list")
            if affinity_text is None:
                skipped += 1
                continue
            try:
                affinity = parse_cpu_list(affinity_text)
            except ValueError:
                skipped += 1
                continue

            row = {
                "pid": pid,
                "tid": tid,
                "process": process_name or "?",
                "thread": status.get("Name", "?"),
                "last_cpu": last_cpu,
                "affinity": affinity_text,
            }
            if last_cpu in selected_cpus:
                running.append(row)
            elif affinity & selected_cpus:
                allowed.append(row)

    key = lambda row: (row["pid"], row["tid"])
    return sorted(running, key=key), sorted(allowed, key=key), skipped


def print_header(colored):
    header = f"{'PID':>7} {'TID':>7}  {'PROCESS':<24} {'THREAD':<24} {'LAST_CPU':>8}  AFFINITY"
    print(colorize(header, "bold") if colored else header)


def print_row(row, colored):
    pid = f"{row['pid']:>7}"
    tid = f"{row['tid']:>7}"
    last_cpu = f"{row['last_cpu']:>8}"
    affinity = row["affinity"]
    if colored:
        pid = colorize(pid, "yellow")
        tid = colorize(tid, "yellow")
        last_cpu = colorize(last_cpu, "yellow")
        affinity = colorize(affinity, "cyan")
    print(
        f"{pid} {tid}  {row['process']:<24.24} {row['thread']:<24.24} "
        f"{last_cpu}  {affinity}"
    )


def print_rows(title, rows, colored, group_by_last_cpu=False):
    heading = f"\n{title}: {len(rows)}"
    print(colorize(heading, "cyan", "bold") if colored else heading)
    if not rows:
        return
    if not group_by_last_cpu:
        print_header(colored)
        for row in rows:
            print_row(row, colored)
        return

    groups = {row["last_cpu"]: [] for row in rows}
    for row in rows:
        groups[row["last_cpu"]].append(row)
    for last_cpu in sorted(groups):
        group_heading = f"\nLast CPU: {last_cpu}"
        print(colorize(group_heading, "cyan", "bold") if colored else group_heading)
        print_header(colored)
        for row in groups[last_cpu]:
            print_row(row, colored)


def main():
    parser = argparse.ArgumentParser(
        description="Show tasks last scheduled on or allowed to run on selected CPUs."
    )
    parser.add_argument("cpus", help="CPU or Linux CPU list, for example: 4 or 4-7,12")
    parser.add_argument(
        "--allowed",
        action="store_true",
        help="also show threads allowed on selected CPU(s) but last scheduled elsewhere",
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

    try:
        selected_cpus = parse_cpu_list(args.cpus)
        online_text = read_text(SYS_CPU / "online")
        online_cpus = parse_cpu_list(online_text) if online_text else set()
    except ValueError as error:
        parser.error(str(error))

    offline = selected_cpus - online_cpus
    if offline:
        parser.error(f"requested offline or nonexistent CPU(s): {','.join(map(str, sorted(offline)))}")

    running, allowed, skipped = task_rows(selected_cpus)
    report = {
        "cpus": sorted(selected_cpus),
        "last_scheduled_on_cpus": running,
        "unreadable_or_exited_tasks": skipped,
    }
    if args.allowed:
        report["allowed_on_cpus_not_last_scheduled_there"] = allowed

    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        cpu_list = ",".join(map(str, sorted(selected_cpus)))
        print(f"Selected CPU(s): {cpu_list}")
        print("Last CPU is procfs scheduling snapshot, not instantaneous execution.")
        group_by_last_cpu = len(selected_cpus) > 1
        print_rows("Last scheduled on selected CPU(s)", running, colored, group_by_last_cpu)
        if args.allowed:
            print_rows(
                "Allowed on selected CPU(s), last scheduled elsewhere",
                allowed,
                colored,
                group_by_last_cpu,
            )
        if skipped:
            print(f"\nSkipped unreadable or exited tasks: {skipped}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    sys.exit(main())
