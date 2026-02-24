"""QQ channel implementation using botpy SDK."""

import asyncio
import os
import re
import subprocess
from collections import deque
from pathlib import Path
from typing import TYPE_CHECKING

import httpx
from loguru import logger

from nanobot.bus.events import OutboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.channels.base import BaseChannel
from nanobot.config.schema import QQConfig

try:
    import botpy
    from botpy.message import C2CMessage

    QQ_AVAILABLE = True
except ImportError:
    QQ_AVAILABLE = False
    botpy = None
    C2CMessage = None

if TYPE_CHECKING:
    from botpy.message import C2CMessage


def _make_bot_class(channel: "QQChannel") -> "type[botpy.Client]":
    """Create a botpy Client subclass bound to the given channel."""
    intents = botpy.Intents(public_messages=True, direct_message=True)

    class _Bot(botpy.Client):
        def __init__(self):
            super().__init__(intents=intents)

        async def on_ready(self):
            logger.info("QQ bot ready: {}", self.robot.name)

        async def on_c2c_message_create(self, message: "C2CMessage"):
            await channel._on_message(message)

        async def on_direct_message_create(self, message):
            await channel._on_message(message)

    return _Bot


class QQChannel(BaseChannel):
    """QQ channel using botpy SDK with WebSocket connection."""

    name = "qq"

    def __init__(self, config: QQConfig, bus: MessageBus):
        super().__init__(config, bus)
        self.config: QQConfig = config
        self._client: "botpy.Client | None" = None
        self._processed_ids: deque = deque(maxlen=1000)
        self._reply_seq: dict[str, int] = {}  # msg_id -> last msg_seq used

    _IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp"}
    _URL_RE = re.compile(r"https?://[^\s\"'<>]+")

    @staticmethod
    def _is_url(value: str) -> bool:
        v = (value or "").strip().lower()
        return v.startswith("http://") or v.startswith("https://")

    def _next_msg_seq(self, reply_to_msg_id: str | None) -> int | None:
        """Return next msg_seq for a reply chain; None when not replying."""
        if not reply_to_msg_id:
            return None
        last = int(self._reply_seq.get(reply_to_msg_id, 0) or 0)
        nxt = last + 1
        self._reply_seq[reply_to_msg_id] = nxt
        return nxt

    def _upload_to_public_url_sync(self, file_path: str) -> str | None:
        """
        Convert a local file path to a public URL using a user-provided command.

        The command should print a URL to stdout. Supports a "{path}" placeholder;
        if not present, the file path is appended as the last argument.
        """
        cmd = (self.config.media_upload_command or "").strip()
        if not cmd:
            return None
        timeout_s = max(1, int(getattr(self.config, "media_upload_timeout_s", 60) or 60))
        if "{path}" in cmd:
            cmdline = cmd.replace("{path}", f"\"{file_path}\"")
        else:
            cmdline = f"{cmd} \"{file_path}\""
        try:
            cp = subprocess.run(
                cmdline,
                shell=True,
                capture_output=True,
                text=True,
                timeout=timeout_s,
            )
        except Exception as e:
            logger.warning("QQ media_upload_command failed: {}", e)
            return None
        out = (cp.stdout or "") + "\n" + (cp.stderr or "")
        m = self._URL_RE.search(out)
        if not m:
            logger.warning("QQ media_upload_command returned no URL (exit={}): {}", cp.returncode, out[:200])
            return None
        return m.group(0)

    async def _download_attachment(self, url: str, filename_hint: str | None, message_id: str, idx: int) -> str | None:
        """Download an attachment URL to ~/.nanobot/media and return local path."""
        if not url:
            return None
        media_dir = Path.home() / ".nanobot" / "media"
        media_dir.mkdir(parents=True, exist_ok=True)
        ext = ""
        if isinstance(filename_hint, str) and filename_hint.strip():
            ext = Path(filename_hint.strip()).suffix
        if not ext:
            ext = ".bin"
        safe_mid = (message_id or "msg")[:16]
        file_path = media_dir / f"qq_{safe_mid}_{idx}{ext}"
        try:
            async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
                resp = await client.get(url)
                if not resp.is_success:
                    return None
                file_path.write_bytes(resp.content)
            return str(file_path)
        except Exception:
            return None

    async def start(self) -> None:
        """Start the QQ bot."""
        if not QQ_AVAILABLE:
            logger.error("QQ SDK not installed. Run: pip install qq-botpy")
            return

        if not self.config.app_id or not self.config.secret:
            logger.error("QQ app_id and secret not configured")
            return

        self._running = True
        BotClass = _make_bot_class(self)
        self._client = BotClass()

        logger.info("QQ bot started (C2C private message)")
        await self._run_bot()

    async def _run_bot(self) -> None:
        """Run the bot connection with auto-reconnect."""
        while self._running:
            try:
                await self._client.start(appid=self.config.app_id, secret=self.config.secret)
            except Exception as e:
                logger.warning("QQ bot error: {}", e)
            if self._running:
                logger.info("Reconnecting QQ bot in 5 seconds...")
                await asyncio.sleep(5)

    async def stop(self) -> None:
        """Stop the QQ bot."""
        self._running = False
        if self._client:
            try:
                await self._client.close()
            except Exception:
                pass
        logger.info("QQ bot stopped")

    async def send(self, msg: OutboundMessage) -> None:
        """Send a message through QQ."""
        if not self._client:
            logger.warning("QQ client not initialized")
            return
        if not (msg.content and str(msg.content).strip()) and not (msg.media and len(msg.media) > 0):
            return

        meta = msg.metadata or {}
        reply_to_msg_id = meta.get("message_id") if isinstance(meta, dict) else None
        if not isinstance(reply_to_msg_id, str) or not reply_to_msg_id.strip():
            reply_to_msg_id = None

        try:
            # QQ requires (msg_id + msg_seq) for passive replies; msg_seq must be unique per msg_id.
            msg_seq = self._next_msg_seq(reply_to_msg_id)

            async def _send_text(text: str) -> None:
                nonlocal msg_seq
                t = (text or "").strip()
                if not t:
                    return
                kwargs = {"openid": msg.chat_id, "msg_type": 0, "content": t}
                if reply_to_msg_id and msg_seq is not None:
                    kwargs.update(msg_id=reply_to_msg_id, msg_seq=msg_seq)
                    msg_seq += 1
                await self._client.api.post_c2c_message(**kwargs)

            async def _send_media_path(path: str) -> None:
                nonlocal msg_seq
                p = (path or "").strip()
                if not p:
                    return

                # If already a public URL, use it directly; otherwise try upload command.
                url = p if self._is_url(p) else None
                if url is None:
                    if not os.path.isfile(p):
                        return
                    ext = Path(p).suffix.lower()
                    # QQ file_type: 1=png/jpg, 2=mp4, 3=silk, 4=file(not generally开放)
                    if ext in self._IMAGE_EXTS:
                        file_type = 1
                    elif ext == ".mp4":
                        file_type = 2
                    elif ext == ".silk":
                        file_type = 3
                    else:
                        await _send_text("（QQ 附件发送受限：当前文件类型无法直接发送。QQ 官方富媒体接口主要支持图片/视频/语音，并要求公网 URL。建议把文件转成图片/视频，或实现上传并发送链接。）")
                        return
                    url = await asyncio.to_thread(self._upload_to_public_url_sync, p)
                if not url:
                    await _send_text("（QQ 附件发送失败：QQ 官方接口要求公网可访问的 URL。请配置 channels.qq.mediaUploadCommand 让它把本地文件上传并输出 URL。）")
                    return

                # Upload-to-QQ to get file_info, then send as media message.
                # If the input was already a URL, we can only best-effort treat it as an image.
                ft = locals().get("file_type", 1)
                media = await self._client.api.post_c2c_file(openid=msg.chat_id, file_type=ft, url=url, srv_send_msg=False)
                kwargs = {"openid": msg.chat_id, "msg_type": 7, "media": media}
                if reply_to_msg_id and msg_seq is not None:
                    kwargs.update(msg_id=reply_to_msg_id, msg_seq=msg_seq)
                    msg_seq += 1
                await self._client.api.post_c2c_message(**kwargs)

            # Media first, then text (closer to Telegram behavior)
            for p in (msg.media or []):
                await _send_media_path(p)

            if msg.content and str(msg.content).strip():
                await _send_text(str(msg.content))
        except Exception as e:
            logger.error("Error sending QQ message: {}", e)

    async def _on_message(self, data: "C2CMessage") -> None:
        """Handle incoming message from QQ."""
        try:
            # Dedup by message ID
            if data.id in self._processed_ids:
                return
            self._processed_ids.append(data.id)

            author = data.author
            user_id = str(getattr(author, 'id', None) or getattr(author, 'user_openid', 'unknown'))
            content_parts: list[str] = []
            content = (getattr(data, "content", None) or "").strip()
            if content:
                content_parts.append(content)

            media_paths: list[str] = []
            attachments = getattr(data, "attachments", None)
            if isinstance(attachments, list) and attachments:
                for i, att in enumerate(attachments):
                    try:
                        url = getattr(att, "url", None)
                        filename = getattr(att, "filename", None)
                        if isinstance(url, str) and url.strip():
                            fp = await self._download_attachment(url.strip(), filename if isinstance(filename, str) else None, data.id, i)
                            if fp:
                                media_paths.append(fp)
                                content_parts.append(f"[attachment: {Path(fp).name}]")
                            else:
                                content_parts.append("[attachment: download failed]")
                    except Exception:
                        content_parts.append("[attachment: download failed]")

            if not content_parts and not media_paths:
                return
            content_out = "\n".join(content_parts).strip() or "[empty message]"

            await self._handle_message(
                sender_id=user_id,
                chat_id=user_id,
                content=content_out,
                media=media_paths,
                metadata={"message_id": data.id, "attachment_count": len(getattr(data, "attachments", []) or [])},
            )
        except Exception:
            logger.exception("Error handling QQ message")
