# Changelog

## 1.0.0 — 2026-11-04

First public release as **Sumie · 墨绘**.

- Studio web workbench in five languages: 简体中文, English, 日本語, 한국어, Deutsch (auto-detected, switchable).
- Technical whitepaper in Chinese and English (`docs/whitepaper.*.md`) with an architecture diagram.
- READMEs in Chinese, English, Japanese, Korean and German.
- One-step installers: `install.sh` (macOS) and `install.ps1` (Windows).
- `tools/make_release.py` and a tag-triggered GitHub release workflow.
- New logo, demo recording and an original test illustration (`examples/fox.svg`).
- Based on the earlier "Copyist" code base: `anime_human` style with pen-pressure model, human pacing,
  virtual layers and brush colouring, offline bucket-leak model, deadline-aware execution.
- Demo recording re-made with the colour-area border option off (`--borders off`).
- Installer now covers macOS Intel and Apple silicon (via Rosetta 2, installed automatically), Windows x64 and Linux x86_64 (experimental, BepInEx linux-x64 pinned with SHA-256).
