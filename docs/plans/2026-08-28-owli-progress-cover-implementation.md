# Owli Progress Cover Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Build a public-safe Owli progress-status cover image source and exportable PNG/GIF assets.

**Architecture:** Create one standalone HTML asset that uses inline CSS and SVG, with no network dependencies. Export static and animated versions through a small Playwright-based script so the same source can support README, project pages, and social sharing.

**Tech Stack:** HTML, CSS, SVG, JavaScript, Playwright Python.

---

### Task 1: Create the Cover Source

**Files:**
- Create: `docs/assets/readme/owli-progress-status-cover.html`

**Step 1: Create a standalone HTML file**

Build a 1200 x 630 social-cover canvas with:
- title `Owli 进展状态`
- subtitle `已越过方案验证，正在打磨真实全链路稳定性`
- center Owli mark
- five short status modules
- one bottom next-step strip

**Step 2: Keep the copy public-safe**

Verify the file does not mention private docs, local paths, commit IDs, test counts, customer names, account state, budgets, or Feishu links.

**Step 3: Review the visual source**

Run:

```bash
sed -n '1,260p' docs/assets/readme/owli-progress-status-cover.html
```

Expected: the file is standalone and contains no external network references.

### Task 2: Add an Export Script

**Files:**
- Create: `scripts/render_owli_status_cover.py`

**Step 1: Write the script**

Use Playwright to open the local HTML file and screenshot `#cover` to:
- `docs/assets/readme/owli-progress-status-cover.png`
- optionally `docs/assets/readme/owli-progress-status-cover.gif`

**Step 2: Add explicit CLI flags**

Support:
- `--html`
- `--png-out`
- `--gif-out`
- `--frames`
- `--fps`
- `--width`
- `--height`

**Step 3: Verify help output**

Run:

```bash
python3 scripts/render_owli_status_cover.py --help
```

Expected: arguments are listed and the script exits cleanly.

### Task 3: Export and Inspect Assets

**Files:**
- Create: `docs/assets/readme/owli-progress-status-cover.png`
- Create: `docs/assets/readme/owli-progress-status-cover.gif`

**Step 1: Export assets**

Run:

```bash
python3 scripts/render_owli_status_cover.py
```

Expected: PNG and GIF files are written under `docs/assets/readme/`.

**Step 2: Inspect generated files**

Run:

```bash
file docs/assets/readme/owli-progress-status-cover.png docs/assets/readme/owli-progress-status-cover.gif
ls -lh docs/assets/readme/owli-progress-status-cover.png docs/assets/readme/owli-progress-status-cover.gif
```

Expected: PNG is 1200 x 630, GIF exists and has a reasonable size.

### Task 4: Final Safety Check

**Files:**
- Review only: `docs/assets/readme/owli-progress-status-cover.html`
- Review only: `scripts/render_owli_status_cover.py`

**Step 1: Search for sensitive terms**

Run:

```bash
rg -n "Feishu|飞书|/Users|commit|token|key|预算|客户|\\.env|docs-ref|M[0-9]" docs/assets/readme/owli-progress-status-cover.html scripts/render_owli_status_cover.py
```

Expected: no public-sensitive terms in the cover copy.

**Step 2: Check git diff**

Run:

```bash
git diff -- docs/plans/2026-08-28-owli-progress-cover-design.md docs/plans/2026-08-28-owli-progress-cover-implementation.md docs/assets/readme/owli-progress-status-cover.html scripts/render_owli_status_cover.py
```

Expected: only intended files changed.
