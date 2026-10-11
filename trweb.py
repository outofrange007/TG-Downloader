#!/usr/bin/env python3
# copyright outofrange007 (out-of-range@gmx.de)
# MIT Licence - see LICENCE for details.

import asyncio
import base64
import io
import json
import logging
import os
import queue
import re
import threading
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

import qrcode
from flask import Flask, jsonify, render_template, request
from telethon import types
from telethon.errors import (
    FloodWaitError,
    PasswordHashInvalidError,
    PhoneCodeExpiredError,
    PhoneCodeInvalidError,
    SessionPasswordNeededError,
)
from telethon.sync import TelegramClient
from telethon.tl.functions.channels import GetForumTopicsRequest


class TelegramRipperWeb:
    def __init__(self, data_path=None, port=None):
        print("Initializing Telegram Ripper Web App...")

        self.web_port = int(port or os.environ.get("WEB_PORT", 5000))
        self.data_dir = Path(data_path or os.environ.get("DATA_PATH", "./data"))

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

        # QR login state (the worker thread owns the client and event loop;
        # the 2FA password is handed over through a queue)
        self.qr_login_state = {
            "status": "idle",  # idle, starting, waiting, 2fa_required, success, expired, error
            "qr_image": None,
            "error": None,
        }
        self.qr_password_queue = queue.Queue()

        self.is_downloading = False
        self.current_status = "Idle"
        self.current_file = "None"

        self.download_counter = 0
        self.skipped_counter = 0
        self.failed_counter = 0
        self.processed_counter = 0

        self.total_files = None
        self.overall_progress = 0.0
        self.current_file_progress = 0.0
        self.file_size_mb = 0.0
        self.download_speed_mbps = 0.0
        self.start_time = time.time()

        self.setup_web_routes()

    # ------------------------------------------------------------------
    # Setup helpers
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

    # ------------------------------------------------------------------
    # Configuration
    # ------------------------------------------------------------------
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
        download_history = set()
        legacy_history = set()

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
                            self.logger.warning("Invalid history entry in line %d", line_number)
                            continue

                        identifier = entry.get("identifier")
                        if identifier:
                            download_history.add(identifier)

            except OSError as exc:
                self.logger.error("Could not read history: %s", exc)

            self.logger.info("Download history loaded: %d entries", len(download_history))
            return download_history, legacy_history

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

                self.logger.info("Legacy history loaded: %d entries", len(legacy_history))
            except OSError as exc:
                self.logger.error("Could not read legacy history: %s", exc)

        return download_history, legacy_history

    @staticmethod
    def make_history_identifier(chat_id, message_id):
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
            self.logger.error("Could not write to download history: %s", exc)

    # ------------------------------------------------------------------
    # File naming
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
        """Unique filename: timestamp + message ID + original name (or generic name)."""
        timestamp = message.date.strftime("%Y-%m-%d_%H-%M-%S")
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

    @staticmethod
    def get_legacy_filename(message):
        """Filename scheme used by older versions (without message ID)."""
        timestamp = message.date.strftime("%Y-%m-%d_%H-%M-%S")
        telegram_file = getattr(message, "file", None)
        original_name = getattr(telegram_file, "name", None) if telegram_file else None
        extension = (getattr(telegram_file, "ext", None) or ".bin") if telegram_file else ".bin"

        if original_name:
            return f"{timestamp}_{original_name}"
        return f"{timestamp}_media{extension}"

    # ------------------------------------------------------------------
    # Media detection and counting
    # ------------------------------------------------------------------
    @staticmethod
    def is_downloadable_media(message, allowed_types=None):
        """Return True if the message has downloadable media matching the filter."""
        if not getattr(message, "media", None):
            return False
        if getattr(message, "file", None) is None:
            return False

        if not allowed_types:
            return True

        media = message.media

        if isinstance(media, types.MessageMediaPhoto):
            return "photos" in allowed_types

        if isinstance(media, types.MessageMediaDocument):
            doc = media.document
            if doc is None:
                return False

            mime = getattr(doc, "mime_type", "") or ""
            attributes = getattr(doc, "attributes", []) or []
            is_video = mime.startswith("video/") or any(
                isinstance(a, types.DocumentAttributeVideo) for a in attributes
            )
            is_audio = mime.startswith("audio/") or any(
                isinstance(a, types.DocumentAttributeAudio) for a in attributes
            )

            if is_video:
                return "videos" in allowed_types
            if is_audio:
                return "audio" in allowed_types
            return "documents" in allowed_types

        return False

    def count_total_media(self, client, chat_id, allowed_types=None, topic_id=None):
        """
        Count downloadable media for the progress display.
        Without a topic, server-side filters are used (cheap).
        With a topic, messages have to be iterated (exact).
        """
        if topic_id is not None:
            try:
                count = 0
                for msg in client.iter_messages(chat_id, reply_to=topic_id):
                    if self.stop_event.is_set():
                        return None
                    if self.is_downloadable_media(msg, allowed_types):
                        count += 1
                self.logger.info("Topic media pre-count finished: %d files", count)
                return count
            except Exception as exc:
                self.logger.warning("Could not count topic messages: %s", exc)
                return None

        filter_map = {
            "photos": ["InputMessagesFilterPhotos"],
            "videos": ["InputMessagesFilterVideo"],
            "documents": ["InputMessagesFilterDocument"],
            "audio": ["InputMessagesFilterMusic", "InputMessagesFilterVoice"],
        }

        if allowed_types:
            filter_names = []
            for media_type in allowed_types:
                filter_names.extend(filter_map.get(media_type, []))
        else:
            filter_names = [
                "InputMessagesFilterPhotos",
                "InputMessagesFilterVideo",
                "InputMessagesFilterDocument",
                "InputMessagesFilterMusic",
                "InputMessagesFilterVoice",
                "InputMessagesFilterGif",
            ]

        total = 0
        for name in filter_names:
            if self.stop_event.is_set():
                return None

            filter_class = getattr(types, name, None)
            if filter_class is None:
                continue

            try:
                result = client.get_messages(chat_id, limit=1, filter=filter_class())
                total += getattr(result, "total", 0) or 0
            except Exception as exc:
                self.logger.warning("Count filter %s error: %s", name, exc)
                return None

        self.logger.info("Media pre-count finished: %d downloadable files", total)
        return total

    # ------------------------------------------------------------------
    # Forum topics
    # ------------------------------------------------------------------
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
            self.logger.warning("Could not load topic %s: %s", topic_id, exc)

        cache[topic_id] = topic_name
        return topic_name

    def get_target_folder(self, client, chat_id, group_entity, message, cache):
        group_name = self.sanitize_path_name(getattr(group_entity, "title", None))
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
            topic_name = self.get_topic_name(client, chat_id, topic_id, cache)
        else:
            topic_name = "General"

        target_folder = base_folder / self.sanitize_path_name(topic_name)
        target_folder.mkdir(parents=True, exist_ok=True)
        return target_folder

    # ------------------------------------------------------------------
    # Status helpers
    # ------------------------------------------------------------------
    def set_status(self, **values):
        with self.lock:
            for key, value in values.items():
                setattr(self, key, value)

    def calculate_progress(self):
        if self.total_files:
            return min(100.0, (self.processed_counter / self.total_files) * 100.0)
        return 0.0

    def wait_or_stop(self, seconds):
        """Interruptible sleep. Returns True if a stop was requested."""
        return self.stop_event.wait(timeout=seconds)

    @staticmethod
    def ensure_event_loop():
        """Every thread that uses telethon.sync needs its own event loop."""
        try:
            asyncio.get_event_loop()
        except RuntimeError:
            asyncio.set_event_loop(asyncio.new_event_loop())

    # ------------------------------------------------------------------
    # Download worker
    # ------------------------------------------------------------------
    def run_download_task(self, chat_id, allowed_types=None, topic_id=None):
        self.ensure_event_loop()
        topic_cache = {}

        try:
            self.set_status(
                current_status="Connecting to Telegram...",
                current_file="None",
                current_file_progress=0.0,
                overall_progress=0.0,
            )

            client = TelegramClient(self.session_name, self.api_id, self.api_hash)
            with client:
                if not client.is_user_authorized():
                    self.set_status(current_status="Telegram session is not authorized")
                    return

                group_entity = client.get_entity(chat_id)
                self.set_status(current_status="Counting media files...")

                total_media = self.count_total_media(client, chat_id, allowed_types, topic_id)
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

                iter_kwargs = {}
                if topic_id is not None:
                    iter_kwargs["reply_to"] = topic_id

                for message in client.iter_messages(chat_id, **iter_kwargs):
                    if self.stop_event.is_set():
                        break

                    if not self.is_downloadable_media(message, allowed_types):
                        continue

                    self.processed_counter += 1
                    identifier = self.make_history_identifier(chat_id, message.id)
                    filename = self.get_message_filename(message)
                    legacy_filename = self.get_legacy_filename(message)

                    target_folder = self.get_target_folder(
                        client, chat_id, group_entity, message, topic_cache
                    )
                    target_path = target_folder / filename
                    temporary_path = Path(str(target_path) + ".part")

                    # Already in the history
                    if identifier in self.download_history:
                        self.skipped_counter += 1
                        self.set_status(
                            skipped_counter=self.skipped_counter,
                            overall_progress=self.calculate_progress(),
                        )
                        continue

                    # Already in the legacy history
                    legacy_names = {legacy_filename, self.sanitize_path_name(legacy_filename)}
                    if legacy_names & self.legacy_history:
                        self.mark_as_downloaded(identifier, filename, chat_id, message.id)
                        self.skipped_counter += 1
                        self.set_status(
                            skipped_counter=self.skipped_counter,
                            overall_progress=self.calculate_progress(),
                        )
                        continue

                    # File already exists on disk (new or legacy naming)
                    legacy_target_paths = [
                        target_folder / legacy_filename,
                        target_folder / self.sanitize_path_name(legacy_filename),
                    ]
                    if target_path.exists() or any(p.exists() for p in legacy_target_paths):
                        self.mark_as_downloaded(identifier, filename, chat_id, message.id)
                        self.skipped_counter += 1
                        self.set_status(
                            skipped_counter=self.skipped_counter,
                            overall_progress=self.calculate_progress(),
                        )
                        continue

                    self.set_status(
                        current_file=filename,
                        current_status=f"Downloading {self.processed_counter}",
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
                        progress = (received / total) * 100.0 if total else 0.0

                        status_update = {
                            "current_file_progress": progress,
                            "file_size_mb": total / (1024 * 1024) if total else 0.0,
                        }

                        if elapsed >= 0.5:
                            status_update["download_speed_mbps"] = (
                                (received - last_bytes) / elapsed / (1024 * 1024)
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

                        if not download_result:
                            # Telegram returned no file (e.g. unsupported media): not an error
                            self.logger.warning(
                                "No downloadable file in message %s; skipping", message.id
                            )
                            self.mark_as_downloaded(identifier, filename, chat_id, message.id)
                            self.skipped_counter += 1
                            self.set_status(
                                skipped_counter=self.skipped_counter,
                                overall_progress=self.calculate_progress(),
                            )
                            continue

                        downloaded_path = Path(str(download_result))
                        os.replace(downloaded_path, target_path)

                        self.mark_as_downloaded(identifier, filename, chat_id, message.id)
                        self.download_counter += 1
                        duration = max(time.time() - file_start, 0.001)
                        average_speed = target_path.stat().st_size / duration / (1024 * 1024)

                        self.logger.info(
                            "Download completed | chat=%s | message=%s | file=%s | speed=%.2f MB/s",
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
                        wait_seconds = int(getattr(exc, "seconds", 30) or 30)
                        self.logger.warning("FloodWait: pausing for %d seconds", wait_seconds)
                        self.set_status(
                            current_status=f"Telegram rate limit: waiting {wait_seconds} seconds"
                        )
                        if temporary_path.exists():
                            try:
                                temporary_path.unlink()
                            except OSError:
                                pass
                        if self.wait_or_stop(wait_seconds):
                            break

                    except Exception:
                        self.failed_counter += 1
                        self.logger.exception(
                            "Download failed | chat=%s | message=%s | file=%s",
                            chat_id,
                            message.id,
                            filename,
                        )
                        self.set_status(
                            failed_counter=self.failed_counter,
                            current_status=f"Error at {filename}; continuing",
                        )
                        if temporary_path.exists():
                            try:
                                temporary_path.unlink()
                            except OSError:
                                pass

        except Exception:
            self.logger.exception("Critical error in download worker")
            self.set_status(current_status="Download ended with error")

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

                if not was_stopped and self.failed_counter == 0:
                    self.overall_progress = 100.0

    # ------------------------------------------------------------------
    # QR login
    # ------------------------------------------------------------------
    @staticmethod
    def generate_qr_base64(url):
        qr = qrcode.QRCode(
            version=None,
            error_correction=qrcode.constants.ERROR_CORRECT_L,
            box_size=8,
            border=2,
        )
        qr.add_data(url)
        qr.make(fit=True)
        image = qr.make_image(fill_color="black", back_color="white")
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        return "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode("utf-8")

    def set_qr_state(self, **values):
        with self.lock:
            self.qr_login_state.update(values)

    def run_qr_login_worker(self):
        """
        Run the whole QR login inside one thread that owns its own client
        and event loop. The 2FA password is handed over via a queue so that
        no Telethon call crosses thread boundaries.
        """
        self.ensure_event_loop()
        client = None

        # Drop stale passwords from a previous attempt
        while not self.qr_password_queue.empty():
            try:
                self.qr_password_queue.get_nowait()
            except queue.Empty:
                break

        try:
            client = TelegramClient(self.session_name, self.api_id, self.api_hash)
            client.connect()

            if client.is_user_authorized():
                self.set_qr_state(status="success", qr_image=None, error=None)
                return

            qr_login = client.qr_login()
            deadline = time.time() + 180

            while time.time() < deadline:
                self.set_qr_state(
                    status="waiting",
                    qr_image=self.generate_qr_base64(qr_login.url),
                    error=None,
                )

                try:
                    qr_login.wait(timeout=25)
                    break  # Scanned and accepted

                except asyncio.TimeoutError:
                    # Token expired or not scanned yet: refresh the QR code
                    qr_login.recreate()
                    continue

                except SessionPasswordNeededError:
                    self.set_qr_state(status="2fa_required", error=None)

                    signed_in = False
                    password_deadline = time.time() + 120
                    while time.time() < password_deadline:
                        try:
                            password = self.qr_password_queue.get(timeout=1)
                        except queue.Empty:
                            continue

                        try:
                            client.sign_in(password=password)
                            signed_in = True
                            break
                        except PasswordHashInvalidError:
                            self.set_qr_state(
                                status="2fa_required",
                                error="Incorrect 2FA password",
                            )

                    if not signed_in:
                        self.set_qr_state(
                            status="expired",
                            error="2FA password was not provided in time.",
                        )
                        return
                    break
            else:
                self.set_qr_state(
                    status="expired",
                    error="QR code expired. Please generate a new one.",
                )
                return

            self.set_qr_state(status="success", qr_image=None, error=None)
            self.logger.info("QR login successful")

        except Exception as exc:
            self.logger.exception("Error during QR login worker")
            self.set_qr_state(status="error", error=str(exc))

        finally:
            if client is not None:
                client.disconnect()

    # ------------------------------------------------------------------
    # Web routes
    # ------------------------------------------------------------------
    def setup_web_routes(self):
        @self.app.route("/")
        def index():
            return render_template("index.html")

        @self.app.route("/api/auth/status")
        def auth_status():
            self.ensure_event_loop()
            if not self.api_id or not self.api_hash:
                return jsonify({"authorized": False, "needs_config": True})

            client = None
            try:
                client = TelegramClient(self.session_name, self.api_id, self.api_hash)
                client.connect()
                authorized = bool(client.is_user_authorized())
                return jsonify({"authorized": authorized, "needs_config": False})
            except Exception:
                self.logger.exception("Error while checking the authentication status")
                return jsonify({"authorized": False, "needs_config": True})
            finally:
                if client is not None:
                    client.disconnect()

        # ----- QR login -----
        @self.app.route("/api/auth/qr/start", methods=["POST"])
        def auth_qr_start():
            data = request.get_json(silent=True) or {}
            api_id = data.get("api_id") or self.api_id
            api_hash = data.get("api_hash") or self.api_hash

            if not api_id or not api_hash:
                return jsonify({"success": False, "error": "API ID and API Hash are required"}), 400

            try:
                api_id = int(api_id)
            except (TypeError, ValueError):
                return jsonify({"success": False, "error": "API ID must be a number"}), 400

            with self.lock:
                if self.qr_login_state.get("status") in ("starting", "waiting", "2fa_required"):
                    return jsonify({"success": False, "error": "QR login already running"}), 400

                self.qr_login_state = {"status": "starting", "qr_image": None, "error": None}

            self.api_id = api_id
            self.api_hash = str(api_hash).strip()
            self.save_config()

            threading.Thread(
                target=self.run_qr_login_worker,
                name="telegram-qr-login-worker",
                daemon=True,
            ).start()
            return jsonify({"success": True})

        @self.app.route("/api/auth/qr/status")
        def auth_qr_status():
            with self.lock:
                return jsonify(dict(self.qr_login_state))

        @self.app.route("/api/auth/qr/2fa", methods=["POST"])
        def auth_qr_2fa():
            data = request.get_json(silent=True) or {}
            password = str(data.get("password", ""))

            if not password or self.qr_login_state.get("status") != "2fa_required":
                return jsonify({"success": False, "error": "No 2FA step pending"}), 400

            self.qr_password_queue.put(password)
            return jsonify({"success": True})

        # ----- Phone code login -----
        @self.app.route("/api/auth/send_code", methods=["POST"])
        def auth_send_code():
            self.ensure_event_loop()
            data = request.get_json(silent=True) or {}

            client = None
            try:
                self.api_id = int(data.get("api_id", 0))
                self.api_hash = str(data.get("api_hash", "")).strip()
                self.phone = str(data.get("phone", "")).strip()

                if not self.api_id or not self.api_hash or not self.phone:
                    return jsonify({
                        "success": False,
                        "error": "API ID, API Hash and phone number are required",
                    }), 400

                client = TelegramClient(self.session_name, self.api_id, self.api_hash)
                client.connect()

                if client.is_user_authorized():
                    self.phone_code_hash = None
                    self.save_config()
                    return jsonify({"success": True, "requires_code": False})

                result = client.send_code_request(self.phone)
                self.phone_code_hash = result.phone_code_hash
                self.save_config()
                return jsonify({"success": True, "requires_code": True})

            except Exception as exc:
                self.logger.exception("Error while sending the login code")
                return jsonify({"success": False, "error": str(exc)}), 400
            finally:
                if client is not None:
                    client.disconnect()

        @self.app.route("/api/auth/verify", methods=["POST"])
        def auth_verify():
            self.ensure_event_loop()
            data = request.get_json(silent=True) or {}
            code = str(data.get("code", "")).strip()
            password = str(data.get("password", "")).strip()

            if not self.phone or not self.phone_code_hash:
                return jsonify({"success": False, "error": "No pending authentication"}), 400

            client = None
            try:
                client = TelegramClient(self.session_name, self.api_id, self.api_hash)
                client.connect()

                try:
                    client.sign_in(
                        phone=self.phone,
                        code=code,
                        phone_code_hash=self.phone_code_hash,
                    )
                except SessionPasswordNeededError:
                    if not password:
                        return jsonify({"success": False, "error": "2fa_required"})
                    client.sign_in(password=password)

                self.phone_code_hash = None
                self.save_config()
                return jsonify({"success": True})

            except PhoneCodeInvalidError:
                return jsonify({"success": False, "error": "Invalid Telegram code"}), 400
            except PhoneCodeExpiredError:
                self.phone_code_hash = None
                self.save_config()
                return jsonify({"success": False, "error": "Telegram code expired"}), 400
            except PasswordHashInvalidError:
                return jsonify({"success": False, "error": "Incorrect 2FA password"}), 400
            except Exception as exc:
                self.logger.exception("Error while verifying the login code")
                return jsonify({"success": False, "error": str(exc)}), 400
            finally:
                if client is not None:
                    client.disconnect()

        # ----- Groups and topics -----
        @self.app.route("/api/groups")
        def api_groups():
            self.ensure_event_loop()
            client = None
            try:
                client = TelegramClient(self.session_name, self.api_id, self.api_hash)
                client.connect()

                if not client.is_user_authorized():
                    return jsonify({"success": False, "error": "Unauthorized"}), 401

                groups = []
                for dialog in client.get_dialogs():
                    if dialog.is_group or dialog.is_channel:
                        groups.append({
                            "id": dialog.id,
                            "title": dialog.title,
                            "is_forum": bool(getattr(dialog.entity, "forum", False)),
                        })

                return jsonify({"success": True, "groups": groups})
            except Exception:
                self.logger.exception("Error while loading groups")
                return jsonify({"success": False, "error": "Failed to load groups"}), 500
            finally:
                if client is not None:
                    client.disconnect()

        @self.app.route("/api/topics")
        def api_topics():
            self.ensure_event_loop()
            chat_id = request.args.get("chat_id", "").strip()
            if not chat_id:
                return jsonify({"success": False, "error": "Missing chat_id"}), 400

            try:
                chat_id = int(chat_id)
            except ValueError:
                return jsonify({"success": False, "error": "Invalid chat_id"}), 400

            client = None
            try:
                client = TelegramClient(self.session_name, self.api_id, self.api_hash)
                client.connect()

                if not client.is_user_authorized():
                    return jsonify({"success": False, "error": "Unauthorized"}), 401

                entity = client.get_entity(chat_id)
                if not getattr(entity, "forum", False):
                    return jsonify({"success": True, "topics": []})

                result = client(GetForumTopicsRequest(
                    channel=entity,
                    offset_date=None,
                    offset_id=0,
                    offset_topic=0,
                    limit=100,
                ))

                topics = []
                for topic in getattr(result, "topics", []):
                    topic_id = getattr(topic, "id", None)
                    if topic_id is None:
                        continue
                    topics.append({
                        "id": topic_id,
                        "title": getattr(topic, "title", None) or f"Topic {topic_id}",
                    })

                return jsonify({"success": True, "topics": topics})
            except Exception as exc:
                self.logger.warning("Could not fetch topics: %s", exc)
                return jsonify({"success": True, "topics": []})
            finally:
                if client is not None:
                    client.disconnect()

        # ----- Download control -----
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
                    "overall_progress": round(self.overall_progress, 1),
                    "current_file_progress": round(self.current_file_progress, 1),
                    "total_files": self.total_files,
                    "downloaded_count": self.download_counter,
                    "skipped_count": self.skipped_counter,
                    "failed_count": self.failed_counter,
                    "processed_count": self.processed_counter,
                    "file_size_mb": round(self.file_size_mb, 2),
                    "download_speed_mbps": round(self.download_speed_mbps, 2),
                    "uptime": f"{hours:02}:{minutes:02}:{seconds:02}",
                })

        @self.app.route("/api/start", methods=["POST"])
        def api_start():
            data = request.get_json(silent=True) or {}
            chat_id_value = str(data.get("chat_id", "")).strip()
            topic_id = data.get("topic_id")
            media_types = data.get("media_types") or []

            try:
                chat_id = int(chat_id_value)
            except ValueError:
                return jsonify({"success": False, "error": "Invalid chat ID"}), 400

            if topic_id is not None and str(topic_id).strip() != "":
                try:
                    topic_id = int(topic_id)
                except ValueError:
                    topic_id = None
            else:
                topic_id = None

            valid_types = {"photos", "videos", "documents", "audio"}
            media_types = [t for t in media_types if t in valid_types]

            with self.lock:
                if self.is_downloading:
                    return jsonify({"success": False, "error": "Download already in progress"}), 400

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
                args=(chat_id, media_types, topic_id),
                name="telegram-download-worker",
                daemon=True,
            )
            self.worker_thread.start()
            return jsonify({"success": True})

        @self.app.route("/api/stop", methods=["POST"])
        def api_stop():
            with self.lock:
                if not self.is_downloading:
                    return jsonify({"success": True, "message": "No download in progress"})

                self.current_status = "Stopping after current file..."
                self.stop_event.set()

            return jsonify({"success": True, "message": "Stop requested"})

    def start(self):
        print(f"Starting web interface at http://localhost:{self.web_port}")
        self.app.run(host="0.0.0.0", port=self.web_port, debug=False, use_reloader=False)


if __name__ == "__main__":
    app_instance = TelegramRipperWeb()
    app_instance.start()