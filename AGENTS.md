# Purpose

This directory stores standalone scripts for debugging or easing LF Energy
SEAPATH development. Primary near-term scope: read-only real-time diagnostics
for scheduling, priority, CPU affinity, CPU isolation, interrupts, and QEMU/KVM
threads.

# SEAPATH Architecture

SEAPATH is an open, high-availability, real-time hypervisor reference
architecture for IEC 61850 digital substations. It hosts virtualized Protection,
Automation, and Control (vPAC/vIED) workloads, not protection algorithms itself.

Core stack:

- Linux with a fully preemptible real-time kernel.
- KVM in kernel, QEMU userspace, libvirt domain management.
- Open vSwitch and Linux bridges; workloads may use virtio, vhost, SR-IOV, or
  PCI passthrough depending on latency needs.
- PTP/NTP time synchronization. PTP is relevant when diagnosing latency.
- Pacemaker, Ceph, and VM Manager support high-availability cluster deployments.
- Debian, Yocto, SLES, and CentOS/RHEL-derived variants exist. Do not assume
  identical paths, services, package names, or CPU-isolation implementation.

SEAPATH favors deterministic latency and jitter control over aggregate
throughput. A valid diagnosis must inspect host scheduling and hardware/network
interrupt placement as well as guest configuration.

# Real-Time Configuration Model

Host configuration comes primarily from `ansible/roles/configure_hypervisor`.

- CPU isolation: `isolcpus` defines host CPUs dedicated to RT VM use. Debian
  uses Ansible after install; Yocto configures RT cores at build time.
- `tuned` profile `seapath-rt-host` extends `realtime-virtual-host`. Current
  profile configures isolated cores, performance governor, reduced C-states,
  `rcu_nocb_poll`, and `kvm.halt_poll_ns=0`.
- Optional systemd/cgroup CPU partitions constrain `system`, `user`, `machine`,
  `machine-rt`, `machine-nort`, and `ovs` workloads. Child slice CPU sets must
  be subsets of parent slice CPU sets.
- `machine-rt` and `machine-nort` place VM processes by libvirt `<resource>`
  partitions. QEMU management/emulator threads remain in VM machine slice, not
  system slice.
- An RT VM needs enough allowed CPUs for every RT vCPU plus management work.
  Pin emulator threads away from RT vCPU cores. Otherwise VM boot or latency can
  fail from CPU starvation.

Default template: `ansible/templates/vm/guest.xml.j2`.

- `vm_features: [rt, isolated]` is normal deterministic-VM configuration.
- `isolated` emits one `<vcpupin>` per vCPU from `cpuset`.
- `rt` emits `<vcpusched scheduler="fifo" priority="...">` for each vCPU,
  places VM in `/machine/rt` when configured, uses host-passthrough CPU model,
  requires `tsc-deadline`, and disables PMU.
- `emulatorpin` pins QEMU emulator/management threads. It should be separate
  from each RT vCPU core and allowed by VM cgroup/slice.
- Default RT priority is 1. Do not infer effective thread policy or priority
  from domain XML; inspect live QEMU threads.

When `configure_libvirt_deploy_seapath_qemu_hook` is enabled, libvirt hook
`/etc/libvirt/hooks/qemu.d/` detects RT guests and applies QEMU affinity plus
`chrt -p 1` to `vhost-<qemu-pid>` and `kvm-pit/<qemu-pid>` threads. Script
output must identify these threads separately from vCPU and emulator threads.

NIC IRQ affinity can be configured by
`ansible/roles/configure_nic_irq_affinity/files/setup_nic_irq_affinity.py`.
It finds IRQs under `/proc/irq/*` whose directory name starts with NIC name, then
writes `smp_affinity_list`. IRQ placement must not disturb isolated RT CPUs
unless intentional and measured. irqbalance can alter placement; inspect its
configuration and current IRQ affinities before drawing conclusions.

# Script Design Rules

- Prefer Python 3 for structured `/proc`, `/sys`, libvirt XML, and JSON parsing.
  Use Bash for small command orchestration only.
- Default to read-only behavior. Never change scheduler, affinity, IRQ masks,
  cgroups, tuned profiles, kernel command line, CPU governor, or VM XML without
  an explicit mutating flag and clear warning.
- Read live kernel state from `/proc` and `/sys`; do not treat Ansible inventory
  or inactive libvirt XML as runtime truth.
- Handle process/thread exit races and permission failures. Continue reporting
  other objects; return nonzero only for fatal or requested-object failures.
- Root may be needed to see all QEMU command lines, task details, IRQ state, and
  scheduler data. Detect insufficient visibility and state it.
- Accept CPU lists in Linux range format (`0-3,8,10-11`). Preserve both supplied
  CPU lists and effective kernel/cgroup-constrained lists in output.
- Avoid external dependencies unless already guaranteed by host image. Prefer
  Python standard library and procfs/sysfs. If invoking `virsh`, `taskset`,
  `chrt`, `ps`, or `systemctl`, report command failures without parsing fragile
  human output when procfs provides equivalent data.
- Script output must be stable and machine-readable when requested. Offer
  `--json` for nontrivial reports; keep human table output concise.
- Never assume CPU IDs map one-to-one to physical cores. Report topology from
  `/sys/devices/system/cpu/cpu*/topology/` and flag sibling contention where
  relevant.
- Do not change workload placement during latency measurement. Diagnostics must
  not themselves perturb scheduling or IRQ routing.

# Real-Time Investigation Checklist

Start with observed runtime state, in this order:

1. Host baseline: kernel release and PREEMPT_RT status, kernel command line,
   online CPUs, CPU topology, current CPU governor, active tuned profile, and
   effective CPU-isolation configuration.
2. Cgroup/slice limits: systemd `AllowedCPUs`, process cgroup membership, and
   effective cpuset masks for QEMU process and individual threads.
3. Domain intent: `virsh dumpxml <domain>` or active libvirt XML. Extract
   resource partition, vCPU count, `vcpupin`, `emulatorpin`, `iothreadpin`, and
   `<vcpusched>` policy/priority.
4. QEMU live state: identify domain QEMU PID from libvirt or command line. For
   every TID, report name, effective affinity (`Cpus_allowed_list`), last CPU,
   scheduler policy, RT priority, kernel priority, nice value, cgroup, and role.
5. Thread roles: distinguish QEMU emulator/main, `CPU n/KVM` vCPU, `vhost-*`,
   `kvm-pit/*`, I/O, migration, and auxiliary threads. Names are version
   dependent: preserve unknown names rather than guessing.
6. Contention: report non-VM tasks allowed on RT CPUs; RT tasks sharing a CPU;
   QEMU management threads sharing vCPU cores; SMT siblings used by unrelated
   tasks; and CPU overcommit versus pinning intent.
7. Interrupts/network: map NIC queues and IRQs from `/proc/interrupts`,
   `/proc/irq/<irq>/smp_affinity_list`, `/sys/class/net`, and IRQ names. Check
   irqbalance state/configuration. Correlate process-bus NIC IRQs with RT CPUs.
8. Latency evidence: use `cyclictest` results and relevant ftrace/perf data only
   when available. Report measurement conditions, duration, CPU affinity, and
   load; never compare unqualified maximum-latency values as equivalent.

# Existing Local References

- https://github.com/seapath/ansible/blob/main/templates/vm/guest.xml.j2 :
  default VM RT, vCPU, and emulator pinning behavior.
- https://github.com/seapath/ansible/tree/main/roles/configure_hypervisor :
  tuned profile, CPU isolation, and
  cgroup slice configuration.
- https://github.com/seapath/ansible/tree/main/roles/configure_nic_irq_affinity :
  applied NIC IRQ affinity.
- https://github.com/seapath/ansible/blob/main/roles/deploy_vms_standalone/README.md :
  documented VM variables.

# Sources

- LF Energy project page: https://lfenergy.org/projects/seapath/
- SEAPATH real-time configuration wiki:
  https://lf-energy.atlassian.net/wiki/spaces/SEAP/pages/519208977
- SEAPATH real-time VM configuration wiki:
  https://lf-energy.atlassian.net/wiki/spaces/SEAP/pages/533856296
- SEAPATH Ansible documentation:
  https://galaxy.ansible.com/ui/repo/published/seapath/ansible/
