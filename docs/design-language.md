# Design language

Cordon's visual identity is cordon tape — black and hazard-amber diagonal stripes. The
connection isn't decorative: the project's whole job is drawing a boundary around a subprocess
and deciding what may cross it, which is exactly what a cordon does. The name picked the palette.

## Palette

| Token | Hex | Use |
|---|---|---|
| `cordon-black` | `#0d0d10` | Background, label side of every badge |
| `cordon-amber` | `#f5c400` | Primary accent — wordmark, stripe, badge fill |
| `cordon-amber-dim` | `#9a5b00` | Secondary state — "partial" / blocked / in-progress |
| `cordon-gray` | `#9a9a9a` | Body text on dark backgrounds, taglines |
| `cordon-gray-dim` | `#5c5c62` | Captions, metadata rows |

Two colors do almost all the work: black and amber. `cordon-amber-dim` exists only to mark
something as not-yet-complete (Stage 2's kernel-side control layer, currently) without
introducing a third hue. Don't add a fourth without a reason — the palette stays this small on
purpose, the same way the codebase stays sync-only on purpose.

## Type

No custom or embedded fonts, anywhere. Wordmarks use `Arial, Helvetica, sans-serif` at heavy
weight with wide letter-spacing (uppercase, tracked out — reads like a stencil). Everything else
— taglines, metadata, repo paths — uses `Consolas, 'Courier New', monospace`. Both are system
font stacks so the SVGs render identically in GitHub's sanitized SVG viewer, a browser, or a
raster export; nothing depends on a webfont surviving GitHub's sanitizer.

One trap when editing a tracked wordmark: browsers add `letter-spacing` after the *final* glyph
too, and `text-anchor="middle"` centres on that full advance — so a tracked word centred at
`x="600"` renders half the tracking left of true centre. The banner compensates with `x="609"`
against `letter-spacing="18"`. Any new tracked, centred wordmark needs the same half-tracking
nudge, or it will sit visibly off-centre and crowd whatever is to its left.

## Assets

- `assets/banner.svg` — README header, 1200×320. Referenced directly in `README.md`; renders
  natively on GitHub, no build step. The canvas is 320 rather than a rounder 300 because the
  bracket glyphs flanking the wordmark descend to y=169 and the tagline sits at y=225 — at 300
  the two collided.
- `assets/mark.svg` — 200×200 square mark (amber "C" monogram, black ground, hazard-stripe
  footer band). Source for the repo avatar and any favicon; export a PNG at whatever size a
  given surface needs rather than committing multiple raster sizes.
- `assets/social-preview.png` — 1280×640, GitHub's required social-preview dimensions. Upload it
  at **Settings → General → Social preview**. GitHub won't pick this up automatically; it has to
  be uploaded by hand once. It is its own composition, not a crop of the banner: the mark stacked
  above an unbracketed wordmark, with the repo URL beneath, and the mark inverted to amber-ground
  so it holds up as a thumbnail in a feed. No vector source is committed for it — if the wordmark
  or tagline changes, rebuild the composition at 1280×640 rather than hand-editing the PNG.

## Badges

All custom badges are shields.io static badges, `style=flat-square`, `labelColor=0d0d10`
(black), `color=f5c400` (amber) — flat-square reads as systems tooling; the rounded default and
`for-the-badge` variants both read as a consumer product, which this isn't. The one exception is
the native GitHub Actions CI badge, which can't be recolored and sits next to the custom ones
without trying to match.

```
[![CI](https://github.com/uncoalesced/cordon/actions/workflows/ci.yml/badge.svg)](https://github.com/uncoalesced/cordon/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-f5c400?style=flat-square&labelColor=0d0d10)](LICENSE)
![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-f5c400?style=flat-square&labelColor=0d0d10)
![Stage 1: measuring](https://img.shields.io/badge/stage%201-measuring-f5c400?style=flat-square&labelColor=0d0d10)
![Stage 2: partial](https://img.shields.io/badge/stage%202-partial-9a5b00?style=flat-square&labelColor=0d0d10)
[![Grounded in AgentCgroup / AgentSight](https://img.shields.io/badge/grounded%20in-AgentCgroup%20%2F%20AgentSight-0d0d10?style=flat-square&labelColor=f5c400)](docs/stage1-design.md)
```

`README.md` carries the same seven badges as raw `<a>`/`<img>` HTML rather than the Markdown
above, because they sit inside a `<p align="center">` and Markdown image syntax can't be centred
on GitHub. Keep the two in sync by URL, not by copying this block over — pasting the Markdown
into the README silently loses the centring.

Update the two stage badges as the project moves — flip "Stage 2: partial" to
`cordon-amber`/"enforcing" once `sched_ext` lands, and drop "measuring" from Stage 1 for
"characterized" once `stage1-findings.md` has a real results table instead of a template.

## Repo metadata (not a file, but part of being found)

Visual identity gets the repo recognized once someone lands on it; GitHub topics get it
surfaced in the first place. Set these under **Settings → General → Topics**:

`ebpf` `cgroups` `cgroup-v2` `linux-kernel` `sched-ext` `resource-management` `ai-agents`
`agentic-ai` `observability` `claude-code` `psutil` `psi`

Repo description (the one-liner GitHub shows under the name in search and on the profile grid):

> Tool-call-granularity CPU/memory characterization and control for AI coding agents, built on
> cgroup v2 and eBPF.
