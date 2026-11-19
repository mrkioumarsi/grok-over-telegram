#!/usr/bin/env python3
"""GrokWire: Telegram bridge to Grok Build. Unknown people get no reply. Only the admin runs as root."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import pwd
import re
import shutil
import signal
import subprocess
import time
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

from telegram import (
    BotCommandScopeAllChatAdministrators,
    BotCommandScopeAllGroupChats,
    BotCommandScopeAllPrivateChats,
    BotCommandScopeChat,
    BotCommandScopeDefault,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    MenuButtonDefault,
    Update,
)
from telegram.error import BadRequest, Forbidden, RetryAfter, TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

log = logging.getLogger("grokwire")

ROOT = Path("/opt/grokwire")
DATA = ROOT / "data"
ACCESS_PATH = DATA / "access.json"
SESSIONS_PATH = DATA / "sessions.json"
WWW = Path("/var/www/workspaces")
BOX_ROOT = Path("/var/lib/grokwire")
BOX_GROUP = "grokwire"
ADMIN_ID = int(os.environ["ADMIN_ID"])
GROK_BIN = os.environ.get("GROK_BIN", "/usr/local/bin/grok")
MAX_RUN_SECONDS = int(os.environ.get("GROK_MAX_SECONDS", "1200"))
MODELS = (
    ("grok-4.7", "Grok 4.7"),
    ("grok-4.7-build-fast", "Grok 4.7 Fast"),
    ("grok-4.6", "Grok 4.6"),
    ("grok-4.5", "Grok 4.5"),
)
MODEL_IDS = {item[0] for item in MODELS}
DEFAULT_MODEL = "grok-4.7"
NEW_USER_MESSAGES = 20
NEW_USER_TOKENS = 200_000

RULES_ADMIN = (
    "The user is the server owner, chatting through Telegram. "
    "Reply in English, in short phone-readable paragraphs. "
    "Do not print /opt/grokwire/.env, Telegram bot tokens, or /root/.grok/auth.json. "
    "Do not stop, restart, or reconfigure x-ui, daberna-web, or nginx unless the user explicitly asks. "
    "Do not print root passwords or private keys."
)
RULES_USER = (
    "You are assisting one Telegram user inside their private workspace. "
    "Reply in English, in short phone-readable paragraphs. "
    "Stay inside the working directory. Do not try to read or change the operating system, "
    "other workspaces, /root, /opt, or service configuration. "
    "Do not print tokens, passwords, private keys, or auth files."
)

RUNNING: dict[int, asyncio.subprocess.Process] = {}
JOBS = DATA / "jobs"
WATCHING: set[int] = set()
CHAT_LOCKS: dict[int, asyncio.Lock] = {}
WORLD = asyncio.Semaphore(1)
PENDING: dict[int, str] = {}
NOTIFIED: dict[str, float] = {}
DIGIT_MAP = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")
GROK_SESSIONS = Path("/root/.grok/sessions")
SESSION_CAP = 8
SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_-]{8,80}$")
PICKS: dict[str, list[str]] = {}


def chat_lock(cid: int) -> asyncio.Lock:
    lock = CHAT_LOCKS.get(cid)
    if lock is None:
        lock = asyncio.Lock()
        CHAT_LOCKS[cid] = lock
    return lock


def load_json(path: Path, default: dict) -> dict:
    if not path.exists():
        return default
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        log.exception("bad json %s", path)
        return default
    return data if isinstance(data, dict) else default


def save_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def workspace_for(uid: str) -> Path:
    safe = "".join(ch for ch in uid if ch.isdigit())
    if uid.startswith("-"):
        safe = "g" + safe
    return WWW / (safe or "user")


def box_ids(name: str) -> tuple[int, int] | None:
    try:
        user = pwd.getpwnam(name)
        return user.pw_uid, user.pw_gid
    except KeyError:
        return None


def ensure_box_account(folder: str) -> str | None:
    name = "gw" + folder
    if box_ids(name):
        return name
    group = subprocess.run(["getent", "group", BOX_GROUP], capture_output=True)
    if group.returncode != 0:
        return None
    made = subprocess.run(
        ["useradd", "--system", "--gid", BOX_GROUP, "--no-create-home", "--shell", "/usr/sbin/nologin", name],
        capture_output=True,
    )
    if made.returncode != 0 and not box_ids(name):
        return None
    return name


def give_box(path: Path, folder: str) -> None:
    name = ensure_box_account(folder)
    ids = box_ids(name) if name else None
    if ids and path.exists():
        os.chown(path, ids[0], ids[1])


def ensure_workspace(uid: str) -> str:
    path = workspace_for(uid)
    path.mkdir(parents=True, exist_ok=True)
    if str(uid) == str(ADMIN_ID):
        os.chmod(path, 0o700)
    else:
        give_box(path, path.name)
        os.chmod(path, 0o700)
    return str(path)


def model_title(model_id: str) -> str:
    for mid, title in MODELS:
        if mid == model_id:
            return title
    return model_id


def fmt_num(value: int) -> str:
    return f"{int(value):,}"


def cap_text(used: int, limit: int) -> str:
    if limit <= 0:
        return f"{fmt_num(used)} · unlimited"
    return f"{fmt_num(used)} of {fmt_num(limit)}"


def prepare_row(uid: str, row: dict) -> dict:
    admin = int(uid) == ADMIN_ID
    row["role"] = "admin" if admin else "user"
    if admin:
        row["enabled"] = True
    if row.get("label") in (None, "", "owner", uid) and admin:
        row["label"] = "Admin"
    row.setdefault("label", uid)
    allowed = [mid for mid in row.get("models") or [] if mid in MODEL_IDS]
    if not allowed:
        current = row.get("model") if row.get("model") in MODEL_IDS else DEFAULT_MODEL
        allowed = [current]
    row["models"] = allowed
    if row.get("model") not in allowed:
        row["model"] = allowed[0]
    if "message_limit" not in row:
        row["message_limit"] = 0 if admin else NEW_USER_MESSAGES
    if "token_limit" not in row:
        row["token_limit"] = 0 if admin else NEW_USER_TOKENS
    for key in ("message_limit", "token_limit", "messages_used", "tokens_used"):
        try:
            row[key] = max(0, int(row.get(key) or 0))
        except (TypeError, ValueError):
            row[key] = 0
    row["workspace"] = ensure_workspace(uid)
    return row


def access_data() -> dict:
    data = load_json(ACCESS_PATH, {"users": {}})
    users = data.setdefault("users", {})
    admin = str(ADMIN_ID)
    users[admin] = prepare_row(admin, users.get(admin) or {})
    for uid, row in list(users.items()):
        if isinstance(row, dict):
            users[uid] = prepare_row(uid, row)
    return data


def save_access(data: dict) -> None:
    save_json(ACCESS_PATH, data)


def sessions_data() -> dict:
    return load_json(SESSIONS_PATH, {"chats": {}})


def save_sessions(data: dict) -> None:
    save_json(SESSIONS_PATH, data)


def user_row(uid: int) -> dict | None:
    return access_data()["users"].get(str(uid))


def is_admin(uid: int | None) -> bool:
    return uid == ADMIN_ID


def is_enabled_id(value: int) -> bool:
    if value == ADMIN_ID:
        return True
    row = user_row(value)
    return bool(row and row.get("enabled") and row.get("role") != "admin")


def can_use(update: Update) -> bool:
    chat = update.effective_chat
    user = update.effective_user
    if chat is None or user is None:
        return False
    if chat.type == "private":
        return is_admin(user.id) or is_enabled_id(user.id)
    return is_enabled_id(chat.id)


def parse_limit(text: str) -> int | None:
    raw = (text or "").strip().translate(DIGIT_MAP).replace(",", "").replace("٬", "").replace(" ", "")
    if not raw.isdigit():
        return None
    value = int(raw)
    if value > 2_000_000_000:
        return None
    return value


def normalize_id(text: str) -> str | None:
    raw = (text or "").strip().translate(DIGIT_MAP).split()
    if not raw:
        return None
    token = raw[0]
    if len(token) >= 2 and token[0] in "+@" and token[1:].isdigit():
        token = token[1:]
    if token.startswith("-") and token[1:].isdigit() and 5 <= len(token) <= 20:
        return token
    if token.isdigit() and 5 <= len(token) <= 16:
        return token
    return None


def glass(text: str, data: str, style: str | None = None) -> InlineKeyboardButton:
    extra = {"api_kwargs": {"style": style}} if style else {}
    return InlineKeyboardButton(text, callback_data=data, **extra)


def pair_rows(buttons: list) -> list:
    return [buttons[index:index + 2] for index in range(0, len(buttons), 2)]


def panel_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        pair_rows(
            [
                glass("➕ Add", "a:add", "primary"),
                glass("👥 List", "a:list"),
                glass("↩️ Back", "m:home"),
            ]
        )
    )


def account_line(uid: str) -> str:
    row = access_data()["users"].get(uid) or {}
    return (
        f"🎛 Model: {model_title(row.get('model') or DEFAULT_MODEL)}\n"
        f"✉️ Messages: {cap_text(row.get('messages_used', 0), row.get('message_limit', 0))}\n"
        f"📊 Usage: {cap_text(row.get('tokens_used', 0), row.get('token_limit', 0))}"
    )


def menu_text(chat_id: str, uid: str, workspace: str) -> str:
    return (
        "✨ Just type. The answer continues this chat.\n\n"
        f"💬 Open chat\n{active_label(chat_id, workspace)}\n\n"
        "New topic? Tap New chat.\n"
        "📎 You can also send a file, photo, video, or voice note."
    )


def main_menu(admin: bool, uid: str) -> InlineKeyboardMarkup:
    buttons = [
        glass("✨ New chat", "m:new", "primary"),
        glass("💬 Chats", "m:sess"),
        glass("📋 Status", "m:stat"),
        glass("⏹ Stop", "m:stop"),
    ]
    if len((access_data()["users"].get(uid) or {}).get("models") or []) > 1:
        buttons.append(glass("🎛 Model", "m:mods"))
    if admin:
        buttons.append(glass("👥 Users", "m:acc"))
    return InlineKeyboardMarkup(pair_rows(buttons))


def menu_markup(update: Update) -> InlineKeyboardMarkup:
    admin = bool(
        update.effective_user
        and is_admin(update.effective_user.id)
        and update.effective_chat
        and update.effective_chat.type == "private"
    )
    return main_menu(admin, subject_id(update))


async def reply(update: Update, text: str, markup=None) -> None:
    if update.message:
        await update.message.reply_text(text[:4000], reply_markup=markup)
    elif update.callback_query and update.callback_query.message:
        await update.callback_query.message.reply_text(text[:4000], reply_markup=markup)


async def deny(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """People the admin did not enable get no chat reply at all."""
    query = update.callback_query
    if query is not None:
        try:
            await query.answer()
        except (BadRequest, Forbidden):
            pass
    who = update.effective_user.id if update.effective_user else 0
    log.info("ignored telegram user %s", who)


def remember_label(user) -> None:
    if user is None:
        return
    data = access_data()
    row = data["users"].get(str(user.id))
    if not row:
        return
    label = ("@" + user.username) if user.username else (user.full_name or "")
    if label and row.get("label") != label and row.get("role") != "admin":
        row["label"] = label[:80]
        save_access(data)


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    PENDING.pop(update.effective_user.id, None)
    if not can_use(update):
        await deny(update, context)
        return
    remember_label(update.effective_user)
    await reply(update, *menu_payload(update))


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await cmd_start(update, context)


async def cmd_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await cmd_start(update, context)


async def cmd_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    chat = update.effective_chat
    await reply(update, f"🆔 Your Telegram id\n{user.id}\n\nThis chat\n{chat.id}", menu_markup(update))


async def cmd_access(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update.effective_user.id):
        await reply(update, "Only the admin can open this.")
        return
    if update.effective_chat.type != "private":
        await reply(update, "Open Users from your private chat with the bot.")
        return
    PENDING.pop(update.effective_user.id, None)
    await reply(
        update,
        "👥 Users\n\nTurn access on, choose a model, and set the message and usage caps.",
        panel_keyboard(),
    )


def set_user(uid: str, enabled: bool, label: str = "") -> str:
    if int(uid) == ADMIN_ID:
        return "The main admin stays enabled."
    data = access_data()
    row = data["users"].get(uid) or {}
    row["role"] = "user"
    row["enabled"] = enabled
    row.setdefault("added", int(time.time()))
    if label:
        row["label"] = label[:80]
    data["users"][uid] = prepare_row(uid, row)
    save_access(data)
    bind_session(uid, data["users"][uid]["workspace"], reset=False)
    state = "open" if enabled else "closed"
    return f"Access for {uid} is {state}."


def remove_user(uid: str) -> str:
    if int(uid) == ADMIN_ID:
        return "The main admin cannot be removed."
    data = access_data()
    if uid not in data["users"]:
        return "That person is not on the list."
    data["users"].pop(uid, None)
    save_access(data)
    sessions = sessions_data()
    sessions.get("chats", {}).pop(uid, None)
    save_sessions(sessions)
    return f"Access for {uid} was removed. Files in the workspace were kept."


def card_text(uid: str) -> str:
    row = access_data()["users"].get(uid)
    if not row:
        return "That person is not on the list."
    state = "open" if row.get("enabled") else "closed"
    role = "admin" if row.get("role") == "admin" else "user"
    allowed = ", ".join(model_title(mid) for mid in row.get("models") or [])
    return (
        f"👤 {row.get('label') or uid}\n\n"
        f"🆔 Id: {uid}\n"
        f"Role: {role}\n"
        f"Access: {state}\n"
        f"🎛 Model: {model_title(row.get('model') or DEFAULT_MODEL)}\n"
        f"Allowed models: {allowed}\n"
        f"✉️ Messages: {cap_text(row.get('messages_used', 0), row.get('message_limit', 0))}\n"
        f"📊 Usage: {cap_text(row.get('tokens_used', 0), row.get('token_limit', 0))}\n"
        f"📁 Workspace:\n{row.get('workspace')}"
    )


def card_keyboard(uid: str) -> InlineKeyboardMarkup:
    row = access_data()["users"].get(uid) or {}
    admin = row.get("role") == "admin"
    buttons = [
        glass("💬 Chats", f"v:{uid}", "primary"),
        glass("🎛 Models", f"mm:{uid}"),
        glass("✉️ Messages", f"lm:{uid}"),
        glass("📊 Usage", f"lt:{uid}"),
        glass("🔄 Reset", f"rs:{uid}"),
    ]
    if not admin:
        toggle = "🔓 Open" if not row.get("enabled") else "🔒 Close"
        action = "on" if not row.get("enabled") else "off"
        buttons.append(glass(toggle, f"u:{uid}:{action}"))
        buttons.append(glass("🗑 Remove", f"u:{uid}:delask", "danger"))
    buttons.append(glass("↩️ Back", "a:list"))
    return InlineKeyboardMarkup(pair_rows(buttons))


def list_keyboard() -> InlineKeyboardMarkup:
    users = access_data()["users"]
    rows = []
    ordered = sorted(users.items(), key=lambda item: (item[1].get("role") != "admin", item[0]))
    for uid, row in ordered:
        state = "🟢" if row.get("enabled") else "🔴"
        label = clip(row.get("label") or uid, 14)
        rows.append(glass(f"{state} {label}"[:22], f"c:{uid}"))
    body = pair_rows(rows[:28])
    body.append([
        glass("➕ Add", "a:add", "primary"),
        glass("↩️ Back", "m:home"),
    ])
    return InlineKeyboardMarkup(body)


def model_keyboard(uid: str) -> InlineKeyboardMarkup:
    allowed = set((access_data()["users"].get(uid) or {}).get("models") or [])
    buttons = []
    for index, (mid, title) in enumerate(MODELS):
        label = f"✅ {title}" if mid in allowed else f"⚪ {title}"
        buttons.append(glass(label, f"mo:{uid}:{index}", "success" if mid in allowed else None))
    buttons.append(glass("↩️ Back", f"c:{uid}"))
    return InlineKeyboardMarkup(pair_rows(buttons))


def user_model_choices(uid: str) -> InlineKeyboardMarkup:
    row = access_data()["users"].get(uid) or {}
    active = row.get("model")
    buttons = []
    allowed = set(row.get("models") or [])
    for index, (mid, title) in enumerate(MODELS):
        if mid not in allowed:
            continue
        label = f"✅ {title}" if mid == active else title
        buttons.append(glass(label, f"us:{index}", "success" if mid == active else None))
    buttons.append(glass("↩️ Back", "m:home"))
    return InlineKeyboardMarkup(pair_rows(buttons))


def quota_block(row: dict | None) -> str | None:
    if not row or not row.get("enabled"):
        return "Your access is off."
    messages = int(row.get("messages_used") or 0)
    tokens = int(row.get("tokens_used") or 0)
    message_limit = int(row.get("message_limit") or 0)
    token_limit = int(row.get("token_limit") or 0)
    if message_limit and messages >= message_limit:
        return "You have used all of your messages.\nAsk the admin to continue."
    if token_limit and tokens >= token_limit:
        return "You have reached your usage cap.\nAsk the admin to continue."
    return None


def charge(uid: str, tokens: int) -> None:
    data = access_data()
    row = data["users"].get(uid)
    if not row:
        return
    row["messages_used"] = int(row.get("messages_used") or 0) + 1
    row["tokens_used"] = int(row.get("tokens_used") or 0) + max(0, int(tokens))
    save_access(data)


def set_limit(uid: str, field: str, value: int) -> str:
    data = access_data()
    row = data["users"].get(uid)
    if not row:
        return "That person is not on the list."
    row[field] = value
    save_access(data)
    title = "Message cap" if field == "message_limit" else "Usage cap"
    shown = "unlimited" if value == 0 else fmt_num(value)
    return f"{title} for this person: {shown}"


def reset_usage(uid: str) -> str:
    data = access_data()
    row = data["users"].get(uid)
    if not row:
        return "That person is not on the list."
    row["messages_used"] = 0
    row["tokens_used"] = 0
    save_access(data)
    return "Messages and usage for this person were reset."


def toggle_model(uid: str, index: int) -> str:
    if index < 0 or index >= len(MODELS):
        return "That model is not valid."
    mid = MODELS[index][0]
    data = access_data()
    row = data["users"].get(uid)
    if not row:
        return "That person is not on the list."
    allowed = list(row.get("models") or [])
    if mid in allowed:
        if len(allowed) == 1:
            return "At least one model must stay on."
        allowed = [item for item in allowed if item != mid]
    else:
        allowed.append(mid)
    row["models"] = allowed
    if row.get("model") not in allowed:
        row["model"] = allowed[0]
    save_access(data)
    return "Allowed models were updated."


def set_active_model(uid: str, index: int) -> str:
    if index < 0 or index >= len(MODELS):
        return "That model is not valid."
    mid = MODELS[index][0]
    data = access_data()
    row = data["users"].get(uid)
    if not row or mid not in (row.get("models") or []):
        return "That model is not open for you."
    row["model"] = mid
    save_access(data)
    return f"🎛 Answer model: {model_title(mid)}"


def bind_session(chat_id: str, workspace: str, reset: bool) -> None:
    sessions = sessions_data()
    row = sessions.setdefault("chats", {}).setdefault(str(chat_id), {})
    if row.get("cwd") != workspace:
        row.pop("session_id", None)
    elif reset:
        row.pop("session_id", None)
    row["cwd"] = workspace
    row.pop("cwd_override", None)
    save_sessions(sessions)


def subject_id(update: Update) -> str:
    chat = update.effective_chat
    user = update.effective_user
    if chat is not None and chat.type != "private":
        return str(chat.id)
    return str(user.id if user else chat.id)


def run_workspace(update: Update) -> str:
    uid = subject_id(update)
    workspace = ensure_workspace(uid)
    if is_admin(update.effective_user.id if update.effective_user else None):
        sessions = sessions_data().get("chats", {}).get(str(update.effective_chat.id), {})
        if sessions.get("cwd_override") and sessions.get("cwd"):
            return sessions["cwd"]
        return workspace
    path = Path(workspace).resolve()
    root = WWW.resolve()
    if path != root and root not in path.parents:
        return str(root / "invalid")
    return str(path)


def admin_only(update: Update) -> bool:
    return bool(update.effective_user and is_admin(update.effective_user.id))


async def show_card(message, uid: str, note: str = "") -> None:
    if note:
        await message.reply_text(note)
    if uid in access_data()["users"]:
        await message.reply_text(card_text(uid), reply_markup=card_keyboard(uid))


async def cmd_allow(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not admin_only(update):
        await reply(update, "Only the admin can open this.")
        return
    if not context.args:
        await reply(update, "Example: /allow 123456789")
        return
    uid = normalize_id(context.args[0])
    if not uid:
        await reply(update, "Send a numeric id.")
        return
    label = " ".join(context.args[1:])[:80]
    await show_card(update.message, uid, set_user(uid, True, label))


async def cmd_disable(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not admin_only(update):
        await reply(update, "Only the admin can open this.")
        return
    if not context.args:
        await reply(update, "Example: /disable 123456789")
        return
    uid = normalize_id(context.args[0])
    if not uid or uid not in access_data()["users"]:
        await reply(update, "That id is not on the list.")
        return
    await show_card(update.message, uid, set_user(uid, False))


async def cmd_enable(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not admin_only(update):
        await reply(update, "Only the admin can open this.")
        return
    if not context.args:
        await reply(update, "Example: /enable 123456789")
        return
    uid = normalize_id(context.args[0])
    if not uid or uid not in access_data()["users"]:
        await reply(update, "That id is not on the list.")
        return
    await show_card(update.message, uid, set_user(uid, True))


async def cmd_remove(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not admin_only(update):
        await reply(update, "Only the admin can open this.")
        return
    if not context.args:
        await reply(update, "Example: /remove 123456789")
        return
    uid = normalize_id(context.args[0])
    if not uid:
        await reply(update, "Send a numeric id.")
        return
    await reply(update, remove_user(uid))


def clip(text: str, limit: int) -> str:
    clean = " ".join(str(text or "").split())
    if len(clean) <= limit:
        return clean or "Untitled chat"
    return clean[: limit - 1] + "…"


def rel_time(iso: str) -> str:
    raw = str(iso or "").strip()
    if not raw:
        return ""
    try:
        stamp = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return ""
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    minutes = int((datetime.now(timezone.utc) - stamp).total_seconds() // 60)
    if minutes < 1:
        return "just now"
    if minutes < 60:
        return f"{minutes}m ago"
    hours = minutes // 60
    if hours < 24:
        return f"{hours}h ago"
    return f"{hours // 24}d ago"


def group_cwd(group: Path) -> str:
    marker = group / ".cwd"
    if marker.is_file():
        try:
            return marker.read_text(encoding="utf-8").strip()
        except OSError:
            return ""
    return urllib.parse.unquote(group.name)


def session_roots(workspace: str) -> list[Path]:
    roots = []
    if GROK_SESSIONS.is_dir():
        roots.append(GROK_SESSIONS)
    folder = Path(workspace).name
    extra = BOX_ROOT / "homes" / folder / ".grok" / "sessions"
    if extra.is_dir():
        roots.append(extra)
    return roots


def list_workspace_sessions(workspace: str) -> list[dict]:
    target = os.path.normpath(workspace)
    found = []
    seen = set()
    for root in session_roots(workspace):
        try:
            groups = list(root.iterdir())
        except OSError:
            continue
        found.extend(sessions_in(groups, target, seen))
    found.sort(key=lambda item: item["updated"], reverse=True)
    return found


def sessions_in(groups: list[Path], target: str, seen: set) -> list[dict]:
    found = []
    for group in groups:
        if not group.is_dir():
            continue
        if os.path.normpath(group_cwd(group)) != target:
            continue
        try:
            children = list(group.iterdir())
        except OSError:
            continue
        for sess in children:
            if not sess.is_dir() or not SESSION_ID_RE.fullmatch(sess.name):
                continue
            summary = sess / "summary.json"
            if not summary.is_file():
                continue
            try:
                meta = json.loads(summary.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(meta, dict):
                continue
            info = meta.get("info") if isinstance(meta.get("info"), dict) else {}
            title = (
                meta.get("title")
                or info.get("title")
                or meta.get("generated_title")
                or meta.get("session_summary")
                or "Untitled chat"
            )
            if sess.name in seen:
                continue
            seen.add(sess.name)
            found.append(
                {
                    "id": sess.name,
                    "title": clip(title, 80),
                    "updated": str(meta.get("updated_at") or meta.get("last_active_at") or ""),
                    "path": sess,
                }
            )
    return found


def remember_picks(scope: str, viewer: int, target: str, items: list[dict]) -> None:
    PICKS[f"{scope}:{viewer}:{target}"] = [item["id"] for item in items]


def resolve_pick(scope: str, viewer: int, target: str, workspace: str, index: int) -> dict | None:
    items = list_workspace_sessions(workspace)
    cached = PICKS.get(f"{scope}:{viewer}:{target}") or []
    if 0 <= index < len(cached):
        wanted = cached[index]
        for item in items:
            if item["id"] == wanted:
                return item
        return None
    shown = items[:SESSION_CAP]
    if 0 <= index < len(shown):
        remember_picks(scope, viewer, target, shown)
        return shown[index]
    return None


def find_session(workspace: str, session_id: str) -> dict | None:
    if not SESSION_ID_RE.fullmatch(session_id or ""):
        return None
    for item in list_workspace_sessions(workspace):
        if item["id"] == session_id:
            return item
    return None


def active_id(chat_id: str) -> str:
    row = sessions_data().get("chats", {}).get(str(chat_id)) or {}
    return str(row.get("session_id") or "")


def active_label(chat_id: str, workspace: str) -> str:
    sid = active_id(chat_id)
    if not sid:
        return "Not started yet"
    item = find_session(workspace, sid)
    if not item:
        return "Chat was not found"
    return item["title"]


def detach_session(chat_id: str) -> bool:
    sessions = sessions_data()
    row = sessions.setdefault("chats", {}).setdefault(str(chat_id), {})
    had = bool(row.get("session_id"))
    row.pop("session_id", None)
    save_sessions(sessions)
    return had


def activate_session(chat_id: str, workspace: str, session_id: str, assigned: str) -> None:
    sessions = sessions_data()
    row = sessions.setdefault("chats", {}).setdefault(str(chat_id), {})
    row["cwd"] = workspace
    row["session_id"] = session_id
    if os.path.normpath(workspace) == os.path.normpath(assigned):
        row.pop("cwd_override", None)
    else:
        row["cwd_override"] = True
    save_sessions(sessions)


def forget_session_id(session_id: str) -> None:
    data = sessions_data()
    changed = False
    for row in data.get("chats", {}).values():
        if isinstance(row, dict) and row.get("session_id") == session_id:
            row.pop("session_id", None)
            changed = True
    if changed:
        save_sessions(data)


def safe_session_dir(workspace: str, session_id: str) -> Path | None:
    item = find_session(workspace, session_id)
    if not item:
        return None
    path = item["path"].resolve()
    roots = {root.resolve() for root in session_roots(workspace)}
    if path.parent.parent not in roots or path.name != session_id:
        return None
    return path


def delete_session_dir(workspace: str, session_id: str) -> bool:
    path = safe_session_dir(workspace, session_id)
    if path is None:
        return False
    shutil.rmtree(path)
    forget_session_id(session_id)
    return True


def job_token(cid: int) -> str:
    return str(cid).replace("-", "n")


def job_dir(cid: int) -> Path:
    return JOBS / job_token(cid)


def unit_for(cid: int) -> str:
    return f"grokwire-job-{job_token(cid)}"


def unit_state(unit: str) -> str:
    try:
        result = subprocess.run(
            ["systemctl", "show", unit, "-p", "ActiveState", "--value"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return result.stdout.strip()


def unit_active(unit: str) -> bool:
    return unit_state(unit) == "active"


def job_open(cid: int) -> bool:
    folder = job_dir(cid)
    if (folder / "stopped").is_file():
        return False
    state = load_json(folder / "state.json", {}) if (folder / "state.json").is_file() else {}
    if state and not state.get("delivered") and not state.get("stopped"):
        return True
    return unit_active(unit_for(cid))


def chat_busy(chat_id: str) -> bool:
    try:
        cid = int(chat_id)
    except ValueError:
        return False
    if cid in WATCHING:
        return True
    return job_open(cid)


def another_job_running(cid: int) -> bool:
    if not JOBS.is_dir():
        return False
    for path in JOBS.glob("*/state.json"):
        state = load_json(path, {})
        try:
            other = int(state.get("chat_id"))
        except (TypeError, ValueError):
            continue
        if other == cid or not state or state.get("delivered") or state.get("stopped"):
            continue
        if (path.parent / "stopped").is_file():
            continue
        return True
    return False


def stop_job(cid: int) -> bool:
    folder = job_dir(cid)
    active = cid in WATCHING or unit_active(unit_for(cid)) or (
        (folder / "state.json").is_file() and not load_json(folder / "state.json", {}).get("delivered")
    )
    if not active:
        return False
    folder.mkdir(parents=True, exist_ok=True)
    os.chmod(folder, 0o700)
    (folder / "stopped").write_text("1\n", encoding="utf-8")
    os.chmod(folder / "stopped", 0o600)
    state = load_json(folder / "state.json", {})
    if state:
        state["stopped"] = True
        state["delivered"] = True
        save_json(folder / "state.json", state)
    try:
        subprocess.run(
            ["systemctl", "stop", unit_for(cid)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=25,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        log.exception("stop failed chat %s", cid)
    proc = RUNNING.pop(cid, None)
    if proc is not None and proc.returncode is None:
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError, OSError):
            proc.terminate()
    return True


def assigned_workspace(uid: str) -> str:
    row = access_data()["users"].get(uid) or {}
    return row.get("workspace") or ensure_workspace(uid)


def button_title(item: dict, active: bool = False) -> str:
    title = clip(item["title"], 12 if active else 16)
    if active:
        return f"🟢 {title}"[:18]
    return title[:18]


def confirm_keyboard(yes_data: str, no_data: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[
            glass("🗑 Delete", yes_data, "danger"),
            glass("↩️ No", no_data),
        ]]
    )


def session_screen(scope: str, viewer: int, target: str, workspace: str, back: str, prefix: str, note: str = "") -> tuple[str, InlineKeyboardMarkup]:
    items = list_workspace_sessions(workspace)[:SESSION_CAP]
    remember_picks(scope, viewer, target, items)
    current = active_id(target)
    lines = ["💬 Earlier chats"]
    if note:
        lines.append(note)
    lines.append(f"Open chat\n{active_label(target, workspace)}")
    if items:
        lines.append("Tap a name to resume it or delete it.")
    else:
        lines.append("No saved chats yet. The first message starts one.")
    buttons = []
    for index, item in enumerate(items):
        active = item["id"] == current
        buttons.append(glass(button_title(item, active), f"{prefix}:i:{index}", "success" if active else None))
    rows = pair_rows(buttons)
    fresh = "m:new" if prefix == "s" else f"{prefix}:n"
    rows.append([
        glass("✨ New", fresh, "primary"),
        glass("↩️ Back", back),
    ])
    return "\n".join(lines), InlineKeyboardMarkup(rows)


def detail_screen(item: dict, active: bool, prefix: str, index: int, back: str) -> tuple[str, InlineKeyboardMarkup]:
    state = "🟢 This chat is open." if active else "💾 This chat is saved."
    when = rel_time(item["updated"]) or "unknown time"
    text = f"💬 {item['title']}\n\n{state}\n🕐 Last time: {when}"
    rows = [
        [
            glass("▶️ Resume", f"{prefix}:r:{index}", "success"),
            glass("↩️ Back", back),
        ],
        [glass("🗑 Delete this chat", f"{prefix}:d:{index}", "danger")],
    ]
    return text, InlineKeyboardMarkup(rows)


def menu_payload(update: Update) -> tuple[str, InlineKeyboardMarkup]:
    uid = subject_id(update)
    admin = bool(update.effective_user and is_admin(update.effective_user.id) and update.effective_chat.type == "private")
    workspace = run_workspace(update)
    return menu_text(str(update.effective_chat.id), uid, workspace), main_menu(admin, uid)


async def show_screen(query, text: str, markup) -> None:
    try:
        await query.edit_message_text(text[:4000], reply_markup=markup)
    except BadRequest as exc:
        if "not modified" in str(exc).lower():
            return
        await query.message.reply_text(text[:4000], reply_markup=markup)


def status_body(update: Update) -> str:
    chat_id = str(update.effective_chat.id)
    workspace = run_workspace(update)
    busy = "Yes, writing an answer" if chat_busy(chat_id) or chat_lock(update.effective_chat.id).locked() else "No"
    return (
        "📋 Your status\n\n"
        f"💬 Open chat\n{active_label(chat_id, workspace)}\n\n"
        f"{account_line(subject_id(update))}\n\n"
        f"Writing now: {busy}"
    )


async def render_own(query, update: Update, note: str = "") -> None:
    chat_id = str(update.effective_chat.id)
    workspace = run_workspace(update)
    text, markup = session_screen(
        "own", update.effective_user.id, chat_id, workspace, "m:home", "s", note
    )
    await show_screen(query, text, markup)


async def render_card_sessions(query, viewer: int, uid: str, note: str = "") -> None:
    workspace = assigned_workspace(uid)
    label = (access_data()["users"].get(uid) or {}).get("label") or uid
    extra = f"{label}\n{note}" if note else label
    text, markup = session_screen("card", viewer, uid, workspace, f"v:{uid}:b", f"v:{uid}", extra)
    await show_screen(query, text, markup)


async def handle_menu(query, update: Update, action: str) -> None:
    chat = update.effective_chat
    user = update.effective_user
    admin = bool(user and is_admin(user.id) and chat.type == "private")
    uid = subject_id(update)
    if action == "new":
        if chat_busy(str(chat.id)):
            text, markup = menu_payload(update)
            await show_screen(query, "⏳ Still writing the previous answer.\nTap Stop if you need to.\n\n" + text, markup)
            return
        bind_session(str(chat.id), assigned_workspace(uid), True)
        text, markup = menu_payload(update)
        await show_screen(
            query,
            "✨ A new chat is ready.\nOlder chats are still saved. The next message starts fresh.\n\n" + text,
            markup,
        )
        return
    if action == "sess":
        await render_own(query, update)
        return
    if action == "stat":
        await show_screen(query, status_body(update), menu_payload(update)[1])
        return
    if action == "stop":
        note = "⏹ Answer stopped." if stop_job(chat.id) else "⏹ Nothing is being written right now."
        text, markup = menu_payload(update)
        await show_screen(query, note + "\n\n" + text, markup)
        return
    if action == "mods":
        allowed = (access_data()["users"].get(uid) or {}).get("models") or []
        if len(allowed) < 2:
            text, markup = menu_payload(update)
            shown = model_title(allowed[0] if allowed else DEFAULT_MODEL)
            await show_screen(query, f"🎛 Your model: {shown}\n\n" + text, markup)
            return
        await show_screen(query, "🎛 Which model should answer?\nThe current one is marked ✅.", user_model_choices(uid))
        return
    if action == "acc":
        if not admin:
            await show_screen(query, "👥 Users opens only in the admin's private chat.", main_menu(False, uid))
            return
        await show_screen(
            query,
            "👥 Users\n\nTurn access on, choose a model, and set the message and usage caps.",
            panel_keyboard(),
        )
        return
    text, markup = menu_payload(update)
    await show_screen(query, text, markup)


async def handle_own(query, update: Update, parts: list[str]) -> None:
    action = parts[1] if len(parts) > 1 else "l"
    index = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else -1
    chat_id = str(update.effective_chat.id)
    viewer = update.effective_user.id
    workspace = run_workspace(update)
    assigned = assigned_workspace(subject_id(update))
    if action in {"r", "x", "y"} and chat_busy(chat_id):
        await render_own(query, update, "⏳ Still writing. Tap Stop first.")
        return
    if action == "i" and index >= 0:
        item = resolve_pick("own", viewer, chat_id, workspace, index)
        if not item:
            await render_own(query, update, "That chat is no longer in the list.")
            return
        text, markup = detail_screen(item, item["id"] == active_id(chat_id), "s", index, "s:l")
        await show_screen(query, text, markup)
        return
    if action == "r" and index >= 0:
        item = resolve_pick("own", viewer, chat_id, workspace, index)
        if not item:
            await render_own(query, update, "That chat is no longer in the list.")
            return
        activate_session(chat_id, workspace, item["id"], assigned)
        text, markup = menu_payload(update)
        await show_screen(
            query,
            f"✅ This chat is open:\n\n“{clip(item['title'], 80)}”\n\nSend your next message.\n\n" + text,
            markup,
        )
        return
    if action == "d" and index >= 0:
        item = resolve_pick("own", viewer, chat_id, workspace, index)
        if not item:
            await render_own(query, update, "That chat is no longer in the list.")
            return
        markup = confirm_keyboard(f"s:y:{index}", "s:l")
        await show_screen(
            query,
            f"🗑 Delete this chat?\n\n“{clip(item['title'], 80)}”\n\nThis cannot be undone.",
            markup,
        )
        return
    if action == "y" and index >= 0:
        item = resolve_pick("own", viewer, chat_id, workspace, index)
        if not item or not delete_session_dir(workspace, item["id"]):
            await render_own(query, update, "That chat was not found.")
            return
        await render_own(query, update, "🗑 Chat deleted.")
        return
    if action == "x":
        detach_session(chat_id)
        await render_own(query, update, "The open chat was set aside. Older chats were kept.")
        return
    await render_own(query, update)


async def handle_card_sessions(query, update: Update, parts: list[str]) -> None:
    uid = parts[1] if len(parts) > 1 else ""
    if not normalize_id(uid) or uid not in access_data()["users"]:
        await show_screen(query, "That person is not on the list.", panel_keyboard())
        return
    action = parts[2] if len(parts) > 2 else "l"
    index = int(parts[3]) if len(parts) > 3 and parts[3].isdigit() else -1
    viewer = update.effective_user.id
    workspace = assigned_workspace(uid)
    if action == "b":
        await show_screen(query, card_text(uid), card_keyboard(uid))
        return
    if action in {"r", "x", "n", "y"} and chat_busy(uid):
        await render_card_sessions(query, viewer, uid, "This person is still getting an answer.")
        return
    if action == "i" and index >= 0:
        item = resolve_pick("card", viewer, uid, workspace, index)
        if not item:
            await render_card_sessions(query, viewer, uid, "That chat is no longer in the list.")
            return
        text, markup = detail_screen(item, item["id"] == active_id(uid), f"v:{uid}", index, f"v:{uid}:l")
        await show_screen(query, text, markup)
        return
    if action == "r" and index >= 0:
        item = resolve_pick("card", viewer, uid, workspace, index)
        if not item:
            await render_card_sessions(query, viewer, uid, "That chat is no longer in the list.")
            return
        activate_session(uid, workspace, item["id"], workspace)
        await render_card_sessions(query, viewer, uid, f"Opened this chat for them:\n“{clip(item['title'], 60)}”")
        return
    if action == "n":
        bind_session(uid, workspace, True)
        await render_card_sessions(query, viewer, uid, "A new chat is ready for this person. Older chats were kept.")
        return
    if action == "x":
        detach_session(uid)
        await render_card_sessions(query, viewer, uid, "Their open chat was set aside. The file was kept.")
        return
    if action == "d" and index >= 0:
        item = resolve_pick("card", viewer, uid, workspace, index)
        if not item:
            await render_card_sessions(query, viewer, uid, "That chat is no longer in the list.")
            return
        markup = confirm_keyboard(f"v:{uid}:y:{index}", f"v:{uid}:l")
        await show_screen(
            query,
            f"🗑 Delete this chat?\n\n“{clip(item['title'], 80)}”\n\nThis cannot be undone.",
            markup,
        )
        return
    if action == "y" and index >= 0:
        item = resolve_pick("card", viewer, uid, workspace, index)
        if not item or not delete_session_dir(workspace, item["id"]):
            await render_card_sessions(query, viewer, uid, "That chat was not found.")
            return
        await render_card_sessions(query, viewer, uid, "🗑 Chat deleted.")
        return
    await render_card_sessions(query, viewer, uid)


async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    user = update.effective_user
    if query is None or user is None or query.message is None:
        return
    data = query.data or ""
    parts = data.split(":")
    kind = parts[0] if parts else ""
    if kind == "us":
        if not can_use(update):
            await query.answer()
            return
        await query.answer()
        uid = subject_id(update)
        if len(parts) == 2 and parts[1] == "menu":
            await show_screen(query, "🎛 Which model should answer?\nThe current one is marked ✅.", user_model_choices(uid))
            return
        if len(parts) == 2 and parts[1].isdigit():
            note = set_active_model(uid, int(parts[1]))
            text, markup = menu_payload(update)
            await show_screen(query, note + "\n\n" + text, markup)
        return
    if kind in {"m", "s"}:
        if not can_use(update):
            await query.answer()
            return
        await query.answer()
        if kind == "m":
            await handle_menu(query, update, parts[1] if len(parts) > 1 else "home")
        else:
            await handle_own(query, update, parts)
        return
    if not is_admin(user.id):
        await query.answer("Only the admin can open this.")
        return
    await query.answer()

    if kind == "v":
        if update.effective_chat.type != "private":
            await query.message.reply_text("Open a person's chats from your private chat.")
            return
        await handle_card_sessions(query, update, parts)
        return

    if kind == "n":
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except BadRequest:
            pass
        return

    if kind == "g" and len(parts) == 2:
        uid = normalize_id(parts[1])
        if not uid:
            await query.message.reply_text("That id is not valid.")
            return
        await show_card(query.message, uid, set_user(uid, True))
        return

    if kind == "a" and len(parts) == 2:
        action = parts[1]
        if action == "add":
            PENDING[user.id] = "add"
            await query.message.reply_text("Send the numeric Telegram id.\nTo cancel, send: cancel")
            return
        if action == "list":
            await query.message.reply_text("👥 These are the bot users.\nTap a name for details.", reply_markup=list_keyboard())
            return

    if kind == "c" and len(parts) == 2 and normalize_id(parts[1]):
        await query.message.reply_text(card_text(parts[1]), reply_markup=card_keyboard(parts[1]))
        return

    if kind == "mm" and len(parts) == 2 and normalize_id(parts[1]):
        await query.message.reply_text(
            "🎛 Models for this person\n\n✅ means they can use it.\nTap a model to turn it on or off.",
            reply_markup=model_keyboard(parts[1]),
        )
        return

    if kind == "mo" and len(parts) == 3 and normalize_id(parts[1]) and parts[2].isdigit():
        note = toggle_model(parts[1], int(parts[2]))
        try:
            await query.edit_message_reply_markup(reply_markup=model_keyboard(parts[1]))
        except BadRequest:
            pass
        if note != "Allowed models were updated.":
            await query.message.reply_text(note)
        return

    if kind == "lm" and len(parts) == 2 and normalize_id(parts[1]):
        PENDING[user.id] = f"ml:{parts[1]}"
        await query.message.reply_text("✉️ How many answers can this person get?\nSend a number. Zero means unlimited.\nTo cancel, send: cancel")
        return

    if kind == "lt" and len(parts) == 2 and normalize_id(parts[1]):
        PENDING[user.id] = f"tl:{parts[1]}"
        await query.message.reply_text("📊 What usage cap should this person have?\nSend a number. Zero means unlimited.\nTo cancel, send: cancel")
        return

    if kind == "rs" and len(parts) == 2 and normalize_id(parts[1]):
        await show_card(query.message, parts[1], reset_usage(parts[1]))
        return

    if kind == "u" and len(parts) == 3:
        uid, action = parts[1], parts[2]
        if not normalize_id(uid):
            await query.message.reply_text("That id is not valid.")
            return
        if action == "off":
            await show_card(query.message, uid, set_user(uid, False))
        elif action == "on":
            await show_card(query.message, uid, set_user(uid, True))
        elif action == "delask":
            markup = confirm_keyboard(f"u:{uid}:delok", f"c:{uid}")
            await query.message.reply_text("🗑 Remove this person's access?\nFiles in the workspace stay.", reply_markup=markup)
        elif action == "delok":
            await query.message.reply_text(remove_user(uid))
        return


async def cmd_new(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not can_use(update):
        await deny(update, context)
        return
    uid = subject_id(update)
    workspace = (access_data()["users"].get(uid) or {}).get("workspace") or ensure_workspace(uid)
    bind_session(str(update.effective_chat.id), workspace, reset=True)
    await reply(
        update,
        "✨ A new chat is ready.\nOlder chats are still saved. The next message starts fresh.",
        main_menu(is_admin(update.effective_user.id) and update.effective_chat.type == "private", subject_id(update)),
    )


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not can_use(update):
        await deny(update, context)
        return
    await reply(
        update,
        status_body(update),
        main_menu(is_admin(update.effective_user.id) and update.effective_chat.type == "private", subject_id(update)),
    )


async def cmd_model(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not can_use(update):
        await deny(update, context)
        return
    uid = subject_id(update)
    allowed = (access_data()["users"].get(uid) or {}).get("models") or []
    if len(allowed) < 2:
        await reply(update, f"🎛 Your model: {model_title(allowed[0] if allowed else DEFAULT_MODEL)}", menu_markup(update))
        return
    await reply(update, "🎛 Which model should answer?\nThe current one is marked ✅.", user_model_choices(uid))


async def cmd_cwd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not can_use(update):
        await deny(update, context)
        return
    uid = subject_id(update)
    workspace = (access_data()["users"].get(uid) or {}).get("workspace") or ensure_workspace(uid)
    if not is_admin(update.effective_user.id):
        await reply(update, "Your workspace is fixed.")
        return
    arg = " ".join(context.args).strip()
    if not arg:
        await reply(update, run_workspace(update))
        return
    if arg in {"reset", "home"}:
        bind_session(str(update.effective_chat.id), workspace, reset=True)
        await reply(update, f"Back in your workspace:\n{workspace}")
        return
    path = os.path.normpath(arg.translate(DIGIT_MAP))
    if not path.startswith("/") or "\x00" in path:
        await reply(update, "Send an absolute path. Example: /var/www/workspaces/174291028")
        return
    parent = Path(path).parent
    if not parent.is_dir():
        await reply(update, "The parent folder does not exist.")
        return
    try:
        Path(path).mkdir(parents=False, exist_ok=True)
    except OSError as exc:
        await reply(update, f"Could not create that path: {exc.strerror or exc}")
        return
    if not Path(path).is_dir():
        await reply(update, "That path is not a folder.")
        return
    sessions = sessions_data()
    row = sessions.setdefault("chats", {}).setdefault(str(update.effective_chat.id), {})
    if row.get("cwd") != path:
        row.pop("session_id", None)
    row["cwd"] = path
    row["cwd_override"] = path != workspace
    save_sessions(sessions)
    await reply(update, f"Working folder:\n{path}")


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id if update.effective_user else 0
    if uid in PENDING:
        PENDING.pop(uid, None)
        await reply(update, "Cancelled.")
        return
    if not can_use(update):
        await deny(update, context)
        return
    if not stop_job(update.effective_chat.id):
        await reply(update, "⏹ Nothing is being written right now.", menu_markup(update))
        return
    await reply(update, "⏹ Answer stopped.", menu_markup(update))


async def unknown_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not can_use(update):
        await deny(update, context)
        return
    admin = bool(update.effective_user and is_admin(update.effective_user.id) and update.effective_chat.type == "private")
    await reply(update, "That is not a command.\nUse the buttons below.", main_menu(admin, subject_id(update)))


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    message = update.message
    if user is None or message is None or not message.text:
        return
    mode = PENDING.get(user.id)
    if mode and is_admin(user.id):
        if (message.text or "").strip().lower() in {"cancel", "stop", "لغو", "انصراف"}:
            PENDING.pop(user.id, None)
            await message.reply_text("Cancelled.", reply_markup=panel_keyboard())
            return
        if mode == "add":
            uid = normalize_id(message.text)
            if not uid:
                await message.reply_text("Send the numeric Telegram id.\nTo cancel, send: cancel")
                return
            PENDING.pop(user.id, None)
            await show_card(message, uid, set_user(uid, True))
            return
        if mode.startswith(("ml:", "tl:")):
            field = "message_limit" if mode.startswith("ml:") else "token_limit"
            target = mode.split(":", 1)[1]
            value = parse_limit(message.text)
            if value is None:
                await message.reply_text("Send a number. Zero means unlimited.\nTo cancel, send: cancel")
                return
            PENDING.pop(user.id, None)
            await show_card(message, target, set_limit(target, field, value))
            return
    if not can_use(update):
        await deny(update, context)
        return
    remember_label(user)
    blocked = quota_block(access_data()["users"].get(subject_id(update)))
    if blocked:
        await message.reply_text(blocked, reply_markup=menu_markup(update))
        return
    await run_turn(update, message.text)


MAX_FILE_BYTES = 20 * 1024 * 1024


def picked_file(message):
    if message.document:
        return message.document, ""
    if message.video:
        return message.video, ".mp4"
    if message.animation:
        return message.animation, ".mp4"
    if message.audio:
        return message.audio, ".mp3"
    if message.voice:
        return message.voice, ".ogg"
    if message.video_note:
        return message.video_note, ".mp4"
    if message.photo:
        return message.photo[-1], ".jpg"
    return None, ""


def stored_name(raw: str, unique: str, suffix: str) -> str:
    base = Path(str(raw or "")).name.replace("\x00", "").replace("/", "_").replace("\\", "_").strip()
    if base in {"", ".", ".."}:
        base = f"file-{unique[:12]}{suffix}"
    if suffix and not Path(base).suffix:
        base += suffix
    if len(base) > 80:
        ext = Path(base).suffix[:12]
        base = Path(base).stem[:60] + ext
    return base


async def on_file(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message
    user = update.effective_user
    if message is None or user is None:
        return
    if PENDING.get(user.id) and is_admin(user.id):
        await message.reply_text("Send the text I asked for.\nTo cancel, send: cancel", reply_markup=panel_keyboard())
        return
    if not can_use(update):
        await deny(update, context)
        return
    remember_label(user)
    markup = menu_markup(update)
    item, suffix = picked_file(message)
    if item is None:
        await message.reply_text("📎 I cannot store that.\nSend a file, photo, video, or voice note.", reply_markup=markup)
        return
    size = int(getattr(item, "file_size", 0) or 0)
    if size > MAX_FILE_BYTES:
        await message.reply_text(
            "📦 This file is over 20 MB.\nTelegram will not hand it to the bot. Send a smaller one.",
            reply_markup=markup,
        )
        return
    workspace = run_workspace(update)
    folder = (Path(workspace) / "uploads").resolve()
    folder.mkdir(parents=True, exist_ok=True)
    if not is_admin(user.id):
        give_box(folder, Path(workspace).name)
        os.chmod(folder, 0o700)
    name = stored_name(getattr(item, "file_name", None) or "", item.file_unique_id, suffix)
    dest = (folder / name).resolve()
    if dest.parent != folder:
        await message.reply_text("📎 That file name is not allowed.", reply_markup=markup)
        return
    if dest.exists():
        stamp = datetime.now().strftime("%H%M%S")
        dest = folder / f"{dest.stem}-{stamp}{dest.suffix}"
        if dest.exists():
            dest = folder / f"{dest.stem}-{item.file_unique_id[:8]}{dest.suffix}"
    try:
        telegram_file = await context.bot.get_file(item.file_id)
        await telegram_file.download_to_drive(custom_path=str(dest))
        if not is_admin(user.id):
            give_box(dest, Path(workspace).name)
            os.chmod(dest, 0o640)
    except Exception as exc:
        log.info("upload failed: %s", type(exc).__name__)
        await message.reply_text(
            "📦 I could not fetch that file.\nIf it is over 20 MB, send a smaller one.",
            reply_markup=markup,
        )
        return
    caption = (message.caption or "").strip()
    if not caption:
        await message.reply_text(
            f"📎 Saved.\n{dest.name}\n\nTell me what to do with it.",
            reply_markup=markup,
        )
        return
    blocked = quota_block(access_data()["users"].get(subject_id(update)))
    if blocked:
        await message.reply_text(f"{blocked}\n\nFile saved: {dest.name}", reply_markup=markup)
        return
    prompt = (
        f"The user's file is saved at:\n{dest}\n"
        f"File name: {dest.name}\n"
        "Use that file if the request needs it. Do not mention the server path in the reply; use the file name.\n\n"
        f"User request:\n{caption}"
    )
    await run_turn(update, prompt)


async def on_other(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not can_use(update):
        await deny(update, context)
        return
    await reply(
        update,
        "📎 Send a file, photo, video, or voice note.\nYou can also just type here.",
        menu_markup(update),
    )


def absorb(acc: str, chunk: str) -> str:
    if not chunk:
        return acc
    if not acc:
        return chunk
    if chunk.startswith(acc):
        return chunk
    if acc.endswith(chunk):
        return acc
    return acc + chunk


def tool_hint(event: dict) -> str:
    name = str(event.get("toolName") or event.get("title") or "tool")
    raw = event.get("rawInput") or {}
    hint = ""
    if isinstance(raw, dict):
        for key in ("path", "file_path", "target_file", "command", "cmd", "query", "url", "pattern"):
            value = raw.get(key)
            if isinstance(value, str) and value.strip():
                hint = " ".join(value.split())[:80]
                break
    line = f"{name} {hint}".strip()
    return line[:120]


async def safe_edit(message, text: str, markup=None) -> None:
    body = text[:4000] or "…"
    extra = {"reply_markup": markup} if markup is not None else {}
    try:
        await message.edit_text(body, **extra)
    except RetryAfter as exc:
        await asyncio.sleep(float(exc.retry_after) + 0.2)
        try:
            await message.edit_text(body, **extra)
        except TelegramError:
            pass
    except TelegramError as exc:
        if "not modified" not in str(exc).lower():
            log.info("edit skipped")


async def push_chunks(bot, chat_id: int, reply_to: int, text: str, markup=None) -> None:
    body = text.strip()
    if not body:
        return
    pieces = []
    start = 0
    while start < len(body):
        piece = body[start:start + 3800]
        if start + 3800 < len(body):
            cut = piece.rfind("\n")
            if cut > 500:
                piece = piece[:cut]
        pieces.append(piece)
        start += len(piece)
    for index, piece in enumerate(pieces):
        last = index == len(pieces) - 1
        await bot.send_message(
            chat_id,
            piece,
            reply_to_message_id=reply_to if index == 0 else None,
            reply_markup=markup if last else None,
        )


async def stop_proc(proc: asyncio.subprocess.Process) -> None:
    if proc.returncode is not None:
        return
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        proc.terminate()
    try:
        await asyncio.wait_for(proc.wait(), timeout=8)
    except asyncio.TimeoutError:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            proc.kill()
        await proc.wait()


def grok_env() -> dict:
    env = os.environ.copy()
    for key in ("BOT_TOKEN", "ADMIN_ID"):
        env.pop(key, None)
    env["HOME"] = "/root"
    env["GROK_DISABLE_AUTOUPDATER"] = "1"
    env["RUST_LOG"] = "error"
    return env


def secret_needles() -> list[str]:
    found: list[str] = []
    token = os.environ.get("BOT_TOKEN", "")
    if len(token) >= 8:
        found.append(token)

    def walk(value) -> None:
        if isinstance(value, str) and len(value) >= 12:
            found.append(value)
        elif isinstance(value, dict):
            for item in value.values():
                walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)

    for path in (Path("/root/.grok/auth.json"), BOX_ROOT / "auth" / "auth.json"):
        try:
            walk(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            continue
    return found


def scrub(text: str) -> str:
    clean = text or ""
    for needle in secret_needles():
        if needle and needle in clean:
            clean = clean.replace(needle, "[private]")
    return clean


def prepare_box(folder: str) -> tuple[str, Path] | None:
    account = ensure_box_account(folder)
    if not account or not Path("/usr/bin/bwrap").is_file() or not Path("/usr/sbin/runuser").is_file():
        return None
    auth_src = Path("/root/.grok/auth.json")
    binary = Path(os.path.realpath(GROK_BIN))
    if not auth_src.is_file() or not binary.is_file():
        return None
    runtime = BOX_ROOT / "runtime"
    auth_dir = BOX_ROOT / "auth"
    home = BOX_ROOT / "homes" / folder
    resolv = BOX_ROOT / "resolv.conf"
    if not resolv.exists():
        BOX_ROOT.mkdir(parents=True, exist_ok=True)
        resolv.write_text("nameserver 1.1.1.1\nnameserver 8.8.8.8\n", encoding="utf-8")
    grok_home = home / ".grok"
    try:
        runtime.mkdir(parents=True, exist_ok=True)
        auth_dir.mkdir(parents=True, exist_ok=True)
        grok_home.mkdir(parents=True, exist_ok=True)
        target_bin = runtime / "grok"
        if not target_bin.is_file() or target_bin.stat().st_mtime < binary.stat().st_mtime:
            shutil.copy2(binary, target_bin)
        os.chmod(target_bin, 0o755)
        shutil.copy2(auth_src, auth_dir / "auth.json")
        os.chmod(auth_dir / "auth.json", 0o640)
        group = grp_gid()
        if group is not None:
            os.chown(auth_dir / "auth.json", 0, group)
            os.chown(auth_dir, 0, group)
        for path in (home, grok_home):
            give_box(path, folder)
            os.chmod(path, 0o700)
        for mount in (grok_home / "auth.json", home / "grok"):
            if not mount.exists():
                mount.touch()
            give_box(mount, folder)
        adopt_sessions(folder, home)
    except OSError:
        log.exception("sandbox setup failed")
        return None
    return account, home


def adopt_sessions(folder: str, home: Path) -> None:
    workspace = str(WWW / folder)
    encoded = urllib.parse.quote(os.path.normpath(workspace), safe="")
    src = GROK_SESSIONS / encoded
    dest_root = home / ".grok" / "sessions"
    dest = dest_root / encoded
    if not src.is_dir() or dest.exists():
        return
    dest_root.mkdir(parents=True, exist_ok=True)
    shutil.copytree(src, dest)
    ids = box_ids("gw" + folder)
    if not ids:
        return
    os.chown(dest_root, *ids)
    for path in [dest, *dest.rglob("*")]:
        os.chown(path, *ids)


def grp_gid() -> int | None:
    try:
        return grp_get(BOX_GROUP)
    except KeyError:
        return None


def grp_get(name: str) -> int:
    import grp
    return grp.getgrnam(name).gr_gid


def sandbox_argv(workspace: str, grok_cmd: list[str]) -> list[str] | None:
    folder = Path(workspace).name
    prepared = prepare_box(folder)
    if not prepared:
        return None
    account, home = prepared
    ws = str(Path(workspace).resolve())
    cmd = [
        "runuser", "-u", account, "--", "bwrap",
        "--unshare-all", "--share-net", "--die-with-parent",
        "--ro-bind", "/usr", "/usr",
        "--ro-bind", "/bin", "/bin",
        "--ro-bind", "/lib", "/lib",
    ]
    if Path("/lib64").exists():
        cmd += ["--ro-bind", "/lib64", "/lib64"]
    cmd += [
        "--tmpfs", "/etc",
        "--ro-bind", "/etc/hosts", "/etc/hosts",
        "--ro-bind", str(BOX_ROOT / "resolv.conf"), "/etc/resolv.conf",
        "--ro-bind", "/etc/ssl", "/etc/ssl",
    ]
    if Path("/etc/ca-certificates").is_dir():
        cmd += ["--ro-bind", "/etc/ca-certificates", "/etc/ca-certificates"]
    if Path("/etc/nsswitch.conf").is_file():
        cmd += ["--ro-bind", "/etc/nsswitch.conf", "/etc/nsswitch.conf"]
    cmd += [
        "--remount-ro", "/etc",
        "--proc", "/proc",
        "--dev", "/dev",
        "--tmpfs", "/tmp",
        "--tmpfs", "/run",
        "--tmpfs", "/root",
        "--bind", ws, ws,
        "--bind", str(home), "/home/grok",
        "--ro-bind", str(BOX_ROOT / "auth" / "auth.json"), "/home/grok/.grok/auth.json",
        "--ro-bind", str(BOX_ROOT / "runtime" / "grok"), "/home/grok/grok",
        "--chdir", ws,
        "--setenv", "HOME", "/home/grok",
        "--setenv", "PATH", "/usr/bin:/bin",
        "--setenv", "LANG", "C.UTF-8",
        "--setenv", "GROK_DISABLE_AUTOUPDATER", "1",
        "--setenv", "RUST_LOG", "error",
        "--",
        "/home/grok/grok",
        *grok_cmd[1:],
    ]
    return cmd


def session_missing(text: str) -> bool:
    low = text.lower()
    needles = (
        "session does not exist",
        "no session",
        "couldn't resume",
        "could not resume",
        "unknown session",
        "can't resume",
        "cannot resume",
    )
    return any(item in low for item in needles)


async def deliver(message, status, answer: str, markup) -> None:
    body = answer.strip()
    if not body:
        return
    if len(body) <= 4000:
        await safe_edit(status, body, markup)
        return
    await safe_edit(status, body[:3900])
    await push_chunks(message.get_bot(), message.chat_id, message.message_id, body[3900:], markup)


def clear_job_output(folder: Path) -> None:
    for name in ("stdout", "stderr", "exit", "stopped"):
        path = folder / name
        try:
            path.unlink()
        except OSError:
            pass


def launch_job(folder: Path, argv: list[str], cwd: str, env: dict) -> None:
    clear_job_output(folder)
    save_json(folder / "argv.json", {"argv": argv, "cwd": cwd, "env": env})
    state_path = folder / "state.json"
    state = load_json(state_path, {})
    if state.get("stopped"):
        state["stopped"] = False
        save_json(state_path, state)
    unit = unit_for(int(state["chat_id"]))
    subprocess.run(
        ["systemctl", "stop", unit],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=20,
        check=False,
    )
    subprocess.run(
        ["systemctl", "reset-failed", unit],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=10,
        check=False,
    )
    result = subprocess.run(
        [
            "systemd-run",
            f"--unit={unit}",
            "--collect",
            "--property=Slice=system.slice",
            f"--property=RuntimeMaxSec={MAX_RUN_SECONDS}",
            "--property=TimeoutStopSec=20",
            "--",
            "/usr/bin/python3",
            str(ROOT / "jobrun.py"),
            str(folder),
        ],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if result.returncode != 0:
        detail = scrub((result.stderr or result.stdout or "systemd-run failed")[-400:])
        log.error("job start failed: %s", detail)
        raise RuntimeError("job start failed")


def parse_line(raw: bytes) -> dict | None:
    text = raw.decode("utf-8", "replace").strip()
    if not text:
        return None
    try:
        event = json.loads(text)
    except json.JSONDecodeError:
        return None
    return event if isinstance(event, dict) else None


def fold_event(answer: str, session_id, err_line: str, spent: int, tools: list[str], event: dict):
    kind = event.get("type")
    if kind == "text":
        answer = absorb(answer, str(event.get("data") or ""))
    elif kind == "tool_call":
        hint = tool_hint(event)
        if not tools or tools[-1] != hint:
            tools.append(hint)
            tools = tools[-4:]
    elif kind == "error":
        err_line = str(event.get("message") or "Grok error")
    elif kind == "end":
        session_id = event.get("sessionId") or session_id
        usage = event.get("usage") if isinstance(event.get("usage"), dict) else {}
        total = int(usage.get("total_tokens") or 0)
        if total <= 0:
            total = sum(int(usage.get(key) or 0) for key in (
                "input_tokens",
                "output_tokens",
                "cache_read_input_tokens",
                "cache_creation_input_tokens",
            ))
        spent = max(spent, total)
        if event.get("stopReason") not in (None, "end_turn", "max_tokens"):
            err_line = err_line or f"Stopped: {event.get('stopReason')}"
    return answer, session_id, err_line, spent, tools


def read_exit(folder: Path) -> int | None:
    path = folder / "exit"
    if not path.is_file():
        return None
    try:
        return int(path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


async def edit_or_send(bot, chat_id: int, status_id: int, text: str, markup=None) -> int | None:
    body = (text or "…")[:4000]
    extra = {"reply_markup": markup} if markup is not None else {}
    if status_id:
        try:
            await bot.edit_message_text(chat_id=chat_id, message_id=status_id, text=body, **extra)
            return status_id
        except RetryAfter as exc:
            await asyncio.sleep(float(exc.retry_after) + 0.2)
            try:
                await bot.edit_message_text(chat_id=chat_id, message_id=status_id, text=body, **extra)
                return status_id
            except TelegramError:
                pass
        except BadRequest as exc:
            if "not modified" in str(exc).lower():
                return status_id
        except TelegramError:
            pass
    try:
        sent = await bot.send_message(chat_id, body, **extra)
        return sent.message_id
    except TelegramError:
        return None


def remember_status(folder: Path, status_id: int) -> None:
    state = load_json(folder / "state.json", {})
    if not state or state.get("status_id") == status_id:
        return
    state["status_id"] = status_id
    save_json(folder / "state.json", state)


def unit_timed_out(unit: str) -> bool:
    try:
        result = subprocess.run(
            ["systemctl", "show", unit, "-p", "Result", "--value"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.stdout.strip() == "timeout"


async def show_progress(bot, folder: Path, cid: int, text: str) -> None:
    if (folder / "stopped").is_file():
        return
    state = load_json(folder / "state.json", {})
    status_id = int(state.get("status_id") or 0)
    chat_id = int(state.get("chat_id") or cid)
    new_id = await edit_or_send(bot, chat_id, status_id, text)
    if new_id and new_id != status_id:
        remember_status(folder, new_id)


def save_session(cid: int, session_id: str, cwd: str) -> None:
    fresh = sessions_data()
    slot = fresh.setdefault("chats", {}).setdefault(str(cid), {})
    slot["session_id"] = session_id
    if cwd:
        slot["cwd"] = cwd
    save_sessions(fresh)


async def watch_job(bot, cid: int) -> dict:
    folder = job_dir(cid)
    stdout = folder / "stdout"
    WATCHING.add(cid)
    answer = ""
    session_id = None
    err_line = ""
    spent = 0
    tools: list[str] = []
    shown = ""
    last_edit = 0.0
    last_status = 0.0
    offset = 0
    buf = b""
    seen_active = False
    grace_until = time.monotonic() + 8
    try:
        while True:
            chunk = b""
            if stdout.is_file():
                with stdout.open("rb") as handle:
                    handle.seek(offset)
                    chunk = handle.read(262144)
            if chunk:
                offset += len(chunk)
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    event = parse_line(line)
                    if event:
                        answer, session_id, err_line, spent, tools = fold_event(
                            answer, session_id, err_line, spent, tools, event
                        )
                now = time.monotonic()
                if tools and now - last_status > 1.2:
                    last_status = now
                    await show_progress(bot, folder, cid, "🔎 Working\n" + "\n".join(tools))
                if answer.strip() and answer != shown and now - last_edit > 1.4:
                    last_edit = now
                    shown = answer
                    await show_progress(bot, folder, cid, scrub(answer.strip()[:3500]))
            stopped = (folder / "stopped").is_file() or load_json(folder / "state.json", {}).get("stopped")
            state_name = unit_state(unit_for(cid))
            if state_name == "active":
                seen_active = True
            running = state_name in {"active", "activating", "deactivating"}
            settled = seen_active or time.monotonic() > grace_until or (folder / "exit").is_file()
            if stopped or (settled and not running):
                if stdout.is_file():
                    with stdout.open("rb") as handle:
                        handle.seek(offset)
                        rest = handle.read()
                    if rest:
                        offset += len(rest)
                        buf += rest
                        continue
                if buf.strip():
                    event = parse_line(buf)
                    if event:
                        answer, session_id, err_line, spent, tools = fold_event(
                            answer, session_id, err_line, spent, tools, event
                        )
                break
            await asyncio.sleep(0.4)
        await asyncio.sleep(0.3)
        code = read_exit(folder)
        if code is None:
            await asyncio.sleep(0.4)
            code = read_exit(folder)
        stderr = ""
        err_path = folder / "stderr"
        if err_path.is_file():
            stderr = err_path.read_text(encoding="utf-8", errors="replace")[-4000:]
        stopped = (folder / "stopped").is_file() or bool(load_json(folder / "state.json", {}).get("stopped"))
        state = load_json(folder / "state.json", {})
        if session_id:
            save_session(cid, str(session_id), str(state.get("cwd") or ""))
        timed_out = (not stopped) and (
            code in {143, 137, -15, -9} or (code is None and unit_timed_out(unit_for(cid)))
        )
        return {
            "answer": answer,
            "session_id": session_id,
            "err_line": err_line,
            "spent": spent,
            "stderr": stderr,
            "code": code,
            "stopped": stopped,
            "timed_out": timed_out,
        }
    finally:
        WATCHING.discard(cid)


async def deliver_ids(bot, chat_id: int, reply_to: int, status_id: int, answer: str, markup) -> int | None:
    body = answer.strip()
    if not body:
        return status_id or None
    if len(body) <= 4000:
        return await edit_or_send(bot, chat_id, status_id, body, markup)
    first = await edit_or_send(bot, chat_id, status_id, body[:3900])
    if first is None:
        return None
    await push_chunks(bot, chat_id, reply_to, body[3900:], markup)
    return first


async def publish_job(bot, cid: int, result: dict) -> None:
    folder = job_dir(cid)
    state = load_json(folder / "state.json", {})
    if not state or state.get("delivered") or state.get("stopped") or (folder / "stopped").is_file():
        return
    uid = str(state.get("user_id") or "")
    admin = bool(state.get("admin"))
    markup = main_menu(admin, uid)
    chat_id = int(state["chat_id"])
    status_id = int(state.get("status_id") or 0)
    reply_to = int(state.get("reply_to") or 0)
    answer = scrub(result.get("answer") or "")
    try:
        if answer.strip():
            sent = await deliver_ids(bot, chat_id, reply_to, status_id, answer, markup)
            if sent is None:
                log.info("delivery waiting chat %s", cid)
                return
            if result.get("code") == 0 and not state.get("charged"):
                charge(uid, int(result.get("spent") or 0))
                state["charged"] = True
        else:
            if result.get("timed_out"):
                detail = "⏱ This answer ran out of time. Send it again."
            elif admin:
                detail = scrub((result.get("err_line") or result.get("stderr") or "No answer came back. Send it again.").strip()[-800:])
            else:
                detail = "No answer came back. Send it again."
            sent = await edit_or_send(bot, chat_id, status_id, detail or "No answer came back. Send it again.", markup)
            if sent is None:
                log.info("delivery waiting chat %s", cid)
                return
    except Forbidden:
        state["delivered"] = True
        save_json(folder / "state.json", state)
        return
    state["delivered"] = True
    save_json(folder / "state.json", state)
    for name in ("argv.json", "stdout", "stderr"):
        try:
            (folder / name).unlink()
        except OSError:
            pass


async def finish_detached(bot, cid: int) -> None:
    held = False
    try:
        await asyncio.wait_for(WORLD.acquire(), timeout=30)
        held = True
        state = load_json(job_dir(cid) / "state.json", {})
        if not state or state.get("delivered") or state.get("stopped"):
            return
        if (job_dir(cid) / "stopped").is_file():
            return
        result = await watch_job(bot, cid)
        if result.get("stopped"):
            return
        await publish_job(bot, cid, result)
    except Exception:
        log.exception("resume failed chat %s", cid)
    finally:
        if held:
            WORLD.release()


async def resume_jobs(bot) -> None:
    if not JOBS.is_dir():
        return
    for state_path in sorted(JOBS.glob("*/state.json")):
        state = load_json(state_path, {})
        if not state or state.get("delivered") or state.get("stopped"):
            continue
        if (state_path.parent / "stopped").is_file():
            continue
        try:
            cid = int(state["chat_id"])
        except (KeyError, TypeError, ValueError):
            continue
        asyncio.create_task(finish_detached(bot, cid))


def close_job(folder: Path, note: str) -> None:
    state = load_json(folder / "state.json", {})
    if not state:
        return
    state["delivered"] = True
    state["stopped"] = True
    save_json(folder / "state.json", state)
    log.info("closed job %s", note)


async def run_turn(update: Update, prompt: str) -> None:
    chat = update.effective_chat
    message = update.message
    cid = chat.id
    lock = chat_lock(cid)
    markup = menu_markup(update)
    if lock.locked() or chat_busy(str(cid)):
        await message.reply_text("⏳ Still writing the previous answer.\nTap Stop if you need to.", reply_markup=markup)
        return
    if another_job_running(cid):
        await message.reply_text("⏳ I am answering someone else.\nSend it again in a moment.", reply_markup=markup)
        return
    try:
        await asyncio.wait_for(WORLD.acquire(), timeout=0.2)
    except asyncio.TimeoutError:
        await message.reply_text("⏳ I am answering someone else.\nSend it again in a moment.", reply_markup=markup)
        return

    held = True
    status = None
    try:
        async with lock:
            uid = subject_id(update)
            profile = access_data()["users"].get(uid) or {}
            cwd = run_workspace(update)
            Path(cwd).mkdir(parents=True, exist_ok=True)
            sessions = sessions_data()
            row = sessions.setdefault("chats", {}).setdefault(str(cid), {})
            if not row.get("cwd_override"):
                row["cwd"] = cwd
                save_sessions(sessions)
            model = profile.get("model") or DEFAULT_MODEL
            rules = RULES_ADMIN if is_admin(update.effective_user.id) else RULES_USER
            rules = f"{rules} Working directory: {cwd}."
            admin_turn = is_admin(update.effective_user.id if update.effective_user else None)
            status = await message.reply_text("✍️ Writing the answer…")
            folder = job_dir(cid)
            folder.mkdir(parents=True, exist_ok=True)
            os.chmod(folder, 0o700)
            save_json(folder / "state.json", {
                "chat_id": cid,
                "user_id": uid,
                "reply_to": message.message_id,
                "status_id": status.message_id,
                "cwd": cwd,
                "admin": admin_turn,
                "delivered": False,
                "charged": False,
                "stopped": False,
            })
            drop_resume = False
            for attempt in (1, 2):
                session_id = None if drop_resume else row.get("session_id")
                cmd = [
                    GROK_BIN,
                    "-p",
                    prompt,
                    "--cwd",
                    cwd,
                    "--model",
                    model,
                    "--output-format",
                    "streaming-json",
                    "--always-approve",
                    "--max-turns",
                    "40",
                    "--rules",
                    rules,
                ]
                if session_id:
                    cmd.extend(["--resume", session_id])
                if attempt == 2:
                    await safe_edit(status, "💬 The previous chat was not found. Starting fresh.")
                if admin_turn:
                    argv = cmd
                    launch_cwd = cwd
                    launch_env = grok_env()
                else:
                    argv = sandbox_argv(cwd, cmd)
                    launch_cwd = "/"
                    launch_env = {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "HOME": "/nonexistent"}
                    if not argv:
                        await safe_edit(
                            status,
                            "The workspace sandbox is not ready. Ask the admin to run: grokwire doctor",
                            markup,
                        )
                        close_job(folder, "sandbox")
                        break
                try:
                    launch_job(folder, argv, launch_cwd, launch_env)
                except Exception:
                    log.exception("launch failed chat %s", cid)
                    await safe_edit(status, "❌ The answer was not written. Send it again.", markup)
                    close_job(folder, "launch")
                    break
                result = await watch_job(message.get_bot(), cid)
                if result.get("stopped"):
                    break
                code = result.get("code")
                failed_resume = (
                    attempt == 1
                    and session_id
                    and not (result.get("answer") or "").strip()
                    and code not in (0, None)
                    and session_missing((result.get("stderr") or "") + (result.get("err_line") or ""))
                )
                if failed_resume:
                    fresh = sessions_data()
                    slot = fresh.setdefault("chats", {}).setdefault(str(cid), {})
                    slot.pop("session_id", None)
                    slot["cwd"] = cwd
                    save_sessions(fresh)
                    row = slot
                    drop_resume = True
                    continue
                await publish_job(message.get_bot(), cid, result)
                log.info("chat %s exit %s chars %s", cid, code, len(result.get("answer") or ""))
                break
    except Exception:
        log.exception("turn failed chat %s", cid)
        asyncio.create_task(finish_detached(message.get_bot(), cid))
    finally:
        if held:
            WORLD.release()
            held = False


async def hide_command_button(bot) -> None:
    scopes = [
        BotCommandScopeDefault(),
        BotCommandScopeAllPrivateChats(),
        BotCommandScopeAllGroupChats(),
        BotCommandScopeAllChatAdministrators(),
    ]
    for uid in list(access_data()["users"]):
        if normalize_id(uid):
            try:
                scopes.append(BotCommandScopeChat(int(uid)))
            except ValueError:
                pass
    for scope in scopes:
        try:
            await bot.delete_my_commands(scope=scope)
        except BadRequest:
            pass
    try:
        await bot.set_chat_menu_button(menu_button=MenuButtonDefault())
    except BadRequest:
        log.warning("default menu button was not cleared")
    for uid in list(access_data()["users"]):
        if not normalize_id(uid):
            continue
        try:
            await bot.set_chat_menu_button(chat_id=int(uid), menu_button=MenuButtonDefault())
        except (BadRequest, Forbidden):
            pass


async def post_init(app: Application) -> None:
    DATA.mkdir(parents=True, exist_ok=True)
    os.chmod(DATA, 0o700)
    WWW.mkdir(parents=True, exist_ok=True)
    save_access(access_data())
    admin_ws = ensure_workspace(str(ADMIN_ID))
    sessions = sessions_data()
    slot = sessions.setdefault("chats", {}).setdefault(str(ADMIN_ID), {})
    if slot.get("cwd") in (None, "", "/root/grok-work"):
        slot.pop("session_id", None)
        slot["cwd"] = admin_ws
        slot.pop("cwd_override", None)
        save_sessions(sessions)
    await app.bot.delete_webhook(drop_pending_updates=False)
    await hide_command_button(app.bot)
    me = await app.bot.get_me()
    log.info("polling as @%s", me.username)
    try:
        await app.bot.send_message(
            ADMIN_ID,
            menu_text(str(ADMIN_ID), str(ADMIN_ID), admin_ws),
            reply_markup=main_menu(True, str(ADMIN_ID)),
        )
    except (Forbidden, BadRequest):
        log.warning("admin has not started the bot yet")
    asyncio.create_task(resume_jobs(app.bot))


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("telegram").setLevel(logging.WARNING)
    token = os.environ.get("BOT_TOKEN", "").strip()
    if not token:
        raise SystemExit("BOT_TOKEN is missing")
    app = (
        Application.builder()
        .token(token)
        .connect_timeout(30)
        .read_timeout(40)
        .write_timeout(40)
        .pool_timeout(20)
        .post_init(post_init)
        .build()
    )
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("menu", cmd_menu))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("id", cmd_id))
    app.add_handler(CommandHandler("access", cmd_access))
    app.add_handler(CommandHandler("allow", cmd_allow))
    app.add_handler(CommandHandler("disable", cmd_disable))
    app.add_handler(CommandHandler("enable", cmd_enable))
    app.add_handler(CommandHandler("remove", cmd_remove))
    app.add_handler(CommandHandler("new", cmd_new))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("model", cmd_model))
    app.add_handler(CommandHandler("cwd", cmd_cwd))
    app.add_handler(CommandHandler("cancel", cmd_cancel))
    uploads = (
        filters.Document.ALL
        | filters.PHOTO
        | filters.VIDEO
        | filters.AUDIO
        | filters.VOICE
        | filters.VIDEO_NOTE
        | filters.ANIMATION
    )
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(uploads, on_file))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.add_handler(MessageHandler(filters.COMMAND, unknown_command))
    app.add_handler(MessageHandler(~filters.TEXT & ~filters.COMMAND, on_other))
    app.add_error_handler(on_error)
    app.run_polling(allowed_updates=["message", "callback_query"], bootstrap_retries=-1)


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    log.exception("handler error: %s", context.error)


if __name__ == "__main__":
    main()
