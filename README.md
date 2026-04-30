# ~▲~ crüx

A keyboard-first terminal UI for browsing, curating, and building sample kits with LLM-powered assistance.

## Quick Start

```bash
cd crux-tui
python3 crux.py              # Launch the TUI
python3 crux.py import ~/samples/  # Import + analyze a folder
```

## Keybindings

```
/            Jump to search input (from any pane)
j/k / arrows Navigate lists
Enter        Add sample to slot / Set target slot (kit grid)
1-8          Send sample directly to slot 1-8
Delete       Clear slot
Space        Lock/unlock slot
P            Play preview
Tab          Cycle browse ↔ kit grid
Ctrl+S       Settings (open/save)
Ctrl+E       Export kit
Ctrl+T       Tag: start/pause/resume batch LLM tagging
Escape       Clear search
```

## Model Selection & Performance

crüx works with any OpenAI-compatible LLM endpoint (LM Studio, Ollama, OpenAI, etc.).

Tagging 14,000 samples at batch size 5 (2800 LLM calls):

| Model | Params | Per batch | Total time | Quality |
|-------|--------|-----------|------------|---------|
| **Qwen 3.5 9B** | 9B | ~3-5s | **~3-4 hrs** | Good — fast tagging, sensible notes |
| **Gemma 4 26B** | 26B | ~8-15s | **~6-10 hrs** | Better — richer descriptions, more accurate genre |

**Recommendation:** Use Qwen 9B for tagging (speed) and Gemma 26B for kit building (quality). Set in `~/.crux/config.toml` or `Ctrl+S` → Settings.

**Tagging is pausable:** `Ctrl+T` starts. `Ctrl+T` again pauses after the current batch. `Ctrl+T` again resumes where it left off. Safe to close the app and resume later — it only tags still-untagged samples.

## Architecture

- **Single-file Python** (~2100 lines) using [Textual](https://textual.textualize.io/)
- **SQLite + FTS5** — portable sample database alongside audio files
- **librosa** — spectral analysis (RMS, centroid, flatness, onset, transients) on import
- **LLM** — build kits from natural language prompts, tag samples, refine by direction

### Data Flow

1. **Import**: `crux.py import ~/samples/` → librosa analyzes spectral features → inserts into SQLite (no LLM yet)
2. **Tag**: `Ctrl+T` → LLM generates tags, genre, and one-line sonic description per sample
3. **Search**: FTS5 full-text search across name, tags, genre, notes, path
4. **Build**: Type a prompt like "build a 909 techno kit" → LLM selects 8 matching samples
5. **Refine**: "refine darker" or "refine the kick" → LLM replaces unlocked slots with better spectral matches
6. **Export**: `Ctrl+E` → numbered WAVs in format-specific naming (Ableton, SP-404, MPC)

## Commands

| Command | What it does |
|---------|-------------|
| `build a heavy techno kit` | LLM builds a full 8-slot kit |
| `refine darker` | Replace unlocked slots with darker-sounding samples |
| `refine the kick` | Only replace the Kick slot |
| `refine 707` | Fill empty slots with 707 samples |
| `/tag punchy 808 reverb` | Edit tags on the currently selected sample |
| `/notes Punchy 808 kick, good for techno` | Edit AI notes on the selected sample |
| `/kick` | FTS5 search (bypasses LLM) |

## Themes

Set in Settings (`Ctrl+S`): **Shark** (dark, default), **Amber**, **Matrix**, **Paper**.

## Config

`~/.crux/config.toml` — LLM provider, model, paths, kit slots (default 8).

## Project

- Built for producers who live in the terminal
- No mouse needed
- No emoji — minimal aesthetic
- Logo: `~▲~` = shark fin cutting through water
