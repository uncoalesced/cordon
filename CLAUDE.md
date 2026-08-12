# CLAUDE.md — Cordon

<!-- Engineered by uncoalesced -->

**Assumption, flagged up front:** "this" = the Stage 1 → Stage 2 build from the Bottom Line of the fact-check pass — an AgentCgroup-inspired, intent-driven agent resource controller, starting with a characterization study. If you meant something else, this file is cheap to redo — nothing has been built yet.

"Cordon" is the name.

**What changed since the last version of this file:** this is the "buffed up" pass — same project, same plan, but now with the actual mechanics of AgentCgroup and AgentSight pulled from the full papers (not just abstracts), so Opus isn't implementing from a summary — it's implementing from the real design.

---

## Table of Contents
1. Project Metadata
2. What This Project Is
3. Key Concepts (read this before anything else if eBPF/cgroups are new territory)
4. The Problem — Why Agent Workloads Break Existing Resource Management
5. How It Works — Full System Architecture
6. Research Grounding — The Real Numbers, In Full
7. The System We're Extending — AgentSight In Detail
8. Hardware / Environment Reality
9. Scope
10. Which Agent to Characterize
11. Implementation Instructions
12. Coding Standards
13. Definition of Done
14. Non-Goals
15. Open Questions
16. References

---

## 1. Project Metadata

| Field | Value |
|---|---|
| Owner | uncoalesced (Joel) |
| Watermark | **YES** — `# Engineered by uncoalesced` in every source file |
| Stack | Python 3.11+ (Stage 1, all of it); C + libbpf (Stage 2 eBPF programs only) |
| Storage | Flat JSON-lines files for Stage 1; PostgreSQL once Stage 2 needs live state |
| Status | Not started — spec only |
| Grounded in | AgentCgroup (arXiv 2602.09345) and AgentSight (arXiv 2508.02736), both fetched and read in full for this spec, not summarized from search snippets |

---

## 2. What This Project Is

A system that watches an AI coding agent's tool calls and characterizes — then controls — how much CPU and memory each one actually needs, at tool-call granularity instead of container granularity. It's a solo, consumer-hardware-scoped adaptation of two real, verified research systems: AgentCgroup (the resource controller) and AgentSight (the observability layer it's built on top of).

The one-sentence version of the whole thesis, straight from the AgentCgroup paper: **agents can tell you what they're about to do, and existing resource controllers throw that information away.** A test suite and a `git status` both look like "a subprocess" to a container-level memory limit — but one needs 500MB and the other needs 13MB. The entire project is about not throwing that information away.

---

## 3. Key Concepts

Read this section first if any of these are unfamiliar — everything downstream assumes you know what these words mean.

**cgroup v2 (control groups):** the Linux kernel's mechanism for grouping processes and applying resource limits/accounting to the group as a whole. Cgroups form a tree — a process belongs to exactly one cgroup, and limits can be set at any level of the tree, inherited by children. The specific interfaces this project uses:
- `memory.high` — a *soft* limit. Crossing it triggers reclaim pressure (the kernel tries to free memory) but does **not** kill anything.
- `memory.max` — a *hard* limit. Crossing it triggers the OOM killer.
- `cgroup.freeze` — stops every process in the subtree (SIGSTOP-like) until unfrozen. Not a kill — the process's memory and state are preserved.
- `cgroup.kill` — terminates every process in the subtree, correctly handling processes that fork mid-kill.
- `memory.oom.group` — makes an OOM kill atomic across the whole group instead of picking one victim process.

**eBPF (extended Berkeley Packet Filter):** a way to run small, verified programs inside the Linux kernel (or, via bpftime, in userspace) without writing a kernel module. An eBPF program attaches to a *hook point* (a syscall, a kernel function entry/exit, a tracepoint, a network event) and runs every time that hook fires. Before it's allowed to load, the **verifier** statically proves the program terminates and can't corrupt memory — this is what makes eBPF safe to run in production instead of requiring a custom kernel module. eBPF programs communicate with userspace via **maps** (key-value structures both kernel and userspace code can read/write) and **ring buffers** (for streaming events out).

**Hook types you'll actually use:**
- **Tracepoint** — a stable, kernel-maintained instrumentation point (e.g. `sched_process_exec`). Won't break across kernel versions.
- **Kprobe/kretprobe** — a dynamic probe on almost any kernel function's entry/exit. Less stable across versions than a tracepoint, more flexible.
- **Uprobe/uretprobe** — the userspace equivalent, probing a *userspace* function (e.g. `SSL_read` in OpenSSL — this is literally how AgentSight reads TLS traffic before it's encrypted).

**sched_ext:** a Linux subsystem (mainlined in 6.12) that lets you define CPU scheduling policy in a BPF program instead of the kernel's built-in scheduler. Comes with automatic fail-safe reversion to the default scheduler if your BPF program errors out — you cannot permanently wedge the machine's scheduler by shipping a bad policy.

**memcg_bpf_ops:** a **not-yet-upstream** (RFC, under review as of Jan 2026) extension that adds BPF hooks into the memory controller — specifically, a hook called `get_high_delay_ms` that lets a BPF program decide how long to throttle a process that's crossed its `memory.high` soft limit, instead of using the kernel's fixed default behavior. This is the piece that needs a patched kernel — see §8.

**PSI (Pressure Stall Information):** the kernel's built-in signal for "processes are stalling waiting on a resource" (CPU, memory, or IO). Tools like `oomd` watch PSI and react when pressure crosses a threshold. Relevant because the AgentCgroup paper explains in detail *why* PSI-based reaction is too slow for agent workloads (§4, below) — you should understand this argument, not just take "PSI is too slow" on faith.

**CO-RE (Compile Once, Run Everywhere):** a technique (via `libbpf`) that lets one compiled eBPF program run across different kernel versions without recompiling, by resolving struct-field offsets at load time instead of compile time. AgentCgroup's prototype uses this.

**Boundary tracing (AgentSight's term):** monitoring an agent from *outside* its application code, at the two points every agent must pass through no matter what framework it's built on — the kernel (for any system action) and the network (for any LLM API call). The alternative — instrumenting inside LangChain/AutoGen/whatever — breaks every time the framework's internals change. Boundary tracing doesn't care what framework you're using.

---

## 4. The Problem — Why Agent Workloads Break Existing Resource Management

This is the actual argument from AgentCgroup §4, in full, because the whole design only makes sense once you understand *why* the obvious approaches fail.

**How agent workloads compare to workloads existing tools were built for** (this is the paper's real Table 1):

| Dimension | Serverless/FaaS | Microservices | Batch/HPC | AI Coding Agent |
|---|---|---|---|---|
| Execution duration | 100ms–2s | Long-running | Minutes–hours | 5–11 minutes |
| Container image | ~50 MB | 100 MB–1 GB | 1–10 GB | 2.9–17.3 GB (median 3.5) |
| Statefulness | Stateless | External state | Stateful | In-process stateful |
| Memory footprint | 128–512 MB | Steady ~1 GB | Scales with data | 185 MB idle, peaks 2–4 GB |
| **Memory peak/avg ratio** | ~1.5× | 2–3× | ~1× | **15.4×** |
| CPU utilization | Brief spike | 10–40% | 80–100% | <13% avg, peaks >175% |
| Determinism | Deterministic | Mostly deterministic | Deterministic | 1.8× variance, same task, different runs |
| Resource pattern | Flat | Steady + daily cycle | Stable rise | Burst-silence alternating |
| Termination cost | Just retry | Can migrate | Lose progress | **Lose all LLM context** |

Nothing in that right-hand column looks like anything existing tools were designed around. That last row matters more than it looks: killing a serverless function costs you a retry; killing an agent mid-task costs you every accumulated reasoning step, and — because agents are non-deterministic — restarting doesn't even guarantee it converges to the same solution the second time.

**Three specific mismatches** (the paper's Table 2, expanded with the actual reasoning):

**1. Granularity mismatch.** Container-level policies set one number for the whole container. `memory.max` set to the task's peak wastes >90% of allocated memory (the peak only happens <2% of the time). Set to the average, and a burst OOM-kills the container and destroys the agent's accumulated context. `memory.high` (soft limit) doesn't fix this either — reclaim pressure can't tell the difference between the framework's own memory (Node.js heap, V8 JIT cache — memory you *don't* want reclaimed) and a subprocess's memory (memory you're fine reclaiming). Kubernetes QoS classes don't help: Guaranteed wastes the same way hard limits do, BestEffort risks killing a stateful agent, Burstable still can't express a per-tool-call quota. **The fix has to operate below container granularity, at the tool-call level.**

**2. Responsiveness mismatch.** Agent memory bursts last 1–2 seconds and can change at multiple GB/second. PSI-based tools (`oomd`, `systemd-oomd`) work by watching pressure and reacting — but the full loop (pressure signal → userspace daemon reads it → daemon decides → daemon writes a control file) takes tens of milliseconds. By the time that round-trip completes, a 1–2 second burst is often already over, or has already triggered the very OOM you were trying to prevent. Kubernetes VPA reacts at Pod-restart or minute-level in-place-resize granularity — three to four orders of magnitude too slow. **The fix has to react at kernel speed (microseconds), not userspace-daemon speed (milliseconds-to-minutes) — which is exactly what eBPF hooks running directly at the cgroup enforcement point buy you.**

**3. Adaptability mismatch.** Google's Borg and Autopilot use historical utilization data to predict future needs — this works because traditional cloud workloads are largely repeatable. Agent workloads violate this three separate ways: (a) resource demand varies **20× across different tasks**, so any single predicted value is wrong most of the time; (b) the *same task run twice* varies 1.8× in execution time and takes a genuinely different solution path each time (this was measured directly — see §6) — so even *task-specific* history isn't reliable; (c) within a *single* run, retry loops cause memory to accumulate progressively, so demand grows unpredictably even mid-execution. And when prediction inevitably fails, the traditional fallback — kill and restart — imposes a triple penalty specific to agents: slow recovery (cold-starting a multi-GB container eats 31–48% of total task time), lost state (context can't be checkpointed like external state can), and non-deterministic re-execution (no guarantee of reaching the same or even a working solution). **The fix has to prefer graceful degradation — throttle, then freeze — over termination, and let the agent itself adapt rather than relying on a history it can't trust.**

---

## 5. How It Works — Full System Architecture

### 5.1 The agent execution model this is built around
Every agent framework this targets (Claude Code, OpenHands, SWE-agent, Cursor Agent) implements the same loop: the LLM reasons about the current state and emits a structured tool-use request (tool name + arguments); the framework parses that request and **forks a subprocess inside a sandboxed container** to actually run the tool (compile, test, edit a file, spawn a sub-agent); the subprocess's result gets collected and fed back to the LLM for the next reasoning step. Every resource spike this project cares about happens inside that forked subprocess, not inside the LLM-reasoning step — LLM reasoning is comparatively resource-light (low, stable CPU/memory); tool execution is where the bursts live.

### 5.2 Stage 1 data flow (measurement only)
```
Agent framework (Claude Code)
  → emits tool-use request
  → [WRAPPER intercepts here, before subprocess spawn]
       records: timestamp, tool type, command
  → subprocess runs
       [WRAPPER samples: psutil memory_info() + cpu_percent() at ~1s intervals]
  → subprocess exits
       [WRAPPER records: end timestamp, peak memory, avg memory, avg CPU]
  → result returned to LLM
  → [WRAPPER appends one JSON line to the run's log file]
```
No cgroups, no eBPF, no root required. This is the whole of Stage 1 — a wrapper and an analysis pass, cross-platform.

### 5.3 Stage 2 data flow (real enforcement)
```
Agent framework emits tool-use request
  → bash_wrapper intercepts (mirrors AgentCgroup's bash_wrapper.sh)
  → creates ephemeral child cgroup: tool_<pid>_<timestamp>/
  → moves the about-to-run subprocess into that cgroup
  → [if AGENT_RESOURCE_HINT env var was set by the agent: apply it as memory.high for this cgroup]
  → subprocess runs INSIDE the child cgroup
       CPU side: sched_ext policy (BPF, in-kernel) prioritizes/deprioritizes based on
                 declared priority + real-time telemetry, sub-microsecond reaction
       MEM side: memcg_bpf_ops hook (get_high_delay_ms) decides throttle duration
                 if memory.high is crossed; escalates to cgroup.freeze (not kill)
                 if throttling alone isn't enough
  → subprocess exits (or gets throttled/frozen and eventually completes)
  → [if the process was throttled/frozen beyond a threshold: wrapper injects
     natural-language feedback into stderr — e.g. "peak memory hit 1.8GB and
     was throttled; consider narrowing scope" — so the AGENT sees this on its
     next reasoning step and can adapt]
  → cgroup removed
```

### 5.4 The intent-declaration protocol, in full
This is the part that makes it "intent-driven" instead of "just another resource limiter," and it's bidirectional:

**Upward (agent → system):** before a tool call the agent *may* set an environment variable expressing expected need, e.g. `AGENT_RESOURCE_HINT=memory:high` before running a test suite, `AGENT_RESOURCE_HINT=memory:low` before a file read. The wrapper maps these hints to a `memory.high` value for that specific tool call's cgroup. These declarations are **advisory, not binding** — the agent can be wrong, and the system doesn't trust them blindly.

**Downward (system → agent):** when a tool call gets OOM-killed, or throttled past a recovery threshold, the wrapper writes a plain-English explanation into the subprocess's stderr instead of just letting it fail silently — peak memory usage observed, and a suggestion to reduce scope. Because this lands in stderr, the agent's own next LLM reasoning step *sees it* as part of the tool call's output, and can decide to retry differently. This closes the loop: the system doesn't just enforce a policy, it teaches the agent (turn by turn) what the policy actually is.

**Why this matters and isn't just a nice-to-have:** recall from §4 that agents can't be predicted from history, but they *can* be told things and *can* read tool output. The intent-declaration protocol is what actually exploits that — it's the mechanism, not just a design flourish.

### 5.5 Worked example — one tool call, start to finish (Stage 2, fully built)
1. Agent decides to run the test suite for a change it just made. It sets `AGENT_RESOURCE_HINT=memory:high` and emits a Bash tool call: `pytest tests/`.
2. The wrapper intercepts before the subprocess spawns. It creates cgroup `tool_48213_1755000000/`, sets `memory.high` to the "high" tier value (say 2GB, configurable), and moves the about-to-be-forked `pytest` process into it.
3. `pytest` runs. Memory climbs to 1.4GB over 6 seconds (matches the paper's observed pattern — test execution is one of the two dominant, memory-heavy Bash categories).
4. Meanwhile, a second concurrent tool call from a different agent session — a `git status` — is running in its own sibling cgroup, using ~13MB. No contention; both proceed normally. (If there *were* contention — memory pressure on the parent — the `sched_ext` and `memcg_bpf_ops` hooks decide in-kernel, in microseconds, which cgroup gets throttled first, based on declared priority, without waiting for a userspace daemon.)
5. `pytest` finishes normally, exits 0. The wrapper records the peak (1.4GB), tears down the cgroup, and returns the result to the agent as normal — no throttling was needed, so no feedback message is injected.
6. **Alternate ending:** if `pytest` had instead spiked to 3.5GB and gotten throttled under contention, step 6 would instead inject into stderr something like: *"Note: this tool call peaked at 3.5GB and was throttled for 340ms due to memory pressure. Consider running a narrower test subset."* — visible to the agent on its next turn.

---

## 6. Research Grounding — The Real Numbers, In Full

Everything below is from AgentCgroup §3 (fetched and read in full, not summarized from a snippet). Their experimental setup: single machine (Intel Core Ultra 9 285K, 24 cores, 128GB DDR5, Ubuntu 24.04, Linux 6.15.11), Podman containers, Claude Code as the agent, two backends (Claude Haiku 4.5 cloud API, GLM-4.7-Flash local GPU), 144 tasks total from SWE-rebench (111 GLM, 33 Haiku — the 33 Haiku tasks are a subset of the 111, so cross-model comparisons use the shared overlap).

**Timing:** tasks run 5–11 minutes (GLM mean 10.8 min, Haiku mean 5.8 min, overall median 8.1 min). Of that: LLM reasoning is 26–44%, tool execution is ~40% of active time (mean 42.5% Haiku / 36.4% GLM, individual tasks ranging 0–86%), initialization is 29–45%. Combined, OS-level overhead (init + tool execution) is **56–74%** of what the user actually waits for.

**What dominates tool execution:** Bash, overwhelmingly. In Haiku, Bash + sub-agent delegation account for >90% of tool time (47.8% / 43.2%); GLM relies almost entirely on Bash (98.1%). Within Bash: **test execution dominates** (pytest etc. — 72.9% of Bash time for Haiku, 43.7% for GLM), followed by package installation (~10%) and Python snippets (26.9% for GLM). Sub-agent calls average ~100s each; plain Bash commands 4–6s; lightweight tools (Read/Edit) under 0.5s.

**Memory has a two-layer structure.** A stable ~185MB framework baseline (Claude Code's Node.js runtime — Haiku 183MB, GLM 188MB) plus tool-call-driven bursts to 500MB–2GB. This is why container-level limits are the wrong tool: the *baseline* is safe to leave alone, the *bursts* are what need per-call control.

**The concrete worked example from the paper itself** (worth keeping in mind while implementing, since it's the single most illustrative data point): task `pydicom/pydicom#2022` (a medical imaging library) hit a peak memory of 4060MB against an average of only 264MB — a **15.4× ratio**, the most extreme case in their dataset, falling back to the ~230MB baseline within seconds. Compare: `streamlink/streamlink#2160` (a network streaming tool) needed only 291MB peak. Same container image budget would have to cover both.

**Bursts are genuinely concentrated in tool calls, not spread out:** in Haiku, tool calls occupy only 50.6% of sampling time yet contain 98.5% of all memory bursts >300MB; in GLM, 35.9% of time contains 67.3% of bursts. Burst duration is 1–2 seconds; peak memory change rate hits 3GB/s.

**Retry loops are the norm, not the exception:** 85% of Haiku tasks (28/33) and 97% of GLM tasks (108/111) contain retry groups (three or more consecutive identical Bash calls, e.g. repeated failing `pytest` invocations). GLM averages 3.9 retry groups per task (up to 56 consecutive retries in one case), consuming 7.4% (Haiku) to 20.5% (GLM) of total execution time. Each retry cycle **doesn't clean up** — memory accumulates progressively, up to 502MB unreleased in the worst observed case. This is precisely why "kill and restart" is the wrong fallback and "throttle/freeze, don't kill" is the right one.

**CPU-memory correlation is unreliable, per-task:** ranges from -0.84 to +0.50 across tasks (average -0.39) — some tasks show CPU and memory rising together, others show them moving in opposite directions. **Don't build anything that assumes they're linked.**

**Unpredictability, quantified:** peak memory ranges 197MB–4GB across tasks (CV=147%) — a 20× spread. The *same* task run three times (`iterative/dvc#777`) took 402s, 222s, and 259s — a 1.8× spread — and produced three genuinely different solutions each time. Even LLM-observable proxies don't help: conversation-round count correlates moderately with execution time (r=+0.57 to +0.82) but essentially not at all with peak memory (r<0.11); output token count vs. peak memory is r=-0.14. **This is the empirical basis for §4's claim that history-based prediction cannot work here — it's not a design opinion, it's a measured result.**

**Container overhead is real:** images average 3.5GB (range 2.9–17.3GB across 114 deduplicated images used in the study) — 7× a typical microservice image, 70× a typical serverless function. Initialization (dominated by Podman's user-namespace remapping of overlay layers, scaling with image size) eats 31–48% of total task time. This is a separate, currently out-of-scope problem (see §9) but worth knowing about since it's a big chunk of the "OS overhead" number.

**The enforcement result** (once AgentCgroup itself runs, from §6 of the paper): replaying three real agent traces concurrently (one HIGH-priority at `memory.high=max`, two LOW-priority at `memory.high=400MB`) at 50× speed under tight memory (1100MB total for ~1233MB combined demand): the no-isolation baseline OOM-kills a LOW process (66% survival); with the BPF controller, **100% of processes complete** by throttling LOW allocations (239 delay triggers observed) while HIGH finishes with only +2.8% overhead. **HIGH-priority P95 allocation latency drops 29% (70.97ms → 50.14ms).** Enforcement overhead is negligible otherwise (P50 latency +0.3%, total completion time -1.1%). This was run on a **4-core, 16GB RAM machine** (Intel Core Ultra 7 258V) — modest, reproducible hardware, not a cluster.

**What the paper itself says is still open** (i.e., legitimate territory for this project to claim): "our current evaluation is limited to trace replay with a proof-of-concept prototype; the characterization covers one agent framework and one benchmark." Named future work: live (non-replayed) agent workloads, validation "across diverse tasks and agent frameworks," and "fine-grained resource control across diverse container runtimes." Any of these is a real, well-scoped contribution — not busywork.

---

## 7. The System We're Extending — AgentSight In Detail

AgentCgroup's prototype is explicitly built "extending AgentSight" — so understanding AgentSight's actual architecture (not just its existence) matters for Stage 2.

**The problem AgentSight solves:** existing observability tools sit on one side of a "semantic gap." Application-level tools (LangSmith, Langfuse, Datadog) see an agent's *intent* (the LLM's reasoning and tool selection) but go blind the instant the agent does something outside the framework's own instrumentation — one raw shell command escapes their view entirely. System-level tools (Falco, Tracee) see every syscall but have zero semantic context — to a syscall tracer, an agent writing a legitimate data-analysis script and a compromised agent writing a malicious payload look identical.

**The fix — boundary tracing:** monitor at the two places every agent, regardless of framework, must pass through: the **kernel** (any system action) and the **network** (any call to an LLM backend). Both are stable and can't be bypassed by an agent without literally not running any code, so this approach is framework-agnostic and doesn't break when LangChain/Claude Code/whatever changes its internals next month.

**Concretely, two eBPF-collected streams:**
- **Intent Stream:** a uprobe on `SSL_read`/`SSL_write` in the crypto library (OpenSSL) intercepts LLM API traffic **after TLS decryption but before it leaves the process** — so you get plaintext prompts/responses without needing a MITM proxy or any network-level packet capture. A userspace reassembly layer handles streaming protocols (Server-Sent Events) since a single logical LLM response arrives as many small reads.
- **Action Stream:** the `sched_process_exec` tracepoint builds a live process lineage tree (so a `bash → python → gcc` chain is understood as one causal unit descending from the agent), and kprobes on syscalls like `openat2`, `connect`, `execve` capture what that lineage actually does. Aggressive **in-kernel filtering** — a BPF program that only forwards events from the agent's own process tree — keeps this from drowning in irrelevant system noise, and keeps overhead low since filtering happens before anything crosses into userspace.

**Correlating the two streams (this is the actual hard part):** a Rust userspace daemon runs a two-stage engine. Stage 1 (real-time, ~100–500ms window) does heuristic linking via three signals used together: **process lineage** (which process descends from the agent), **temporal proximity** (did this syscall happen right after that LLM response), and **argument matching** (does a filename/URL/command in the LLM's response literally appear in the following syscall's arguments). Stage 2 takes the resulting coherent trace and hands it to a **second LLM acting as a security analyst**, which returns natural-language reasoning plus a confidence score — this is the "AI watching AI" layer, and it's what catches things that don't match a predefined rule.

**Implementation scale, for realistic expectations:** the real system is ~6000 lines of Rust/C for the daemon + eBPF programs, plus a ~3000-line TypeScript frontend. This is not a weekend project to reproduce in full — but you're not reproducing it, you're extending/forking specific pieces (the eBPF probes and cgroup wiring, primarily), not rebuilding the correlation engine or the frontend.

**Real measured overhead** (their actual results, not the ambiguous ">95% coverage" figure from the original document, which does not appear anywhere in the paper and should not be cited): Understand Repo 127.98s→132.33s (+3.4%), Code Writing 22.54s→23.64s (+4.9%), Repo Compilation 92.40s→92.72s (+0.4%) — average 2.9% overhead, consistent with their "<3%" headline claim.

**What it's actually caught, per their case studies (useful as test scenarios for your own build):** an indirect prompt-injection attack (a README pointed the agent to a URL with a hidden prompt instructing it to exfiltrate `/etc/passwd`; 521 raw events correlated down to 37 in the causal chain); a reasoning loop (a crewai + gpt-4o-mini agent stuck retrying the identical failing web-search call — a real "try-fail-re-reason" failure mode worth specifically testing for, since retry loops are also exactly what AgentCgroup's memory-accumulation finding is about); and multi-agent coordination bottlenecks (6 concurrent Claude Code sub-agents, 3153 events, revealed file-locking contention between a frontend agent and a test agent).

---

## 8. Hardware / Environment Reality

Current setup: primary machine is Windows (RTX 5050, 8GB VRAM, 16GB RAM) — fine for Stage 1, not usable for Stage 2 as-is. Secondary laptop (i5-8300, GTX 1050) needs an OS reinstall that hasn't happened yet. No currently-accessible Linux box.

| Option | CPU-side (sched_ext) | Memory-side (memcg_bpf_ops) | Effort | Recommendation |
|---|---|---|---|---|
| **A. Finish the laptop reinstall** (Debian/Arch) | Works out of the box on 6.12+ | Needs manual patch build (apply RFC series, rebuild kernel) | Medium | **Best long-term option** |
| **B. WSL2 on the Windows machine** | Only with a custom-built WSL2 kernel; fiddly | Same problem, compounded | Low to start, high to finish | Fine for **Stage 1 only** |
| **C. Cloud Linux VM** | Works with a 6.12+ image | You control the kernel, so buildable | Low setup, ongoing cost | Good **bridge option** |

**Recommendation:** do Stage 1 now, on Windows, no blockers. Use Stage 1's timeline as the deadline to finish the laptop reinstall. Attempt `sched_ext` (Stage 2a, no patch needed) first once the laptop is up; treat `memcg_bpf_ops` (Stage 2b) as a separate, harder task — either build the patched kernel yourself, or periodically check whether the upstream RFC has landed (https://lwn.net/Articles/1055698/).

---

## 9. Scope

**In scope:** Stage 1 characterization wrapper + analysis (cross-platform); Stage 2a CPU-side enforcement via `sched_ext`; Stage 2b memory-side enforcement via `memcg_bpf_ops` (gated on §8); a real evaluation replicating §6's measurement shape on a different agent framework, benchmark, or live-workload setting than the original paper used.

**Out of scope for now** (separate task if it ever happens, don't let it creep in): multi-tenant cluster-scale evaluation; anything touching `bpftime`'s own internals or the eBPF verifier; GPU memory control (architecture #5 from the original research — rated low-viability separately); a polished UI/dashboard (CLI + logs + plots is enough for a research-grade result); solving the container-initialization overhead problem (31-48% of task time, per §6 — real, but a distinct problem from resource control).

---

## 10. Which Agent to Characterize

Earlier I suggested the GraphRAG legal engine. Flagging now, before any code is written: that engine was built during the Varma & Varma internship — confirm you're clear to reuse/profile that code before using it. If there's any doubt, don't.

**Primary recommendation:** characterize **Claude Code itself**, on a handful of real or SWE-rebench-style coding tasks. Closest one-to-one replication of the actual paper's method (they used Claude Code too — same agent, same general task shape), directly comparable numbers, zero IP ambiguity.

**Optional stretch, once the primary result exists and only if cleared:** the GraphRAG engine gives a second, genuinely novel data point — a retrieval-heavy agent instead of a pure coding agent, which the original paper doesn't cover. Don't block Stage 1 on resolving this.

---

## 11. Implementation Instructions

### Stage 1 — Characterization wrapper
1. Repo setup: feature-organized structure (`features/wrapper/`, `features/analysis/` — not layered `models/`/`views/`), `docs/` folder for design notes (comments minimal, code close to self-explanatory), pytest + coverage from commit one.
2. **Data schema** — one JSON line per tool call, appended to a per-run log file:
   ```json
   {"task_id": "...", "tool_type": "bash|read|edit|subagent|...",
    "command": "...", "start_ts": 0.0, "end_ts": 0.0,
    "peak_memory_mb": 0.0, "avg_memory_mb": 0.0, "avg_cpu_pct": 0.0,
    "samples": [{"t": 0.0, "mem_mb": 0.0, "cpu_pct": 0.0}, ...]}
   ```
   Keep the raw per-second samples, not just the aggregates — you'll want them for burst-shape analysis later (§6's "1-2 second burst" and "3GB/s change rate" findings both came from per-second sampling, not just peak/avg).
3. Build the interception wrapper: hook into Claude Code's tool-use loop (or your chosen CLI agent) so every tool call is timestamped at start and end, with a background sampler polling `psutil.Process(pid).memory_info()` and `.cpu_percent()` at ~1s intervals for the duration.
4. Run across a batch of tasks — dozens is enough for a first real result, doesn't need to match the paper's 144.
5. **Analysis pass, concretely:**
   - OS-execution-vs-reasoning split (compare against 56–74% / 26–44%)
   - Peak-to-average ratio, overall and per-task (compare against 15.4×, and specifically check whether your data has a pydicom-style outlier task)
   - Per-tool-type breakdown (compare against the Bash-dominance / test-execution-dominance finding in §6)
   - Retry-loop detection: flag 3+ consecutive identical Bash calls, measure what fraction of tasks have them (compare against 85-97%)
   - CPU-memory correlation per task (compare against the -0.84 to +0.50 spread — don't be surprised if yours doesn't correlate either)
6. Write findings to `docs/stage1-findings.md`: where your numbers match the paper's *shape*, where they diverge, and your best explanation why (different agent/tasks/hardware are all legitimate — the interesting question is whether burst-silence and memory-not-CPU-as-bottleneck hold up).

**Definition of done, Stage 1:** a script anyone (including Opus, next session) can re-run against a fresh task batch and get comparable characterization output, plus the written comparison doc.

### Stage 2a — CPU-side enforcement (sched_ext)
1. Don't start until the target machine is confirmed on 6.12+ with `sched_ext` available (`ls /sys/kernel/sched_ext/`).
2. Start from AgentCgroup's `scx_flatcg` component (GPL-2.0, open) rather than writing a scheduler from zero.
3. Wire it to the Stage 1 wrapper: each intercepted tool call gets its own ephemeral cgroup (`tool_<pid>_<ts>/`, mirroring the real naming).
4. Test with synthetic contention (two tool calls competing for CPU) before real agent traffic, so there's a clean before/after number — mirror the paper's tight-memory experiment shape (§6) even though this half is CPU, not memory.

### Stage 2b — Memory-side enforcement (memcg_bpf_ops)
1. Gated on §8 — needs the RFC-patched kernel. Start after 2a is solid.
2. Start from AgentCgroup's `memcg/` component, not from zero.
3. Implement the full bidirectional intent-declaration protocol from §5.4 — the env-var hint upward, the stderr feedback-on-throttle downward. This is the part that's easy to skip and shouldn't be — it's the actual novel contribution, not the enforcement mechanics alone.
4. Reproduce the paper's own evaluation shape as your test: concurrent HIGH/LOW priority traces under tight memory, measure survival rate and P95 latency, compare against their 66%→100% survival and 29% P95 improvement.

### Throughout
- **Git:** one file per commit. `CHANGELOG.md` at file-level granularity. Opus gives you `git commit -m "..."` commands to run yourself — never pushes with its own credentials unless told otherwise.
- **Errors:** log and continue (graceful degradation), not fail-fast. Full stack trace + context on failure.
- **Tests:** unit tests for wrapper parsing/timing, integration tests for a full tool-call-to-log cycle, end-to-end once Stage 2 enforcement exists. pytest + coverage report as standard.
- **Type hints:** present, not exhaustive. **Async:** don't use it — sync throughout.
- **Performance/logging/security:** built in from each stage's first commit, not retrofitted — sloppy instrumentation overhead would directly contaminate the measurements Stage 1 exists to collect.
- **Scope creep:** if Opus wants to build a dashboard, add a database before Stage 2 needs one, or start 2b before 2a is solid — stop and flag it explicitly rather than just doing it.

---

## 12. Coding Standards
Feature-organized structure · minimal comments, self-explanatory code · docs/ folder, not inline docstrings · graceful degradation on errors, full context in logs · sync only · pragmatic type hints · pytest + coverage · PostgreSQL if/when persistent storage is needed · one file = one commit · commit messages treated as real documentation · watermark comment in every source file.

---

## 13. Definition of Done
**Stage 1:** reproducible characterization script + written comparison against the paper's findings (§6).
**Stage 2a:** working `sched_ext` policy demonstrating a measurable before/after under synthetic CPU contention.
**Stage 2b:** working `memcg_bpf_ops` policy reproducing the paper's survival-rate and P95-latency experiment shape, plus the full intent-declaration protocol (both directions) actually wired up and demonstrated on at least one real throttling event.

---

## 14. Non-Goals
Cluster-scale multi-tenant evaluation · touching bpftime/verifier internals · GPU memory control · a UI/dashboard · fixing container-initialization overhead (real problem, different problem).

---

## 16. References
- AgentCgroup paper (full text used for this spec): https://arxiv.org/abs/2602.09345
- AgentCgroup repo: https://github.com/eunomia-bpf/agentcgroup
- AgentSight paper (full text used for this spec): https://arxiv.org/abs/2508.02736
- AgentSight repo: https://github.com/eunomia-bpf/agentsight
- SWE-rebench: https://arxiv.org/abs/2505.20411
- memcg BPF hooks RFC (the not-yet-upstream patch): https://lwn.net/Articles/1055698/
- sched_ext docs: https://docs.kernel.org/scheduler/sched-ext.html
- cgroup v2 docs: https://docs.kernel.org/admin-guide/cgroup-v2.html
- bpftime (userspace eBPF runtime, referenced by both papers): https://github.com/eunomia-bpf/bpftime
