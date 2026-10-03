#!/usr/bin/env python3
# copyright outofrange007 (out-of-range@gmx.de)
# MIT Licence - see LICENCE for details.

import asyncio
import json
import logging
import os
import re
import threading
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

from flask import Flask, jsonify, render_template, request
from telethon import types
from telethon.sync import TelegramClient
from telethon.errors import (
    FloodWaitError,
    PhoneCodeExpiredError,
    PhoneCodeInvalidError,
    SessionPasswordNeededError,
)


class TelegramRipperWeb:
    def __init__(self, data_path=None, port=None):
        print("Initializing Telegram Ripper Web App...")

        self.web_port = int(port or os.environ.get("WEB_PORT", 5000))
        self.data_dir = Path(
            data_path or os.environ.get("DATA_PATH", "./data")
        )

        self.savepath = self.data_dir / "downloads"
        self.config_file = self.data_dir / "config.json"
        self.history_file = self.data_dir / "download_history.jsonl"
        self.session_name = str(self.data_dir / "scraping_session")

        self.api_id = None
        self.api_hash = None
        self.phone = None
        self.phone_code_hash = None

        self.setup_directories()
        self.setup_logging()

        self.load_config()
        self.download_history, self.legacy_history = self.load_history()

        self.app = Flask(__name__)
        self.lock = threading.Lock()
        self.stop_event = threading.Event()
        self.worker_thread = None

        self.is_downloading = False
        self.current_status = "Idle"
        self.current_file = "None"

        self.download_counter = 0
        self.skipped_counter = 0
        self.failed_counter = 0
        self.processed_counter = 0

        # Total number of downloadable media files, determined by a
        # lightweight Telegram filter count (not a full second scan).
        self.total_files = None

        self.overall_progress = 0.0
        self.current_file_progress = 0.0
        self.file_size_mb = 0.0
        self.download_speed_mbps = 0.0
        self.start_time = time.time()

        self.setup_web_routes()

    # ------------------------------------------------------------------
    # Basic setup
    # ------------------------------------------------------------------

    def setup_directories(self):
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.savepath.mkdir(parents=True, exist_ok=True)

    def setup_logging(self):
        self.logger = logging.getLogger("TelegramRipper")
        self.logger.setLevel(logging.INFO)
        self.logger.propagate = False

        if self.logger.handlers:
            return

        formatter = logging.Formatter(
            "%(asctime)s | %(levelname)s | %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )

        console_handler = logging.StreamHandler()
        console_handler.setFormatter(formatter)

        log_dir = self.data_dir / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)

        file_handler = RotatingFileHandler(
            log_dir / "telegram-ripper.log",
            maxBytes=5 * 1024 * 1024,
            backupCount=3,
            encoding="utf-8",
        )
        file_handler.setFormatter(formatter)

        self.logger.addHandler(console_handler)
        self.logger.addHandler(file_handler)

    def load_config(self):
        if not self.config_file.exists():
            return

        try:
            with self.config_file.open("r", encoding="utf-8") as file:
                config = json.load(file)

            self.api_id = config.get("api_id")
            self.api_hash = config.get("api_hash")
            self.phone = config.get("phone")
            self.phone_code_hash = config.get("phone_code_hash")

        except json.JSONDecodeError as exc:
            self.logger.error("Invalid config.json: %s", exc)
        except OSError as exc:
            self.logger.error("Could not read config.json: %s", exc)

    def save_config(self):
        config = {
            "api_id": self.api_id,
            "api_hash": self.api_hash,
            "phone": self.phone,
            "phone_code_hash": self.phone_code_hash,
        }

        temporary_file = self.config_file.with_suffix(".tmp")

        try:
            with temporary_file.open("w", encoding="utf-8") as file:
                json.dump(config, file, indent=2)

            os.replace(temporary_file, self.config_file)

        except OSError as exc:
            self.logger.error("Could not save configuration: %s", exc)
            if temporary_file.exists():
                temporary_file.unlink()

    # ------------------------------------------------------------------
    # Download history
    # ------------------------------------------------------------------

    def load_history(self):
        """
        Loads the download history.

        Returns two sets:
        - download_history: new identifiers ("chat_id:message_id")
          from download_history.jsonl
        - legacy_history: plain filenames from the old
          download_history.log of the previous app version

        Legacy entries are matched best-effort during the download
        loop so that files from the old version are skipped.
        """
        download_history = set()
        legacy_history = set()

        # New JSONL history
        if self.history_file.exists():
            try:
                with self.history_file.open("r", encoding="utf-8") as file:
                    for line_number, line in enumerate(file, start=1):
                        line = line.strip()

                        if not line:
                            continue

                        try:
                            entry = json.loads(line)
                        except json.JSONDecodeError:
                            self.logger.warning(
                                "Invalid history entry in line %d",
                                line_number,
                            )
                            continue

                        identifier = entry.get("identifier")
                        if identifier:
                            download_history.add(identifier)

            except OSError as exc:
                self.logger.error("Could not read history: %s", exc)

            self.logger.info(
                "Download history loaded: %d entries",
                len(download_history),
            )
            return download_history, legacy_history

        # Legacy log file from the old app version
        legacy_file = self.data_dir / "download_history.log"

        if legacy_file.exists():
            try:
                with legacy_file.open("r", encoding="utf-8") as file:
                    for line in file:
                        line = line.strip()

                        if "] " in line:
                            legacy_name = line.split("] ", 1)[1].strip()
                            if legacy_name:
                                legacy_history.add(legacy_name)

                self.logger.info(
                    "Legacy history loaded: %d entries",
                    len(legacy_history),
                )

            except OSError as exc:
                self.logger.error("Could not read legacy history: %s", exc)

        return download_history, legacy_history

    def make_history_identifier(self, chat_id, message_id):
        return f"{chat_id}:{message_id}"

    def mark_as_downloaded(self, identifier, filename, chat_id, message_id):
        if identifier in self.download_history:
            return

        entry = {
            "identifier": identifier,
            "chat_id": chat_id,
            "message_id": message_id,
            "filename": filename,
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        }

        try:
            with self.history_file.open("a", encoding="utf-8") as file:
                file.write(json.dumps(entry, ensure_ascii=False) + "\n")
                file.flush()
                os.fsync(file.fileno())

            self.download_history.add(identifier)

        except OSError as exc:
            self.logger.error(
                "Could not write to download history: %s",
                exc,
            )

    # ------------------------------------------------------------------
    # Filenames and Telegram media
    # ------------------------------------------------------------------

    @staticmethod
    def sanitize_path_name(name):
        if not name:
            return "Unknown"

        sanitized = re.sub(r'[\\/*?:"<>|]', "_", str(name))
        sanitized = sanitized.replace("\x00", "_")
        sanitized = sanitized.strip(" .")

        return sanitized[:180] or "Unknown"

    def get_message_filename(self, message):
        """
        Unique filename: timestamp + Telegram message ID + original name.

        The message ID prevents overwrites caused by identical
        timestamps or identical original filenames.
        """
        message_date = message.date or time.localtime()
        timestamp = message_date.strftime("%Y-%m-%d_%H-%M-%S")

        telegram_file = getattr(message, "file", None)

        original_name = None
        extension = ".bin"

        if telegram_file is not None:
            original_name = getattr(telegram_file, "name", None)
            extension = getattr(telegram_file, "ext", None) or extension

        if original_name:
            original_name = self.sanitize_path_name(original_name)
            return f"{timestamp}_{message.id}_{original_name}"

        return f"{timestamp}_{message.id}_media{extension}"

    def get_legacy_filename(self, message):
        """
        Rebuilds the filename scheme of the OLD app version
        (timestamp + original name, without the message ID).

        Used only to detect files that were already downloaded
        by the previous version.
        """
        message_date = message.date or time.localtime()
        timestamp = message_date.strftime("%Y-%m-%d_%H-%M-%S")

        telegram_file = getattr(message, "file", None)

        original_name = (
            getattr(telegram_file, "name", None) if telegram_file else None
        )
        extension = (
            (getattr(telegram_file, "ext", None) or ".bin")
            if telegram_file
            else ".bin"
        )

        if original_name:
            return f"{timestamp}_{original_name}"

        return f"{timestamp}_media{extension}"

    @staticmethod
    def is_downloadable_media(message):
        """
        Only media with a real file object is downloadable.

        This covers photos and all document types (including
        additional Telegram media documents) while excluding
        message types that have no file at all, such as webpage
        previews, polls or locations. Downloading those would
        return nothing and only produce errors.
        """
        if not getattr(message, "media", None):
            return False

        return getattr(message, "file", None) is not None

    def count_total_media(self, client, chat_id):
        """
        Counts downloadable media files using Telegram's media search
        filters. Each filter reports its total count already with the
        first response, so one lightweight request (limit=1) per media
        type is enough - no full second scan of the chat is needed.

        Returns the summed file count or None if counting failed.
        """
        filter_names = (
            "InputMessagesFilterPhotos",
            "InputMessagesFilterVideo",
            "InputMessagesFilterDocument",
            "InputMessagesFilterMusic",
            "InputMessagesFilterVoice",
            "InputMessagesFilterGif",
        )

        media_filters = [
            getattr(types, name)
            for name in filter_names
            if hasattr(types, name)
        ]

        total = 0

        for media_filter in media_filters:
            if self.stop_event.is_set():
                return None

            try:
                iterator = client.iter_messages(
                    chat_id,
                    limit=1,
                    filter=media_filter,
                )

                # A single fetch triggers the API response that
                # also contains the total count for this filter.
                next(iterator, None)

                filter_total = getattr(iterator, "total", None) or 0
                total += filter_total

            except Exception as exc:
                self.logger.warning(
                    "Could not count media with filter %s: %s",
                    media_filter,
                    exc,
                )
                return None

        self.logger.info(
            "Media pre-count finished: %d downloadable files",
            total,
        )

        return total

    def get_topic_name(self, client, chat_id, topic_id, cache):
        if topic_id in cache:
            return cache[topic_id]

        topic_name = f"Topic_{topic_id}"

        try:
            topic_message = client.get_messages(chat_id, ids=topic_id)

            action = getattr(topic_message, "action", None)
            title = getattr(action, "title", None)

            if title:
                topic_name = self.sanitize_path_name(title)

        except Exception as exc:
            self.logger.warning(
                "Could not load topic %s: %s",
                topic_id,
                exc,
            )

        cache[topic_id] = topic_name
        return topic_name

    def get_target_folder(self, client, chat_id, group_entity, message, cache):
        group_name = self.sanitize_path_name(
            getattr(group_entity, "title", None)
        )

        base_folder = self.savepath / group_name

        if not getattr(group_entity, "forum", False):
            base_folder.mkdir(parents=True, exist_ok=True)
            return base_folder

        topic_id = None
        reply_to = getattr(message, "reply_to", None)

        if reply_to:
            topic_id = (
                getattr(reply_to, "reply_to_top_id", None)
                or getattr(reply_to, "reply_to_msg_id", None)
            )

        if topic_id:
            topic_name = self.get_topic_name(
                client,
                chat_id,
                topic_id,
                cache,
            )
        else:
            topic_name = "General"

        target_folder = base_folder / self.sanitize_path_name(topic_name)
        target_folder.mkdir(parents=True, exist_ok=True)

        return target_folder

    # ------------------------------------------------------------------
    # Status and stop control
    # ------------------------------------------------------------------

    def set_status(self, **values):
        with self.lock:
            for key, value in values.items():
                setattr(self, key, value)

    def calculate_progress(self):
        """
        Progress is based on processed media files versus the
        pre-counted total number of files.
        """
        if self.total_files:
            return min(
                100.0,
                (self.processed_counter / self.total_files) * 100.0,
            )

        return 0.0

    def wait_or_stop(self, seconds):
        return self.stop_event.wait(timeout=seconds)

    @staticmethod
    def ensure_event_loop():
        """
        Flask serves each request from its own worker thread and
        plain threads have no asyncio event loop by default.
        Telethon requires one, so create a loop for the current
        thread if none exists.
        """
        try:
            asyncio.get_event_loop()
        except RuntimeError:
            asyncio.set_event_loop(asyncio.new_event_loop())

    # ------------------------------------------------------------------
    # Download worker
    # ------------------------------------------------------------------

    def run_download_task(self, chat_id):
        """
        The worker runs entirely in its own thread.

        Telethon is created and used inside this thread only. No event
        loop from the Flask thread is shared and no event loop is
        manually passed between requests.
        """
        self.ensure_event_loop()

        client = None
        topic_cache = {}

        try:
            self.set_status(
                current_status="Connecting to Telegram...",
                current_file="None",
                current_file_progress=0.0,
                overall_progress=0.0,
            )

            client = TelegramClient(
                self.session_name,
                self.api_id,
                self.api_hash,
            )

            with client:
                if not client.is_user_authorized():
                    self.set_status(
                        current_status="Telegram session is not authorized"
                    )
                    return

                group_entity = client.get_entity(chat_id)

                # Lightweight media count before the download pass.
                self.set_status(current_status="Counting media files...")

                total_media = self.count_total_media(client, chat_id)

                if self.stop_event.is_set():
                    return

                self.set_status(
                    current_status="Download started",
                    total_files=total_media,
                    processed_counter=0,
                    download_counter=0,
                    skipped_counter=0,
                    failed_counter=0,
                )

                # Single full pass over the chat messages for the
                # downloads themselves. The total file count comes
                # from the cheap filter count above.
                for message in client.iter_messages(chat_id, limit=None):
                    if self.stop_event.is_set():
                        break

                    if not self.is_downloadable_media(message):
                        continue

                    self.processed_counter += 1

                    identifier = self.make_history_identifier(
                        chat_id,
                        message.id,
                    )

                    filename = self.get_message_filename(message)
                    legacy_filename = self.get_legacy_filename(message)

                    target_folder = self.get_target_folder(
                        client,
                        chat_id,
                        group_entity,
                        message,
                        topic_cache,
                    )

                    target_path = target_folder / filename
                    temporary_path = Path(str(target_path) + ".part")

                    # --- 1. New history check (chat_id:message_id) ---
                    if identifier in self.download_history:
                        self.skipped_counter += 1
                        self.set_status(
                            skipped_counter=self.skipped_counter,
                            overall_progress=self.calculate_progress(),
                        )
                        continue

                    # --- 2. Legacy history check (old app version) ---
                    # The old version did not sanitize filenames, so both
                    # the raw and the sanitized name are compared.
                    legacy_names = {
                        legacy_filename,
                        self.sanitize_path_name(legacy_filename),
                    }

                    if legacy_names & self.legacy_history:
                        # Migrate the entry to the new history format
                        self.mark_as_downloaded(
                            identifier,
                            filename,
                            chat_id,
                            message.id,
                        )

                        self.skipped_counter += 1
                        self.set_status(
                            skipped_counter=self.skipped_counter,
                            overall_progress=self.calculate_progress(),
                        )
                        continue

                    # --- 3. Existing file check (new or legacy name) ---
                    legacy_target_paths = [
                        target_folder / legacy_filename,
                        target_folder / self.sanitize_path_name(legacy_filename),
                    ]

                    if (
                        target_path.exists()
                        or any(path.exists() for path in legacy_target_paths)
                    ):
                        self.mark_as_downloaded(
                            identifier,
                            filename,
                            chat_id,
                            message.id,
                        )

                        self.skipped_counter += 1
                        self.set_status(
                            skipped_counter=self.skipped_counter,
                            overall_progress=self.calculate_progress(),
                        )
                        continue

                    self.set_status(
                        current_file=filename,
                        current_status=(
                            f"Downloading {self.processed_counter}"
                        ),
                        current_file_progress=0.0,
                        file_size_mb=0.0,
                        download_speed_mbps=0.0,
                        overall_progress=self.calculate_progress(),
                    )

                    file_start = time.time()
                    last_bytes = 0
                    last_time = file_start

                    def progress_callback(received, total):
                        nonlocal last_bytes, last_time

                        now = time.time()
                        elapsed = now - last_time

                        progress = (
                            (received / total) * 100.0
                            if total
                            else 0.0
                        )

                        status_update = {
                            "current_file_progress": progress,
                            "file_size_mb": (
                                total / (1024 * 1024)
                                if total
                                else 0.0
                            ),
                        }

                        # Only overwrite the speed when a new measurement
                        # exists; keep the last value between measurements
                        # instead of resetting it to zero.
                        if elapsed >= 0.5:
                            status_update["download_speed_mbps"] = (
                                (received - last_bytes)
                                / elapsed
                                / (1024 * 1024)
                            )
                            last_bytes = received
                            last_time = now

                        self.set_status(**status_update)

                    try:
                        download_result = client.download_media(
                            message=message,
                            file=str(temporary_path),
                            progress_callback=progress_callback,
                        )

                        if self.stop_event.is_set():
                            if temporary_path.exists():
                                temporary_path.unlink()
                            break

                        # Telethon returns None when the message has
                        # no downloadable media (for example a webpage
                        # preview that slipped through). Skip it
                        # instead of counting it as a failure.
                        if not download_result:
                            self.logger.warning(
                                "No downloadable media in message %s; "
                                "skipping",
                                message.id,
                            )

                            self.mark_as_downloaded(
                                identifier,
                                filename,
                                chat_id,
                                message.id,
                            )

                            self.skipped_counter += 1
                            self.set_status(
                                skipped_counter=self.skipped_counter,
                                overall_progress=self.calculate_progress(),
                            )
                            continue

                        # Telethon may have chosen a slightly different
                        # path (for example an added extension); trust
                        # the returned path over our assumption.
                        downloaded_path = Path(str(download_result))

                        if downloaded_path != temporary_path:
                            self.logger.info(
                                "Telethon saved the file as %s instead "
                                "of %s",
                                downloaded_path.name,
                                temporary_path.name,
                            )

                        os.replace(downloaded_path, target_path)

                        self.mark_as_downloaded(
                            identifier,
                            filename,
                            chat_id,
                            message.id,
                        )

                        self.download_counter += 1
                        duration = max(time.time() - file_start, 0.001)

                        average_speed = (
                            target_path.stat().st_size
                            / duration
                            / (1024 * 1024)
                        )

                        self.logger.info(
                            "Download completed | chat=%s | message=%s "
                            "| file=%s | speed=%.2f MB/s",
                            chat_id,
                            message.id,
                            filename,
                            average_speed,
                        )

                        self.set_status(
                            download_counter=self.download_counter,
                            current_file_progress=100.0,
                            download_speed_mbps=0.0,
                            overall_progress=self.calculate_progress(),
                        )

                    except FloodWaitError as exc:
                        wait_seconds = int(
                            getattr(exc, "seconds", 30) or 30
                        )

                        self.logger.warning(
                            "FloodWait: pausing for %d seconds",
                            wait_seconds,
                        )

                        self.set_status(
                            current_status=(
                                f"Telegram rate limit: waiting "
                                f"{wait_seconds} seconds"
                            )
                        )

                        if self.wait_or_stop(wait_seconds):
                            break

                    except Exception:
                        self.failed_counter += 1

                        self.logger.exception(
                            "Download failed | chat=%s | message=%s "
                            "| file=%s",
                            chat_id,
                            message.id,
                            filename,
                        )

                        self.set_status(
                            failed_counter=self.failed_counter,
                            current_status=(
                                f"Error at {filename}; continuing"
                            ),
                        )

                        if temporary_path.exists():
                            try:
                                temporary_path.unlink()
                            except OSError:
                                self.logger.warning(
                                    "Could not delete temporary file: %s",
                                    temporary_path,
                                )

        except Exception:
            self.logger.exception("Critical error in download worker")
            self.set_status(
                current_status="Download ended with error"
            )

        finally:
            with self.lock:
                was_stopped = self.stop_event.is_set()
                self.is_downloading = False
                self.current_file = "None"
                self.current_file_progress = 0.0
                self.download_speed_mbps = 0.0

                if was_stopped:
                    self.current_status = "Stopped"
                elif self.failed_counter:
                    self.current_status = "Finished with errors"
                else:
                    self.current_status = "Finished / Idle"

                # 100% only after a clean completion. A manual stop
                # keeps the real state.
                if not was_stopped and self.failed_counter == 0:
                    self.overall_progress = 100.0

    # ------------------------------------------------------------------
    # Flask API routes
    # ------------------------------------------------------------------

    def setup_web_routes(self):
        @self.app.route("/")
        def index():
            return render_template("index.html")

        @self.app.route("/api/auth/status")
        def auth_status():
            self.ensure_event_loop()

            if not self.api_id or not self.api_hash:
                return jsonify({
                    "authorized": False,
                    "needs_config": True,
                })

            client = None

            try:
                client = TelegramClient(
                    self.session_name,
                    self.api_id,
                    self.api_hash,
                )
                client.connect()

                authorized = client.is_user_authorized()

                return jsonify({
                    "authorized": authorized,
                    "needs_config": False,
                })

            except Exception:
                self.logger.exception(
                    "Error while checking the authentication status"
                )
                return jsonify({
                    "authorized": False,
                    "needs_config": True,
                })

            finally:
                if client is not None:
                    client.disconnect()

        @self.app.route("/api/auth/send_code", methods=["POST"])
        def auth_send_code():
            self.ensure_event_loop()

            data = request.get_json(silent=True) or {}

            try:
                self.api_id = int(data.get("api_id", 0))
                self.api_hash = str(data.get("api_hash", "")).strip()
                self.phone = str(data.get("phone", "")).strip()

                if not self.api_id or not self.api_hash or not self.phone:
                    return jsonify({
                        "success": False,
                        "error": "API ID, API Hash and phone number are required",
                    }), 400

                client = TelegramClient(
                    self.session_name,
                    self.api_id,
                    self.api_hash,
                )

                try:
                    client.connect()

                    if client.is_user_authorized():
                        self.phone_code_hash = None
                        self.save_config()

                        return jsonify({
                            "success": True,
                            "requires_code": False,
                        })

                    result = client.send_code_request(self.phone)
                    self.phone_code_hash = result.phone_code_hash
                    self.save_config()

                    return jsonify({
                        "success": True,
                        "requires_code": True,
                    })

                finally:
                    client.disconnect()

            except Exception as exc:
                self.logger.exception("Error while sending the Telegram code")
                return jsonify({
                    "success": False,
                    "error": str(exc),
                }), 400

        @self.app.route("/api/auth/verify", methods=["POST"])
        def auth_verify():
            self.ensure_event_loop()

            data = request.get_json(silent=True) or {}

            code = str(data.get("code", "")).strip()
            password = str(data.get("password", "")).strip()

            if not self.phone or not self.phone_code_hash:
                return jsonify({
                    "success": False,
                    "error": "No pending authentication. Request a code first.",
                }), 400

            client = None

            try:
                client = TelegramClient(
                    self.session_name,
                    self.api_id,
                    self.api_hash,
                )
                client.connect()

                try:
                    client.sign_in(
                        phone=self.phone,
                        code=code,
                        phone_code_hash=self.phone_code_hash,
                    )

                except SessionPasswordNeededError:
                    if not password:
                        return jsonify({
                            "success": False,
                            "error": "2fa_required",
                        })

                    client.sign_in(password=password)

                self.phone_code_hash = None
                self.save_config()

                return jsonify({"success": True})

            except PhoneCodeInvalidError:
                return jsonify({
                    "success": False,
                    "error": "Invalid Telegram code",
                }), 400

            except PhoneCodeExpiredError:
                self.phone_code_hash = None
                self.save_config()

                return jsonify({
                    "success": False,
                    "error": "The Telegram code has expired",
                }), 400

            except Exception as exc:
                self.logger.exception(
                    "Error during Telegram authentication"
                )
                return jsonify({
                    "success": False,
                    "error": str(exc),
                }), 400

            finally:
                if client is not None:
                    client.disconnect()

        @self.app.route("/api/groups")
        def api_groups():
            self.ensure_event_loop()

            client = None

            try:
                client = TelegramClient(
                    self.session_name,
                    self.api_id,
                    self.api_hash,
                )
                client.connect()

                if not client.is_user_authorized():
                    return jsonify({
                        "success": False,
                        "error": "Telegram is not authorized",
                    }), 401

                groups = []

                for dialog in client.get_dialogs():
                    if dialog.is_group or dialog.is_channel:
                        groups.append({
                            "id": dialog.id,
                            "title": dialog.title,
                        })

                return jsonify({
                    "success": True,
                    "groups": groups,
                })

            except Exception:
                self.logger.exception(
                    "Error while loading the Telegram groups"
                )
                return jsonify({
                    "success": False,
                    "error": "Failed to load groups",
                }), 500

            finally:
                if client is not None:
                    client.disconnect()

        @self.app.route("/api/status")
        def api_status():
            with self.lock:
                uptime_seconds = int(time.time() - self.start_time)
                minutes, seconds = divmod(uptime_seconds, 60)
                hours, minutes = divmod(minutes, 60)

                return jsonify({
                    "is_downloading": self.is_downloading,
                    "status_text": self.current_status,
                    "current_file": self.current_file,
                    "overall_progress": round(
                        self.overall_progress,
                        1,
                    ),
                    "current_file_progress": round(
                        self.current_file_progress,
                        1,
                    ),
                    "total_files": self.total_files,
                    "downloaded_count": self.download_counter,
                    "skipped_count": self.skipped_counter,
                    "failed_count": self.failed_counter,
                    "processed_count": self.processed_counter,
                    "file_size_mb": round(self.file_size_mb, 2),
                    "download_speed_mbps": round(
                        self.download_speed_mbps,
                        2,
                    ),
                    "uptime": f"{hours:02}:{minutes:02}:{seconds:02}",
                })

        @self.app.route("/api/start", methods=["POST"])
        def api_start():
            data = request.get_json(silent=True) or {}
            chat_id_value = str(data.get("chat_id", "")).strip()

            try:
                chat_id = int(chat_id_value)
            except ValueError:
                return jsonify({
                    "success": False,
                    "error": "Invalid chat ID",
                }), 400

            # Atomic check and reservation of the worker.
            with self.lock:
                if self.is_downloading:
                    return jsonify({
                        "success": False,
                        "error": "Download already in progress",
                    }), 400

                self.is_downloading = True
                self.stop_event.clear()

                self.download_counter = 0
                self.skipped_counter = 0
                self.failed_counter = 0
                self.processed_counter = 0
                self.total_files = None
                self.overall_progress = 0.0
                self.current_file_progress = 0.0
                self.current_status = "Download starting..."

            self.worker_thread = threading.Thread(
                target=self.run_download_task,
                args=(chat_id,),
                name="telegram-download-worker",
                daemon=True,
            )
            self.worker_thread.start()

            return jsonify({"success": True})

        @self.app.route("/api/stop", methods=["POST"])
        def api_stop():
            with self.lock:
                if not self.is_downloading:
                    return jsonify({
                        "success": True,
                        "message": "No download in progress",
                    })

                self.current_status = "Stopping after current file..."
                self.stop_event.set()

            return jsonify({
                "success": True,
                "message": "Stop requested",
            })

    def start(self):
        print(
            f"Starting web interface at "
            f"http://localhost:{self.web_port}"
        )

        self.app.run(
            host="0.0.0.0",
            port=self.web_port,
            debug=False,
            use_reloader=False,
        )


if __name__ == "__main__":
    app_instance = TelegramRipperWeb()
    app_instance.start()