#!/usr/bin/env python3
"""
crüx — sample curation TUI
───────────────────────────
A keyboard-first prompt-driven sample browser + kit builder.
Powered by FTS5 search + LM Studio for smart curation.
Shares the SonicVault database (13K+ tagged samples).

Usage:
  ./crux.py                     # open the TUI
  ./crux.py import ~/samples/   # import & analyze + LLM-tag a folder
"""

import sys, os, sqlite3, re, json, subprocess, time, asyncio, uuid, concurrent.futures
from pathlib import Path
from datetime import datetime
from typing import Optional
from textual import on, work
from textual.app import App, ComposeResult
from textual.containers import Container, Horizontal, Vertical, ScrollableContainer
from textual.widgets import Header, Footer, Input, Static, ListView, ListItem, Label, Button, TextArea, Select
from textual.binding import Binding
from textual.screen import Screen, ModalScreen
from textual.widget import Widget
from textual.reactive import reactive
from textual import events
from textual.message import Message
from textual.css.query import NoMatches

# ─── Config ──────────────────────────────────────────────────────────────────
CONFIG_DIR = os.path.expanduser("~/.crux")
CONFIG_FILE = os.path.join(CONFIG_DIR, "config.toml")

# Provider presets
PROVIDER_PRESETS = {
    "lm_studio": {"url": "http://localhost:1234/v1/chat/completions", "model": "gemma-4-26b-a4b-it-mlx"},
    "ollama":    {"url": "http://localhost:11434/v1/chat/completions", "model": "llama3"},
    "openai":    {"url": "https://api.openai.com/v1/chat/completions", "model": "gpt-4o-mini"},
}

def load_config():
    cfg = {
        "general": {"db_path": "", "library_path": ""},
        "llm": {
            "provider": "lm_studio",
            "url": "http://localhost:1234/v1/chat/completions",
            "model": "gemma-4-26b-a4b-it-mlx",
            "api_key": "",
        },
        "import": {"recursive": True, "analyze_bpm": True, "analyze_key": False, "audio_formats": ["wav","mp3","aiff","aif","flac","ogg","m4a"], "tag_batch_size": 3},
        "ui": {"theme": "default", "samples_per_page": 500, "kit_slots": 8},
    }
    # Parse config.toml manually (Python 3.9 doesn't have tomllib)
    try:
        section = None
        for line in open(CONFIG_FILE):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("[") and line.endswith("]"):
                section = line[1:-1].strip()
                # Map old section names to new
                if section == "lm_studio":
                    section = "llm"
                    if "provider" not in cfg.setdefault("llm", {}):
                        cfg["llm"]["provider"] = "lm_studio"
                if section not in cfg:
                    cfg[section] = {}
            elif "=" in line and section:
                k, v = line.split("=", 1)
                k = k.strip()
                v = v.strip().strip('"').strip("'").strip()
                if v.lower() == "true": v = True
                elif v.lower() == "false": v = False
                elif v.isdigit(): v = int(v)
                elif v.startswith("[") and v.endswith("]"):
                    v = [x.strip().strip('"').strip("'") for x in v[1:-1].split(",")]
                cfg[section][k] = v
    except:
        pass
    return cfg

def save_config(cfg):
    """Write config back to config.toml."""
    os.makedirs(CONFIG_DIR, exist_ok=True)
    lines = ["# ── crüx configuration ──\n", "# Edit this file or use Settings (Ctrl+S)\n", "\n"]
    for section, vals in cfg.items():
        lines.append(f"[{section}]\n")
        for k, v in vals.items():
            if isinstance(v, bool):
                v = str(v).lower()
            elif isinstance(v, list):
                v = '[' + ', '.join(f'"{x}"' for x in v) + ']'
            lines.append(f'{k} = "{v}"\n')
        lines.append("\n")
    with open(CONFIG_FILE, "w") as f:
        f.writelines(lines)

_config = load_config()

# Default DB: alongside samples if library_path set, else in ~/.crux/
_raw_db = _config["general"].get("db_path", "")
if _raw_db:
    DB_PATH = os.path.expanduser(_raw_db)
else:
    lib = _config["general"].get("library_path", "")
    if lib:
        DB_PATH = os.path.join(os.path.expanduser(lib), "crux.db")
    else:
        DB_PATH = os.path.join(CONFIG_DIR, "crux.db")

LMSTUDIO_URL = _config["llm"]["url"]
LMSTUDIO_MODEL = _config["llm"]["model"]
LLM_API_KEY = _config["llm"].get("api_key", "")
LLM_PROVIDER = _config["llm"].get("provider", "lm_studio")
DEFAULT_CANDIDATES = 100
SLOT_NAMES = ["Kick","Snare","Clap","Perc","Tom","Hat","Ride","Crash",
              "Shaker","Cowbell","Conga","Bongo","Clav","Marimba","Fx","Bass"]
KIT_SLOTS = _config["ui"]["kit_slots"]
PAGE_SIZE = _config["ui"]["samples_per_page"]

# ─── DB Helpers ──────────────────────────────────────────────────────────────
class DB:
    def __init__(self, path=None):
        self.path = path or DB_PATH
        self.conn = None
    def connect(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        self.conn = sqlite3.connect(self.path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        # Ensure schema exists
        self.conn.execute("""CREATE TABLE IF NOT EXISTS samples (
            id TEXT PRIMARY KEY, name TEXT NOT NULL, path TEXT NOT NULL UNIQUE,
            duration_ms INTEGER DEFAULT 0, bpm REAL, key TEXT,
            tags TEXT DEFAULT '[]', ai_notes TEXT, genre TEXT, machine TEXT,
            created_at TEXT DEFAULT (datetime('now')),
            rms_db REAL, spectral_centroid_hz REAL, spectral_flatness REAL,
            transient_score REAL, onset_confidence REAL
        )""")
        self.conn.execute("""CREATE VIRTUAL TABLE IF NOT EXISTS samples_fts USING fts5(
            id UNINDEXED, name, tags, genre, machine, ai_notes, path,
            content='samples', content_rowid='rowid'
        )""")
        self.conn.execute("""CREATE TRIGGER IF NOT EXISTS samples_ai_insert AFTER INSERT ON samples BEGIN
            INSERT INTO samples_fts(rowid, id, name, tags, genre, machine, ai_notes, path)
            VALUES (new.rowid, new.id, new.name, COALESCE(new.tags,'[]'), COALESCE(new.genre,''),
                    COALESCE(new.machine,''), COALESCE(new.ai_notes,''), COALESCE(new.path,''));
        END""")
        self.conn.execute("""CREATE TRIGGER IF NOT EXISTS samples_au_update AFTER UPDATE ON samples BEGIN
            INSERT INTO samples_fts(rowid, id, name, tags, genre, machine, ai_notes, path)
            VALUES (new.rowid, new.id, new.name, COALESCE(new.tags,'[]'), COALESCE(new.genre,''),
                    COALESCE(new.machine,''), COALESCE(new.ai_notes,''), COALESCE(new.path,''));
        END""")
        self.conn.commit()
    async def search(self, query: str, limit: int = 1000) -> list[dict]:
        if not self.conn: self.connect()
        return await asyncio.to_thread(self._search_sync, query, limit)
    def _search_sync(self, query: str, limit: int) -> list[dict]:
        if not query.strip():
            cur = self.conn.execute("SELECT * FROM samples ORDER BY created_at DESC LIMIT ?", (limit,))
            return [self._parse_row(r) for r in cur.fetchall()]
        words = query.strip().lower().split()
        fts = " AND ".join(f'"{w}"*' for w in words if len(w) > 1)
        sql = """SELECT s.* FROM samples s
                 JOIN samples_fts f ON s.id = f.id
                 WHERE samples_fts MATCH ?
                 ORDER BY rank LIMIT ?"""
        try:
            cur = self.conn.execute(sql, (fts, limit))
        except:
            return []
        return [self._parse_row(r) for r in cur.fetchall()]
    def _parse_row(self, r: sqlite3.Row) -> dict:
        d = dict(r)
        if isinstance(d.get("tags"), str):
            try: d["tags"] = json.loads(d["tags"])
            except: d["tags"] = []
        return d
    async def get_sample(self, sid: str) -> Optional[dict]:
        if not self.conn: self.connect()
        return await asyncio.to_thread(self._get_sample_sync, sid)
    def _get_sample_sync(self, sid: str) -> Optional[dict]:
        cur = self.conn.execute("SELECT * FROM samples WHERE id = ?", (sid,))
        r = cur.fetchone()
        return self._parse_row(r) if r else None
    async def get_stats(self) -> dict:
        if not self.conn: self.connect()
        return await asyncio.to_thread(self._stats_sync)
    def _stats_sync(self) -> dict:
        total = self.conn.execute("SELECT COUNT(*) FROM samples").fetchone()[0]
        tagged = self.conn.execute("SELECT COUNT(*) FROM samples WHERE tags != '[]' AND tags IS NOT NULL").fetchone()[0]
        return {"total": total, "tagged": tagged}
    def update_tags(self, sid: str, tags: list[str], genre=None, machine=None, notes=None):
        if not self.conn: self.connect()
        for attempt in range(2):
            try:
                cur = self.conn.execute("UPDATE samples SET tags=?, genre=COALESCE(?,genre), machine=COALESCE(?,machine), ai_notes=COALESCE(?,ai_notes) WHERE id=?",
                                       (json.dumps(tags), genre, machine, notes, sid))
                self.conn.commit()
                if cur.rowcount == 0:
                    print(f"[db] update_tags: no row matched for sid={sid[:20]}...", file=sys.stderr)
                return cur.rowcount
            except sqlite3.Error:
                if attempt == 0:
                    self.conn = None
                    self.connect()
                else:
                    print(f"[db] update_tags failed for sid={sid[:20]}...", file=sys.stderr)
                    return 0
    async def get_some(self, limit: int = 50) -> list[dict]:
        """Quick fetch for random/initial load."""
        if not self.conn: self.connect()
        cur = self.conn.execute("SELECT * FROM samples ORDER BY RANDOM() LIMIT ?", (limit,))
        return [self._parse_row(r) for r in cur.fetchall()]
    def close(self):
        if self.conn: self.conn.close()

# ─── LLM Helper ──────────────────────────────────────────────────────────────
import aiohttp
async def llm_chat(messages: list[dict], temperature=0.1, max_tokens=2000,
                   override_url=None, override_model=None, override_key=None) -> Optional[str]:
    url = override_url or LMSTUDIO_URL
    model = override_model or LMSTUDIO_MODEL
    api_key = override_key or LLM_API_KEY
    
    for attempt in range(3):
        try:
            timeout = aiohttp.ClientTimeout(total=120, connect=15)
            headers = {"Content-Type": "application/json"}
            if api_key:
                headers["Authorization"] = f"Bearer {api_key}"
            connector = aiohttp.TCPConnector(force_close=True)
            async with aiohttp.ClientSession(timeout=timeout, headers=headers, connector=connector) as session:
                async with session.post(url, json={
                    "model": model,
                    "messages": messages,
                    "stream": False,
                    "temperature": temperature,
                    "max_tokens": max_tokens,
                }) as resp:
                    data = await resp.json()
                    msg = data["choices"][0]["message"]
                    # Some models (qwen with thinking mode) put output in reasoning_content
                    c = (msg.get("content") or "").strip()
                    if not c:
                        c = (msg.get("reasoning_content") or "").strip()
                # Strip thinking boilerplate
                if c.startswith("Thinking") and "\n\n" in c:
                    after = c.split("\n\n", 1)[-1].strip()
                    if after:
                        c = after
                return c or None
        except Exception as e:
            print(f"[llm_chat] attempt {attempt+1}/3 failed: {e}", file=sys.stderr)
            if attempt < 2:
                await asyncio.sleep(2)
            else:
                return None
def extract_json(text: str):
    m = re.search(r'\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}', text, re.DOTALL)
    if m:
        try: return json.loads(m.group())
        except: pass
    return None

# ─── Audio Analysis with librosa ─────────────────────────────────────────
import struct, math

def describe_audio(feats: dict) -> str:
    """Convert numerical audio features to a descriptive string for the LLM."""
    parts = []
    
    dur = feats.get("duration_ms", 0)
    if dur > 0:
        secs = dur / 1000
        if secs < 0.3:
            parts.append("very short hit")
        elif secs < 1.0:
            parts.append("short one-shot")
        elif secs < 3.0:
            parts.append(f"{secs:.1f}s sample")
        else:
            parts.append(f"{secs:.1f}s loop")
    
    bpm = feats.get("bpm")
    if bpm:
        if bpm < 80:
            parts.append(f"slow ({int(bpm)}bpm)")
        elif bpm < 120:
            parts.append(f"mid-tempo ({int(bpm)}bpm)")
        elif bpm < 160:
            parts.append(f"uptempo ({int(bpm)}bpm)")
        else:
            parts.append(f"fast ({int(bpm)}bpm)")
    
    rms = feats.get("rms_db")
    if rms is not None:
        if rms < -20:
            parts.append("very quiet")
        elif rms < -12:
            parts.append("moderate volume")
        elif rms < -6:
            parts.append("loud")
        else:
            parts.append("very loud")
    
    centroid = feats.get("spectral_centroid_hz")
    if centroid is not None:
        if centroid < 500:
            parts.append("dark/sub-bass focused")
        elif centroid < 1500:
            parts.append("warm/mid focused")
        elif centroid < 3000:
            parts.append("bright/present")
        else:
            parts.append("very bright/airy")
    
    flatness = feats.get("spectral_flatness")
    if flatness is not None:
        if flatness < 0.1:
            parts.append("pure tone/tonal")
        elif flatness < 0.3:
            parts.append("musical/tuned")
        elif flatness < 0.6:
            parts.append("noise-tinged")
        else:
            parts.append("noisy/textural")
    
    onset = feats.get("onset_confidence")
    if onset is not None:
        if onset > 0.6:
            parts.append("sharp attack")
        elif onset > 0.3:
            parts.append("moderate attack")
        else:
            parts.append("soft attack")
    
    transient = feats.get("transient_score")
    if transient is not None:
        if transient > 0.5:
            parts.append("percussive/hit")
        else:
            parts.append("sustained")
    
    return " · ".join(parts) if parts else "unknown"


def analyze_audio(path: str) -> dict:
    """Full spectral analysis using librosa.
    
    Extracts: duration, BPM, key, RMS energy, spectral centroid,
    spectral flatness, onset strength, transient character.
    All values are stored for later LLM consumption.
    """
    features = {
        "duration_ms": 0, "bpm": None, "key": None,
        "rms_db": None, "spectral_centroid_hz": None,
        "spectral_flatness": None, "transient_score": None,
        "onset_confidence": None,
    }
    
    # Try librosa first for full spectral analysis
    try:
        import librosa
        import numpy as np
        
        y, sr = librosa.load(path, sr=22050, duration=5, mono=True)
        if len(y) == 0:
            return features
        
        features["duration_ms"] = int(len(y) / sr * 1000)
        
        # BPM via beat tracking
        try:
            tempo, _ = librosa.beat.beat_track(y=y, sr=sr)
            if tempo and tempo > 0:
                features["bpm"] = round(float(tempo), 1)
        except:
            pass
        
        # Spectral centroid (brightness)
        try:
            cent = librosa.feature.spectral_centroid(y=y, sr=sr)
            features["spectral_centroid_hz"] = round(float(np.mean(cent)), 1)
        except:
            pass
        
        # RMS energy (loudness)
        try:
            rms = librosa.feature.rms(y=y)
            rms_db_val = 20 * np.log10(np.mean(rms) + 1e-10)
            features["rms_db"] = round(float(rms_db_val), 1)
        except:
            pass
        
        # Spectral flatness (tonal vs noisy)
        try:
            flat = librosa.feature.spectral_flatness(y=y, sr=sr)
            features["spectral_flatness"] = round(float(np.mean(flat)), 3)
        except:
            pass
        
        # Onset strength (attack character)
        try:
            onset_env = librosa.onset.onset_strength(y=y, sr=sr)
            features["onset_confidence"] = round(float(np.mean(onset_env) / (np.max(onset_env) + 1e-10)), 3)
        except:
            pass
        
        # Transient ratio (percussive vs sustained)
        try:
            # Use spectral flux as a proxy for transience
            onset_frames = librosa.onset.onset_detect(y=y, sr=sr, backtrack=False)
            if len(y) > 0:
                ratio = len(onset_frames) / (len(y) / sr)
                features["transient_score"] = round(min(ratio / 5.0, 1.0), 3)
        except:
            pass
            
    except ImportError:
        # Fallback: ffprobe duration, ffmpeg autocorrelation BPM
        try:
            r = subprocess.run(["ffprobe", "-v", "0", "-show_entries", "format=duration",
                               "-of", "csv=p=0", path], capture_output=True, text=True, timeout=5)
            if r.returncode == 0:
                features["duration_ms"] = int(float(r.stdout.strip()) * 1000)
        except:
            pass
        try:
            r = subprocess.run(["ffmpeg", "-i", path, "-ac", "1", "-ar", "22050",
                               "-f", "f32le", "-", "-y"], capture_output=True, timeout=10)
            if r.returncode == 0:
                samples = struct.unpack(f"{len(r.stdout)//4}f", r.stdout)
                if len(samples) > 4410:
                    corr = []
                    for lag in range(int(22050/200), int(22050/60)):
                        s = sum(samples[i]*samples[i+lag] for i in range(min(len(samples)-lag, 22050)))
                        corr.append((lag, s))
                    if corr:
                        best = max(corr, key=lambda x: x[1])
                        features["bpm"] = round(60.0 / (best[0] / 22050.0), 1)
        except:
            pass
    
    return features

def render_waveform_ascii(path: str, width: int = 50, height: int = 3) -> str:
    """Render a waveform as unicode block chars. Returns multi-line string."""
    try:
        import librosa
        import numpy as np
        y, sr = librosa.load(path, sr=22050, duration=3, mono=True)
        if len(y) < 100:
            return ""
        # Downsample to fit width
        chunk = len(y) // width
        envelope = np.array([np.abs(y[i*chunk:(i+1)*chunk]).max() for i in range(width)])
        # Normalize
        peak = envelope.max()
        if peak > 0:
            envelope = envelope / peak
        # Block chars: ▁▂▃▄▅▆▇█
        blocks = [" ", "▁", "▂", "▃", "▄", "▅", "▆", "▇", "█"]
        levels = len(blocks) - 1
        lines = []
        for row in range(height):
            threshold = (height - row) / height
            line = ""
            for v in envelope:
                idx = min(int(v * levels), levels)
                char = blocks[idx]
                # Only show blocks above this row's threshold
                if v >= threshold:
                    line += char
                else:
                    line += " "
            lines.append(line)
        return "\n".join(lines)
    except:
        return ""

# ─── Import Pipeline ─────────────────────────────────────────────────────────
AUDIO_EXTS = {".wav", ".mp3", ".aiff", ".aif", ".flac", ".ogg", ".m4a"}

async def import_pipeline(folder: str, db: DB, app_ref=None):
    """Walk folder, analyze audio, insert immediately (no LLM tagging—run tag separately)."""
    if not db.conn: db.connect()
    folder = os.path.expanduser(folder)
    
    # Walk and count files
    files = []
    for root, dirs, fnames in os.walk(folder):
        for f in fnames:
            if Path(f).suffix.lower() in AUDIO_EXTS:
                files.append(os.path.join(root, f))
    total = len(files)
    if total == 0:
        msg = "no audio files found"
        print(msg, file=sys.stderr)
        if app_ref: app_ref.post_message(StatusMsg(msg))
        return 0
    
    # Ensure schema
    db.conn.execute("""CREATE TABLE IF NOT EXISTS samples (
        id TEXT PRIMARY KEY, name TEXT, path TEXT UNIQUE,
        duration_ms INTEGER DEFAULT 0, bpm REAL, key TEXT,
        tags TEXT DEFAULT '[]', ai_notes TEXT, genre TEXT, machine TEXT,
        created_at TEXT DEFAULT (datetime('now')),
        rms_db REAL, spectral_centroid_hz REAL, spectral_flatness REAL,
        transient_score REAL, onset_confidence REAL
    )""")
    db.conn.execute("""CREATE VIRTUAL TABLE IF NOT EXISTS samples_fts USING fts5(
        id UNINDEXED, name, tags, genre, machine, ai_notes, path,
        content='samples', content_rowid='rowid'
    )""")
    db.conn.commit()
    
    msg = f"Found {total} files, analyzing + inserting..."
    print(msg, file=sys.stderr)
    if app_ref: app_ref.post_message(StatusMsg(msg))
    
    imported = 0
    last_report = 0
    
    # Process files concurrently — up to 4 librosa analyses at a time
    loop = asyncio.get_running_loop()
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        async def _import_one(fpath: str) -> bool:
            nonlocal imported
            name = Path(fpath).stem
            feats = await loop.run_in_executor(pool, analyze_audio, fpath)
            sid = str(uuid.uuid4())
            try:
                db.conn.execute(
                    "INSERT OR IGNORE INTO samples (id, name, path, duration_ms, bpm, rms_db, spectral_centroid_hz, spectral_flatness, transient_score, onset_confidence) VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (sid, name, fpath, feats["duration_ms"], feats["bpm"],
                     feats["rms_db"], feats["spectral_centroid_hz"],
                     feats["spectral_flatness"], feats["transient_score"],
                     feats["onset_confidence"]))
                db.conn.commit()
                imported += 1
                return True
            except:
                return False
    
        for i in range(0, total, 4):
            window = files[i:i + 4]
            await asyncio.gather(*(_import_one(f) for f in window))
        
        # Report every 5%
        pct = min((i + 4) * 100 // total, 100) if i + 4 < total else 100
        if pct // 5 > last_report // 5:
            last_report = pct
            msg = f"[{pct}%] {i+1}/{total} imported"
            print(msg, file=sys.stderr)
            if app_ref: app_ref.post_message(StatusMsg(msg))
    
    final = f"✓ imported {imported}/{total} samples — press Ctrl+T to LLM-tag"
    print(final, file=sys.stderr)
    if app_ref: app_ref.post_message(StatusMsg(final))
    return imported

# ─── TUI Widgets & Screens ────────────────────────────────────────────────────
async def tag_pipeline(db: DB, batch_size: int = 20, app_ref=None, pause_check=None, progress=None):
    """LLM-tag untagged samples: generate tags, genre, and ai_notes from spectral data.
    Uses 4 concurrent workers — one per LM Studio slot — for parallel tagging.
    Pauses between batches if pause_check() returns True.
    progress: optional mutable list [tagged_so_far, total] for live spinner updates.
    """
    if not db.conn: db.connect()
    cur = db.conn.execute("SELECT * FROM samples WHERE tags IS NULL OR tags = '[]' OR ai_notes IS NULL OR ai_notes = '' ORDER BY RANDOM()")
    untagged = [db._parse_row(r) for r in cur.fetchall()]
    total = len(untagged)
    if total == 0:
        msg = "all samples already tagged"
        if app_ref: app_ref.post_message(StatusMsg(msg))
        return 0
    
    if progress is not None:
        progress[1] = total
    
    # Chunk into batches
    batches = [untagged[i:i + batch_size] for i in range(0, total, batch_size)]
    
    sys_msg = {"role": "system", "content": "You are crüx. Tags describe the sample. Genres REQUIRED (min 1, default ['house']). Return EXACTLY {len(batch)} entries. ONLY raw JSON — no markdown."}
    
    tagged = 0
    concurrency = 8
    sem = asyncio.Semaphore(concurrency)
    

    
    async def _tag_batch(batch: list[dict]) -> int:
        nonlocal tagged
        if pause_check and pause_check():
            return 0
        
        async with sem:
            try:
                batch_text = ""
                for s in batch:
                    feats = {
                        "duration_ms": s.get("duration_ms", 0),
                        "bpm": s.get("bpm"),
                        "rms_db": s.get("rms_db"),
                        "spectral_centroid_hz": s.get("spectral_centroid_hz"),
                        "spectral_flatness": s.get("spectral_flatness"),
                        "transient_score": s.get("transient_score"),
                        "onset_confidence": s.get("onset_confidence"),
                    }
                    char = describe_audio(feats)
                    folder = os.path.basename(os.path.dirname(s.get("path",""))) if s.get("path") else ""
                    batch_text += f"{s['id']}: {s['name']} | {folder} | {s.get('machine') or ''} | {char}\n"
                
                user_msg = {"role": "user", "content": f"Tag these {len(batch)} samples.\n\nRULES:\n- Field names MUST be: id, tags, genres, sonics, notes\n- Do NOT use: category, type, style, description, instrument\n- 'id' must be EXACTLY the ID from each line below — copy it verbatim\n- 'genres' REQUIRED (min 1). If unsure: [\"house\"]\n- Return EXACTLY {len(batch)} entries\n- Return ONLY JSON. No markdown.\n\nFormat: {{\"samples\": [{{\"id\": \"...\", \"tags\": [\"kick\", \"808\"], \"genres\": [\"house\"], \"sonics\": [\"punchy\"], \"notes\": \"Description\"}}]}}\n\nSamples:\n{batch_text}"}
                
                resp = None
                for attempt in range(3):
                    resp = await llm_chat([sys_msg, user_msg], temperature=0.2, max_tokens=1000)
                    if resp:
                        break
                    if attempt < 2:
                        await asyncio.sleep(2)
                if not resp:
                    return 0
                
                count = 0
                entries = []
                
                clean = resp.strip()
                if clean.startswith("```"):
                    clean = clean.split("```")[1]
                    if clean.startswith("json"):
                        clean = clean[4:]
                    clean = clean.strip()
                
                try:
                    j = json.loads(clean)
                    if isinstance(j, list):
                        entries = j
                    else:
                        entries = j.get("samples")
                        if entries is None:
                            entries = [j]
                except json.JSONDecodeError:
                    pass
                
                if not entries:
                    for m in re.finditer(r'\{"id":\s*"[^"]+"[^}]+\}', clean, re.DOTALL):
                        try:
                            entry = json.loads(m.group())
                            if entry.get("id"):
                                entries.append(entry)
                        except json.JSONDecodeError:
                            continue
                
                for idx, entry in enumerate(entries):
                    sid = entry.get("id", "")
                    if not sid and idx < len(batch):
                        sid = batch[idx]["id"]
                    elif sid and idx < len(batch):
                        if sid not in (b["id"] for b in batch):
                            sid = batch[idx]["id"]
                    
                    tags = entry.get("tags")
                    if not tags:
                        for k in ("category", "type", "instrument", "class", "labels", "keywords"):
                            v = entry.get(k)
                            if isinstance(v, list): tags = v; break
                    if not tags:
                        for k, v in entry.items():
                            if isinstance(v, list) and v and isinstance(v[0], str): tags = v; break
                    if not tags: tags = []
                    if isinstance(tags, str): tags = [tags]
                    
                    sonics = entry.get("sonics", [])
                    if sonics:
                        for s_tag in sonics:
                            if s_tag not in tags: tags.append(s_tag)
                    
                    genres_raw = entry.get("genres") or entry.get("genre")
                    if not genres_raw:
                        for k in ("style", "styles", "mood"):
                            genres_raw = entry.get(k)
                            if genres_raw: break
                    genre_str = ", ".join(g for g in genres_raw if g) if isinstance(genres_raw, list) else str(genres_raw) if genres_raw else ""
                    
                    notes = entry.get("notes") or entry.get("description") or ""
                    if not notes:
                        for k, v in entry.items():
                            if k not in ("id", "tags", "genres", "sonics", "genre") and isinstance(v, str) and len(v) > 10:
                                notes = v; break
                    notes = notes[:200]
                    if sid:
                        if db.update_tags(sid, tags, genre=genre_str, notes=notes):
                            count += 1
                return count
            except Exception as e:
                print(f"[tag_batch] worker error: {e}", file=sys.stderr)
                import traceback
                traceback.print_exc()
                return 0
    
    # Process batches concurrently with pause support
    batch_idx = 0
    while batch_idx < len(batches):
        if pause_check and pause_check():
            while pause_check():
                await asyncio.sleep(0.5)
            cur = db.conn.execute("SELECT * FROM samples WHERE tags IS NULL OR tags = '[]' OR ai_notes IS NULL OR ai_notes = '' ORDER BY RANDOM()")
            remaining = [db._parse_row(r) for r in cur.fetchall()]
            batches = [remaining[i:i + batch_size] for i in range(0, len(remaining), batch_size)]
            batch_idx = 0
            if not batches:
                break
            continue
        
        window = batches[batch_idx:batch_idx + concurrency]
        results = await asyncio.gather(*(_tag_batch(b) for b in window))
        batch_tagged = sum(results)
        tagged += batch_tagged
        if progress is not None:
            progress[0] = tagged
        batch_idx += concurrency
        
        msg = f"tagged {tagged}/{total}"
        if app_ref:
            app_ref.post_message(StatusMsg(msg))
    
    return tagged

class StatusMsg(Message):
    def __init__(self, text: str):
        self.text = text
        super().__init__()

class SamplesUpdated(Message):
    def __init__(self, samples: list[dict], query: str):
        self.samples = samples
        self.query = query
        super().__init__()

class KitRefined(Message):
    def __init__(self, slots: list[Optional[dict]]):
        self.slots = slots
        super().__init__()

# ─── Settings Screen ──────────────────────────────────────────────────────────
class SettingsScreen(Screen):
    """Configure LLM provider and paths."""
    
    BINDINGS = [
        Binding("escape", "close_settings", "Close"),
        Binding("ctrl+s", "save", "Save", priority=True),
    ]
    
    def action_close_settings(self):
        self.dismiss(None)
    
    @staticmethod
    def _normalize_theme(val: str) -> str:
        valid = {"shark", "amber", "matrix", "paper"}
        return val if val in valid else "shark"

    def __init__(self, theme_colors=None):
        super().__init__()
        self.cfg = load_config()
        self._dirty = False
        t = theme_colors or {
            "bg": "#0b1a20", "surface": "#0f2128", "surface2": "#152a33",
            "fg": "#b8c8c8", "accent": "#1a9e9e", "border": "#1a3a45",
            "dim": "#5a8a8a",
        }
        self._theme = t
    
    def compose(self):
        llm = self.cfg.get("llm", {})
        gen = self.cfg.get("general", {})
        prov = llm.get("provider", "lm_studio")
        yield ScrollableContainer(
            Static("╔═ crüx Settings ═══════════════════════", classes="shdr"),
            Static("Provider", classes="slbl"),
            Horizontal(
                Button("LM Studio", id="p-lm_studio"),
                Button("Ollama", id="p-ollama"),
                Button("OpenAI", id="p-openai"),
                Button("Custom", id="p-custom"),
                id="prov-btns",
            ),
            Static("API URL", classes="slbl"),
            Input(value=llm.get("url", ""), id="s-url",
                  placeholder="http://localhost:1234/v1/chat/completions"),
            Static("Model", classes="slbl"),
            Input(value=llm.get("model", ""), id="s-model",
                  placeholder="gemma-4-26b-a4b-it-mlx"),
            Static("API Key (blank for local)", classes="slbl"),
            Input(value=llm.get("api_key", ""), id="s-key", password=True,
                  placeholder="sk-..."),
            Static("Library Path", classes="slbl"),
            Input(value=gen.get("library_path", ""), id="s-lib",
                  placeholder="~/Music/Samples"),
            Static("Theme", classes="slbl"),
            Select(
                [(t.capitalize(), t) for t in ("shark", "amber", "matrix", "paper")],
                prompt="Theme",
                value=self._normalize_theme(self.cfg.get("ui",{}).get("theme","shark")),
                id="s-theme",
            ),
            Static("", id="s-result"),
            Horizontal(
                Button("Test", id="s-test"),
                Button("Tag all", id="s-tag"),
                Button("Save", id="s-save", variant="primary"),
                Button("Cancel", id="s-cancel"),
                id="s-actions",
            ),
            id="settings-wrap",
        )
    
    CSS = """
    SettingsScreen { background: #0b1a20; }
    #settings-wrap { width: 60; height: 100%; margin: 1 2; }
    .shdr { color: #1a9e9e; text-style: bold; height: 2; }
    .slbl { color: #b8c8c8; height: 2; }
    SettingsScreen Input { background: #0f2128; color: #e8f0f0; border: solid #1a3a45; height: 3; min-width: 40; }
    SettingsScreen Input:focus { border: solid #1a9e9e; }
    SettingsScreen Select { background: #0f2128; color: #e8f0f0; border: solid #1a3a45; min-width: 40; }
    SettingsScreen Button { background: #152a33; color: #b8c8c8; border: solid #1a3a45; height: 3; min-width: 14; padding: 0 2; }
    SettingsScreen Button:hover { border: solid #1a9e9e; }
    SettingsScreen Button.primary { background: #1a9e9e; color: #0b1a20; }
    #prov-btns { layout: horizontal; height: 5; margin: 0 0 1 0; }
    #prov-btns Button { width: 1fr; min-width: 12; height: 3; }
    #s-actions { height: 5; margin-top: 1; }
    #s-actions Button { height: 3; }
    #s-result { color: #5a8a8a; height: 2; }
    """
    
    def on_mount(self):
        t = self._theme
        try:
            self.screen.styles.background = t["bg"]
        except:
            pass
        self._highlight_provider()
    
    def _highlight_provider(self):
        prov = self.cfg.get("llm", {}).get("provider", "lm_studio")
        for pid in ("lm_studio", "ollama", "openai", "custom"):
            try:
                btn = self.query_one(f"#p-{pid}", Button)
                btn.variant = "primary" if pid == prov else "default"
            except:
                pass
    
    def on_input_changed(self, event: Input.Changed):
        if event.input.id == "s-url":
            self._detect_provider_from_url(event.value)
    
    def _detect_provider_from_url(self, url: str):
        url_lower = url.strip().lower()
        if "localhost:1234" in url_lower or "lmstudio" in url_lower:
            prov = "lm_studio"
        elif "localhost:11434" in url_lower:
            prov = "ollama"
        elif "openai.com" in url_lower:
            prov = "openai"
        else:
            return  # Don't override Custom or unrecognized
        self.cfg.setdefault("llm", {})["provider"] = prov
        self._highlight_provider()
    
    def on_button_pressed(self, event: Button.Pressed):
        btn_id = event.button.id
        if btn_id and btn_id.startswith("p-"):
            prov = btn_id[2:]
            self.cfg.setdefault("llm", {})["provider"] = prov
            if prov in PROVIDER_PRESETS:
                p = PROVIDER_PRESETS[prov]
                self.query_one("#s-url", Input).value = p["url"]
                self.query_one("#s-model", Input).value = p["model"]
                if prov != "openai":
                    self.query_one("#s-key", Input).value = ""
            self._highlight_provider()
        elif btn_id == "s-test":
            self._test()
        elif btn_id == "s-save":
            self._save()
        elif btn_id == "s-tag":
            self.dismiss("tag")
        elif btn_id == "s-cancel":
            self.dismiss(None)
    
    def _test(self):
        url = self.query_one("#s-url", Input).value.strip()
        model = self.query_one("#s-model", Input).value.strip()
        key = self.query_one("#s-key", Input).value.strip()
        result = self.query_one("#s-result", Static)
        result.update("Testing...")
        self._do_test(url, model, key, result)
    
    @work
    async def _do_test(self, url, model, key, result):
        try:
            resp = await llm_chat(
                [{"role": "user", "content": "Say ok"}],
                max_tokens=5, override_url=url, override_model=model, override_key=key,
            )
            result.update(f"✓ {resp[:50]}" if resp else "✗ No response")
        except Exception as e:
            result.update(f"✗ {e}")
    
    def _save(self):
        llm = self.cfg.setdefault("llm", {})
        # Find which provider button is active
        prov_found = False
        for pid in ("lm_studio", "ollama", "openai", "custom"):
            try:
                btn = self.query_one(f"#p-{pid}", Button)
                if btn.variant == "primary":
                    llm["provider"] = pid
                    prov_found = True
                    break
            except:
                pass
        if not prov_found:
            # Fallback: detect from URL
            url = self.query_one("#s-url", Input).value.strip().lower()
            if "localhost:1234" in url or "lm-studio" in url:
                llm["provider"] = "lm_studio"
            elif "localhost:11434" in url:
                llm["provider"] = "ollama"
            elif "openai.com" in url:
                llm["provider"] = "openai"
            else:
                llm["provider"] = "custom"
        llm["url"] = self.query_one("#s-url", Input).value.strip()
        llm["model"] = self.query_one("#s-model", Input).value.strip()
        llm["api_key"] = self.query_one("#s-key", Input).value.strip()
        self.cfg.setdefault("general", {})["library_path"] = self.query_one("#s-lib", Input).value.strip()
        raw_theme = self.query_one("#s-theme", Select).value
        self.cfg.setdefault("ui", {})["theme"] = self._normalize_theme(raw_theme)
        save_config(self.cfg)
        self.dismiss(True)

# ─── Export Screen ────────────────────────────────────────────────────────────
class ExportScreen(Screen):
    """Export the current kit to various hardware/DAW formats."""
    
    BINDINGS = [
        Binding("escape", "close", "Close"),
    ]
    
    CSS = """
    ExportScreen { background: #0b1a20; }
    #export-wrap { width: 60; height: 100%; margin: 1 2; }
    .ehdr { color: #1a9e9e; text-style: bold; height: 2; }
    .elbl { color: #b8c8c8; height: 2; }
    Button {
        background: #152a33; color: #b8c8c8;
        border: solid #1a3a45; height: 3; min-width: 14;
    }
    Button:hover { border: solid #1a9e9e; }
    Button.primary { background: #1a9e9e; color: #0b1a20; }
    #e-result { color: #5a8a8a; height: 2; }
    """
    
    def compose(self):
        yield ScrollableContainer(
            Static("╔═ Export Kit ════════════════════════", classes="ehdr"),
            Static("Format", classes="elbl"),
            Container(
                Button("Ableton Drum Rack", id="f-ableton"),
                Button("SP-404 MKII", id="f-sp404"),
                Button("MPC 1000", id="f-mpc1k"),
                Button("MPC 2000 XL", id="f-mpc2k"),
                id="e-formats",
            ),
            Static("", id="e-result"),
            Horizontal(
                Button("Export", id="e-export", variant="primary"),
                Button("Close", id="e-close"),
            ),
            id="export-wrap",
        )
    
    def on_mount(self):
        self._format = "ableton"
        self._highlight()
    
    def _highlight(self):
        for fid in ("ableton", "sp404", "mpc1k", "mpc2k"):
            try:
                btn = self.query_one(f"#f-{fid}", Button)
                btn.variant = "primary" if fid == self._format else "default"
            except:
                pass
    
    def on_button_pressed(self, event):
        btn_id = event.button.id
        if btn_id and btn_id.startswith("f-"):
            self._format = btn_id[2:]
            self._highlight()
        elif btn_id == "e-export":
            self._do_export()
        elif btn_id == "e-close":
            self.dismiss(None)
    
    def action_close(self):
        self.dismiss(None)
    
    def _do_export(self):
        kit = self.app._kit  # Access the parent app's kit
        if not any(kit):
            self.query_one("#e-result", Static).update("Kit is empty — add samples first")
            return
        result = self.query_one("#e-result", Static)
        result.update("Exporting...")
        self._run_export(kit, result)
    
    @work
    async def _run_export(self, kit, result):
        import zipfile, io
        export_dir = os.path.expanduser("~/Desktop/Crux Exports")
        os.makedirs(export_dir, exist_ok=True)
        
        fmt = self._format
        kit_name = f"crüx-kit-{int(time.time())}"
        zip_path = os.path.join(export_dir, f"{kit_name}_{fmt}.zip")
        
        slot_labels = SLOT_NAMES[:KIT_SLOTS]
        
        try:
            with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
                for i in range(KIT_SLOTS):
                    s = kit[i]
                    if not s:
                        continue
                    path = s.get("path", "")
                    if not path or not os.path.exists(path):
                        continue
                    label = slot_labels[i] if i < len(slot_labels) else f"Pad{i+1}"
                    ext = os.path.splitext(path)[1] or ".wav"
                    
                    if fmt == "ableton":
                        arcname = f"{i:02d}_{label}{ext}"
                    elif fmt == "sp404":
                        arcname = f"{i:03d}_00{ext}"
                    elif fmt in ("mpc1k", "mpc2k"):
                        arcname = f"{i:02d}-{label}{ext}"
                    else:
                        arcname = f"{i:02d}_{label}{ext}"
                    
                    zf.write(path, arcname)
            
            count = sum(1 for i in range(KIT_SLOTS) if kit[i])
            result.update(f"✓ Exported {count} samples → {zip_path}")
            result.classes = "s-success"
        except Exception as e:
            result.update(f"✗ Export failed: {e}")

# ─── Main App ─────────────────────────────────────────────────────────────────
class CruxApp(App):
    CSS = f"""
    Screen {{ background: $bg; }}

    #main-container {{
        height: 100%;
        layout: grid;
        grid-size: 1 5;
        grid-rows: auto auto auto 1fr auto;
    }}
    #header-bar {{
        height: 2;
        background: $surface;
        padding: 0 1;
        content-align: center middle;
    }}
    #header-bar > Static {{
        color: $accent;
        text-style: bold;
    }}
    #prompt-bar {{
        height: 3;
        background: $surface;
        padding: 0 1;
    }}
    #prompt-input {{
        background: $bg;
        color: $fg;
        border: solid $border;
        padding: 0 1;
    }}
    #prompt-input:focus {{
        border: solid $accent;
    }}
    #content-area {{
        height: 100%;
        layout: grid;
        grid-size: 2 1;
        grid-columns: 3fr 2fr;
    }}
    #sample-panel {{
        background: $bg;
        border-right: solid $border;
        height: 100%;
    }}
    #waveform-bar {{
        height: 4;
        width: 100%;
        background: $bg;
        border-bottom: solid $border;
    }}
    #waveform-view {{
        height: 100%;
        padding: 0 1;
        color: $accent;
        overflow: hidden;
    }}
    #sample-list {{
        height: 100%;
        overflow-y: auto;
    }}
    #sample-list ListView {{
        height: 100%;
        border: none;
        background: transparent;
    }}
    ListItem {{
        background: transparent;
        padding: 0 1;
        height: 1;
    }}
    ListItem:hover {{ background: $hover; }}
    ListItem > Label {{ color: $fg; }}
    ListView:focus .list-item--focused {{
        background: $accent 20%;
    }}
    ListItem > Label {{
        color: $fg;
    }}
    #kit-panel {{
        background: $surface;
        height: 100%;
        padding: 0 0 0 1;
    }}
    #kit-grid {{
        height: auto;
        overflow-y: auto;
    }}
    #kit-grid ListView {{
        height: auto;
        border: none;
        background: transparent;
    }}
    #kit-input {{
        background: $bg;
        color: $fg;
        border: solid $border;
        padding: 0 1;
        height: 3;
    }}
    #kit-input:focus {{
        border: solid $accent;
    }}
    #kit-grid ListItem {{
        background: transparent;
        border-bottom: solid $border;
        height: 2;
        padding: 0;
    }}
    #kit-grid ListItem > Label {{ color: $fg; }}
    #kit-detail {{
        height: 8;
        padding: 0 1;
        color: $fg;
        background: $surface;
        border-top: solid $border;
        overflow: hidden;
    }}
    .kit-slot {{ padding: 0 1; }}
    .slot-label {{ color: $accent; text-style: bold; min-width: 8; }}
    .slot-name {{ color: $fg; }}
    .slot-empty {{ color: $muted; text-style: italic; }}
    .slot-locked {{ color: $muted; }}
    #status-bar {{
        height: 1;
        background: $surface;
        padding: 0 1;
    }}
    #status-bar > Static {{ color: $muted; }}

    Button {{
        background: $surface2;
        color: $fg;
        border: solid $border;
        min-width: 8;
        height: 2;
    }}
    Button:hover {{ border: solid $accent; }}
    Button:focus {{ border: solid $accent; }}
    Button.accent {{ color: #e0673a; border: solid #7a3a20; }}

    #import-screen {{ align: center middle; }}
    #import-box {{ width: 60; height: 20; background: $surface; border: solid $accent; padding: 1 2; }}
    #import-title {{ color: $accent; text-style: bold; }}
    #import-status {{ color: $fg; height: 1; }}
    #import-log {{ height: 14; overflow-y: auto; background: $bg; border: solid $border; padding: 0 1; }}
    #import-log > Static {{ color: $fg; }}
    #sample-list, #kit-grid, #import-log {{
        scrollbar-color: $muted;
        scrollbar-color-hover: $dim;
        scrollbar-background: $bg;
        scrollbar-background-hover: $bg;
    }}
    ListView {{
        scrollbar-color: $muted;
        scrollbar-color-hover: $dim;
        scrollbar-background: $bg;
        scrollbar-background-hover: $bg;
    }}
    """
    
    BINDINGS = [
        Binding("ctrl+c", "quit", "Quit", priority=True),
        Binding("ctrl+q", "quit", "Quit"),
        Binding("escape", "clear_search", "Clear"),
        Binding("f5", "refresh", "Refresh"),
        Binding("tab", "focus_next", "Browse/Kit panes"),
        Binding("/", "focus_search", "Search"),
        Binding("p", "play", "Play"),
        Binding("ctrl+s", "settings", "Settings", priority=True),
        Binding("ctrl+e", "export", "Export kit"),
        Binding("ctrl+t", "tag", "Tag samples"),

        Binding("space", "toggle_lock", "Lock/unlock"),
        Binding("delete", "clear_kit_slot", "Clear slot"),
        Binding("j", "cursor_down", "↓"),
        Binding("k", "cursor_up", "↑"),
        Binding("1", "slot_1", "Slot 1"),
        Binding("2", "slot_2", "Slot 2"),
        Binding("3", "slot_3", "Slot 3"),
        Binding("4", "slot_4", "Slot 4"),
        Binding("5", "slot_5", "Slot 5"),
        Binding("6", "slot_6", "Slot 6"),
        Binding("7", "slot_7", "Slot 7"),
        Binding("8", "slot_8", "Slot 8"),
        Binding("9", "slot_9", "Slot 9"),
        Binding("0", "slot_10", "Slot 10"),
        Binding("ctrl+1", "slot_11", "Slot 11"),
        Binding("ctrl+2", "slot_12", "Slot 12"),
        Binding("ctrl+3", "slot_13", "Slot 13"),
        Binding("ctrl+4", "slot_14", "Slot 14"),
        Binding("ctrl+5", "slot_15", "Slot 15"),
        Binding("ctrl+6", "slot_16", "Slot 16"),
    ]
    
    def __init__(self, import_path=None):
        super().__init__()
        self.db = DB()
        self.db.connect()
        self._samples: list[dict] = []
        self._query = ""
        self._selected = set()
        self._kit: list[Optional[dict]] = [None] * KIT_SLOTS
        self._kit_locked: list[bool] = [False] * KIT_SLOTS
        self._import_path = import_path
        self._stats = {"total": 0, "tagged": 0}
        self._kit_index = 0
        self._current_audio: Optional[subprocess.Popen] = None
        self._last_selected_id: Optional[str] = None
        self._tag_paused: bool = False
    
    def get_css_variables(self) -> dict[str, str]:
        """Return CSS variables matching the current theme."""
        base = super().get_css_variables()
        t = getattr(self, '_theme', None) or {
            "bg": "#0b1a20", "surface": "#0f2128", "surface2": "#152a33",
            "fg": "#b8c8c8", "accent": "#1a9e9e", "border": "#1a3a45",
            "dim": "#5a8a8a", "muted": "#3a5a65", "hover": "#0f2128",
        }
        base.update({
            "bg": t["bg"],
            "surface": t["surface"],
            "surface2": t["surface2"],
            "fg": t["fg"],
            "accent": t["accent"],
            "border": t["border"],
            "dim": t["dim"],
            "muted": t["muted"],
            "hover": t["hover"],
        })
        return base
    
    def load_theme(self):
        """Apply the selected theme from config."""
        theme = _config.get("ui", {}).get("theme", "default").lower()
        themes = {
            "default": {"bg": "#0b1a20", "surface": "#0f2128", "surface2": "#152a33", "fg": "#b8c8c8", "accent": "#1a9e9e", "border": "#1a3a45", "dim": "#5a8a8a", "muted": "#3a5a65", "hover": "#0f2128"},
            "shark":   {"bg": "#0b1a20", "surface": "#0f2128", "surface2": "#152a33", "fg": "#b8c8c8", "accent": "#1a9e9e", "border": "#1a3a45", "dim": "#5a8a8a", "muted": "#3a5a65", "hover": "#0f2128"},
            "amber":   {"bg": "#1a0e00", "surface": "#2a1800", "surface2": "#3a2400", "fg": "#d4a030", "accent": "#ffb000", "border": "#5a3a00", "dim": "#8a6a20", "muted": "#6a4a10", "hover": "#3a2000"},
            "matrix":  {"bg": "#000000", "surface": "#0a0a0a", "surface2": "#0a1a0a", "fg": "#00cc00", "accent": "#00ff41", "border": "#003a00", "dim": "#008800", "muted": "#005500", "hover": "#0a1a0a"},
            "paper":   {"bg": "#f5f0e0", "surface": "#f5f0e0", "surface2": "#ede5d5", "fg": "#5c4b37", "accent": "#8b6914", "border": "#d8c8b0", "dim": "#7a6a52", "muted": "#b09878", "hover": "#ede5d5"},
        }
        self._theme = themes.get(theme, themes["default"])
        self.refresh_css()
        self.render_kit()
        if self._samples:
            self.search(self._query)
    
    def compose(self):
        yield Container(
            Container(
                Static("~▲~ crüx"),
                id="header-bar",
            ),
            Container(
                Input(placeholder="search · build · refine — one prompt to rule them all", id="prompt-input"),
                id="prompt-bar",
            ),
            Container(
                Static("~▲~ crüx\narrow to browse a sample", id="waveform-view"),
                id="waveform-bar",
            ),
            Container(
                Container(
                    ListView(id="sample-list"),
                    id="sample-panel",
                ),
                Container(
                    Vertical(
                        ListView(id="kit-grid"),
                        Static("arrow to browse\na sample to see\ndetails here", id="kit-detail"),
                    ),
                    id="kit-panel",
                ),
                id="content-area",
            ),
            Container(
                Static("↑↓/jk=navigate · enter=add · 1-0,^1-^6=slots · delete=clear · p=play · space=lock · /=search · Tab=browse/kit · Ctrl+T=tag · Ctrl+S=settings"),
                id="status-bar",
            ),
            id="main-container",
        )
    
    def on_mount(self) -> None:
        self.load_theme()
        self.query_one("#prompt-input", Input).focus()
        self.load_stats()
        self.search("")
        self.render_kit()
        if self._import_path:
            self.run_import(self._import_path)
    
    @work
    async def load_stats(self):
        self._stats = await self.db.get_stats()
        self._update_header()
        self._update_search_status()
    
    def _update_header(self):
        try:
            hdr = self.query_one("#header-bar", Container).query(Static).first()
            hdr.update(f"~▲~ crüx  │  {self._stats['total']} samples  │  {self._stats['tagged']} tagged")
        except:
            pass
    
    def _update_search_status(self):
        try:
            total = self._stats.get("total", 0) or 0
            self.query_one("#status-bar", Container).query(Static).first().update(
                f"{len(self._samples)} results  •  {total} total")
        except:
            pass
    
    # ─── Search ──────────────────────────────────────────────────────────────
    @work(exclusive=True)
    async def search(self, query: str) -> None:
        self._query = query
        self._samples = await self.db.search(query, PAGE_SIZE)
        lv = self.query_one("#sample-list", ListView)
        lv.clear()
        t = getattr(self, '_theme', None) or {"fg": "#b8c8c8", "bg": "#0b1a20"}
        for s in self._samples:
            name = s.get("name", "?")
            # Extract parent folder from path for context
            fpath = s.get("path", "")
            folder = os.path.basename(os.path.dirname(fpath)) if fpath else ""
            folder_tag = f" [dim]{folder}[/]" if folder else ""
            bpm = f" [orange1]{int(s['bpm'])}bpm[/]" if s.get("bpm") else ""
            dur = f" {s.get('duration_ms',0)//1000}s" if s.get("duration_ms") else ""
            machine = f" [cyan]{s['machine']}[/]" if s.get("machine") else ""
            genre = f" [{s['genre']}]" if s.get("genre") else ""
            tags = (s.get("tags") or [])
            tag_str = " " + " ".join(t[:8] for t in tags[:3]) if tags else ""
            lv.append(ListItem(Label(f"[{t['fg']}]{name}[/]{folder_tag}{machine}{genre}{bpm}{dur}{tag_str}")))
        self._update_search_status()
    
    async def on_input_submitted(self, event: Input.Submitted) -> None:
        q = event.value.strip()
        event.input.clear()
        if not q:
            # Empty Enter → add highlighted sample to current kit slot
            lv = self.query_one("#sample-list", ListView)
            idx = lv.index
            if idx is not None and 0 <= idx < len(self._samples):
                s = self._samples[idx]
                if self._kit_locked[self._kit_index]:
                    self.set_status("slot is locked — unlock with space first")
                    return
                self._kit[self._kit_index] = s
                self._advance_kit_slot()
                self.render_kit()
                slot_name = SLOT_NAMES[self._kit_index] if self._kit_index < len(SLOT_NAMES) else f"Slot {self._kit_index+1}"
                self.set_status(f"added {s['name']} → {slot_name}")
            return
        
        # /tag <words> — edit tags on last selected sample
        if q.startswith("/tag "):
            new_tags = q[5:].strip().split()
            if self._last_selected_id and new_tags:
                try:
                    self.db.update_tags(self._last_selected_id, new_tags)
                    for s in self._samples:
                        if s["id"] == self._last_selected_id:
                            s["tags"] = new_tags
                    for s in self._kit:
                        if s and s["id"] == self._last_selected_id:
                            s["tags"] = new_tags
                    self.set_status(f"tags: {' '.join(new_tags)}")
                    self.search(self._query)
                except Exception as e:
                    self.set_status(f"tag error: {e}")
            else:
                self.set_status("select a sample first")
            return
        
        # /autotag — LLM-tag the selected sample from spectral data
        if q.strip() == "/autotag":
            if self._last_selected_id:
                self.set_status("tagging sample...")
                sample = None
                for s in self._samples:
                    if s["id"] == self._last_selected_id:
                        sample = s
                        break
                if not sample:
                    for s in self._kit:
                        if s and s["id"] == self._last_selected_id:
                            sample = s
                            break
                if sample:
                    feats = {
                        "duration_ms": sample.get("duration_ms", 0),
                        "bpm": sample.get("bpm"),
                        "rms_db": sample.get("rms_db"),
                        "spectral_centroid_hz": sample.get("spectral_centroid_hz"),
                        "spectral_flatness": sample.get("spectral_flatness"),
                        "transient_score": sample.get("transient_score"),
                        "onset_confidence": sample.get("onset_confidence"),
                    }
                    char = describe_audio(feats)
                    folder = os.path.basename(os.path.dirname(sample.get("path",""))) if sample.get("path") else ""
                    prompt = f"Sample: {sample['name']} | {folder} | {sample.get('machine') or ''} | {char}\nReturn JSON: {{\"tags\":[\"kick\",\"808\",\"dark\"],\"genres\":[\"techno\",\"house\"],\"sonics\":[\"dark\",\"punchy\"],\"notes\":\"...\"}}"
                    sys_msg = {"role": "system", "content": "You are crüx. Tags describe what (kick, 808, dark). Genres are an array — a sample can fit multiple (techno, house). Sonics capture tonal character. Use spectral data."}
                    user_msg = {"role": "user", "content": prompt}
                    resp = await llm_chat([sys_msg, user_msg], temperature=0.2, max_tokens=500)
                    if resp:
                        try:
                            j = json.loads(resp)
                            # Find tags: any list field
                            tags = j.get("tags")
                            if not tags:
                                for key in ("category", "type", "instrument", "class", "labels", "keywords"):
                                    val = j.get(key)
                                    if isinstance(val, list):
                                        tags = val
                                        break
                            if not tags:
                                for key, val in j.items():
                                    if isinstance(val, list) and val and isinstance(val[0], str):
                                        tags = val
                                        break
                            if not tags:
                                tags = []
                            if isinstance(tags, str):
                                tags = [tags]
                            sonics = j.get("sonics", [])
                            for s_t in sonics:
                                if s_t not in tags:
                                    tags.append(s_t)
                            # Find genres: list or string
                            genres_raw = j.get("genres") or j.get("genre")
                            if not genres_raw:
                                for key in ("style", "styles", "mood"):
                                    genres_raw = j.get(key)
                                    if genres_raw:
                                        break
                            genre_str = ", ".join(g for g in genres_raw if g) if isinstance(genres_raw, list) else str(genres_raw) if genres_raw else ""
                            # Find notes: any string field
                            notes = j.get("notes") or j.get("description") or ""
                            if not notes:
                                for key, val in j.items():
                                    if key not in ("id", "tags", "genres", "sonics", "genre") and isinstance(val, str) and len(val) > 10:
                                        notes = val
                                        break
                            notes = notes[:200]
                            self.db.update_tags(self._last_selected_id, tags, genre=genre_str, notes=notes)
                            for s in self._samples:
                                if s["id"] == self._last_selected_id:
                                    s["tags"] = tags
                                    s["genre"] = genre
                                    s["ai_notes"] = notes
                            self.set_status(f"tagged: {sample['name']}")
                            self.search(self._query)
                        except:
                            self.set_status("tag: bad LLM response")
                    else:
                        self.set_status("LLM offline")
                else:
                    self.set_status("sample not found")
            else:
                self.set_status("select a sample first")
            return
        
        # /notes <text> — edit ai notes on last selected sample
        if q.startswith("/notes "):
            note = q[7:].strip()[:200]
            if self._last_selected_id:
                try:
                    existing_tags = []
                    for s in self._samples:
                        if s["id"] == self._last_selected_id:
                            existing_tags = s.get("tags") or []
                            break
                    self.db.update_tags(self._last_selected_id, existing_tags, notes=note)
                    for s in self._samples:
                        if s["id"] == self._last_selected_id:
                            s["ai_notes"] = note
                    for s in self._kit:
                        if s and s["id"] == self._last_selected_id:
                            s["ai_notes"] = note
                    self.set_status(f"notes updated")
                    self.search(self._query)
                except Exception as e:
                    self.set_status(f"notes error: {e}")
            else:
                self.set_status("select a sample first")
            return
        
        # /prefix forces direct FTS5 search, bypasses LLM entirely
        if q.startswith("/"):
            self.search(q[1:])
            return
        
        first = q.split()[0].lower()
        has_kit = any(s is not None for s in self._kit)
        
        # Explicit command keywords → LLM build
        if first in ("build", "make", "create", "new"):
            self.set_status(f"▶ LLM: {q}…")
            self.run_llm(q)
            return
        
        # Explicit refine commands → kit_refine (no word limit)
        if first in ("refine", "swap", "change", "replace", "remix"):
            if has_kit:
                self.kit_refine(q)
            else:
                self.set_status("build a kit first, then refine")
            return
        
        # If we have a kit, treat ambiguous input as refine first
        if has_kit and first in ("darker", "heavier", "softer", "warmer", "brighter", "more", "less", "dub", "punchier", "cleaner", "looser", "tighter"):
            self.kit_refine(q)
            return
        
        # Single genre word with a kit → build new kit
        if first in ("techno", "house", "ambient", "lofi", "trap", "funk", "soul", "garage", "drill", "dnb", "jungle", "breakbeat", "electro", "hiphop", "jazz", "rock", "metal", "pop", "reggae", "dubstep", "drum", "bass"):
            self.run_llm(q)
            return
        
        # Has a kit with short input → try refine, fallback to search
        if has_kit and len(q.split()) <= 3:
            self.set_status(f"refining: {q}…")
            self.kit_refine(q)
            return
        
        # Default: search
        self.search(q)
    
    # ─── LLM Commands ────────────────────────────────────────────────────────
    @work(exclusive=True)
    async def run_llm(self, prompt: str) -> None:
        try:
            self._status_spinner = True
            self._spin_task = asyncio.create_task(self._status_spin("LLM working"))
            await self._run_llm_impl(prompt)
        except Exception as e:
            self.set_status(f"LLM error: {e}")
            import traceback
            traceback.print_exc()
        finally:
            self._status_spinner = False
            if hasattr(self, '_spin_task'):
                self._spin_task.cancel()
    
    async def _run_llm_impl(self, prompt: str) -> None:
        self.set_status(f"LLM: {prompt}…")
        
        # Strip command words + generic filler to get the real search intent
        stop_words = {"build", "make", "create", "new", "find", "search", "get", "give", "show",
                      "kit", "drum", "sample", "samples", "me", "a", "the", "an", "with", "of", "for"}
        search_words = [w for w in prompt.lower().split() if w not in stop_words]
        search_query = " ".join(search_words) or prompt
        
        # Ensure stats are fresh
        stats = await self.db.get_stats()
        self._stats = stats
        self._update_header()
        
        # Search broadly: get a balanced set of candidates across drum types
        seen_ids = set()
        all_candidates = []
        
        # 1) Broad FTS5 search for the query
        broad = await self.db.search(search_query, 200)
        for s in broad:
            if s["id"] not in seen_ids:
                all_candidates.append(s)
                seen_ids.add(s["id"])
        
        # 2) If too few results, fall back to LIKE search
        if len(all_candidates) < 30:
            try:
                like = f"%{search_query}%"
                cur = self.db.conn.execute(
                    "SELECT * FROM samples WHERE tags LIKE ? OR name LIKE ? OR machine LIKE ? ORDER BY RANDOM() LIMIT 200",
                    (like, like, like))
                for r in cur.fetchall():
                    d = self.db._parse_row(r)
                    if d["id"] not in seen_ids:
                        all_candidates.append(d)
                        seen_ids.add(d["id"])
            except:
                pass
        
        # 3) For each drum slot type, search specifically for that type + query
        slot_aliases = {
            "Hat": ["Hat", "Hihat", "Hi-hat", "hh", "OH", "CH", "Cymbal"],
            "Ride": ["Ride", "Cymbal"],
            "Crash": ["Crash", "Cymbal"],
            "Perc": ["Perc", "Percussion", "Shaker", "Tambourine", "Click", "Rim"],
        }
        for slot_name in list(SLOT_NAMES[:8]):
            try:
                terms = slot_aliases.get(slot_name, [slot_name])
                for term in terms[:4]:  # search up to 4 aliases per slot
                    slot_q = f"{term} {search_query}"
                    slot_results = await self.db.search(slot_q, 20)
                    for s in slot_results:
                        if s["id"] not in seen_ids:
                            all_candidates.append(s)
                            seen_ids.add(s["id"])
            except:
                pass
        
        # 4) If still too few, pull random samples to give the LLM a diverse palette
        if len(all_candidates) < 60:
            try:
                cur = self.db.conn.execute(
                    "SELECT * FROM samples WHERE id NOT IN ({}) ORDER BY RANDOM() LIMIT 100".format(
                        ",".join(f"'{x}'" for x in list(seen_ids)[:500]) or "''"))
                for r in cur.fetchall():
                    d = self.db._parse_row(r)
                    if d["id"] not in seen_ids:
                        all_candidates.append(d)
                        seen_ids.add(d["id"])
            except:
                pass
        
        has_genre_match = len(broad) > 5
        candidates = ""
        for i, s in enumerate(all_candidates[:80]):
            tags = (s.get("tags") or [])
            feats_dict = {
                "duration_ms": s.get("duration_ms", 0),
                "bpm": s.get("bpm"),
                "rms_db": s.get("rms_db"),
                "spectral_centroid_hz": s.get("spectral_centroid_hz"),
                "spectral_flatness": s.get("spectral_flatness"),
                "transient_score": s.get("transient_score"),
                "onset_confidence": s.get("onset_confidence"),
            }
            char = describe_audio(feats_dict)
            tag_str = ' '.join(tags[:4])
            machine = s.get('machine') or ''
            folder = os.path.basename(os.path.dirname(s.get("path",""))) if s.get("path") else ""
            # Extract likely slot type from filename (primary indicator)
            fname = (s.get("name") or "").lower()
            slot_hint = ""
            for hint_word, hint_slot in [
                ("kick", "KICK"), ("kik", "KICK"), ("bd", "KICK"),
                ("snare", "SNARE"), ("snr", "SNARE"), ("sna", "SNARE"),
                ("hat", "HAT"), ("hihat", "HAT"), ("hi-hat", "HAT"), ("hh", "HAT"),
                ("oh", "HAT"), ("ch", "HAT"), ("open_hat", "HAT"), ("closed_hat", "HAT"),
                ("clap", "CLAP"),
                ("perc", "PERC"), ("tamb", "PERC"), ("shaker", "PERC"),
                ("tom", "TOM"),
                ("ride", "RIDE"),
                ("crash", "CRASH"), ("cymbal", "CRASH"),
                ("rim", "PERC"), ("click", "PERC"),
            ]:
                if hint_word in fname:
                    slot_hint = f" [{hint_slot}]"
                    break
            candidates += f"{s['id']}: {s['name']}{slot_hint} | {machine} | {folder} | {tag_str} | {char}\n"
        
        # Per-slot spectral guidance for the LLM
        slot_guide = {
            0: "Kick — low centroid (<1500Hz), high RMS, sharp onset, very short",
            1: "Snare — medium centroid (1500-3000Hz), moderate RMS, sharp onset, noise tail",
            2: "Hat — high centroid (>2500Hz), low RMS, very short, noisy",
            3: "Clap — mid-high centroid, moderate RMS, sharp onset, noise burst",
            4: "Perc — varies widely",
        }
        slot_spec = ', '.join(f"{i}={SLOT_NAMES[i] if i < len(SLOT_NAMES) else f'Slot{i+1}'}" for i in range(KIT_SLOTS))
        slot_guide_str = '\n'.join(f"Slot {k}: {v}" for k, v in slot_guide.items() if k < KIT_SLOTS)
        
        context_note = f'Genre-matched' if has_genre_match else 'No exact genre matches — using diverse samples'
        
        sys_msg = {'role': 'system', 'content': f'You are crüx, a sample curator. Match samples to slots by filename FIRST, then confirm with spectral features.'}
        user_msg = {'role': 'user', 'content': f'Request: "{prompt}"\n\nDECISION RULES (priority order):\n1. FILENAME is the PRIMARY indicator — if a filename says "kick", it belongs in the Kick slot.\n2. Spectral features are SECONDARY — use them to confirm the filename match or break ties.\n3. Never put a Ride sample in a Hat slot just because both are cymbals.\n\nSLOT GUIDE (spectral expectations):\n{slot_guide_str}\n\nAll slots: {slot_spec}\n\nCANDIDATES ({len(all_candidates)}):\n{candidates}\n\nAssign EVERY slot (0 through {KIT_SLOTS-1}) the best matching candidate. Fill ALL {KIT_SLOTS} slots — leave none empty. Use filename as primary signal, spectral as confirmation. JSON: {{"action":"kit","slots":[{{"slot":0,"sampleId":"id"}},{{"slot":1,"sampleId":"id"}}...ALL {KIT_SLOTS} SLOTS],"name":"..."}}. Return ONLY JSON with exactly {KIT_SLOTS} slot entries.'}
        
        resp = await llm_chat([sys_msg, user_msg], temperature=0.1, max_tokens=1500)
        if not resp:
            self.set_status("LM Studio offline — start it for LLM commands")
            return
        
        j = extract_json(resp)
        if not j:
            self.set_status(f"LLM: {resp[:80]}…")
            return
        
        action = j.get("action", "message")
        if action == "search":
            self.search(j.get("query", prompt))
            self.set_status(f"searched: {j.get('query','')}")
        elif action == "kit":
            slots = j.get("slots", [])
            if not slots:
                # Fallback: old format with just ids
                ids = j.get("ids", [])
                if len(ids) < 2:
                    self.set_status(f"LLM: not enough samples")
                    return
                slots = [{"slot": i, "sampleId": sid} for i, sid in enumerate(ids)]
            name = j.get("name", f"kit-{int(time.time())}")
            self._kit = [None] * KIT_SLOTS
            # Save locked slots — don't wipe them
            locked_slots = {}
            for i in range(KIT_SLOTS):
                if self._kit_locked[i] and self._kit[i]:
                    locked_slots[i] = self._kit[i]
            self._kit = [None] * KIT_SLOTS
            self._kit_locked = [False] * KIT_SLOTS
            # Restore locked slots
            for i, s in locked_slots.items():
                self._kit[i] = s
                self._kit_locked[i] = True
            assigned = len(locked_slots)
            # Fetch all slot samples concurrently
            fetch_tasks = []
            slot_indices = []
            for entry in slots:
                idx = entry.get("slot")
                sid = entry.get("sampleId")
                if idx is None or not sid:
                    continue
                if idx < 0 or idx >= KIT_SLOTS:
                    continue
                if self._kit_locked[idx]:
                    continue
                fetch_tasks.append(self.db.get_sample(sid))
                slot_indices.append(idx)
            results = await asyncio.gather(*fetch_tasks) if fetch_tasks else []
            for idx, s in zip(slot_indices, results):
                if s:
                    self._kit[idx] = s
                    assigned += 1
            self.render_kit()
            self.set_status(f"built \"{name}\" ({assigned}/{KIT_SLOTS} slots)")
            # Restore samples in browse pane after LLM completes
            self.search(self._query)
        else:
            self.set_status(j.get("message", "ok"))
            self.search(self._query)
    
    # ─── Kit ─────────────────────────────────────────────────────────────────
    def _show_waveform(self, path: str, name: str, sample: Optional[dict] = None):
        """Show sample info in top bar and kit detail panel."""
        lines = [(name or "?")[:60]]
        if sample:
            dur = sample.get("duration_ms", 0)
            dur_str = f"{dur//1000}s" if dur else "—"
            bpm = f"{int(sample['bpm'])}bpm" if sample.get("bpm") else "—"
            machine = (sample.get("machine") or "—")[:20]
            folder = os.path.basename(os.path.dirname(sample.get("path",""))) if sample.get("path") else "—"
            tags = (sample.get("tags") or [])
            tag_str = " ".join(tags) if tags else "—"
            genre = (sample.get("genre") or "—")[:15]
            lines.append(f"{dur_str}  {bpm}  {machine}")
            lines.append(f"{folder}  {genre}")
            lines.append(f"tags: {tag_str}")
            notes = (sample.get("ai_notes") or "")[:80]
            if notes:
                lines.append(f"notes: {notes}")
            lines.append("---")
            lines.append('/tag <w> /autotag /notes <t> to edit')
        meta = "\n".join(lines)
        try:
            self.query_one("#waveform-view", Static).update(meta)
        except:
            pass
        try:
            self.query_one("#kit-detail", Static).update(meta)
        except:
            pass
    
    def render_kit(self):
        lv = self.query_one("#kit-grid", ListView)
        lv.clear()
        t = getattr(self, '_theme', None) or {
            "accent": "#1a9e9e", "fg": "#b8c8c8", "muted": "#3a5a65"
        }
        lock_color = "#e0673a"
        for i in range(KIT_SLOTS):
            s = self._kit[i]
            label = SLOT_NAMES[i] if i < len(SLOT_NAMES) else f"Slot {i+1}"
            locked = self._kit_locked[i]
            slot_color = lock_color if locked else t["accent"]
            name_color = lock_color if locked else t["fg"]
            if s:
                bpm = f" {int(s['bpm'])}bpm" if s.get("bpm") else ""
                dur = f" {s.get('duration_ms',0)//1000}s" if s.get("duration_ms") else ""
                machine = f" {s['machine']}" if s.get("machine") else ""
                tags = (s.get("tags") or [])
                tag_str = " " + " ".join(t[:6] for t in tags[:2]) if tags else ""
                lv.append(ListItem(Label(
                    f"[bold {slot_color}]{label:>6}[/] [{name_color}]{s['name']}[/]{machine}{bpm}{dur}{tag_str}"
                )))
            else:
                lv.append(ListItem(Label(
                    f"[bold {slot_color}]{label:>6}[/] [italic {t['muted']}]— empty[/]"
                )))
        if lv.children:
            lv.index = min(self._kit_index, len(lv.children) - 1)
    
    @on(ListView.Highlighted)
    def handle_list_highlight(self, event: ListView.Highlighted) -> None:
        """Update UI when navigating lists."""
        lv = event.list_view
        idx = lv.index
        if idx is None:
            return
        name = "?"
        path = ""
        sample = None
        if lv.id == "kit-grid" and 0 <= idx < KIT_SLOTS:
            self._kit_index = idx
            sample = self._kit[idx]
            if sample:
                name = sample.get("name", "?")
                path = sample.get("path", "")
        elif lv.id == "sample-list" and 0 <= idx < len(self._samples):
            sample = self._samples[idx]
            name = sample.get("name", "?")
            path = sample.get("path", "")
        if path and os.path.exists(path):
            self._play_audio(path)
        if sample:
            self._last_selected_id = sample.get("id")
        self._show_waveform(path, name, sample=sample)
        self.set_status(f"▶ {name}")
    
    @on(ListView.Selected)
    def handle_list_selected(self, event: ListView.Selected) -> None:
        """Enter key — set target slot from kit-grid, or add sample from browse."""
        lv = event.list_view
        if lv.id == "kit-grid":
            idx = lv.index
            if idx is not None and 0 <= idx < KIT_SLOTS:
                self._kit_index = idx
                self.render_kit()
                slot_name = SLOT_NAMES[idx] if idx < len(SLOT_NAMES) else f"Slot {idx+1}"
                self.set_status(f"→ targeting: {slot_name}  (Tab to browse, Enter to add)")
        elif lv.id == "sample-list":
            idx = lv.index
            if idx is not None and 0 <= idx < len(self._samples):
                s = self._samples[idx]
                slot_name = SLOT_NAMES[self._kit_index] if self._kit_index < len(SLOT_NAMES) else f"Slot {self._kit_index+1}"
                if self._kit_locked[self._kit_index]:
                    self.set_status(f"{slot_name} is locked — unlock with space first")
                    return
                self._kit[self._kit_index] = s
                self._advance_kit_slot()
                self.render_kit()
                next_slot = SLOT_NAMES[self._kit_index] if self._kit_index < len(SLOT_NAMES) else f"Slot {self._kit_index+1}"
                self.set_status(f"{s['name']} → {slot_name}  |  next: {next_slot}")
    
    def _play_audio(self, path: str):
        """Play audio, killing any previous playback to prevent bleed."""
        if self._current_audio:
            try:
                self._current_audio.kill()
            except:
                pass
        if path and os.path.exists(path):
            self._current_audio = subprocess.Popen(
                ["afplay", path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
            )
    
    def _advance_kit_slot(self):
        """Move to next empty kit slot."""
        next_empty = next((i for i in range(self._kit_index + 1, KIT_SLOTS) if not self._kit[i]), None)
        if next_empty is not None:
            self._kit_index = next_empty
        elif self._kit_index < KIT_SLOTS - 1:
            self._kit_index += 1
    
    def kit_refine(self, direction: str):
        if not direction.strip(): return
        
        # Detect if user is targeting specific slot(s) by name
        slot_name_map = {}
        for i, name in enumerate(SLOT_NAMES):
            slot_name_map[name.lower()] = i
            # Also map partial/shorthand
            if name.lower() == "percussion":
                slot_name_map["perc"] = i
        
        words = direction.lower().split()
        targeted_slots = set()
        remaining_words = []
        for w in words:
            if w in slot_name_map:
                targeted_slots.add(slot_name_map[w])
            else:
                remaining_words.append(w)
        
        if targeted_slots:
            # Refine mentioned slots (unlocked OR empty), skip locked
            target = [i for i in targeted_slots if i < KIT_SLOTS and not self._kit_locked[i]]
            if not target:
                self.set_status("those slots are locked — unlock to refine")
                return
        else:
            # No specific slot mentioned — refine all unlocked AND empty slots
            target = [i for i in range(KIT_SLOTS) if not self._kit_locked[i]]
        
        if not target:
            self.set_status("all slots locked — unlock some to refine")
            return
        
        # Build search query from remaining words (strip filler)
        stop_words = {"build", "make", "create", "new", "refine", "remix", "swap", "replace", "change",
                      "the", "a", "an", "with", "of", "for", "in", "to", "my", "it", "up", "out"}
        search_words = [w for w in remaining_words if w not in stop_words]
        if search_words:
            search_query = " ".join(search_words)
        else:
            slot_names = [SLOT_NAMES[i] if i < len(SLOT_NAMES) else f"slot{i+1}" for i in target]
            search_query = " ".join(slot_names)
        
        # Translate vague directions into searchable terms
        dir_map = {
            "darker": "dark deep bass sub low heavy low_centroid",
            "brighter": "bright hi hat cymbal high shimmer high_centroid",
            "heavier": "heavy hard punch loud thick high_rms",
            "softer": "soft gentle light quiet warm low_rms",
            "warmer": "warm analog tape saturated rich mid_centroid",
            "punchier": "punchy attack transient snap sharp high_transient",
            "cleaner": "clean clear crisp digital precise low_flatness",
            "looser": "loose sloppy groove swing organic",
            "tighter": "tight compressed controlled punch snappy high_onset",
        }
        translated = " ".join(dir_map.get(w, w) for w in search_words)
        
        self.run_kit_refine(translated, target)
    
    @work(exclusive=True)
    async def run_kit_refine(self, direction: str, target_slots: list[int]):
        self._status_spinner = True
        self._spin_task = asyncio.create_task(self._status_spin(f"refining: {direction}"))
        try:
            slot_context_words = []
            for i in target_slots:
                if i < len(SLOT_NAMES):
                    slot_context_words.append(SLOT_NAMES[i])
            # Only add slot context when targeting specific slots (<=half), not broad refine
            if slot_context_words and len(target_slots) <= KIT_SLOTS // 2:
                search_query = " ".join(slot_context_words) + " " + direction
            else:
                search_query = direction
            
            # Merge FTS5 + random candidates for rich spectral pool
            kit_ids = set()
            for s in self._kit:
                if s:
                    kit_ids.add(s["id"])
            
            # FTS5 matches first (priority), then random samples (spectral variety)
            relevant = await self.db.search(search_query, 40)
            candidates = [s for s in relevant if s["id"] not in kit_ids]
            random_samples = await self.db.get_some(40)
            for s in random_samples:
                if s["id"] not in kit_ids:
                    candidates.append(s)
            cand_str = ""
            for i, s in enumerate(candidates[:80]):
                tags = " ".join(s.get("tags") or [])[:60]
                feats_dict = {
                    "duration_ms": s.get("duration_ms", 0),
                    "bpm": s.get("bpm"),
                    "rms_db": s.get("rms_db"),
                    "spectral_centroid_hz": s.get("spectral_centroid_hz"),
                    "spectral_flatness": s.get("spectral_flatness"),
                    "transient_score": s.get("transient_score"),
                    "onset_confidence": s.get("onset_confidence"),
                }
                char = describe_audio(feats_dict)
                # Extract filename slot hint
                fname = (s.get("name") or "").lower()
                slot_hint = ""
                for hint_word, hint_slot in [
                    ("kick", "KICK"), ("kik", "KICK"), ("bd", "KICK"),
                    ("snare", "SNARE"), ("snr", "SNARE"), ("sna", "SNARE"),
                    ("hat", "HAT"), ("hihat", "HAT"), ("hh", "HAT"),
                    ("oh", "HAT"), ("ch", "HAT"), ("open_hat", "HAT"), ("closed_hat", "HAT"),
                    ("clap", "CLAP"),
                    ("perc", "PERC"), ("tamb", "PERC"), ("shaker", "PERC"),
                    ("tom", "TOM"),
                    ("ride", "RIDE"),
                    ("crash", "CRASH"), ("cymbal", "CRASH"),
                    ("rim", "PERC"), ("click", "PERC"),
                ]:
                    if hint_word in fname:
                        slot_hint = f" [{hint_slot}]"
                        break
                cand_str += f"{s['id']}: {s['name']}{slot_hint} | {tags} | {s.get('genre') or '-'} | {char}\n"
            
            kit_str = ""
            for i in range(KIT_SLOTS):
                s = self._kit[i]
                lock = "🔒" if self._kit_locked[i] else "🔓"
                label = SLOT_NAMES[i] if i < len(SLOT_NAMES) else f"Slot{i+1}"
                if s:
                    kit_str += f"{i}:{lock}{label}={s['name']} "
                else:
                    kit_str += f"{i}:{lock}{label}=— "
            
            sys_msg = {'role': 'system', 'content': 'You are crüx, a sample curator. Match by filename FIRST, confirm with spectral features.'}
            user_msg = {'role': 'user', 'content': f'Refine "{direction}"\nRULES: Filename is primary slot indicator. Spectral features confirm the match.\nKit so far: {kit_str}\nONLY refine these specific slots: {target_slots}\nCandidates:\n{cand_str}\nReturn JSON with exactly {len(target_slots)} entries: {{"reassignments":[{{"slotIndex":0,"sampleId":"id"}},...]}}'}
            
            resp = await llm_chat([sys_msg, user_msg], temperature=0.1, max_tokens=1500)
            if not resp:
                self.set_status("LLM offline — can't refine")
                return
            j = extract_json(resp)
            if not j or "reassignments" not in j:
                self.set_status("no changes suggested")
                return
            
            count = 0
            for r in j["reassignments"]:
                idx = r.get("slotIndex")
                sid = r.get("sampleId")
                if idx is not None and 0 <= idx < KIT_SLOTS and not self._kit_locked[idx] and sid:
                    s = await self.db.get_sample(sid)
                    if s:
                        self._kit[idx] = s
                        count += 1
            self.render_kit()
            slot_label = "slot" if len(target_slots) == 1 else "slots"
            self.set_status(f"refined {count} {slot_label}: {direction}")
            self.search(self._query)
        finally:
            self._status_spinner = False
            if hasattr(self, '_spin_task'):
                self._spin_task.cancel()
    
    # ─── Import ──────────────────────────────────────────────────────────────
    @work(exclusive=True)
    async def run_tag(self):
        """Batch-tag all untagged samples via LLM. Pause/resume with Ctrl+T."""
        self._tag_paused = False
        self._status_spinner = True
        
        # Shared progress: [tagged_so_far, total_untagged]
        progress = [0, 0]
        
        async def _pausable_spinner():
            chars = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]
            i = 0
            while getattr(self, '_status_spinner', False):
                if self._tag_paused:
                    self.set_status("⏸ paused — Ctrl+T to resume")
                else:
                    t = progress[0]
                    total = progress[1]
                    if total > 0:
                        pct = min(t * 100 // total, 100)
                        self.set_status(f"{chars[i % len(chars)]} tagging  ({t}/{total}) {pct}%")
                    else:
                        self.set_status(f"{chars[i % len(chars)]} tagging")
                i += 1
                try:
                    await asyncio.sleep(0.2)
                except asyncio.CancelledError:
                    break
        
        def _is_paused():
            return self._tag_paused
        
        self._spin_task = asyncio.create_task(_pausable_spinner())
        try:
            cfg_batch = _config.get("import", {}).get("tag_batch_size", 12)
            results = await tag_pipeline(self.db, batch_size=cfg_batch, app_ref=self, pause_check=_is_paused, progress=progress)
            if results:
                self._stats = await self.db.get_stats()
                self._update_header()
                self.search(self._query)
                self.set_status(f"✓ tagged {results} samples")
            elif self._tag_paused:
                self.set_status("tag paused")
            else:
                self.set_status("no untapped samples")
        finally:
            self._status_spinner = False
            if hasattr(self, '_spin_task'):
                self._spin_task.cancel()
    
    @work
    async def run_import(self, path: str):
        self.set_status(f"importing: {path}…")
        self._import_path = None
        results = await import_pipeline(path, self.db, app_ref=self)
        if results:
            self.set_status(f"✓ imported {results} samples")
            self._stats = await self.db.get_stats()
            self._update_header()
            self.search("")
        else:
            self.set_status("import: no new samples found")
    
    def on_status_msg(self, msg: StatusMsg) -> None:
        self.set_status(msg.text)
    
    async def _status_spin(self, label: str):
        """Animate a spinner in the status bar while a background task runs."""
        chars = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]
        i = 0
        while getattr(self, '_status_spinner', False):
            self.set_status(f"{chars[i % len(chars)]} {label}")
            i += 1
            try:
                await asyncio.sleep(0.1)
            except asyncio.CancelledError:
                break
    
    def set_status(self, text: str):
        try:
            self.query_one("#status-bar", Container).query(Static).first().update(str(text)[:80])
        except:
            pass
    
    # ─── Actions ─────────────────────────────────────────────────────────────
    def action_clear_search(self):
        self.search("")
        self.query_one("#prompt-input", Input).clear()
        self.query_one("#prompt-input", Input).focus()
    
    def action_refresh(self):
        self.set_status("refreshing…")
        self.load_stats()
        self.search(self._query)
    
    def action_play(self):
        """Play the currently selected sample — from kit slot or sample list."""
        # Check if kit grid is focused
        focused_id = self.focused.id if self.focused else None
        if focused_id == "kit-grid":
            s = self._kit[self._kit_index] if self._kit_index < len(self._kit) else None
            if s:
                path = s.get("path", "")
                if path and os.path.exists(path):
                    self._play_audio(path)
                    self.set_status(f"▶ slot {s['name']}")
                    return
        # Fallback: play from sample list
        lv = self.query_one("#sample-list", ListView)
        idx = lv.index
        if idx is not None and 0 <= idx < len(self._samples):
            s = self._samples[idx]
            path = s.get("path", "")
            if path and os.path.exists(path):
                self._play_audio(path)
                self.set_status(f"▶ {s.get('name','?')}")
    
    def action_add_to_kit(self):
        """Add highlighted sample from list to the active kit slot."""
        lv = self.query_one("#sample-list", ListView)
        idx = lv.index
        if idx is not None and 0 <= idx < len(self._samples):
            s = self._samples[idx]
            if self._kit_locked[self._kit_index]:
                slot_name = SLOT_NAMES[self._kit_index] if self._kit_index < len(SLOT_NAMES) else f"Slot {self._kit_index+1}"
                self.set_status(f"{slot_name} is locked — unlock with space first")
                lv.focus()
                return
            self._kit[self._kit_index] = s
            self._advance_kit_slot()
            self.render_kit()
            lv.focus()
            slot_name = SLOT_NAMES[self._kit_index] if self._kit_index < len(SLOT_NAMES) else f"Slot {self._kit_index+1}"
            self.set_status(f"added {s['name']} → {slot_name}")
    
    def action_toggle_lock(self):
        """Toggle lock on the active kit slot."""
        self._kit_locked[self._kit_index] = not self._kit_locked[self._kit_index]
        self.render_kit()
        status = "🔒 locked" if self._kit_locked[self._kit_index] else "🔓 unlocked"
        slot_name = SLOT_NAMES[self._kit_index] if self._kit_index < len(SLOT_NAMES) else f"Slot {self._kit_index+1}"
        self.set_status(f"{slot_name} {status}")
    
    def action_clear_kit_slot(self):
        """Clear the active kit slot."""
        self._kit[self._kit_index] = None
        self._kit_locked[self._kit_index] = False
        self.render_kit()
        slot_name = SLOT_NAMES[self._kit_index] if self._kit_index < len(SLOT_NAMES) else f"Slot {self._kit_index+1}"
        self.set_status(f"{slot_name} cleared")
    
    def action_cursor_up(self):
        """Move cursor up (Vim k style)."""
        lv = self.focused
        if lv and hasattr(lv, "action_cursor_up"):
            lv.action_cursor_up()
    
    def action_cursor_down(self):
        """Move cursor down (Vim j style)."""
        lv = self.focused
        if lv and hasattr(lv, "action_cursor_down"):
            lv.action_cursor_down()
    
    def _add_highlighted_to_slot(self, slot_idx: int):
        """Add the highlighted sample from browse list directly to a specific kit slot."""
        lv = self.query_one("#sample-list", ListView)
        idx = lv.index
        if idx is not None and 0 <= idx < len(self._samples):
            s = self._samples[idx]
            if self._kit_locked[slot_idx]:
                slot_name = SLOT_NAMES[slot_idx] if slot_idx < len(SLOT_NAMES) else f"Slot {slot_idx+1}"
                self.set_status(f"{slot_name} is locked — unlock with space first")
                return
            self._kit_index = slot_idx
            self._kit[slot_idx] = s
            self.render_kit()
            slot_name = SLOT_NAMES[slot_idx] if slot_idx < len(SLOT_NAMES) else f"Slot {slot_idx+1}"
            self.set_status(f"{s['name']} → {slot_name}")
    
    def action_slot_1(self): self._add_highlighted_to_slot(0)
    def action_slot_2(self): self._add_highlighted_to_slot(1)
    def action_slot_3(self): self._add_highlighted_to_slot(2)
    def action_slot_4(self): self._add_highlighted_to_slot(3)
    def action_slot_5(self): self._add_highlighted_to_slot(4)
    def action_slot_6(self): self._add_highlighted_to_slot(5)
    def action_slot_7(self): self._add_highlighted_to_slot(6)
    def action_slot_8(self): self._add_highlighted_to_slot(7)
    def action_slot_9(self): self._add_highlighted_to_slot(8)
    def action_slot_10(self): self._add_highlighted_to_slot(9)
    def action_slot_11(self): self._add_highlighted_to_slot(10)
    def action_slot_12(self): self._add_highlighted_to_slot(11)
    def action_slot_13(self): self._add_highlighted_to_slot(12)
    def action_slot_14(self): self._add_highlighted_to_slot(13)
    def action_slot_15(self): self._add_highlighted_to_slot(14)
    def action_slot_16(self): self._add_highlighted_to_slot(15)
    
    def action_export(self):
        """Open the export modal."""
        self.push_screen(ExportScreen())
    
    def action_tag(self):
        """Toggle pause/resume tagging, or start if not running."""
        if self._tag_paused:
            self._tag_paused = False
            self.set_status("resuming tag...")
        elif hasattr(self, '_spin_task') and not self._spin_task.done():
            self._tag_paused = True
            self.set_status("pausing after current batch...")
        else:
            self.run_tag()
    
    def action_settings(self):
        """Open the settings modal."""
        def _on_settings_done(result):
            if result == "tag":
                self.run_tag()
                return
            if result:
                global _config, LMSTUDIO_URL, LMSTUDIO_MODEL, LLM_API_KEY, DB_PATH, KIT_SLOTS, PAGE_SIZE
                _config = load_config()
                _db = _config["general"].get("db_path", "")
                if _db:
                    DB_PATH = os.path.expanduser(_db)
                else:
                    lib = _config["general"].get("library_path", "")
                    if lib:
                        DB_PATH = os.path.join(os.path.expanduser(lib), "crux.db")
                    else:
                        DB_PATH = os.path.join(CONFIG_DIR, "crux.db")
                LMSTUDIO_URL = _config["llm"]["url"]
                LMSTUDIO_MODEL = _config["llm"]["model"]
                LLM_API_KEY = _config["llm"].get("api_key", "")
                KIT_SLOTS = _config["ui"]["kit_slots"]
                PAGE_SIZE = _config["ui"]["samples_per_page"]
                self.db.close()
                self.db = DB()
                self.db.connect()
                self._kit = [None] * KIT_SLOTS
                self._kit_locked = [False] * KIT_SLOTS
                self.load_stats()
                self.load_theme()
                self.search(self._query)
                self.render_kit()
                self.set_status("Settings saved")
        t = getattr(self, '_theme', None)
        self.push_screen(SettingsScreen(theme_colors=t), _on_settings_done)
    
    def action_focus_next(self):
        """Cycle focus between sample list and kit grid only (skip prompt input)."""
        focused_id = self.focused.id if self.focused else None
        if focused_id == "kit-grid":
            try:
                self.query_one("#sample-list", ListView).focus()
            except:
                pass
        else:
            try:
                self.query_one("#kit-grid", ListView).focus()
            except:
                pass
    
    def action_focus_search(self):
        """Jump to the search/prompt input from anywhere."""
        try:
            inp = self.query_one("#prompt-input", Input)
            inp.focus()
            inp.action_home()
        except:
            pass

# ─── Entry ────────────────────────────────────────────────────────────────────
def main():
    import_path = None
    if len(sys.argv) > 1 and sys.argv[1] == "import":
        import_path = sys.argv[2] if len(sys.argv) > 2 else os.getcwd()
    
    app = CruxApp(import_path=import_path)
    app.run()

if __name__ == "__main__":
    main()
