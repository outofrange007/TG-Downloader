# Telegram Downloader

A Flask-based web application that downloads media files from Telegram groups, supergroups, channels, and forum topics.

The application uses the Telegram user API through Telethon. Downloaded media is stored in an automatically created folder structure based on the Telegram group and, where applicable, the forum topic.

## Features

- Web dashboard for Telegram authentication and download control
- Download media from Telegram groups, supergroups, and channels
- Support for Telegram forum topics
- Automatic folder structure based on group and topic names
- Persistent Telethon user session
- Persistent Telegram configuration
- Duplicate protection using `chat_id:message_id` identifiers
- Compatibility with the legacy `download_history.log`
- Existing-file detection on disk
- Unique filenames containing the timestamp and Telegram message ID
- Temporary `.part` files during downloads
- Atomic file replacement after successful downloads
- Graceful download stopping
- Protection against multiple simultaneous downloads
- Live total-file count and overall progress
- Live current-file progress and download speed
- Downloaded, skipped, processed, and failed counters
- Telegram flood-wait handling
- Rotating log files
- Docker, Docker Compose, and Portainer support

## Requirements

- Telegram `api_id` and `api_hash` from [my.telegram.org](https://my.telegram.org)
- A Telegram user account
- Access to the Telegram groups or channels that should be downloaded
- Docker and Docker Compose, or Portainer with Docker access
- A persistent writable data directory

A bot token is not required. The application uses a Telegram user session through Telethon.

## Telegram API Credentials

Create an application at [my.telegram.org](https://my.telegram.org). The following values are required:

- `api_id`
- `api_hash`
- Phone number of the Telegram account

The credentials can be entered through the web interface and are stored in `config.json`.

## Authentication

1. Open the web interface.
2. Enter the Telegram API ID, API hash, and phone number.
3. Request a login code.
4. Enter the code received from Telegram.
5. Enter the Telegram 2FA password if requested.
6. Select a Telegram group or channel.

The Telethon session is stored persistently so authentication does not normally need to be repeated after a container restart.

## Folder Structure

Downloaded files are stored below the configured data directory.

For a normal group or channel:

    data/
    └── downloads/
        └── Group Name/
            ├── 2026-09-16_10-52-03_244_photo.jpg
            ├── 2026-09-16_10-55-12_245_document.pdf
            └── 2026-09-16_11-04-36_246_video.mp4

For a Telegram forum with topics:

    data/
    └── downloads/
        └── Group Name/
            ├── General/
            │   └── 2026-09-16_10-52-03_244_photo.jpg
            ├── Topic One/
            │   └── 2026-09-16_10-55-12_245_document.pdf
            └── Topic Two/
                └── 2026-09-16_11-04-36_246_video.mp4

Group names, topic names, and filenames are sanitized before they are used as filesystem paths.

## Filename Format

The current filename format is:

    YYYY-MM-DD_HH-MM-SS_MESSAGE_ID_ORIGINAL_FILENAME

Example:

    2026-09-16_10-52-03_244_photo.jpg

If Telegram does not provide an original filename, a fallback name is generated:

    2026-09-16_10-52-03_244_media.jpg

The Telegram message ID prevents overwrites caused by identical timestamps or original filenames.

## Downloadable Media

The application downloads Telegram messages that provide a real Telethon file object. This includes photos, videos, documents, music, voice messages, GIFs, archives, and other downloadable Telegram files.

Messages without a real downloadable file are ignored. This prevents errors caused by webpage previews, link previews, polls, locations, and unavailable media references.

## Duplicate Protection

The application uses three protection levels:

1. The current JSONL history file is checked using `chat_id:message_id`.
2. The legacy `download_history.log` is checked for files from older versions.
3. Existing target files on disk are checked before downloading.

The current history is stored in:

    data/download_history.jsonl

Each successful or migrated entry contains the Telegram chat ID, message ID, filename, and timestamp.

## Data Directory

The application stores runtime data below `DATA_PATH`.

| File or directory | Purpose |
|---|---|
| `config.json` | Telegram API configuration and authentication data |
| `scraping_session.session` | Persistent Telethon user session |
| `download_history.jsonl` | Current download history |
| `download_history.log` | Legacy download history |
| `downloads/` | Downloaded Telegram media |
| `logs/` | Application log files |
| `logs/telegram-ripper.log` | Main rotating log file |

Example:

    data/
    ├── config.json
    ├── scraping_session.session
    ├── download_history.jsonl
    ├── downloads/
    └── logs/
        └── telegram-ripper.log

Do not commit credentials, session files, personal media, or logs to a public repository.

## Environment Variables

| Variable | Default | Description |
|---|---:|---|
| `DATA_PATH` | `./data` | Application data directory |
| `WEB_PORT` | `5000` | Internal Flask web server port |

Recommended container values:

    DATA_PATH=/app/data
    WEB_PORT=5000

## Docker Compose Deployment

The prebuilt image can be started with the following Compose configuration:

    version: "3.8"

    services:
      telegram-ripper:
        image: ghcr.io/outofrange007/tg-downloader:latest
        container_name: tg_downloader
        restart: unless-stopped
        ports:
          - "5000:5000"
        volumes:
          - ./data:/app/data
        environment:
          - TZ=Europe/Berlin
          - DATA_PATH=/app/data
          - WEB_PORT=5000

Start the application:

    docker compose up -d

The web interface is available at:

    http://localhost:5000

View the logs:

    docker compose logs -f telegram-ripper

Stop the application:

    docker compose down

## Portainer and TrueNAS Deployment

Example Portainer Stack:

    version: "3.8"

    services:
      telegram-ripper:
        image: ghcr.io/outofrange007/tg-downloader:latest
        container_name: tg_downloader
        restart: unless-stopped
        ports:
          - "5000:5000"
        volumes:
          - /opt/telegram_ripper/data:/app/data
        environment:
          - TZ=Europe/Berlin
          - DATA_PATH=/app/data
          - WEB_PORT=5000

The host directory must be persistent and writable by the container. Create it before deployment if necessary:

    mkdir -p /opt/telegram_ripper/data

Do not delete `scraping_session.session` unless a new Telegram login is intended.

If permissions prevent the container from writing to the directory, adjust ownership and permissions according to the user IDs used by the container.

## Web API

| Endpoint | Method | Description |
|---|---|---|
| `/` | GET | Web dashboard |
| `/api/auth/status` | GET | Check Telegram authentication status |
| `/api/auth/send_code` | POST | Save credentials and request a Telegram code |
| `/api/auth/verify` | POST | Verify the Telegram code and optional 2FA password |
| `/api/groups` | GET | Load available Telegram groups and channels |
| `/api/status` | GET | Return the current download status |
| `/api/start` | POST | Start downloading from a selected chat |
| `/api/stop` | POST | Request a graceful download stop |

## Download Process

The worker:

1. Connects to Telegram using the persistent session.
2. Verifies the authentication state.
3. Loads the selected chat.
4. Counts media files using Telegram media filters without a second full chat scan.
5. Iterates through the chat messages once for downloading.
6. Ignores messages without a real downloadable file.
7. Creates a unique sanitized filename.
8. Determines the group and forum-topic destination folder.
9. Checks history and existing files.
10. Downloads new files to a `.part` file.
11. Verifies the result returned by Telethon.
12. Moves the completed file to its final path.
13. Writes the message identifier to `download_history.jsonl`.
14. Updates the dashboard counters and progress values.

## Temporary Files and Stop Handling

Files are first downloaded with a `.part` suffix:

    2026-09-16_10-52-03_244_photo.jpg.part

After a successful download, the file is moved to its final name. Incomplete temporary files are removed where possible after a failed or interrupted operation.

The Stop button sets a stop event. The worker finishes or interrupts the current operation where possible, removes incomplete temporary files, and stops processing additional messages.

## Progress Display

The dashboard can display:

- Total number of files
- Processed files
- Downloaded files
- Skipped files
- Failed files
- Overall progress
- Current-file progress
- Current filename
- Current file size
- Current download speed
- Current status

The speed is measured using Telethon's download progress callback and displayed in MB/s.

## Telegram Flood-Wait Handling

When Telegram requests a flood-wait, the application logs the required waiting period, shows the waiting status in the web interface, waits for the requested time, and continues unless a stop was requested.

## Logging

Logs are written to:

    data/logs/telegram-ripper.log

The log uses rotating logging with a maximum size of 5 MB and up to three backup files.

## Technology Stack

- Python
- Flask
- Telethon
- `cryptg`
- Docker
- Docker Compose
- Portainer
- Telegram MTProto API

## Python Dependencies

The dependencies are defined in `requirements.txt`:

    Flask==3.0.0
    Telethon==1.33.1
    cryptg==0.4.0

Install them manually with:

    pip install -r requirements.txt

## Local Development

Create a virtual environment:

    python3 -m venv .venv

Activate it on Linux:

    source .venv/bin/activate

Install the dependencies:

    pip install -r requirements.txt

Set the environment variables:

    export DATA_PATH=./data
    export WEB_PORT=5000

Start the application:

    python trweb.py

Open the web interface at:

    http://localhost:5000

## Security

- Use the web interface only in a trusted local network unless authentication and HTTPS are added.
- Never commit Telegram API credentials.
- Treat `scraping_session.session` like a password.
- Do not share `config.json` or session files.
- Protect the persistent data directory.
- Review host filesystem permissions.
- Use a reverse proxy with authentication for remote access.

## Troubleshooting

### Authentication status returns an error

Check the API ID, API hash, phone number, outbound container connectivity, data-directory permissions, and persistence of the session file.

### Telegram groups are not displayed

Confirm that the account is authenticated, the session file exists, the account has access to the groups, and the application log contains no authentication errors.

### Files are skipped

A file may be skipped because its `chat_id:message_id` already exists in `download_history.jsonl`, a matching legacy entry exists, the file already exists on disk, or the message has no real downloadable file.

### A file fails without an output file

Telegram may return no file for webpage previews, link previews, unavailable media, or unsupported media references. These messages are skipped where the application can identify them before downloading.

### Authentication is requested again

Check that the data volume is mounted correctly, `scraping_session.session` is writable, the container uses the expected `/app/data` path, and the container was not recreated with an empty data directory.

## License

This project is licensed under the MIT License.

See the [LICENSE](LICENSE) file for the complete license text.
