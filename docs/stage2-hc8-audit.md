# HC8 — Stage 2 feasibility audit

Audited 2026-08-14 over SSH (`u0_a153@100.97.181.3:8022`). HC8 was proposed as the Linux host
for Stage 2's kernel-level enforcement, since the Windows development machine cannot provide one.

**Verdict: NO-GO, on every check.** Not one of the seven passed. This is not a configuration gap
that installing packages closes — it is a kernel-generation and privilege-domain gap.

## What HC8 actually is

Not what the brief assumed. It is **not** proot-distro, **not** a chroot, and **not** an Android
VM (crosvm/AVF). It is **Termux running natively as an unprivileged Android application**, using
Termux's Debian package format — which is why it looks like Debian from inside.

| Signal | Value | Reads as |
|---|---|---|
| `TERMUX_MAIN_PACKAGE_FORMAT` | `debian` | dpkg/apt packaging, not a Debian userland |
| `TERMUX__ROOTFS_DIR` | `/data/data/com.termux/files` | Termux's own prefix, not a distro rootfs |
| `$PREFIX/var/lib/proot-distro/installed-rootfs/` | does not exist | no proot-distro guest installed |
| `/proc/self/maps` grep `proot|talloc` | no match | no proot loaded into this process |
| `id` | `uid=10153(u0_a153) ... context=u:r:untrusted_app_27:s0:c153,...` | real Android app uid + SELinux domain |
| `/etc/os-release` | does not exist | not a Debian rootfs |
| `mount` | `not found` | Termux minimal userland, no util-linux |

The `id` line is the decisive one. Under proot-distro, `id` reports `uid=0(root)` because proot
fakes it; here it reports the genuine Android application uid together with the
`untrusted_app_27` SELinux context. Nothing is being virtualized — this is an ordinary Android
app process that happens to have a shell in it.

`/proc/1/cmdline` is `No such file` and `/proc/version` is `Permission denied`: PID 1 is Android's
real `init`, which an untrusted app is not permitted to inspect. Under proot, PID 1 would be the
virtualized guest init and would read fine.

## The seven checks

### 1. Kernel version — FAIL

```
$ uname -r
4.14.356-openela-rc1-PoWeR
$ uname -a
Linux localhost 4.14.356-openela-rc1-PoWeR #1 SMP PREEMPT Fri Mar 27 13:36:26 CET 2026 aarch64 Android
```

`sched_ext` was mainlined in **6.12**. HC8 runs **4.14**, an Android LTS vendor kernel — ten major
releases and roughly five years behind the feature. This is not a kernel that can be configured
into compliance; reaching 6.12 on this device would mean porting it to a mainline-class kernel.

### 2. sched_ext interface — FAIL

```
$ ls -la /sys/kernel/sched_ext/
ls: cannot access '/sys/kernel/sched_ext/': No such file or directory
$ zcat /proc/config.gz | grep -i sched_class_ext
no /proc/config.gz
```

Absent, as the version predicts. `/proc/config.gz` is not exposed either, so the build config
cannot even be inspected.

### 3. Privilege — FAIL

```
$ id
uid=10153(u0_a153) gid=10153(u0_a153) groups=10153(u0_a153),1079(ext_obb_rw),3003(inet),
9997(everybody),20153(u0_a153_cache),50153(all_a153) context=u:r:untrusted_app_27:s0:c153,c256,c512,c768
$ whoami
u0_a153
```

Not root, and not fake-root either — this is the real, unprivileged Android application uid. The
SELinux domain `untrusted_app_27` is the most restricted app domain Android has.

### 4. cgroup v2 — FAIL

```
$ ls -la /sys/fs/cgroup/
ls: cannot open directory '/sys/fs/cgroup/': Permission denied
$ cat /sys/fs/cgroup/cgroup.controllers
cat: /sys/fs/cgroup/cgroup.controllers: Permission denied
$ mkdir -p /sys/fs/cgroup/cordon_probe
mkdir: cannot create directory '/sys/fs/cgroup': Permission denied
$ echo 268435456 > /sys/fs/cgroup/cordon_probe/memory.high
Permission denied
$ echo $$ > /sys/fs/cgroup/cordon_probe/cgroup.procs
Permission denied
```

Not "cgroup v2 exists but is undelegated" — the directory cannot even be **listed**. Android
places app processes into cgroups managed by `init`; an app has no read, let alone write, access.
Every mechanism in CLAUDE.md §5.3 depends on writing `cgroup.procs`, so this alone is fatal
independently of the kernel version.

### 5. Capabilities — FAIL, and permanently

```
$ cat /proc/self/status | grep Cap
CapInh:	0000000000000000
CapPrm:	0000000000000000
CapEff:	0000000000000000
CapBnd:	0000000000000000
CapAmb:	0000000000000000
```

Every set is zero. `CapBnd` — the **bounding** set — being zero is the important one: it means
this process can never acquire any capability by any route, including through a setuid binary.
No `CAP_BPF`, no `CAP_SYS_ADMIN`, no `CAP_SYS_RESOURCE`, and no path to obtaining them. Loading a
BPF program is impossible here even in principle.

### 6. BPF tooling and kernel BTF — FAIL

```
$ command -v bpftool
bpftool NOT installed
$ ls -la /sys/kernel/btf/vmlinux
ls: cannot access '/sys/kernel/btf/vmlinux': No such file or directory
$ cat /proc/sys/kernel/unprivileged_bpf_disabled
Permission denied
```

No kernel BTF. CO-RE (CLAUDE.md §3) resolves struct offsets at load time from exactly this blob,
so its absence rules out the CO-RE approach AgentCgroup's prototype uses — and 4.14 predates
`CONFIG_DEBUG_INFO_BTF` being common on Android at all. Installing `bpftool` would change nothing:
there is no capability to call `bpf()` with.

### 7. Device root — FAIL

```
$ command -v su
/data/data/com.termux/files/usr/bin/su
$ ls -d /sbin/.magisk /data/adb/magisk /data/adb
ls: cannot access '/sbin/.magisk': No such file or directory
ls: cannot access '/data/adb/magisk': Permission denied
ls: cannot access '/data/adb': Permission denied
$ getprop ro.build.version.release
16
```

The `su` on `PATH` is Termux's own wrapper, which execs a system `su` if one exists; its presence
is not evidence of root. No Magisk at `/sbin/.magisk`, and `/data/adb` is permission-denied — the
signature of an **unrooted** device seen from an app sandbox. Android 16, LineageOS.

### 8. Bonus failure — Cordon's own dependency does not build here

Not part of the brief's seven checks, found while trying to run the suite on HC8:

```
$ pip install psutil
      platform android is not supported
      [end of output]
ERROR: Failed to build 'psutil' when getting requirements to build wheel
```

`psutil` — Cordon's only runtime dependency — refuses to build on Android; its own `setup.py`
rejects `sys.platform == "android"`. Termux packages a patched `python-psutil` 7.2.2, but it
installs into `lib/python3.14/site-packages` while the interpreter present is **3.13.13** and no
`python3.14` binary exists, so it is unimportable:

```
$ python3 -c "import psutil"
ModuleNotFoundError: No module named 'psutil'
$ ls $PREFIX/bin | grep ^python
python  python-config  python3  python3-config  python3.13  python3.13-config
```

Resolving that means upgrading the device's Python to 3.14, which is a change to a machine that
has other software on it, and is not a change worth making for a host that has already failed
every enforcement check. So **Stage 1 measurement cannot run on HC8 either**, independently of
Stage 2.

## Why this is not recoverable on this device

Fixing HC8 would require, in order, every one of:

1. Rooting the device (Magisk or equivalent) — LineageOS makes this plausible.
2. A kernel of **6.12 or newer**, built with `CONFIG_SCHED_CLASS_EXT`. HC8 is on 4.14. For an
   Android device this means a full mainline-class kernel port, not a config flag. Devices on
   4.14 GKI essentially never receive one.
3. `CONFIG_DEBUG_INFO_BTF` in that kernel, which Android builds usually strip.
4. Escaping the `untrusted_app_27` SELinux domain, or running the controller from a context that
   was never in it.
5. On top of all of that, the **unmerged** `memcg_bpf_ops` RFC series for Stage 2b.

Steps 2 and 3 are the wall. CLAUDE.md §8's own options remain the realistic routes: **finish the
laptop reinstall** (option A) or **a cloud Linux VM** (option C). A cloud VM clears every check on
this page in one step, since the kernel is yours to choose.

## What the audit changed in the code

The audit was not only a verdict — HC8 exposed four ways `cordon control probe` reported the
truth inaccurately, each now fixed and covered by tests:

0. **The reported kernel version was wrong on Android, and wrong in the dangerous direction.**
   `kernel_release()` read `platform.uname().release`, which on Android builds of CPython returns
   the **Android** release — `16` — not the kernel. `os.uname()` is the syscall and returns
   `4.14.356-openela-rc1-PoWeR`. A probe that reports "16" against a 6.12 threshold is a probe
   that can talk itself into a GO on a 4.14 kernel. `platform.system()` is likewise `Android`
   there, not `Linux`, so the platform check would have reported "not Linux" on a Linux kernel.
   Both now read `os.uname()` and fall back to `platform` only where it is unavailable.

1. **Denied is not missing.** `/sys/fs/cgroup/cgroup.controllers` returned `EACCES`, and the probe
   reported "no such file". Those have completely different fixes — one is "mount cgroup v2", the
   other is "you will never be allowed to touch it" — so `EACCES`/`EPERM` are now distinguished
   from `ENOENT` for the cgroup mount and for kernel BTF.
2. **Capabilities were not checked at all.** HC8's all-zero `CapBnd` is the single most decisive
   signal on the machine and the probe was blind to it. `probe_capabilities` now reads
   `/proc/self/status`, reports which of `CAP_SYS_ADMIN` / `CAP_SYS_RESOURCE` / `CAP_PERFMON` /
   `CAP_BPF` are effective, and calls out an empty bounding set as permanent rather than merely
   currently-unsatisfied. The `bpf` enforcement tier now requires it.
3. **The environment was not named.** "Linux 4.14.356" is true and useless; "Linux 4.14.356
   (Android, Termux app sandbox)" tells the reader why the rest of the page is red. `probe_platform`
   now detects Android, Termux and proot and says so.

## The corrected probe, run live on HC8

```
$ cd ~/cordon-scratch && python3 -c "from features.control.probe import probe, render; print(render(probe()))"
enforcement tier: none

  linux             yes  Linux 4.14.356-openela-rc1-PoWeR (Android, Termux app sandbox)
  capabilities      no   bounding set is empty: no capability can ever be acquired by this process
  cgroup2           no   /sys/fs/cgroup/cgroup.controllers exists but access is denied
  cgroup2_writable  no   cannot create /sys/fs/cgroup/cordon_probe: no delegated write access
  psi               no   no /proc/pressure/memory; throttle stall time will read as zero
  sched_ext         no   kernel 4.14.356-openela-rc1-PoWeR is below 6.12
  memcg_bpf_ops     no   no /sys/kernel/btf/vmlinux to inspect; CO-RE needs kernel BTF

No cgroup v2 available. Cordon will record what it would have enforced and run the
command unchanged. See docs/stage2-design.md for what unblocks real enforcement.
```

Every line names the real blocker and the shape of its fix. The psutil-free part of the suite —
the capability probe and the watermark check — was run on HC8 itself:

```
$ python3 -m pytest tests/test_control_probe.py tests/test_watermark.py -q --no-cov
71 passed in 0.91s
```

aarch64, Android, CPython 3.13.13. The rest of the suite (283 passed, 1 skipped) runs on the
development machine, because of the psutil gap above.

## Consequence for Stage 2

Unchanged from `docs/stage2-design.md`: the enforcement layer runs at whatever tier the machine
supports, and on HC8 that tier is `none`. `select_backend()` returns `NullBackend`, which records
what it would have applied, runs the command unchanged, and emits no feedback — because telling an
agent it was throttled when nothing was enforcing is worse than saying nothing.

Nothing was stubbed to make HC8 look like it works. `cordon control probe` exits non-zero there.
