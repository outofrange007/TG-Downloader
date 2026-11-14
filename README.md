# Telegram Downloader

A Flask-based web application for downloading media from Telegram groups, supergroups, channels, and forum topics. It uses a Telegram user account through Telethon and stores downloads in a group- and topic-based folder structure.

## Features

- Web dashboard for Telegram login and download control
- Login with a phone number and Telegram code, or QR code
- Optional Telegram 2FA password support
- Downloads photos, videos, documents, audio, and other supported media
- Supports Telegram forum topics
- Parallel downloads for documents of at least 8 MiB using FastTelethon
- Configurable number of parallel connections per file
- Single-connection fallback if FastTelethon is unavailable or a parallel download fails
- Persistent Telegram session and configuration
- Download history to avoid downloading the same message twice
- Compatibility with legacy `download_history.log` entries
- Checks for files already present on disk
- Unique filenames containing the Telegram message ID
- Temporary `.part` files and atomic finalization
- Download progress, current-file progress, and speed in the dashboard
- Graceful stop requests
- Telegram flood-wait handling
- Optional background archiving to a separate storage path
- Rotating application logs
- Docker Compose and Portainer deployment

## Requirements

- A Telegram account with access to the groups or channels to download
- Telegram API credentials (`api_id` and `api_hash`) from [my.telegram.org](https://my.telegram.org)
- Docker and Docker Compose, or Portainer with Docker access
- A persistent, writable data directory

A bot token is not required. The application uses a Telegram user session.

## Telegram API credentials and login

Create an application at [my.telegram.org](https://my.telegram.org) to obtain an API ID and API hash. Enter these in the web interface.

You can log in using either:

- **Phone login:** enter the phone number, request a Telegram code, then enter the code and—if requested—the 2FA password.
- **QR login:** enter the API ID and API hash, scan the displayed QR code in Telegram, and provide the 2FA password if requested.

The application stores its configuration and Telethon session under `DATA_PATH`. Keep this directory persistent so you normally do not need to log in again after a container restart.

## Docker Compose deployment

Example Compose configuration:

```yaml
services:
  telegram-downloader:
    image: ghcr.io/outofrange007/tg-downloader:latest
    container_name: telegram_ripper
    restart: unless-stopped
    ports:
      - "5000:5000"
    volumes:
      - /opt/tg_ripper_data:/app/data
      # Optional: separate destination for completed downloads
      - /mnt/usb_share/telegram_ripper:/app/archive
    environment:
      - TZ=Europe/Berlin
      - DATA_PATH=/app/data
      - WEB_PORT=5000
      # Optional: parallel connections per qualifying file
      - DL_CONNECTIONS=6
      # Optional: enables moving completed downloads to archive storage
      - ARCHIVE_PATH=/app/archive
      # Optional: maximum pending archive data in MiB; default is 8192
      - ARCHIVE_MAX_PENDING_MB=8192
```

Create the host directories before deployment, adjusting the paths to your setup:

```bash
mkdir -p /opt/tg_ripper_data
mkdir -p /mnt/usb_share/telegram_ripper
```

Start or update the stack:

```bash
docker compose up -d
```

Open the web interface at:

```text
http://localhost:5000
```

View application logs:

```bash
docker compose logs -f telegram-downloader
```

Stop the stack:

```bash
docker compose down
```

The archive volume and its environment variables are optional. If `ARCHIVE_PATH` is unset or empty, files remain in the data directory.

## Environment variables

| Variable | Default | Description |
|---|---:|---|
| `DATA_PATH` | `./data` | Directory for configuration, session, download history, downloads, and logs |
| `WEB_PORT` | `5000` | Web interface port inside the container |
| `DL_CONNECTIONS` | `6` | Number of connections used by FastTelethon for qualifying parallel downloads; minimum is 1 |
| `ARCHIVE_PATH` | unset | Optional destination for completed files; enables background archiving when set |
| `ARCHIVE_MAX_PENDING_MB` | `8192` | Pending archive data threshold in MiB; downloading waits while the pending amount is at or above this limit |

### Parallel downloads

FastTelethon is used for Telegram documents whose size is at least **8 MiB**. Smaller files and photos use Telethon's regular download method because parallel connection setup may not help for small files.

The `DL_CONNECTIONS` setting controls the number of connections per qualifying file. The default is `6`. If Telegram rate limits occur, reduce this value. If FastTelethon cannot be loaded or a parallel transfer fails, the application falls back to a single-connection download.

`FastTelethon.py` is included in the repository root and is copied into the image by the Dockerfile.

### Optional archive storage

When `ARCHIVE_PATH` is set, completed files are queued for background archiving. The worker copies each file to the archive destination, finalizes the copy, and then removes the local source file. The archive path should be mounted to persistent storage and writable by the container.

`ARCHIVE_MAX_PENDING_MB` limits how much completed data may be waiting for archival. When the pending amount reaches the configured threshold, the download worker waits for the backlog to decrease before continuing.

## Storage layout

The application stores runtime data below `DATA_PATH`. Downloads are written under its `downloads/` directory.

Example:

```text
/app/data/
├── config.json
├── scraping_session.session
├── download_history.jsonl
├── downloads/
│   └── Group Name/
│       └── Topic Name/
└── logs/
    └── telegram-ripper.log
```

For a group or channel without forum topics, files are stored directly in the group folder. For a forum, files are organized into topic folders.

Names are sanitized before being used as filesystem paths. Download filenames include the message timestamp and Telegram message ID, followed by the original filename when available.

Example:

```text
2026-11-14_16-26-29_205927_2020 - Fiona Apple - Fetch The Bolt Cutters.zip
```

If Telegram does not provide a filename, the application uses a generated name based on the message ID and media extension.

## Download history and duplicate detection

The current history is stored in:

```text
DATA_PATH/download_history.jsonl
```

Each successful or migrated entry records the message identifier, chat ID, message ID, filename, and timestamp. The application also checks the legacy `download_history.log` when the current JSONL history file does not exist, and checks the target folder for existing files.

Do not delete the persistent data directory if you want to retain the Telegram session and download history.

## Download behavior

For each selected chat or forum topic, the application:

1. Checks Telegram authorization.
2. Counts media for the progress display.
3. Iterates through messages and filters for downloadable media.
4. Skips messages already recorded in history or already present on disk.
5. Downloads new files to a temporary `.part` path.
6. Moves a completed file to its final name.
7. Records the message in the download history.
8. Queues the file for archival if archive storage is enabled.

The dashboard displays counts and progress for the current download. The Stop button requests a graceful stop; the worker stops processing additional messages after the current operation is handled where possible.

## Logging

Logs are written to:

```text
DATA_PATH/logs/telegram-ripper.log
```

The application also writes logs to the container console. The rotating log file has a maximum size of 5 MiB and keeps up to three backup files.

## Web API

| Endpoint | Method | Description |
|---|---|---|
| `/` | GET | Web dashboard |
| `/api/auth/status` | GET | Check Telegram authorization status |
| `/api/auth/send_code` | POST | Save phone-login credentials and request a Telegram code |
| `/api/auth/verify` | POST | Verify the Telegram code and optional 2FA password |
| `/api/auth/qr/start` | POST | Start QR login |
| `/api/auth/qr/status` | GET | Check QR-login status |
| `/api/auth/qr/2fa` | POST | Submit the 2FA password during QR login |
| `/api/groups` | GET | List available groups and channels |
| `/api/topics?chat_id=...` | GET | List forum topics for a chat |
| `/api/status` | GET | Return current download status |
| `/api/start` | POST | Start a download |
| `/api/stop` | POST | Request a graceful stop |

## Local development

The project requires Python 3.11 or a compatible Python version and the dependencies listed in `requirements.txt`.

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Set the relevant environment variables as needed:

```bash
export DATA_PATH=./data
export WEB_PORT=5000
export DL_CONNECTIONS=6
```

Run the application:

```bash
python trweb.py
```

Then open:

```text
http://localhost:5000
```

## Security

- Use the web interface only on a trusted network unless you protect remote access with suitable authentication and HTTPS.
- Treat `scraping_session.session` like a password.
- Do not publish or share `config.json`, session files, or downloaded media.
- Do not commit credentials, session files, logs, or personal downloads to a public repository.
- Ensure the host data and archive directories are persistent and writable by the container.

## Troubleshooting

### Telegram login is requested again

Check that the data directory is mounted at the expected `DATA_PATH`, that `scraping_session.session` is present and writable, and that the container was not recreated with an empty data directory.

### Groups or channels are missing

Confirm that the account is authorized and has access to the chats. Check the container logs for authentication or connection errors.

### Files are skipped

A message may be skipped because its identifier is already in `download_history.jsonl`, a legacy history entry matches, the target file already exists, or the message does not contain downloadable media.

### Parallel downloads are not enabled

Check the startup logs for:

```text
FastTelethon loaded: parallel downloads enabled
```

If FastTelethon cannot be imported, the application logs a warning and uses single-connection downloads. Confirm that `FastTelethon.py` is included in the image and that the running container uses the updated image.

### Archiving fails

Confirm that `ARCHIVE_PATH` points to the mounted archive directory and that the container can write to it. Check the logs for the specific file and filesystem error. If archival is disabled, leave `ARCHIVE_PATH` unset or empty.

### Telegram rate limits occur

Reduce `DL_CONNECTIONS` and redeploy the container. The application handles Telegram flood-wait responses by waiting for the requested period unless a stop is requested.

## License

This project is licensed under the MIT License. See the `LICENSE` file for the complete license text.
