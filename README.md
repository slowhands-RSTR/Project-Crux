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

## Local Curation & Tagging

crüx uses LLMs to automatically tag every sample with: instrument type, applicable genres (multi-genre), sonic character descriptors (dark, bright, punchy, lofi, etc.), and a natural-language description. This powers FTS5 search across your entire sample library.

### Tag Pipeline Architecture

The tagging pipeline runs **concurrently** to maximize throughput:

```
N concurrent workers → LLM API → Parse response → SQLite write
     │                      │            │               │
     │ (batch_size samples │ Prompt:    │ Accepts       │ Auto-reconnect
     │  per worker)        │ ~150 tok   │ any field     │ on stale
     │                     │ per sample │ names via     │ connection
     │                     │            │ catch-all     │
     │                     │            │ parser        │
     └─ Each worker fires  │           └─ Regex salvage └─ WAL mode
        independently      │              for truncated  handles
                           │              JSON responses concurrency
```

### Cloud vs Local

| Aspect | Cloud (DeepSeek, OpenAI) | Local (LM Studio, Ollama) |
|--------|--------------------------|---------------------------|
| **Speed** | 100-200 samples/min | 10-20 samples/min (4 parallel slots) |
| **Cost** | ~$0.03 for 14K samples | Free (electricity only) |
| **Latency** | ~3-5s per batch of 20 | ~15-30s per batch of 8 |
| **Concurrency** | No limit (8-16 workers) | Limited to GPU slots (1-4) |
| **Setup** | API key only | Download model, run LM Studio |
| **Privacy** | Data leaves your machine | Fully offline |
| **Quality** | Excellent (large models) | Good to very good |

**Recommendation:** Use DeepSeek for bulk tagging (fast + cheap). Use local models for ongoing curation and kit building where latency doesn't matter.

### Model Selection Guide

| Model | Params | Active | Use Case | Speed |
|-------|--------|--------|----------|-------|
| **DeepSeek V4** (cloud) | ? | ? | **Tagging** — fastest, cheapest | ~5s/batch of 20 |
| **Qwen 3.5 9B** (local) | 9B dense | 9B | **All-round** — good balance | ~20-30s/batch of 12 |
| **Gemma 4 26B** (local) | 26B MoE | 4B | **Kit building** — richest output | ~8-15s/batch of 8 |
| **Qwen 3.6 27B** (local) | 27B dense | 27B | Not recommended — needs 22GB RAM | N/A |

### Tag Pipeline Configuration

Set in `~/.crux/config.toml` or `Ctrl+S` → Settings:

```toml
[llm]
provider = "openai"           # lm_studio | ollama | openai | custom
url = "https://api.deepseek.com/v1/chat/completions"
model = "deepseek-chat"
api_key = "sk-..."

[import]
tag_batch_size = 20            # Samples per LLM call (higher = fewer calls)
```

| Setting | Local (LM Studio) | Cloud (DeepSeek/OpenAI) |
|---------|-------------------|------------------------|
| tag_batch_size | 8-12 | 20-30 |
| Concurrency (hardcoded) | 4 | 8-16 |
| Timeout | 120s | 30s |

### Genre-Aware Tagging

Each sample receives:

```json
{
  "id": "abc-123-def-456",
  "tags": ["kick", "808", "dark", "punchy"],
  "genres": ["techno", "house"],
  "sonics": ["dark", "punchy", "warm"],
  "notes": "Punchy 808 kick with dark sub-bass, works for techno and house"
}
```

- **tags**: Instrument type + sonic descriptors (kick, 808, dark, punchy, lofi, bright, warm, dirty, clean, boomy, tight, gritty, airy)
- **genres**: Array — a sample can fit MULTIPLE genres (techno AND house share 909 kicks)
- **sonics**: Tonal character derived from spectral analysis (bright=high centroid, dark=low centroid, noisy=high flatness, pure=low flatness, loud=high rms, quiet=low rms)
- **notes**: One-line sonic description

All fields are FTS5-indexed. Searching "techno house dark 909 kick" finds everything relevant.

### Tagging Operations

| Key | Action |
|-----|--------|
| Ctrl+T | Start tagging all untagged samples |
| Ctrl+T (again) | Pause after current batch completes |
| Ctrl+T (again) | Resume tagging where it left off |
| /autotag | Tag the currently selected sample (single) |
| /tag kick 808 dark | Manually edit tags on selected sample |
| /notes Punchy 808 kick | Manually edit notes on selected sample |

**Retag with a different model:**
```bash
sqlite3 /Volumes/LaCie/"Unified Samples"/Samples/Master/crux.db \
  "UPDATE samples SET tags='[]', genre='', ai_notes='' WHERE tags != '[]';"
```
Then switch model in Settings (Ctrl+S) and press Ctrl+T.

### Performance Tuning

**Local (LM Studio):**
- Use models with 4+ parallel slots (Qwen 3.5 9B: parallel: 4)
- Set context length to 32K for batch processing
- Enable flash attention and KV cache offload to GPU
- Models consuming >20GB RAM (27B dense) won't load on 32GB systems

**Cloud:**
- Increase tag_batch_size to 20-30 to amortize network latency
- Concurrency is essentially unlimited — 8-16 concurrent requests
- Most providers cost <$0.05 for a full 14K sample library tag
- DeepSeek is the cheapest at ~$0.03 for the entire library

### Troubleshooting

| Symptom | Cause | Fix |
|---------|-------|-----|
| First batch tags but then nothing | SQLite connection went stale | Auto-reconnect in update_tags() |
| LLM returns valid JSON but nothing written | Wrong field names (category vs tags) | Catch-all parser finds fields by type |
| Truncated JSON responses | LLM hit max_tokens | Regex salvage extracts partial entries |
| Requests timing out | Timeout too short for model speed | Increase timeout: 30s cloud, 120s local |
| Insufficient system resources | Model too large for RAM | Use smaller model or reduce parallel slots |
| Process crashes after 5-8 minutes | asyncio.Lock deadlock | Removed — SQLite WAL handles concurrency |
| TCP connections lost mid-run | aiohttp connection pooling | force_close for local; not needed for cloud |

### Tagging is Pausable

Safe to quit the app mid-tag. All completed tags are written to the DB immediately. On restart, Ctrl+T only picks up still-untagged samples.

### Data Flow Summary

1. **Import**: `crux.py import ~/samples/` → librosa analyzes spectral features → inserts into SQLite (no LLM yet)
2. **Tag**: Ctrl+T → LLM generates tags, genre, and sonic description per sample → written to DB immediately
3. **Search**: FTS5 full-text search across name, tags, genre, notes, path
4. **Build**: Type a prompt like "build a 909 techno kit" → LLM selects 8 matching samples
5. **Refine**: "refine darker" or "refine the kick" → LLM replaces unlocked slots with better spectral matches
6. **Export**: Ctrl+E → numbered WAVs in format-specific naming (Ableton, SP-404, MPC)

## Architecture

- **Single-file Python** (~2100 lines) using [Textual](https://textual.textualize.io/)
- **SQLite + FTS5** — portable sample database alongside audio files
- **librosa** — spectral analysis (RMS, centroid, flatness, onset, transients) on import
- **LLM** — build kits from natural language prompts, tag samples, refine by direction

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
