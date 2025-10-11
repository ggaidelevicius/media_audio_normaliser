import time
import threading
from pathlib import Path
from datetime import datetime
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

# Import the processing function from your existing script
from normalise_audio import (
    process_file,
    load_state,
    should_skip_as_sample,
    STATE_FILE,
    ROOT_DIRS,
    VIDEO_EXTS,
    log_print,
)


class VideoFileHandler(FileSystemEventHandler):
    """Monitors for new/modified video files and triggers normalization."""

    def __init__(self):
        super().__init__()
        self.pending_files = {}  # {path: last_modified_time}
        self.lock = threading.Lock()
        self.state = load_state(STATE_FILE)

        # Start background thread to process pending files
        self.processing_thread = threading.Thread(target=self._process_pending_loop, daemon=True)
        self.processing_thread.start()

    def _is_video_file(self, path: Path) -> bool:
        """Check if file is a video we should process."""
        return path.suffix.lower() in VIDEO_EXTS and not should_skip_as_sample(path)

    def _add_to_pending(self, path: Path):
        """Add file to pending queue with current timestamp."""
        with self.lock:
            self.pending_files[str(path)] = time.time()
            log_print(f"[WATCHER] Detected new file: {path.name}")

    def on_created(self, event):
        """Handle file creation events."""
        if event.is_directory:
            return

        path = Path(str(event.src_path))
        if self._is_video_file(path):
            self._add_to_pending(path)

    def on_moved(self, event):
        """Handle file move events (e.g., files moved into watched directory)."""
        if event.is_directory:
            return

        path = Path(str(event.dest_path))
        if self._is_video_file(path):
            self._add_to_pending(path)

    def _is_file_ready(self, path: Path) -> bool:
        """Check if file has finished copying by monitoring size stability."""
        try:
            if not path.exists():
                return False

            # Check if file is exclusively locked (still being written)
            try:
                with path.open('rb') as _:
                    pass  # Just check we can open it
            except PermissionError:
                return False  # File is locked by another process

            # Check size stability - file size should be stable for at least 3 checks
            size1 = path.stat().st_size
            if size1 == 0:
                return False

            time.sleep(1)
            size2 = path.stat().st_size

            if size1 != size2:
                return False  # Still growing

            time.sleep(1)
            size3 = path.stat().st_size

            return size2 == size3  # Stable for 2 seconds

        except (FileNotFoundError, OSError):
            return False

    def _process_pending_loop(self):
        """Background thread that processes files after they've finished writing."""
        MIN_WAIT_TIME = 5  # Minimum seconds to wait before checking if ready

        while True:
            time.sleep(3)  # Check every 3 seconds

            ready_to_process = []
            current_time = time.time()

            with self.lock:
                for path_str, added_time in list(self.pending_files.items()):
                    # Wait minimum time first
                    if current_time - added_time < MIN_WAIT_TIME:
                        continue

                    path = Path(path_str)

                    # Check if file is ready (releases lock during check)
                    self.lock.release()
                    try:
                        is_ready = self._is_file_ready(path)
                    finally:
                        self.lock.acquire()

                    if is_ready:
                        ready_to_process.append(path)
                        del self.pending_files[path_str]

            # Process files outside the lock
            for path in ready_to_process:
                self._process_file_safe(path)

    def _process_file_safe(self, path: Path):
        """Process a single file with error handling."""
        try:
            log_print(f"\n[WATCHER] Starting normalization: {path.name}")

            # Reload state to get latest (in case main script ran)
            self.state = load_state(STATE_FILE)

            success = process_file(path, self.state)

            if success:
                log_print(f"[WATCHER]  Successfully normalized: {path.name}")
            else:
                log_print(f"[WATCHER] 9 Skipped (already normalized or no audio): {path.name}")

        except Exception as e:
            log_print(f"[WATCHER]  Error processing {path.name}: {e}")


def main():
    log_print("\n" + "="*70)
    log_print("VIDEO FILE WATCHER - Audio Normalization Monitor")
    log_print("="*70)
    log_print(f"Started at: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    log_print("Monitoring directories:")

    for root_dir in ROOT_DIRS:
        root_path = Path(root_dir)
        if root_path.exists():
            log_print(f"   {root_dir}")
        else:
            log_print(f"   {root_dir} (NOT FOUND)")

    log_print(f"\nWatching for file types: {', '.join(VIDEO_EXTS)}")
    log_print("Press Ctrl+C to stop\n")
    log_print("="*70 + "\n")

    event_handler = VideoFileHandler()
    observer = Observer()

    # Set up observers for each root directory
    for root_dir in ROOT_DIRS:
        root_path = Path(root_dir)
        if root_path.exists():
            observer.schedule(event_handler, str(root_path), recursive=True)
            log_print(f"[WATCHER] Monitoring started: {root_path}")

    observer.start()

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        log_print("\n[WATCHER] Shutting down...")
        observer.stop()

    observer.join()
    log_print("[WATCHER] Stopped")


if __name__ == "__main__":
    main()
