#!/usr/bin/env python3
# Copyright (C) 2026 Savoir-faire Linux Inc.
# SPDX-License-Identifier: Apache-2.0
"""Report tasks last scheduled on, or allowed to run on, selected CPUs."""

import argparse
import json
import sys
from pathlib import Path


PROC = Path("/proc")
SYS_CPU = Path("/sys/devices/system/cpu")


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
                "effective_affinity": affinity_text,
            }
            if last_cpu in selected_cpus:
                running.append(row)
            elif affinity & selected_cpus:
                allowed.append(row)

    key = lambda row: (row["pid"], row["tid"])
    return sorted(running, key=key), sorted(allowed, key=key), skipped


def print_rows(title, rows):
    print(f"\n{title}: {len(rows)}")
    if not rows:
        return
    print(f"{'PID':>7} {'TID':>7}  {'PROCESS':<24} {'THREAD':<24} {'LAST_CPU':>8}  EFFECTIVE_AFFINITY")
    for row in rows:
        print(
            f"{row['pid']:>7} {row['tid']:>7}  {row['process']:<24.24} "
            f"{row['thread']:<24.24} {row['last_cpu']:>8}  {row['effective_affinity']}"
        )


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
    args = parser.parse_args()

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
        print_rows("Last scheduled on selected CPU(s)", running)
        if args.allowed:
            print_rows("Allowed on selected CPU(s), last scheduled elsewhere", allowed)
        if skipped:
            print(f"\nSkipped unreadable or exited tasks: {skipped}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    sys.exit(main())
