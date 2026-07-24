# SEAPATH scripts

Ease SEAPATH debug and deployment

## QEMU CPU pinning report

Display QEMU/KVM thread ID, name, scheduler, RT priority, priority, last CPU,
and effective affinity per VM. Pass a VM name to output only that VM and its
associated `kvm-pit` task.

```sh
./qemu_cpu_pinning.py
./qemu_cpu_pinning.py myvm
./qemu_cpu_pinning.py --json
```

Run as root for complete QEMU command-line and thread visibility. `LAST_CPU` is
procfs last-scheduled CPU snapshot, not proof thread executes on CPU while
report prints. JSON output uses same thread columns as table output.

## Isolated CPU task report

Display every process thread last scheduled on selected CPU. Pass `--allowed`
to also display threads whose effective affinity allows selected CPU but last
scheduler snapshot is elsewhere. When multiple CPUs are selected, tasks are
grouped by last CPU.

```sh
./isolated_cpu_tasks.py 4
./isolated_cpu_tasks.py 4-7,12 --allowed --json
```

Run as root for complete task visibility. `LAST_CPU` is procfs last-scheduled
CPU snapshot, not proof thread executes on CPU while report prints.

## Network IRQ report

Display one IRQ table per physical network interface. Tables include IRQ number,
thread ID, IRQ name, scheduler, RT priority, priority, last CPU, and effective
IRQ affinity. Thread and scheduler fields for hard IRQs or invisible IRQ
threads are `?`.

```sh
./network_irq_report.py
./network_irq_report.py eno1
./network_irq_report.py --json
```

IRQ discovery uses device `msi_irqs` and legacy `irq` sysfs entries, with
`/proc/interrupts` name matching as fallback. Run as root for complete IRQ
thread visibility. Pass an interface name to output only that network card.
