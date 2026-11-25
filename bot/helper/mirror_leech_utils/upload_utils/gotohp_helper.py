import shutil
from os import path as ospath
from asyncio import create_subprocess_exec, Event, wait_for, TimeoutError, subprocess
from aiofiles import open as aiopen
from aiofiles.os import makedirs, remove, path as aiopath
from pyrogram.filters import create, regex
from pyrogram.handlers import MessageHandler, CallbackQueryHandler

from .... import LOGGER, user_data
from ...ext_utils.db_handler import database
from ...ext_utils.bot_utils import update_user_ldata
from ...telegram_helper.button_build import ButtonMaker
from ...telegram_helper.message_utils import send_message, delete_message

GOTOHP_RISK_WARNING = """⚠️ <b>GOOGLE PHOTOS (GOTOHP) RISK NOTICE</b> ⚠️

<b>READ CAREFULLY BEFORE PROCEEDING:</b>
• <b>Account Ban / Termination Risk:</b> This tool uses reverse-engineered Google Pixel mobile APIs to spoof device identifiers and bypass Google One storage quotas. Google actively detects irregular mobile device fingerprints, and using unauthorized or unethical credentials may result in permanent suspension or termination of your Google Account.
• <b>Data Loss:</b> Uploaded photos and videos may be rejected, removed, or purged by Google at any time without notice.
• <b>Terms of Service Violation:</b> Bypassing Google One storage limits directly violates Google's Terms of Service and Abuse policies.
• <b>Privacy & Security:</b> Your mobile credentials grant direct access to your Google Photos library.

<i>It is STRONGLY RECOMMENDED to use a secondary or burner Google Account, NOT your primary personal account!</i>"""


def get_gotohp_binary():
    """Find gotohp-cli binary on the system."""
    for bin_name in ["gotohp-cli", "gotohp"]:
        if path := shutil.which(bin_name):
            return path
    for default_path in [
        "/usr/local/bin/gotohp-cli",
        "/usr/bin/gotohp-cli",
        "./gotohp-cli",
        "gotohp-cli",
    ]:
        if ospath.exists(default_path):
            return default_path
    return None


async def add_gotohp_credential(user_id: int, cred_string: str) -> str:
    """
    Stores credential inside user's gotohp config file (gotohp/{user_id}.config)
    using `gotohp-cli creds add` if binary exists, or direct file write.
    Syncs with MongoDB database.
    """
    await makedirs("gotohp", exist_ok=True)
    config_path = f"gotohp/{user_id}.config"

    if not await aiopath.exists(config_path):
        async with aiopen(config_path, "w") as f:
            await f.write("")

    cli_path = get_gotohp_binary()
    cred_string = cred_string.strip()

    if cli_path:
        LOGGER.info(f"Adding gotohp credential for user {user_id} using {cli_path}")
        proc = await create_subprocess_exec(
            cli_path,
            "--config",
            config_path,
            "creds",
            "add",
            cred_string,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            err = (stderr or stdout).decode().strip()
            LOGGER.warning(
                f"gotohp-cli creds add exit code {proc.returncode}: {err}. Falling back to stdin."
            )
            # Try piping via stdin: creds add -
            proc2 = await create_subprocess_exec(
                cli_path,
                "--config",
                config_path,
                "creds",
                "add",
                "-",
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            stdout2, stderr2 = await proc2.communicate(input=cred_string.encode())
            if proc2.returncode != 0:
                err2 = (stderr2 or stdout2).decode().strip()
                LOGGER.error(f"Failed to add credentials via gotohp-cli: {err2}")
                # Save credential string to config file as raw fallback
                async with aiopen(config_path, "w") as f:
                    await f.write(cred_string)
    else:
        LOGGER.warning(
            f"gotohp-cli binary not found. Storing raw credential into {config_path}"
        )
        async with aiopen(config_path, "w") as f:
            await f.write(cred_string)

    update_user_ldata(user_id, "GOTOHP_CONFIG", config_path)
    await database.update_user_doc(user_id, "GOTOHP_CONFIG", config_path)
    await database.update_user_data(user_id)
    return config_path


async def prompt_gotohp_credentials(client, message, user_id: int) -> str:
    """
    Prompts the user interactively for their gotohp credential on first use,
    warns about risks, and saves to user's config file.
    """
    config_path = f"gotohp/{user_id}.config"
    if await aiopath.exists(config_path):
        async with aiopen(config_path, "r", errors="ignore") as f:
            content = await f.read()
            if content.strip():
                return config_path

    buttons = ButtonMaker()
    buttons.data_button("Cancel", f"gotohp_cancel {user_id}")
    prompt_text = (
        f"{GOTOHP_RISK_WARNING}\n\n"
        "<b>Action Required:</b> Please reply to this message with your Google Photos (gotohp) credential:\n"
        "• <code>oauth_token</code> cookie value\n"
        "• Or raw mobile credential / Android ID string (e.g. <code>androidId=...</code>)\n"
        "• Or upload a text file containing your credential.\n\n"
        "<i>Timeout: 60 seconds (Send /cancel or click Cancel below to abort)</i>"
    )

    prompt_msg = await send_message(message, prompt_text, buttons.build_menu(1))

    event = Event()
    result = {"status": None, "data": None}

    async def event_filter(_, __, event_msg):
        u = event_msg.from_user or event_msg.sender_chat
        return bool(
            u
            and u.id == user_id
            and event_msg.chat.id == message.chat.id
            and (event_msg.text or event_msg.document)
        )

    async def handle_message(_, event_msg):
        if event_msg.text:
            txt = event_msg.text.strip()
            if txt.lower() == "/cancel":
                result["status"] = "cancelled"
            else:
                result["status"] = "ok"
                result["data"] = txt
        elif event_msg.document:
            doc_path = await event_msg.download()
            try:
                async with aiopen(doc_path, "r", errors="ignore") as f:
                    result["data"] = (await f.read()).strip()
                result["status"] = "ok"
            finally:
                if await aiopath.exists(doc_path):
                    await remove(doc_path)
        try:
            await delete_message(event_msg)
        except Exception:
            pass
        event.set()

    async def handle_callback(_, query):
        if query.from_user.id != user_id:
            await query.answer("Not Yours!", show_alert=True)
            return
        await query.answer()
        result["status"] = "cancelled"
        event.set()

    msg_handler = client.add_handler(
        MessageHandler(handle_message, filters=create(event_filter)),
        group=-1,
    )
    cb_handler = client.add_handler(
        CallbackQueryHandler(
            handle_callback, filters=regex(f"^gotohp_cancel {user_id}")
        ),
        group=-1,
    )

    try:
        await wait_for(event.wait(), timeout=60)
    except TimeoutError:
        result["status"] = "timeout"
    finally:
        try:
            client.remove_handler(*msg_handler)
            client.remove_handler(*cb_handler)
        except Exception:
            pass
        try:
            await delete_message(prompt_msg)
        except Exception:
            pass

    if result["status"] == "cancelled":
        raise ValueError("Task cancelled by user during Google Photos credential setup.")
    elif result["status"] == "timeout":
        raise ValueError("Timed out waiting for Google Photos credentials!")
    elif result["status"] == "ok" and result["data"]:
        saved_path = await add_gotohp_credential(user_id, result["data"])
        await send_message(
            message,
            "✅ <b>Google Photos credential saved to your config file successfully!</b>\nProceeding with task...",
        )
        return saved_path
    else:
        raise ValueError("No valid Google Photos credentials provided.")
