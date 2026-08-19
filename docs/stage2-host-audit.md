# Stage 2 host audit — the two remaining Debian devices

Audited 2026-08-15, following `docs/stage2-hc8-audit.md`. Two devices were offered as the Linux
host for Stage 2's kernel-level enforcement.

**Verdict: no host secured. Device 1 is the same machine as HC8 and remains NO-GO. Device 2 could
not be audited at all — SSH refused the available key, so it is unaudited, not cleared and not
rejected.** Stage 2a therefore did not run.

| Device | Tailscale name | OS per Tailscale | Result |
|---|---|---|---|
| 1 — `u0_a153@100.97.181.3:8022` | `headless-chicken-8-pro` | android | **NO-GO** — this is HC8 |
| 2 — `joel-folding@100.102.57.7` | `joel-folding` | linux | **UNAUDITED** — SSH auth failed |

## Device 1 is HC8

The brief presented this as a new Debian device. It is not new. The SSH endpoint is character-for-
character the one audited yesterday, and the machine behind it is unchanged:

```
$ ssh u0_a153@100.97.181.3 -p 8022 'id; uname -a'
uid=10153(u0_a153) gid=10153(u0_a153) groups=10153(u0_a153),1079(ext_obb_rw),3003(inet),
9997(everybody),20153(u0_a153_cache),50153(all_a153) context=u:r:untrusted_app_27:s0:c153,c256,c512,c768
Linux localhost 4.14.356-openela-rc1-PoWeR #1 SMP PREEMPT Fri Mar 27 13:36:26 CET 2026 aarch64 Android
```

Same uid, same `untrusted_app_27` SELinux context, same kernel build string and build date as
yesterday. Tailscale independently names it `headless-chicken-8-pro` and types it `android`, which
is where the HC8 initials come from.

Re-confirmed it is still the Termux app sandbox rather than a Debian guest someone installed since:

```
$ cat /etc/os-release
cat: /etc/os-release: No such file or directory
$ echo $TERMUX__ROOTFS_DIR
/data/data/com.termux/files
$ echo $TERMUX_VERSION
0.118.3
$ grep -c -i "proot\|talloc" /proc/self/maps
0
$ ls $PREFIX/var/lib/proot-distro/installed-rootfs/
ls: cannot access '.../installed-rootfs/': No such file or directory
```

No proot in the process map, no proot-distro guest, no distro rootfs. Unchanged from yesterday.

### The branch, cloned rather than copied

```
$ git clone --branch stage2-control --single-branch --depth 1 https://github.com/uncoalesced/cordon.git cordon-audit
Cloning into 'cordon-audit'...
$ git log --oneline -1
4072164 docs: log the Stage 2 control layer and the HC8 audit
$ git rev-parse --abbrev-ref HEAD
stage2-control
```

### `cordon control probe`, verbatim

```
$ python3 -m features.wrapper.cli control probe
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
EXIT=1
```

Identical to yesterday's table, line for line. Exit 1, NO-GO. The probe needed no correction on
this device — every lesson HC8 taught it still reads true.

## Device 2 could not be audited

Not a NO-GO. The machine is up, SSH is listening, and the account exists — it refused the key:

```
$ ssh joel-folding@100.102.57.7 'id'
joel-folding@100.102.57.7: Permission denied (publickey,password).
```

Three routes were tried and all failed on the same wall:

```
$ ssh -v ... | grep -i offering
debug1: Offering public key: .../id_ed25519 ED25519 SHA256:bModcu6ivU3HafTo9lDF+eh5rr4VqIznZiibZc+JxjY
debug1: Authentications that can continue: publickey,password

$ ssh joel-folding@joel-folding.tail9824be.ts.net 'id'
joel-folding@joel-folding.tail9824be.ts.net: Permission denied (publickey,password).

$ ssh u0_a153@100.97.181.3 -p 8022 'ssh joel-folding@100.102.57.7 id'
joel-folding@100.102.57.7: Permission denied (publickey,password).
```

The MagicDNS attempt matters: reaching the tailnet name and still being asked for `publickey,password`
proves Tailscale SSH is **not** enabled server-side on this node — it is plain OpenSSH, so tailnet
membership alone grants nothing. Device 1 was tried as a jump host and holds only `authorized_keys`,
no private key of its own.

Password authentication was not attempted: this session cannot enter credentials.

Tailscale does report the node as `linux` and `active; direct 192.168.0.109:41641`, so it is a
genuine Linux host and a genuine candidate. Nothing about its kernel, cgroup delegation or
capabilities is known, and none of it should be guessed. **It stays unaudited until the key lands.**

To unblock, on device 2:

```
mkdir -p ~/.ssh && printf '%s\n' 'ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIAnezQcB+4fxGib7JM6OomEqJ3Je7BriHE/sKwT/783l joel anthony@Joel' >> ~/.ssh/authorized_keys && chmod 700 ~/.ssh && chmod 600 ~/.ssh/authorized_keys
```

## Stage 2b: still blocked, and now dated

CLAUDE.md §15 Q8 asks whether the `memcg_bpf_ops` RFC has landed or whether a patched kernel build
is the plan. Checked https://lwn.net/Articles/1055698/ directly:

- The series is **RFC PATCH bpf-next v3**, "mm: memcontrol: Add BPF hooks for memory controller",
  dated **2026-01-23**. Twelve patches against `bpf-next`. Still under review, not merged.
- A later LWN piece, "Controlling memory management with BPF" (**2026-05-15**), covers the memory-
  management-plus-BPF area generally and describes the work as at an early stage, discussing a
  further proposed callback rather than reporting anything merged.

So as of this audit the answer to Q8 is unchanged and now has a date attached: **not upstream, no
sign of landing imminently.** Stage 2b requires a self-built patched kernel or it does not happen.
Per §13 its definition of done is untouched — no `memcg_bpf_ops` policy, no survival-rate or P95
reproduction. It was never in reach this session regardless, since no device cleared Stage 2a.

## What the audit changed in the code

One finding, of the same class HC8 surfaced in `tests/conftest.py` — and left half-fixed.

**`cordon control probe` could not run through its own entry point on a host without psutil.**
Running it on device 1 as the brief directs produced no table at all:

```
$ python3 -m features.wrapper.cli control probe
  File ".../features/control/intent.py", line 10, in <module>
    import psutil
ModuleNotFoundError: No module named 'psutil'
```

The HC8 audit worked around this by importing `features.control.probe` directly, which is why the
gap did not show up then. That workaround hid a real defect: the probe exists precisely to be the
first thing run on an unknown, unprepared machine, and it demanded the project's only compiled
dependency — one that, as HC8 established, does not build on every target — before it would print
a single line. The command most needed on a bare box was the one that could not run there.

HC8 already fixed this once, in `tests/conftest.py`, by making that file's psutil import lazy. The
same coupling survived in `features/wrapper/cli.py`, which is the sibling caller: the fix had been
applied to the symptom rather than to the pattern. Two chains reached psutil from the CLI:

1. `cli.py` → `control.guard` → `wrapper.sampler` → `psutil`, via `DEFAULT_INTERVAL_S`
2. `cli.py` → `control.contention` → `control.cgroup` → `control.intent` → `psutil`

Both are now cut at the root rather than at the call sites:

- `DEFAULT_INTERVAL_S` moved to `features/wrapper/schema.py`, which has no psutil dependency.
  `sampler.py` re-exports it, so `sampler.DEFAULT_INTERVAL_S` and `hook.DEFAULT_INTERVAL_S` still
  resolve and nothing downstream changed.
- `intent.py` imports psutil inside `total_memory_bytes()`, the single function that uses it. Its
  existing `except Exception` already covered the failure, so a host without psutil now falls back
  to the documented 16GB tier-scaling assumption and logs it, instead of failing at import.
- `cli.py` imports `sampler` and `hook` inside the two commands that need them.

Nothing in the sampling loop was touched, so Stage 1's measured overhead is unaffected.

Verified on the one machine that genuinely has no psutil, through the real entry point:

```
$ python3 -m features.wrapper.cli control probe
enforcement tier: none
  ...
EXIT=1
$ python3 -m pytest tests/test_control_probe.py tests/test_watermark.py -q --no-cov
71 passed in 0.89s
```

aarch64, Android, CPython 3.13.13. The full suite is 284 passed, 1 skipped on the development
machine. `tests/test_cli.py` gained a check that runs `control probe` in a subprocess with psutil
blocked at import; it fails against the previous arrangement, so the regression is pinned.

## Consequence for Stage 2

Unchanged in shape from the HC8 audit, and now with one candidate genuinely untested rather than
two rejected. Per CLAUDE.md §8 the realistic routes remain **option A, finish the laptop reinstall**
and **option C, a cloud Linux VM** — the latter clears every check in one step because the kernel
is yours to choose. Device 2 is worth auditing before either is started: it is a real Linux host on
the tailnet, and one `authorized_keys` line is all that stands between it and a verdict. §15 Q9
(when the laptop reinstall happens) remains the other open gate, and remains unanswered.
