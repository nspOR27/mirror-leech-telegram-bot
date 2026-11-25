from asyncio import create_subprocess_exec, subprocess
from logging import getLogger
from os import path as ospath, walk
from re import search as re_search
from time import time
from aiofiles.os import path as aiopath

from .gotohp_helper import get_gotohp_binary
from ...ext_utils.bot_utils import sync_to_async

LOGGER = getLogger(__name__)

MEDIA_EXTENSIONS = {
    # Images
    ".jpg",
    ".jpeg",
    ".png",
    ".webp",
    ".heic",
    ".heif",
    ".gif",
    ".tiff",
    ".tif",
    ".bmp",
    ".raw",
    ".dng",
    ".cr2",
    ".nef",
    ".arw",
    # Videos
    ".mp4",
    ".mkv",
    ".mov",
    ".avi",
    ".wmv",
    ".flv",
    ".webm",
    ".m4v",
    ".3gp",
    ".ts",
    ".mts",
    ".m2ts",
}


class GoToHPUploader:
    def __init__(self, listener, path):
        self._listener = listener
        self._path = path
        self._subproc = None
        self._processed_bytes = 0
        self._total_bytes = 0
        self._uploaded_files = 0
        self._total_files = 0
        self._start_time = time()
        self._user_id = listener.user_id
        self._config_path = f"gotohp/{self._user_id}.config"

    @property
    def processed_bytes(self):
        return self._processed_bytes

    @property
    def total_bytes(self):
        return self._total_bytes or self._listener.size

    @property
    def speed(self):
        try:
            return self._processed_bytes / (time() - self._start_time)
        except Exception:
            return 0

    async def _scan_media_files(self):
        """Scans path for supported media files and counts files and bytes."""
        total_size = 0
        media_count = 0
        skipped_count = 0

        if await aiopath.isfile(self._path):
            ext = ospath.splitext(self._path)[1].lower()
            if ext in MEDIA_EXTENSIONS:
                size = ospath.getsize(self._path)
                return size, 1
            return 0, 0

        def _sync_scan():
            nonlocal total_size, media_count, skipped_count
            for root, _, files in walk(self._path):
                for f in files:
                    ext = ospath.splitext(f)[1].lower()
                    full_p = ospath.join(root, f)
                    if ext in MEDIA_EXTENSIONS:
                        total_size += ospath.getsize(full_p)
                        media_count += 1
                    else:
                        skipped_count += 1

        await sync_to_async(_sync_scan)
        if skipped_count > 0:
            LOGGER.info(
                f"Google Photos: Skipped {skipped_count} non-media files in {self._path}"
            )
        return total_size, media_count

    async def upload(self):
        cli_path = get_gotohp_binary()
        if not cli_path:
            await self._listener.on_upload_error(
                "gotohp-cli binary not found! Please ensure gotohp-cli is installed in system PATH or root folder."
            )
            return

        if not await aiopath.exists(self._config_path):
            await self._listener.on_upload_error(
                "No Google Photos config found for this user! Please configure credentials first."
            )
            return

        total_size, media_count = await self._scan_media_files()
        if media_count == 0:
            await self._listener.on_upload_error(
                "No supported media files (photos or videos) found to upload to Google Photos!"
            )
            return

        self._total_bytes = total_size
        self._total_files = media_count
        self._start_time = time()

        cmd = [
            cli_path,
            "upload",
            self._path,
            "--config",
            self._config_path,
            "--no-tui",
            "--recursive",
            "--threads",
            "2",
        ]

        # Extract account destination if provided (e.g. photos:user@gmail.com)
        up_dest = self._listener.up_dest or ""
        for prefix in ["gp:", "photos:", "ptotos:", "gotohp:"]:
            if up_dest.startswith(prefix):
                account = up_dest[len(prefix) :].strip()
                if account:
                    cmd.extend(["--account", account])
                break

        LOGGER.info(
            f"Starting Google Photos upload: {self._path} ({media_count} files, {total_size} bytes)"
        )

        try:
            self._subproc = await create_subprocess_exec(
                *cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            self._listener.subproc = self._subproc
        except Exception as e:
            await self._listener.on_upload_error(f"Failed to start gotohp-cli: {e}")
            return

        stderr_lines = []

        async def _read_stderr():
            while line := await self._subproc.stderr.readline():
                decoded = line.decode().strip()
                if decoded:
                    stderr_lines.append(decoded)
                    LOGGER.debug(f"[gotohp stderr] {decoded}")

        from asyncio import create_task

        stderr_task = create_task(_read_stderr())

        try:
            while line := await self._subproc.stdout.readline():
                if self._listener.is_cancelled:
                    break
                decoded = line.decode().strip()
                if not decoded:
                    continue
                LOGGER.debug(f"[gotohp stdout] {decoded}")

                # Progress parsing heuristics:
                # e.g. "Uploaded: filename", "[1/5]", "100%", "Progress: 20%"
                if "uploaded" in decoded.lower():
                    self._uploaded_files += 1
                    if self._total_files > 0:
                        self._processed_bytes = min(
                            self._total_bytes,
                            int(
                                (self._uploaded_files / self._total_files)
                                * self._total_bytes
                            ),
                        )

                if match := re_search(r"(\d+)%", decoded):
                    pct = int(match.group(1))
                    if pct > 0:
                        self._processed_bytes = min(
                            self._total_bytes, int((pct / 100) * self._total_bytes)
                        )

            await self._subproc.wait()
            await stderr_task
        except Exception as e:
            LOGGER.error(f"Error during gotohp upload: {e}")
            await self._listener.on_upload_error(str(e))
            return

        if self._listener.is_cancelled:
            return

        if self._subproc.returncode == 0:
            self._processed_bytes = self._total_bytes
            self._uploaded_files = self._total_files
            LOGGER.info(f"Google Photos upload completed: {self._listener.name}")
            await self._listener.on_upload_complete(
                "https://photos.google.com",
                self._uploaded_files,
                0,
                "Google Photos",
            )
        else:
            err_msg = (
                "\n".join(stderr_lines[-5:])
                if stderr_lines
                else f"Process exited with code {self._subproc.returncode}"
            )
            LOGGER.error(f"gotohp upload error: {err_msg}")
            await self._listener.on_upload_error(
                f"Google Photos upload failed:\n<code>{err_msg}</code>"
            )

    async def cancel_task(self):
        LOGGER.info(f"Cancelling Google Photos upload: {self._listener.name}")
        self._listener.is_cancelled = True
        if self._subproc is not None and self._subproc.returncode is None:
            try:
                self._subproc.kill()
            except Exception:
                pass
        await self._listener.on_upload_error("Google Photos upload cancelled by user!")
