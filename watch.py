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

    def _process_pending_loop(self):
        """Background thread that processes files after they've finished writing."""
        SETTLE_TIME = 5  # seconds to wait for file to finish writing

        while True:
            time.sleep(2)  # Check every 2 seconds

            ready_to_process = []
            current_time = time.time()

            with self.lock:
                for path_str, added_time in list(self.pending_files.items()):
                    # Wait for file to "settle" (no writes for SETTLE_TIME seconds)
                    if current_time - added_time >= SETTLE_TIME:
                        path = Path(path_str)

                        # Verify file still exists and has content
                        try:
                            if path.exists() and path.stat().st_size > 0:
                                ready_to_process.append(path)
                            del self.pending_files[path_str]
                        except (FileNotFoundError, OSError):
                            # File was deleted or isn't accessible yet
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
