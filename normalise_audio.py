import json
import os
import subprocess
import re
import hashlib
import threading
import time
from pathlib import Path
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed

# =========================== Logging Setup ===========================
LOG_FILE = rf"{os.path.dirname(__file__)}\log.txt" if __file__ else "log.txt"
log_lock = threading.Lock()

def log_print(*args, **kwargs):
    """Print to console and write to log.txt with timestamp."""
    # Print to console normally
    print(*args, **kwargs)

    # Convert args to string like print does
    message = " ".join(str(arg) for arg in args)
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    log_line = f"[{timestamp}] {message}\n"

    # Write to log file (thread-safe)
    with log_lock:
        try:
            with open(LOG_FILE, "a", encoding="utf-8") as f:
                f.write(log_line)
        except Exception:
            pass  # Fail silently if logging fails
# ====================================================================

# =========================== Configuration ===========================
MEDIA_BASE = r"F:\MEDIA"  # Base folder containing MOVIES and TV SHOWS

ROOT_DIRS = [
    rf"{MEDIA_BASE}\MOVIES",
    rf"{MEDIA_BASE}\TV SHOWS",
]

STATE_FILE = rf"{MEDIA_BASE}\.normalise_state.json"  # Tracks processed files
VIDEO_EXTS = {".mkv", ".mp4", ".mov", ".m4v"}

# Peak normalisation target (sample peak, not loudness)
TARGET_PEAK_DBFS = (
    -0.1
)  # dBFS (0.0 = full scale; -0.1 helps avoid intersample clipping)

# Default bitrate for lossy encoders when re-encoding main audio
AUDIO_BITRATE = "192k"

# Progress & container behaviour
FASTSTART = (
    True  # for mp4/m4v/mov; move moov atom to front (may add a "finalising" tail)
)

# Fast fingerprint settings (avoid full hashing)
QUICK_FP_BLOCK_MB = 4  # per sampled block
QUICK_FP_BLOCKS = 3  # fixed at 3 (head/middle/tail)

# Parallelism (tuned for i5-14400F: 14C/20T). Adjust as desired.
WORKERS = (
    3  # files to process concurrently (try 3–4 for throughput, 2 for responsiveness)
)
FFMPEG_THREADS_PER_JOB = 4  # ffmpeg thread count per job (avoid oversubscription)

# Optional: skip tiny/sample files often bundled with releases
SKIP_SAMPLES = True
SAMPLE_NAME_TOKENS = {"sample", "trailer", "teaser"}
MIN_FILE_SIZE_BYTES = 50 * 1024 * 1024  # skip < 50 MB if SKIP_SAMPLES

# Timeouts and validation thresholds
SUBPROCESS_TIMEOUT_SECONDS = 3600  # 1 hour max per subprocess call
MIN_OUTPUT_FILE_SIZE_BYTES = 1024  # Minimum valid output file size

# ====================================================================

# ---------- Utilities & state ----------
state_lock = threading.Lock()
VOL_MAX_RE = re.compile(
    r"max_volume:\s*([-\+]?inf|[-+]?\d+(?:\.\d+)?)\s*dB", re.IGNORECASE
)


def utc_now_z() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def load_state(path: str) -> dict:
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
                if not isinstance(data, dict) or "files" not in data:
                    log_print("Warning: Invalid state file format, resetting state")
                    return {"files": {}}
                return data
        except (json.JSONDecodeError, IOError) as e:
            log_print(f"Warning: Could not load state file ({e}), resetting state")
    return {
        "files": {}
    }  # { "files": { "abs_path": {"sig":"...","qfp":"...","done_at":"..."} } }


def save_state(path: str, data: dict) -> None:
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, path)
    except Exception as e:
        log_print(f"Warning: Failed to save state: {e}")
        if os.path.exists(tmp):
            try:
                os.unlink(tmp)
            except Exception:
                pass


def file_signature(p: Path) -> str:
    st = p.stat()
    return f"{st.st_size}-{st.st_mtime_ns}"


def compute_quick_fingerprint(p: Path) -> str:
    """
    Hash three short slices (head/middle/tail). Much faster than full SHA on big files.
    """
    size = p.stat().st_size
    block_size = QUICK_FP_BLOCK_MB * 1024 * 1024
    if size == 0:
        return "0" * 32
    h = hashlib.sha256()
    with p.open("rb") as f:
        # Head
        f.seek(0)
        h.update(f.read(min(block_size, size)))
        # Middle
        mid_start = max(0, (size // 2) - (block_size // 2))
        f.seek(mid_start)
        h.update(f.read(min(block_size, max(0, size - mid_start))))
        # Tail
        tail_start = max(0, size - block_size)
        f.seek(tail_start)
        h.update(f.read(min(block_size, max(0, size - tail_start))))
    return h.hexdigest()[:32]


def run(
    cmd: list[str], timeout: int = SUBPROCESS_TIMEOUT_SECONDS
) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )


# ---------- Probing & stream selection ----------
def ffprobe_streams(path: Path) -> dict:
    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-print_format",
        "json",
        "-show_streams",
        "-show_format",
        str(path),
    ]
    proc = run(cmd)
    if proc.returncode != 0:
        raise RuntimeError(f"ffprobe failed: {proc.stderr}")

    if not proc.stdout:
        raise RuntimeError("ffprobe returned no output")

    try:
        data = json.loads(proc.stdout)
        if not isinstance(data, dict):
            raise ValueError("ffprobe output is not a valid JSON object")
        return data
    except json.JSONDecodeError as e:
        raise RuntimeError(f"Failed to parse ffprobe JSON output: {e}")


def find_main_audio_abs_index(meta: dict) -> int | None:
    streams = meta.get("streams", [])
    audio_streams = [s for s in streams if s.get("codec_type") == "audio"]
    if not audio_streams:
        return None
    for s in audio_streams:
        if s.get("disposition", {}).get("default", 0) == 1:
            return s["index"]
    return audio_streams[0]["index"]


def build_audio_absindex_to_order(meta: dict) -> dict[int, int]:
    mapping, order = {}, 0
    for s in meta.get("streams", []):
        if s.get("codec_type") == "audio":
            mapping[s["index"]] = order
            order += 1
    return mapping


# ---------- Peak measurement ----------
def measure_max_peak_db(src: Path, main_abs_index: int) -> float | None:
    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-threads",
        str(FFMPEG_THREADS_PER_JOB),
        "-y",
        "-i",
        str(src),
        "-map",
        f"0:{main_abs_index}",
        "-af",
        "volumedetect",
        "-f",
        "null",
        "-",
    ]
    proc = run(cmd)
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg volumedetect failed: {proc.stderr}")
    text = proc.stderr or proc.stdout
    matches = VOL_MAX_RE.findall(text)
    if not matches:
        return None
    val = matches[-1]
    if val.lower() == "inf":
        return None
    try:
        return float(val)
    except ValueError:
        return None


# ---------- Codec selection (preserve original where sensible) ----------
# Map ffprobe codec_name -> ffmpeg encoder name
CODEC_ENCODER_MAP = {
    "ac3": "ac3",
    "eac3": "eac3",
    "aac": "aac",
    "mp3": "libmp3lame",
    "opus": "libopus",
    "flac": "flac",
    "dts": "dca",  # DTS encoder (some builds may lack this)
    "truehd": "truehd",  # often undesirable to re-encode; may not be present
    "alac": "alac",
    "pcm_s16le": "pcm_s16le",
    "pcm_s24le": "pcm_s24le",
}

# Optional remap for very high-bitrate formats to something more compatible
PREFER_DOWNMAP = {
    # Comment out to keep original codec always
    "dts": "ac3",
    "truehd": "ac3",
}


def encoder_for_codec(codec_name: str) -> str:
    codec = (codec_name or "").lower()
    if codec in PREFER_DOWNMAP:
        codec = PREFER_DOWNMAP[codec]
    return CODEC_ENCODER_MAP.get(codec, "aac")  # fallback to AAC


# ---------- Mapping, size estimates & progress ----------
def get_main_audio_bitrate_bps(meta: dict, main_abs_index: int) -> int | None:
    for s in meta.get("streams", []):
        if s.get("index") == main_abs_index and s.get("codec_type") == "audio":
            br = s.get("bit_rate")
            try:
                return int(br) if br is not None else None
            except Exception:
                return None
    return None


def estimate_main_audio_size_bytes(
    duration_sec: float, bitrate_bps: int | None
) -> int | None:
    if not duration_sec or not bitrate_bps:
        return None
    return int(duration_sec * bitrate_bps / 8)


def parse_bitrate_to_bps(bstr: str) -> int | None:
    if not bstr:
        return None
    s = bstr.strip().lower()
    try:
        if s.endswith("k"):
            return int(float(s[:-1]) * 1000)
        if s.endswith("m"):
            return int(float(s[:-1]) * 1000 * 1000)
        return int(s)
    except Exception:
        return None


def estimate_output_size_bytes(
    src: Path,
    meta: dict,
    main_abs_index: int,
    target_audio_bitrate_str: str,
    duration_sec: float,
) -> int | None:
    try:
        input_size = src.stat().st_size
    except FileNotFoundError:
        return None
    target_bps = parse_bitrate_to_bps(target_audio_bitrate_str)
    orig_bps = get_main_audio_bitrate_bps(meta, main_abs_index)
    est_orig_audio = estimate_main_audio_size_bytes(duration_sec, orig_bps)
    est_new_audio = estimate_main_audio_size_bytes(duration_sec, target_bps)
    if est_orig_audio is None or est_new_audio is None:
        return input_size  # rough fallback
    return max(1, input_size - est_orig_audio + est_new_audio)


def build_map_and_codecs(
    meta: dict, main_abs_index: int, skip_subtitles: bool = False
) -> tuple[list[str], list[str], int]:
    if skip_subtitles:
        # Map video and audio streams only (skip subtitles to avoid codec issues)
        maps = ["-map", "0:v?", "-map", "0:a"]
    else:
        # Try to include everything
        maps = ["-map", "0"]

    codecs: list[str] = []

    # Copy video/data/subtitles
    codecs += ["-c:v", "copy", "-c:d", "copy"]
    if not skip_subtitles:
        codecs += ["-c:s", "copy"]

    abs_to_aorder = build_audio_absindex_to_order(meta)
    if main_abs_index not in abs_to_aorder:
        raise RuntimeError("Main audio stream not found in audio order mapping")
    main_audio_order = abs_to_aorder[main_abs_index]

    audio_streams = [
        s for s in meta.get("streams", []) if s.get("codec_type") == "audio"
    ]
    for s in audio_streams:
        a_ord = abs_to_aorder[s["index"]]
        if s["index"] == main_abs_index:
            codec_name = (s.get("codec_name") or "").lower()
            enc = encoder_for_codec(codec_name)
            codecs += [f"-c:a:{a_ord}", enc]
            # Bitrate for lossy encoders (ignored by PCM/FLAC/TrueHD etc.)
            if enc not in ("flac", "alac", "truehd", "pcm_s16le", "pcm_s24le"):
                codecs += [f"-b:a:{a_ord}", AUDIO_BITRATE]
        else:
            codecs += [f"-c:a:{a_ord}", "copy"]

    return maps, codecs, main_audio_order


# ---------- Swap robustness & cleanup ----------
def atomic_swap_with_retry(
    final_path: Path, tmp_path: Path, backup_path: Path, retries: int = 6
) -> bool:
    """
    Atomically replace final_path with tmp_path.
    Creates a backup, then replaces, then removes the backup.
    Retries on transient errors (e.g., AV/Indexer briefly locking the file).
    """
    backoff = 0.5  # seconds
    for attempt in range(1, retries + 1):
        try:
            # Safely remove any existing backup first
            try:
                if backup_path.exists():
                    backup_path.unlink()
            except (PermissionError, OSError):
                # If we can't delete the backup, use a unique name instead
                backup_path = backup_path.with_suffix(
                    backup_path.suffix + f".{int(time.time())}"
                )

            os.replace(final_path, backup_path)
            os.replace(tmp_path, final_path)

            # Clean up backup with error tolerance
            try:
                backup_path.unlink()
            except (PermissionError, OSError):
                log_print(
                    f"  Note: Backup file retained at {backup_path} (could not auto-delete)"
                )

            return True
        except (PermissionError, OSError) as e:
            if not tmp_path.exists():
                log_print(f"  ! Swap failed (tmp missing): {e}")
                return False
            if attempt < retries:
                time.sleep(backoff)
                backoff *= 1.5
            else:
                log_print(f"  ! Swap failed after {retries} attempts: {e}")
                return False

    return False  # Fallback if loop exits without return


def cleanup_orphan_tmps(max_age_hours: int = 0) -> None:
    """
    Remove stale *.normalised.tmp* files left from interrupted runs.
    If max_age_hours=0, removes all tmp files regardless of age.
    """
    for root_path in ROOT_DIRS:
        root = Path(root_path)
        if not root.exists():
            continue
        for tmp in root.rglob("*.normalised.tmp*"):
            try:
                if max_age_hours > 0:
                    st = tmp.stat()
                    cutoff = time.time() - (max_age_hours * 3600)
                    if st.st_mtime >= cutoff:
                        continue  # Skip files newer than cutoff

                log_print(f"~ Cleaning orphan tmp: {tmp}")
                tmp.unlink(missing_ok=True)
            except FileNotFoundError:
                pass
            except Exception as e:
                log_print(f"~ Could not clean tmp {tmp}: {e}")


# ---------- Core processing ----------
def should_skip_as_sample(p: Path) -> bool:
    if not SKIP_SAMPLES:
        return False
    name = p.name.lower()
    if any(tok in name for tok in SAMPLE_NAME_TOKENS):
        return True
    try:
        if p.stat().st_size < MIN_FILE_SIZE_BYTES:
            return True
    except FileNotFoundError:
        return True
    return False


def should_process(path: Path, state: dict) -> bool:
    if path.suffix.lower() not in VIDEO_EXTS:
        return False
    if should_skip_as_sample(path):
        return False

    sig_now = file_signature(path)
    rec = state["files"].get(str(path))

    if rec is None:
        return True

    # If fast signature matches, skip without hashing
    if rec.get("sig") == sig_now:
        return False

    # Fast sig changed (or timestamps preserved oddly) → compute quick fp once
    qfp_now = compute_quick_fingerprint(path)
    if rec.get("qfp") == qfp_now:
        with state_lock:
            rec["sig"] = sig_now
            rec["done_at"] = utc_now_z()
            state["files"][str(path)] = rec
            save_state(STATE_FILE, state)
        return False

    return True  # content differs → process


def estimate_output_size_and_duration(
    src: Path, meta: dict, main_abs_index: int
) -> tuple[float, int | None]:
    duration = float(meta.get("format", {}).get("duration", 0)) or 0.0
    est_out_size = estimate_output_size_bytes(
        src, meta, main_abs_index, AUDIO_BITRATE, duration
    )
    return duration, est_out_size


def apply_peak_gain(
    src: Path,
    dst: Path,
    meta: dict,
    main_abs_index: int,
    gain_db: float,
    file_prefix: str = "",
    skip_subtitles: bool = False,
) -> None:
    maps, codecs, main_audio_order = build_map_and_codecs(
        meta, main_abs_index, skip_subtitles
    )
    volume_filter = f"volume={gain_db:+.3f}dB"
    duration, est_out_size = estimate_output_size_and_duration(
        src, meta, main_abs_index
    )

    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-threads",
        str(FFMPEG_THREADS_PER_JOB),
        "-y",
        "-i",
        str(src),
    ]
    # Map all streams first
    cmd += maps
    # Apply volume filter ONLY to the output audio stream order that corresponds to main audio
    cmd += [f"-filter:a:{main_audio_order}", volume_filter]
    cmd += codecs

    # For .m4v files, force mp4 format to support HEVC (otherwise ffmpeg uses ipod format)
    if src.suffix.lower() == ".m4v":
        cmd += ["-f", "mp4"]

    if FASTSTART and src.suffix.lower() in {".mp4", ".m4v", ".mov"}:
        cmd += ["-movflags", "+faststart"]
    cmd += ["-progress", "pipe:1", "-nostats", str(dst)]

    # Set process priority
    creationflags = 0
    try:
        creationflags = subprocess.BELOW_NORMAL_PRIORITY_CLASS  # type: ignore[attr-defined]
    except Exception:
        pass

    # Use subprocess.run() with proper I/O handling
    try:
        result = subprocess.run(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=SUBPROCESS_TIMEOUT_SECONDS,
            creationflags=creationflags,
        )

        if result.returncode != 0:
            stderr_output = result.stderr or ""
            # Look for actual error messages (not just "Conversion failed!")
            error_lines = [
                line.strip() for line in stderr_output.split("\n") if line.strip()
            ]
            # Find lines with actual errors (usually contain "Error" or codec names)
            meaningful_errors = [
                line
                for line in error_lines
                if any(
                    keyword in line.lower()
                    for keyword in [
                        "error",
                        "invalid",
                        "cannot",
                        "failed",
                        "encoder",
                        "decoder",
                        "permission",
                        "not supported",
                        "could not",
                    ]
                )
                and "conversion failed" not in line.lower()
            ]
            if meaningful_errors:
                # Show last 3 meaningful errors for context
                last_error = " | ".join(meaningful_errors[-3:])
            else:
                # Fall back to last 5 lines of any output
                last_error = (
                    " | ".join(error_lines[-5:]) if error_lines else "Unknown error"
                )
            raise RuntimeError(f"ffmpeg failed: {last_error}")

    except subprocess.TimeoutExpired:
        raise RuntimeError(
            f"ffmpeg process timed out after {SUBPROCESS_TIMEOUT_SECONDS} seconds"
        )


def process_file(path: Path, state: dict) -> bool:
    filename = path.name
    prefix = f"[{filename[:40]}...]" if len(filename) > 40 else f"[{filename}]"

    log_print(f"\n{prefix} Processing: {path}")

    meta = ffprobe_streams(path)
    main_abs_idx = find_main_audio_abs_index(meta)
    if main_abs_idx is None:
        log_print(f"{prefix} ⊘ No audio stream found, skipping")
        return False

    max_peak_db = measure_max_peak_db(path, main_abs_idx)
    if max_peak_db is None:
        log_print(f"{prefix} ⊘ Could not measure max peak, skipping")
        return False

    gain_db = TARGET_PEAK_DBFS - max_peak_db

    # Already at/above target? Skip but record state so we don't re-check needlessly.
    if gain_db <= 0.05:
        log_print(f"{prefix} ✓ Already normalized (peak: {max_peak_db:.2f} dBFS)")
        with state_lock:
            state["files"][str(path)] = {
                "sig": file_signature(path),
                "qfp": compute_quick_fingerprint(path),
                "done_at": utc_now_z(),
            }
            save_state(STATE_FILE, state)
        return False

    log_print(
        f"{prefix} ⟳ Normalizing: peak {max_peak_db:.2f} dBFS → {TARGET_PEAK_DBFS} dBFS (gain: {gain_db:+.2f} dB)"
    )

    tmp_path = path.with_name(path.stem + ".normalised.tmp" + path.suffix)
    swap_success = False

    try:
        # Try with subtitles first
        try:
            apply_peak_gain(
                path,
                tmp_path,
                meta,
                main_abs_idx,
                gain_db,
                prefix,
                skip_subtitles=False,
            )
        except RuntimeError as e:
            error_msg = str(e).lower()
            # If it's a subtitle-related error or container header issue, retry without subtitles
            if any(
                keyword in error_msg
                for keyword in [
                    "subtitle",
                    "srt",
                    "binding an input stream",
                    "codec 0 is not supported",
                    "could not write header",
                    "function not implemented",
                    "incorrect codec parameters",
                ]
            ):
                log_print(
                    f"{prefix} ⚠ Stream compatibility issue, retrying without subtitles..."
                )
                # Clean up failed tmp file
                if tmp_path.exists():
                    tmp_path.unlink()
                apply_peak_gain(
                    path,
                    tmp_path,
                    meta,
                    main_abs_idx,
                    gain_db,
                    prefix,
                    skip_subtitles=True,
                )
            else:
                # Not a subtitle error, re-raise
                raise

        if (
            not tmp_path.exists()
            or tmp_path.stat().st_size < MIN_OUTPUT_FILE_SIZE_BYTES
        ):
            raise RuntimeError("Output file looks invalid or too small")

        backup = path.with_suffix(path.suffix + ".bak")

        ok = atomic_swap_with_retry(
            final_path=path, tmp_path=tmp_path, backup_path=backup
        )
        if not ok:
            log_print(f"{prefix} ✗ Swap failed; tmp file retained for retry")
            return False
        swap_success = True

        log_print(f"{prefix} ✓ Completed successfully")

        with state_lock:
            state["files"][str(path)] = {
                "sig": file_signature(path),
                "qfp": compute_quick_fingerprint(path),
                "done_at": utc_now_z(),
            }
            save_state(STATE_FILE, state)

        return True

    finally:
        # Clean tmp only if final file is in place; otherwise keep for manual retry/debug
        try:
            if tmp_path.exists() and swap_success:
                tmp_path.unlink()
        except Exception:
            pass


def scan_and_collect() -> list[Path]:
    candidates: list[Path] = []
    for root_path in ROOT_DIRS:
        root = Path(root_path)
        if not root.exists():
            log_print(f"Warning: root does not exist: {root}")
            continue
        for p in root.rglob("*"):
            if p.is_file() and p.suffix.lower() in VIDEO_EXTS:
                candidates.append(p)
    return candidates


def scan_and_process():
    cleanup_orphan_tmps()  # tidy up old tmp files first
    state = load_state(STATE_FILE)
    candidates = scan_and_collect()
    to_do = [p for p in candidates if should_process(p, state)]

    if not to_do:
        log_print("No new or changed files to process.")
        return

    log_print(f"Found {len(to_do)} file(s) to normalise.")

    def worker(p: Path) -> tuple[Path, bool, str | None]:
        try:
            ok = process_file(p, state)
            return (p, ok, None)
        except Exception as e:
            return (p, False, str(e))

    # Thread pool (I/O bound + external ffmpeg processes → threads are fine)
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futures = [ex.submit(worker, p) for p in to_do]
        for fut in as_completed(futures):
            p, ok, err = fut.result()
            if not ok and err:
                filename = p.name
                prefix = (
                    f"[{filename[:40]}...]" if len(filename) > 40 else f"[{filename}]"
                )
                log_print(f"{prefix} ✗ Failed: {err}")


if __name__ == "__main__":
    scan_and_process()
