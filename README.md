# Media Audio Normaliser

An automated audio normalization tool for video files that monitors and processes movies and TV shows to maintain consistent audio levels across your media library.

## Overview

This project consists of two main components:
- **normalise_audio.py**: Batch processor that scans and normalises existing media files
- **watch.py**: File watcher that automatically normalises new media files as they arrive

## Features

- **Peak normalization** to a configurable target level (default: -0.1 dBFS)
- **Codec preservation** where possible (AC3, EAC3, AAC, etc.)
- **Smart bitrate handling** based on source quality and channel count
- **Parallel processing** with configurable worker threads
- **State tracking** to avoid reprocessing unchanged files
- **Fast fingerprinting** for efficient duplicate detection
- **Automatic retry logic** with fallback for subtitle/codec issues
- **Real-time monitoring** for new files added to watched directories
- **Comprehensive logging** to file and console
- **Auto-update capability** that checks for and pulls updates from the repository

## Requirements

- Python 3.10+
- FFmpeg (with ffprobe)
- Python packages:
  - `watchdog` (for file monitoring in watch.py)

## Installation

1. **Install FFmpeg**:
   - Windows: Download from [ffmpeg.org](https://ffmpeg.org/download.html) and add to PATH
   - macOS: `brew install ffmpeg`
   - Linux: `sudo apt install ffmpeg` or equivalent

2. **Clone or download this repository**:
   ```bash
   git clone https://github.com/ggaidelevicius/media_audio_normaliser.git
   cd media_audio_normaliser
   ```

3. **Set up Python virtual environment**:
   ```bash
   # Create virtual environment
   python -m venv venv

   # Activate virtual environment
   # On Windows:
   venv\Scripts\activate
   # On macOS/Linux:
   source venv/bin/activate
   ```

4. **Install Python dependencies**:
   ```bash
   pip install -r requirements.txt
   ```

5. **Configure the script**:
   Edit the configuration section in [normalise_audio.py](normalise_audio.py) (lines 36-77) to set:
   - `MEDIA_BASE`: Your media directory path
   - `ROOT_DIRS`: Directories to scan (MOVIES, TV SHOWS, etc.)
   - `TARGET_PEAK_DBFS`: Target peak level (default: -0.1)
   - `WORKERS`: Number of concurrent processing threads
   - `FFMPEG_THREADS_PER_JOB`: FFmpeg threads per file

## Usage

### Initial Setup - IMPORTANT

**The first time you run this tool, you MUST use the batch processor:**

```bash
python normalise_audio.py
```

This will:
- Scan all existing media files in your configured directories
- Normalise any files that need processing
- Create a state file (`.normalise_state.json`) to track processed files
- Generate a log file (`log.txt`) with processing details

### Continuous Monitoring

After the initial batch processing is complete, you can start the file watcher to automatically process new files:

```bash
python watch.py
```

The watcher will:
- Monitor configured directories for new video files
- Wait for files to finish copying/downloading
- Automatically normalise new files as they arrive
- Use the same state tracking as the batch processor

### Running as a Background Service

#### Windows (Scheduled Task)

1. Open Task Scheduler
2. Create a new task:
   - **Trigger**: At login
   - **Action**: Start a program
   - **Program/script**: `C:\path\to\media_audio_normaliser\venv\Scripts\pythonw.exe`
   - **Arguments**: `"C:\path\to\media_audio_normaliser\watch.py"`
   - **Start in**: `C:\path\to\media_audio_normaliser`


#### macOS (launchd)

Create a file at `~/Library/LaunchAgents/com.user.media-audio-normaliser.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.user.media-audio-normaliser</string>
    <key>ProgramArguments</key>
    <array>
        <string>/path/to/media_audio_normaliser/venv/bin/python</string>
        <string>/path/to/media_audio_normaliser/watch.py</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>/tmp/media-normaliser.log</string>
    <key>StandardErrorPath</key>
    <string>/tmp/media-normaliser-error.log</string>
</dict>
</plist>
```

Then load it:
```bash
launchctl load ~/Library/LaunchAgents/com.user.media-audio-normaliser.plist
```

#### Linux (systemd)

Create a file at `~/.config/systemd/user/media-audio-normaliser.service`:

```ini
[Unit]
Description=Media Audio Normaliser Watcher
After=network.target

[Service]
Type=simple
ExecStart=/path/to/media_audio_normaliser/venv/bin/python /path/to/media_audio_normaliser/watch.py
Restart=always
RestartSec=10

[Install]
WantedBy=default.target
```

Enable and start the service:
```bash
systemctl --user enable media-audio-normaliser.service
systemctl --user start media-audio-normaliser.service
```

Check status:
```bash
systemctl --user status media-audio-normaliser.service
```

View logs:
```bash
journalctl --user -u media-audio-normaliser.service -f
```

## Configuration

Key settings in [normalise_audio.py](normalise_audio.py):

| Setting | Default | Description |
|---------|---------|-------------|
| `TARGET_PEAK_DBFS` | -0.1 | Target peak level (0.0 = full scale) |
| `DEFAULT_AUDIO_BITRATE` | 192k | Minimum bitrate for lossy re-encoding |
| `WORKERS` | 3 | Concurrent file processing threads |
| `FFMPEG_THREADS_PER_JOB` | 4 | FFmpeg threads per file |
| `SKIP_SAMPLES` | True | Skip small/sample files |
| `MIN_FILE_SIZE_BYTES` | 50 MB | Minimum file size to process |
| `FASTSTART` | True | Enable MP4 faststart (streaming optimization) |
| `AUTO_UPDATE_ENABLED` | True | Enable automatic update checks |
| `AUTO_UPDATE_CHECK_INTERVAL_HOURS` | 24 | How often to check for updates |

## Auto-Update Feature

The scripts include built-in auto-update functionality that keeps your installation current:

- **Automatic checks**: Once every 24 hours (configurable), the script checks for updates
- **Safe updates**: Only pulls if there are no local changes to avoid conflicts
- **Automatic restart**: After a successful update, the script restarts automatically
- **Rate limiting**: Uses a timestamp file to avoid excessive remote checks

### How it works:
1. On startup, checks if 24 hours have passed since last check
2. Fetches latest commits from the remote repository
3. Compares local commit with remote commit
4. If update available and no local changes, pulls the update
5. Automatically restarts the script with the new version

### Disabling auto-update:
Set `AUTO_UPDATE_ENABLED = False` in [normalise_audio.py](normalise_audio.py) line 80.

## How It Works

### Processing Pipeline

1. **File Discovery**: Scans configured directories for video files (.mkv, .mp4, .mov, .m4v)
2. **State Check**: Compares file signature against state database
3. **Audio Detection**: Identifies main audio stream using ffprobe
4. **Peak Measurement**: Measures current peak level using ffmpeg volumedetect
5. **Normalization**: Applies volume filter to reach target peak level
6. **Codec Handling**: Preserves original codec where possible, re-encodes if necessary
7. **Atomic Swap**: Safely replaces original file with normalised version
8. **State Update**: Records processing in state file

### File Watcher Behavior

The watcher ([watch.py](watch.py)) uses the following logic:
- Detects new files via filesystem events (create/move)
- Waits 5 seconds minimum before checking file readiness
- Verifies file is complete by checking size stability
- Processes files using the same normalization logic
- Runs in parallel with configurable worker threads

## State File

The state file (`.normalise_state.json`) tracks:
- **sig**: File signature (size + modification time)
- **qfp**: Quick fingerprint (hash of file chunks)
- **done_at**: Processing timestamp

This ensures files are only reprocessed when actually modified.

## Logging

All operations are logged to both console and `log.txt` with timestamps:
- Processing status for each file
- Peak levels detected and applied gain
- Error messages with context
- File watcher events

## Troubleshooting

### Files not processing
- Check that FFmpeg is in your PATH: `ffmpeg -version`
- Verify directory paths are correct in configuration
- Check `log.txt` for error messages

### Watcher not detecting files
- Ensure the initial batch processing completed successfully
- Verify watched directories exist and are accessible
- Check file permissions

### High CPU usage
- Reduce `WORKERS` count
- Reduce `FFMPEG_THREADS_PER_JOB`
- Adjust thread count based on your CPU (total threads = WORKERS × FFMPEG_THREADS_PER_JOB)

### Subtitle errors
- The script automatically retries without subtitles if codec issues occur
- This is normal for certain container/codec combinations

## License

This project is provided as-is for personal use.

## Contributing

Feel free to submit issues or pull requests for improvements.
