import json
import logging
from datetime import datetime
from pathlib import Path

from telegram import Update
from telegram.constants import ChatType
from telegram.error import ChatMigrated
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes, MessageHandler, filters

import config

logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


def ensure_json(path: Path, default):
    if not path.exists():
        path.write_text(json.dumps(default, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except json.JSONDecodeError:
        return default


LOGS_FILE = config.LOGS_FILE
ALLOWED_FILE = config.ALLOWED_FILE
MAPPING_FILE = Path(__file__).resolve().parent / "reply_map.json"
ensure_json(LOGS_FILE, [])
ensure_json(ALLOWED_FILE, [])
ensure_json(MAPPING_FILE, {})
BANNED_FILE = Path(__file__).resolve().parent / "banned_users.json"
SPAM_FILE = Path(__file__).resolve().parent / "spam_times.json"
ensure_json(BANNED_FILE, [])
ensure_json(SPAM_FILE, {})
WARN_FILE = Path(__file__).resolve().parent / "warns.json"
ensure_json(WARN_FILE, {})

allowed_users = set(ensure_json(ALLOWED_FILE, []))
allowed_users.add(config.OWNER_ID)

# Ensure .env GROUP_ID / AUTO_UPDATE_GROUP_ID are respected even if OS env differs.
env_file_path = Path(__file__).resolve().parent / ".env"
if env_file_path.exists():
    try:
        with env_file_path.open("r", encoding="utf-8") as ef:
            for line in ef:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if line.startswith("GROUP_ID="):
                    raw = line.split("=", 1)[1].strip()
                    # keep as int if possible
                    try:
                        config.GROUP_ID = int(raw)
                    except ValueError:
                        config.GROUP_ID = raw
                if line.startswith("AUTO_UPDATE_GROUP_ID="):
                    raw = line.split("=", 1)[1].strip()
                    config.AUTO_UPDATE_GROUP_ID = raw in ("1", "true", "True")
    except Exception:
        logger.exception("Не удалось прочитать .env напрямую")


def get_group_id():
    return config.GROUP_ID


def update_group_id(new_id):
    # Only change the in-memory and persisted GROUP_ID if AUTO_UPDATE_GROUP_ID is enabled
    if getattr(config, "AUTO_UPDATE_GROUP_ID", False):
        config.GROUP_ID = new_id
        try:
            config.set_env_value("GROUP_ID", new_id)
            logger.info("AUTO_UPDATE_GROUP_ID=1: обновил GROUP_ID на %s и сохранил в .env", new_id)
        except Exception:
            logger.exception("Не удалось сохранить GROUP_ID в .env")
    else:
        logger.info("AUTO_UPDATE_GROUP_ID=0: обнаружен новый chat_id=%s, не меняю config.GROUP_ID", new_id)


async def bot_group_call_with_migrate_retry(func, *args, **kwargs):
    try:
        return await func(*args, **kwargs)
    except ChatMigrated as exc:
        new_chat_id = getattr(exc, "new_chat_id", None)
        if new_chat_id is None:
            raise
        # Retry the request using the migrated chat id, but only update config if allowed
        if getattr(config, "AUTO_UPDATE_GROUP_ID", False):
            update_group_id(new_chat_id)
            kwargs["chat_id"] = get_group_id()
        else:
            kwargs["chat_id"] = new_chat_id
        return await func(*args, **kwargs)

published_channel_ids = set()
reply_mapping = ensure_json(MAPPING_FILE, {})
banned_users = set(ensure_json(BANNED_FILE, []))
spam_times = ensure_json(SPAM_FILE, {})
STATE_KEY = "_last_forwarded_group_message_id"


def save_json(path: Path, data):
    with path.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def add_allowed(user_id: int):
    allowed_users.add(user_id)
    save_json(ALLOWED_FILE, sorted(list(allowed_users)))


def remove_allowed(user_id: int):
    if user_id == config.OWNER_ID:
        return False
    allowed_users.discard(user_id)
    save_json(ALLOWED_FILE, sorted(list(allowed_users)))
    return True


def load_logs():
    return ensure_json(LOGS_FILE, [])


def save_logs(logs):
    save_json(LOGS_FILE, logs)


def add_log(entry: dict):
    logs = load_logs()
    logs.append(entry)
    save_logs(logs)
    logger.info("Сохранил лог: %s", entry)
    if entry.get("channel_message_id") is not None:
        published_channel_ids.add(entry["channel_message_id"])


def save_mapping(mapping: dict):
    save_json(MAPPING_FILE, mapping)


def add_reply_mapping(group_message_id: int, user_id: int):
    reply_mapping[str(group_message_id)] = user_id
    save_mapping(reply_mapping)


def set_last_forwarded_group_message_id(message_id: int):
    reply_mapping[STATE_KEY] = message_id
    save_mapping(reply_mapping)


def get_last_forwarded_group_message_id():
    return reply_mapping.get(STATE_KEY)


def get_reply_user(group_message_id: int):
    return reply_mapping.get(str(group_message_id))


def save_banned(users):
    save_json(BANNED_FILE, sorted(list(users)))


def save_warns(data: dict):
    save_json(WARN_FILE, data)


def add_warn_record(user_id: int, by_id: int, by_username: str, reason: str):
    key = str(user_id)
    rec = {
        "by_id": by_id,
        "by_username": by_username,
        "reason": reason,
        "date": datetime.now().strftime("%d.%m.%Y"),
        "time": datetime.now().strftime("%H:%M"),
    }
    current = ensure_json(WARN_FILE, {})
    arr = current.get(key, [])
    arr.append(rec)
    current[key] = arr
    save_warns(current)


def get_warns_for_user(user_id: int):
    current = ensure_json(WARN_FILE, {})
    return current.get(str(user_id), [])


def clear_warns_for_user(user_id: int):
    current = ensure_json(WARN_FILE, {})
    key = str(user_id)
    if key in current:
        del current[key]
        save_warns(current)
        return True
    return False


def add_ban(user_id: int, by_id: int = None, username: str = None, admin_username: str = None):
    logger.info("Добавляю бан user=%s by=%s", user_id, by_id)
    banned_users.add(user_id)
    save_banned(banned_users)
    # Запись в логи о бане
    now = datetime.now()
    ban_entry = {
        "action": "ban",
        "user_id": user_id,
        "username": username,
        "banned_by_id": by_id or config.OWNER_ID,
        "banned_by_username": admin_username,
        "date": now.strftime("%d.%m.%Y"),
        "time": now.strftime("%H:%M"),
    }
    add_log(ban_entry)


def remove_ban(user_id: int, by_id: int = None, username: str = None, admin_username: str = None):
    logger.info("Удаляю бан user=%s by=%s", user_id, by_id)
    banned_users.discard(user_id)
    save_banned(banned_users)
    # Запись в логи о разбане
    now = datetime.now()
    unban_entry = {
        "action": "unban",
        "user_id": user_id,
        "username": username,
        "unbanned_by_id": by_id or config.OWNER_ID,
        "unbanned_by_username": admin_username,
        "date": now.strftime("%d.%m.%Y"),
        "time": now.strftime("%H:%M"),
    }
    add_log(unban_entry)


def is_banned(user_id: int) -> bool:
    return user_id in banned_users


def save_spam_times():
    save_json(SPAM_FILE, spam_times)


def get_last_message_time(user_id: int):
    return float(spam_times.get(str(user_id))) if str(user_id) in spam_times else None


def update_last_message_time(user_id: int, ts: float):
    spam_times[str(user_id)] = ts
    save_spam_times()


def get_logs_for_period(logs, days_ago: int = 0):
    """Get logs from a specific day (0=today, -1=yesterday, etc)"""
    from datetime import timedelta
    target_date = (datetime.now() - timedelta(days=days_ago)).strftime("%d.%m.%Y")
    return [entry for entry in logs if entry.get("date") == target_date]


def get_logs_for_week():
    """Get logs from the last 7 days"""
    from datetime import timedelta
    start_date = (datetime.now() - timedelta(days=6)).strftime("%d.%m.%Y")
    return [entry for entry in load_logs() if entry.get("date") >= start_date]


def get_logs_for_month():
    """Get logs from the current month"""
    current_month = datetime.now().strftime("%m.%Y")
    return [entry for entry in load_logs() if entry.get("date", "").endswith(current_month)]


def make_channel_link(chat_or_id, message_id: int) -> str:
    if not chat_or_id:
        return "нет ссылки"

    if isinstance(chat_or_id, str):
        channel = chat_or_id.strip()
        if channel.startswith("@"):
            channel = channel[1:]
        if channel.isdigit() or (channel.startswith("-100") and channel[1:].isdigit()):
            try:
                chat_id = int(channel)
            except ValueError:
                return f"https://t.me/{channel}/{message_id}"
            raw_id = str(chat_id)
            if raw_id.startswith("-100"):
                short_id = raw_id[4:]
            else:
                short_id = raw_id.lstrip("-")
            return f"https://t.me/c/{short_id}/{message_id}"
        return f"https://t.me/{channel}/{message_id}"

    username = getattr(chat_or_id, "username", None)
    if username:
        return f"https://t.me/{username}/{message_id}"

    chat_id = getattr(chat_or_id, "id", None)
    try:
        chat_id = int(chat_id)
    except (TypeError, ValueError):
        return "нет ссылки"

    if chat_id < 0:
        raw_id = str(chat_id)
        if raw_id.startswith("-100"):
            short_id = raw_id[4:]
        else:
            short_id = raw_id.lstrip("-")
        return f"https://t.me/c/{short_id}/{message_id}"

    return "нет ссылки"


def user_info(user):
    if user.username:
        return f"@{user.username}"
    if user.first_name or user.last_name:
        return f"{user.first_name or ''} {user.last_name or ''}".strip()
    return str(user.id)


def chat_matches_target(chat, target_id) -> bool:
    if target_id is None or chat is None:
        return False
    if chat.username and isinstance(target_id, str) and target_id.startswith("@"):
        return chat.username.lower() == target_id[1:].lower()
    try:
        chat_id = int(chat.id)
    except (TypeError, ValueError):
        return False
    try:
        target_id = int(target_id)
    except (TypeError, ValueError):
        return False
    return chat_id == target_id


def ensure_current_group_chat(chat) -> bool:
    if chat_matches_target(chat, get_group_id()):
        return True
    if chat and chat.type in {ChatType.GROUP, ChatType.SUPERGROUP}:
        current = get_group_id()
        if chat.id != current:
            if getattr(config, "AUTO_UPDATE_GROUP_ID", False):
                update_group_id(chat.id)
                logger.info(
                    "AUTO_UPDATE_GROUP_ID=1: обновлена GROUP_ID на новый ID группы=%s (chat.type=%s)",
                    chat.id,
                    chat.type,
                )
                return True
            logger.info(
                "AUTO_UPDATE_GROUP_ID=0: обнаружен новый ID группы=%s, не меняю GROUP_ID=%s",
                chat.id,
                current,
            )
            return False
        return True
    return False


def allowed_check(user_id: int) -> bool:
    return user_id in allowed_users


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    logger.info("start_command invoked by user=%s", user_id)
    if user_id == config.OWNER_ID:
        text = (
            "Привет! Я бот для публикации предложений.\n"
            "Используй /help, чтобы увидеть команды."
        )
    else:
        text = (
            "Привет! Я бот для публикации предложений.\n"
            "Отправляй фото, видео, стикер или текст, чтобы предложить пост."
        )
    await update.message.reply_text(text)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id == config.OWNER_ID:
        text = (
            "Команды бота:\n"
            "/publish — переслать ответное сообщение в канал.\n"
            "/logs — логи за сегодня (публикации + баны).\n"
            "/logs_week — логи за последние 7 дней.\n"
            "/logs_month — логи за текущий месяц.\n"
            "/status — проверить текущие настройки бота.\n"
            "/ban <user_id> или ответ + /ban — забанить пользователя.\n"
            "/unban <user_id> — разбанить пользователя.\n"
            "/banned — список забаненных.\n"
            "/allow <user_id> — разрешить доступ к логам (только владелец).\n"
            "/revoke <user_id> — отозвать доступ (только владелец).\n"
            "/allowed — список пользователей с доступом.\n"
        )
    else:
        text = (
            "Я бот для публикации предложений. Отправь сообщение в личку, "
            "а админ в группе должен ответить на него /publish, чтобы отправить в канал.\n"
            "Используй /help, чтобы увидеть команды."
        )
    await update.message.reply_text(text)


async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id != config.OWNER_ID:
        await update.message.reply_text("Только владелец бота может проверять статус.")
        return
    
    text = (
        f"Статус бота:\n"
        f"Owner ID: {config.OWNER_ID}\n"
        f"Channel ID: {config.CHANNEL_ID}\n"
        f"Group ID: {config.GROUP_ID}\n"
    )
    await update.message.reply_text(text)


async def allowed_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not allowed_check(user_id):
        await update.message.reply_text("У вас нет доступа к этой команде.")
        return
    users = sorted(allowed_users)
    lines = ["Разрешённые пользователи:"]
    for user in users:
        lines.append(str(user))
    await update.message.reply_text("\n".join(lines))


async def allow_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id != config.OWNER_ID:
        await update.message.reply_text("Только владелец бота может давать доступ.")
        return

    if not context.args:
        await update.message.reply_text("Используйте: /allow <user_id>")
        return

    try:
        target_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("Неверный user_id. Укажите число.")
        return

    add_allowed(target_id)
    await update.message.reply_text(f"Пользователю {target_id} дан доступ к логам.")


async def revoke_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id != config.OWNER_ID:
        await update.message.reply_text("Только владелец бота может отзывать доступ.")
        return

    if not context.args:
        await update.message.reply_text("Используйте: /revoke <user_id>")
        return

    try:
        target_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("Неверный user_id. Укажите число.")
        return

    if not remove_allowed(target_id):
        await update.message.reply_text("Нельзя отозвать доступ владельца бота.")
        return

    await update.message.reply_text(f"Доступ пользователя {target_id} отозван.")


async def warn_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    logger.info(
        "warn_command invoked chat=%s user=%s reply=%s args=%s",
        update.effective_chat.id if update.effective_chat else None,
        user_id,
        bool(update.effective_message.reply_to_message),
        context.args,
    )

    if not allowed_check(user_id):
        await update.message.reply_text("У вас нет доступа к команде /warn.")
        return

    message = update.effective_message
    # Must be used in the group
    if not ensure_current_group_chat(message.chat):
        await message.reply_text("Команда /warn доступна только в чате предложки.")
        return

    # Determine target user via reply or arg
    target_id = None
    if message.reply_to_message and message.reply_to_message.from_user:
        target_id = message.reply_to_message.from_user.id
    elif context.args:
        try:
            target_id = int(context.args[0])
        except ValueError:
            await message.reply_text("Укажите корректный user_id или выполните команду в ответе на сообщение.")
            return
    else:
        await message.reply_text("Используйте: ответ на сообщение админа + /warn <причина> или /warn <user_id> <причина>")
        return

    if target_id == config.OWNER_ID:
        await message.reply_text("Нельзя выдавать варн владельцу.")
        return

    # Reason
    reason = " ".join(context.args[1:]) if context.args and message.reply_to_message is None else " ".join(context.args)
    reason = reason.strip() or "не указана"

    admin_user = update.effective_user
    admin_uname = f"@{admin_user.username}" if admin_user.username else user_info(admin_user)

    add_warn_record(target_id, by_id=user_id, by_username=admin_uname, reason=reason)
    warns = get_warns_for_user(target_id)
    count = len(warns)

    add_log({
        "action": "warn",
        "user_id": target_id,
        "warned_by_id": user_id,
        "warned_by_username": admin_uname,
        "reason": reason,
        "warn_count": count,
        "date": datetime.now().strftime("%d.%m.%Y"),
        "time": datetime.now().strftime("%H:%M"),
    })

    await message.reply_text(f"Варн выдан пользователю {target_id}. Причина: {reason}. Всего варнов: {count}/3")

    # If reached 3 warns — remove from group
    if count >= 3:
        try:
            await bot_group_call_with_migrate_retry(
                context.bot.ban_chat_member,
                chat_id=get_group_id(),
                user_id=target_id,
            )
            add_log({
                "action": "kicked_by_warns",
                "user_id": target_id,
                "by_id": user_id,
                "by_username": admin_uname,
                "date": datetime.now().strftime("%d.%m.%Y"),
                "time": datetime.now().strftime("%H:%M"),
            })
            await message.reply_text(f"Пользователь {target_id} удалён из чата предложки (3 варна).")
        except Exception:
            logger.exception("Не удалось удалить пользователя %s после 3 варнов", target_id)


async def warns_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id != config.OWNER_ID and not allowed_check(user_id):
        await update.message.reply_text("У вас нет доступа к этой команде.")
        return

    message = update.effective_message
    if message.reply_to_message and message.reply_to_message.from_user:
        target_id = message.reply_to_message.from_user.id
    elif context.args:
        try:
            target_id = int(context.args[0])
        except ValueError:
            await message.reply_text("Укажите корректный user_id или выполните команду в ответе на сообщение.")
            return
    else:
        await message.reply_text("Используйте: ответ на сообщение + /warns или /warns <user_id>")
        return

    warns = get_warns_for_user(target_id)
    if not warns:
        await message.reply_text(f"У пользователя {target_id} нет варнов.")
        return

    lines = [f"Варны для {target_id} (всего: {len(warns)}):"]
    for i, w in enumerate(warns, start=1):
        lines.append(f"{i}. от {w.get('by_username')} {w.get('date')} {w.get('time')}: {w.get('reason')}")

    await message.reply_text("\n".join(lines))


async def clearwarns_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id != config.OWNER_ID:
        await update.message.reply_text("Только владелец бота может убирать варны.")
        return

    message = update.effective_message
    if message.reply_to_message and message.reply_to_message.from_user:
        target_id = message.reply_to_message.from_user.id
    elif context.args:
        try:
            target_id = int(context.args[0])
        except ValueError:
            await message.reply_text("Укажите корректный user_id или выполните команду в ответе на сообщение.")
            return
    else:
        await message.reply_text("Используйте: ответ на сообщение + /clearwarns или /clearwarns <user_id>")
        return

    ok = clear_warns_for_user(target_id)
    if ok:
        await message.reply_text(f"Варны для пользователя {target_id} удалены.")
    else:
        await message.reply_text(f"У пользователя {target_id} не было варнов.")


async def ban_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not allowed_check(user_id):
        await update.message.reply_text("У вас нет доступа к этой команде.")
        return
    logger.info("ban_command invoked by %s args=%s reply=%s", user_id, context.args, bool(update.effective_message.reply_to_message))

    target_id = None
    # if reply in group, get original user id
    if update.effective_message.reply_to_message:
        replied = update.effective_message.reply_to_message
        # try to map group message id -> original user id
        mapped = get_reply_user(replied.message_id)
        if mapped:
            target_id = int(mapped)

    if not target_id and context.args:
        try:
            target_id = int(context.args[0])
        except ValueError:
            await update.message.reply_text("Укажите корректный user_id или ответьте на сообщение в группе.")
            return

    if not target_id:
        await update.message.reply_text("Не удалось определить пользователя для бана.")
        return

    # Попытаемся получить username для записи в лог
    uname = None
    try:
        user_obj = await context.bot.get_chat(target_id)
        uname = f"@{user_obj.username}" if getattr(user_obj, "username", None) else None
    except Exception:
        uname = None

    # Получаем username админа, который забанил
    admin_user = update.effective_user
    admin_uname = f"@{admin_user.username}" if admin_user.username else user_info(admin_user)

    add_ban(target_id, by_id=user_id, username=uname, admin_username=admin_uname)
    await update.message.reply_text(f"Пользователь {target_id} забанен.")
    try:
        await context.bot.send_message(chat_id=target_id, text="Вы были забанены.")
    except Exception:
        logger.info("Не удалось уведомить пользователя %s о бане", target_id)


async def unban_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not allowed_check(user_id):
        await update.message.reply_text("У вас нет доступа к этой команде.")
        return
    logger.info("unban_command invoked by %s args=%s", user_id, context.args)

    if not context.args:
        await update.message.reply_text("Используйте: /unban <user_id>")
        return

    try:
        target_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("Неверный user_id. Укажите число.")
        return

    # попробуем получить username
    uname = None
    try:
        user_obj = await context.bot.get_chat(target_id)
        uname = f"@{user_obj.username}" if getattr(user_obj, "username", None) else None
    except Exception:
        uname = None

    # Получаем username админа, который разбанил
    admin_user = update.effective_user
    admin_uname = f"@{admin_user.username}" if admin_user.username else user_info(admin_user)

    remove_ban(target_id, by_id=user_id, username=uname, admin_username=admin_uname)
    await update.message.reply_text(f"Пользователь {target_id} разбанен.")
    try:
        await context.bot.send_message(chat_id=target_id, text="Вы были разбанены.")
    except Exception:
        logger.info("Не удалось уведомить пользователя %s о разбане", target_id)


async def banned_list_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not allowed_check(user_id):
        await update.message.reply_text("У вас нет доступа к этой команде.")
        return

    if not banned_users:
        await update.message.reply_text("Пока нет забаненных пользователей.")
        return

    lines = [str(x) for x in sorted(banned_users)]
    await update.message.reply_text("Забаненные пользователи:\n" + "\n".join(lines))


async def logs_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != ChatType.PRIVATE:
        return

    user_id = update.effective_user.id
    if not allowed_check(user_id):
        await update.message.reply_text("У вас нет доступа к логам.")
        return

    all_logs = load_logs()
    if not all_logs:
        await update.message.reply_text("Логи пока пустые.")
        return

    # Разделяем логи по периодам
    logs_today = get_logs_for_period(all_logs, days_ago=0)
    logs_week = get_logs_for_week()
    logs_month = get_logs_for_month()

    # Начинаем сборку ответа
    lines = []

    # Логи за ДЕНЬ (сегодня)
    if logs_today:
        lines.append("=== ЛОГИ НА СЕГОДНЯ ===")
        post_logs_today = [e for e in logs_today if e.get("published_by") in {"manual_publish", "channel_post"}]
        ban_logs_today = [e for e in logs_today if e.get("action") in {"ban", "unban"}]

        if post_logs_today:
            lines.append("\n📌 Публикации:")
            for entry in reversed(post_logs_today):
                lines.append(f"ссылка: {entry.get('post_link')}")
                lines.append(f"админ: {entry.get('admin_username', 'Неизвестно')}")
                lines.append(f"время: {entry.get('time')}")
                lines.append("---")

        if ban_logs_today:
            lines.append("\n🚫 Баны/Разбаны:")
            for entry in reversed(ban_logs_today):
                typ = entry.get('action')
                action_str = "забанен" if typ == "ban" else "разбанен"
                lines.append(f"юзер: {entry.get('username') or entry.get('user_id')}")
                lines.append(f"{action_str} от: {entry.get('banned_by_username') or entry.get('unbanned_by_username')}")
                lines.append(f"время: {entry.get('time')}")
                lines.append("---")

        warn_logs_today = [e for e in logs_today if e.get('action') == 'warn']
        if warn_logs_today:
            lines.append("\n⚠️ Варны:")
            for entry in reversed(warn_logs_today):
                lines.append(f"юзер: {entry.get('user_id')}")
                lines.append(f"от: {entry.get('warned_by_username')}")
                lines.append(f"причина: {entry.get('reason')}")
                lines.append(f"время: {entry.get('time')}")
                lines.append("---")

    # Логи за НЕДЕЛЮ
    week_specific = [e for e in logs_week if e not in logs_today]
    if week_specific:
        lines.append("\n=== ЛОГИ НА НЕДЕЛЮ ===")
        post_logs_week = [e for e in week_specific if e.get("published_by") in {"manual_publish", "channel_post"}]
        ban_logs_week = [e for e in week_specific if e.get("action") in {"ban", "unban"}]

        if post_logs_week:
            lines.append("\n📌 Публикации:")
            for entry in reversed(post_logs_week[-10:]):  # последние 10
                lines.append(f"ссылка: {entry.get('post_link')}")
                lines.append(f"админ: {entry.get('admin_username', 'Неизвестно')}")
                lines.append(f"дата: {entry.get('date')} {entry.get('time')}")
                lines.append("---")

        if ban_logs_week:
            lines.append("\n🚫 Баны/Разбаны:")
            for entry in reversed(ban_logs_week[-10:]):  # последние 10
                typ = entry.get('action')
                action_str = "забанен" if typ == "ban" else "разбанен"
                lines.append(f"юзер: {entry.get('username') or entry.get('user_id')}")
                lines.append(f"{action_str} от: {entry.get('banned_by_username') or entry.get('unbanned_by_username')}")
                lines.append(f"дата: {entry.get('date')} {entry.get('time')}")
                lines.append("---")

        warn_logs_week = [e for e in week_specific if e.get('action') == 'warn']
        if warn_logs_week:
            lines.append("\n⚠️ Варны:")
            for entry in reversed(warn_logs_week[-10:]):
                lines.append(f"юзер: {entry.get('user_id')}")
                lines.append(f"от: {entry.get('warned_by_username')}")
                lines.append(f"причина: {entry.get('reason')}")
                lines.append(f"дата: {entry.get('date')} {entry.get('time')}")
                lines.append("---")

    # Логи за МЕСЯЦ
    month_specific = [e for e in logs_month if e not in logs_week]
    if month_specific:
        lines.append("\n=== ЛОГИ НА МЕСЯЦ ===")
        post_logs_month = [e for e in month_specific if e.get("published_by") in {"manual_publish", "channel_post"}]
        ban_logs_month = [e for e in month_specific if e.get("action") in {"ban", "unban"}]

        if post_logs_month:
            lines.append("\n📌 Публикации:")
            for entry in reversed(post_logs_month[-10:]):  # последние 10
                lines.append(f"ссылка: {entry.get('post_link')}")
                lines.append(f"админ: {entry.get('admin_username', 'Неизвестно')}")
                lines.append(f"дата: {entry.get('date')} {entry.get('time')}")
                lines.append("---")

        if ban_logs_month:
            lines.append("\n🚫 Баны/Разбаны:")
            for entry in reversed(ban_logs_month[-10:]):  # последние 10
                typ = entry.get('action')
                action_str = "забанен" if typ == "ban" else "разбанен"
                lines.append(f"юзер: {entry.get('username') or entry.get('user_id')}")
                lines.append(f"{action_str} от: {entry.get('banned_by_username') or entry.get('unbanned_by_username')}")
                lines.append(f"дата: {entry.get('date')} {entry.get('time')}")
                lines.append("---")

    await update.message.reply_text("\n".join(lines))


async def logs_week_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show logs for the last 7 days"""
    if update.effective_chat.type != ChatType.PRIVATE:
        return

    user_id = update.effective_user.id
    if not allowed_check(user_id):
        await update.message.reply_text("У вас нет доступа к логам.")
        return

    logs_week = get_logs_for_week()
    if not logs_week:
        await update.message.reply_text("Логи на неделю пока пустые.")
        return

    lines = ["=== ЛОГИ НА НЕДЕЛЮ (7 дней) ==="]
    post_logs = [e for e in logs_week if e.get("published_by") in {"manual_publish", "channel_post"}]
    ban_logs = [e for e in logs_week if e.get("action") in {"ban", "unban"}]

    if post_logs:
        lines.append("\n📌 Публикации:")
        for entry in reversed(post_logs[-20:]):  # последние 20
            lines.append(f"ссылка: {entry.get('post_link')}")
            lines.append(f"админ: {entry.get('admin_username', 'Неизвестно')}")
            lines.append(f"дата: {entry.get('date')} {entry.get('time')}")
            lines.append("---")

    if ban_logs:
        lines.append("\n🚫 Баны/Разбаны:")
        for entry in reversed(ban_logs[-20:]):  # последние 20
            typ = entry.get('action')
            action_str = "забанен" if typ == "ban" else "разбанен"
            lines.append(f"юзер: {entry.get('username') or entry.get('user_id')}")
            lines.append(f"{action_str} от: {entry.get('banned_by_username') or entry.get('unbanned_by_username')}")
            lines.append(f"дата: {entry.get('date')} {entry.get('time')}")
            lines.append("---")

    warn_logs = [e for e in logs_week if e.get('action') == 'warn']
    if warn_logs:
        lines.append("\n⚠️ Варны:")
        for entry in reversed(warn_logs[-20:]):
            lines.append(f"юзер: {entry.get('user_id')}")
            lines.append(f"от: {entry.get('warned_by_username')}")
            lines.append(f"причина: {entry.get('reason')}")
            lines.append(f"дата: {entry.get('date')} {entry.get('time')}")
            lines.append("---")

    await update.message.reply_text("\n".join(lines))


async def logs_month_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show logs for the current month"""
    if update.effective_chat.type != ChatType.PRIVATE:
        return

    user_id = update.effective_user.id
    if not allowed_check(user_id):
        await update.message.reply_text("У вас нет доступа к логам.")
        return

    logs_month = get_logs_for_month()
    if not logs_month:
        await update.message.reply_text("Логи на месяц пока пустые.")
        return

    lines = ["=== ЛОГИ НА МЕСЯЦ ==="]
    post_logs = [e for e in logs_month if e.get("published_by") in {"manual_publish", "channel_post"}]
    ban_logs = [e for e in logs_month if e.get("action") in {"ban", "unban"}]

    if post_logs:
        lines.append(f"\n📌 Публикации ({len(post_logs)} всего):")
        for entry in reversed(post_logs[-20:]):  # последние 20
            lines.append(f"ссылка: {entry.get('post_link')}")
            lines.append(f"админ: {entry.get('admin_username', 'Неизвестно')}")
            lines.append(f"дата: {entry.get('date')} {entry.get('time')}")
            lines.append("---")

    if ban_logs:
        lines.append(f"\n🚫 Баны/Разбаны ({len(ban_logs)} всего):")
        for entry in reversed(ban_logs[-20:]):  # последние 20
            typ = entry.get('action')
            action_str = "забанен" if typ == "ban" else "разбанен"
            lines.append(f"юзер: {entry.get('username') or entry.get('user_id')}")
            lines.append(f"{action_str} от: {entry.get('banned_by_username') or entry.get('unbanned_by_username')}")
            lines.append(f"дата: {entry.get('date')} {entry.get('time')}")
            lines.append("---")

    warn_logs = [e for e in logs_month if e.get('action') == 'warn']
    if warn_logs:
        lines.append(f"\n⚠️ Варны ({len(warn_logs)} всего):")
        for entry in reversed(warn_logs[-20:]):
            lines.append(f"юзер: {entry.get('user_id')}")
            lines.append(f"от: {entry.get('warned_by_username')}")
            lines.append(f"причина: {entry.get('reason')}")
            lines.append(f"дата: {entry.get('date')} {entry.get('time')}")
            lines.append("---")

    await update.message.reply_text("\n".join(lines))
    text = (
        f"CHANNEL_ID={config.CHANNEL_ID}\n"
        f"GROUP_ID={config.GROUP_ID}\n"
        f"OWNER_ID={config.OWNER_ID}\n"
        "Если вы используете приватный канал, ссылка в логах не будет работать.\n"
        "Для публикации в канал используйте /publish в группе предложке."
    )
    await update.message.reply_text(text)


async def publish_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    user = update.effective_user
    logger.info(
        "publish_command invoked chat=%s user=%s reply=%s",
        message.chat.id if message.chat else None,
        user.id if user else None,
        bool(message.reply_to_message),
    )

    if not ensure_current_group_chat(message.chat):
        await message.reply_text("Команда /publish работает только в группе предложка.")
        return

    if not allowed_check(user.id):
        await message.reply_text("У вас нет доступа для публикации.")
        return

    if not message.reply_to_message:
        last_forwarded_group_message_id = get_last_forwarded_group_message_id()
        if last_forwarded_group_message_id:
            await message.reply_text(
                "Команда /publish не была ответом. "
                "Использую последнее пересланное сообщение в группе."
            )
            source_msg_id = last_forwarded_group_message_id
            source_chat_id = get_group_id()
        else:
            await message.reply_text("Ответьте на сообщение, которое нужно опубликовать в канале.")
            return
    else:
        source_msg_id = message.reply_to_message.message_id
        source_chat_id = message.reply_to_message.chat.id

    try:
        sent = await context.bot.copy_message(
            chat_id=config.CHANNEL_ID,
            from_chat_id=source_chat_id,
            message_id=source_msg_id,
        )
    except Exception as exc:
        logger.exception("Ошибка при копировании сообщения в канал")
        await message.reply_text(
            f"Ошибка публикации в канал: {exc.__class__.__name__}: {exc}"
        )
        return

    channel_link = make_channel_link(config.CHANNEL_ID, sent.message_id)
    logger.info(
        "publish_command sent to channel %s message_id=%s link=%s",
        config.CHANNEL_ID,
        sent.message_id,
        channel_link,
    )
    user_name = user_info(user)
    caption = f"Переслано от {user_name}"

    try:
        await bot_group_call_with_migrate_retry(
            context.bot.copy_message,
            chat_id=get_group_id(),
            from_chat_id=sent.chat.id,
            message_id=sent.message_id,
            caption=caption,
        )
    except Exception as exc:
        logger.exception("Ошибка при копировании сообщения из канала обратно в группу")

    now = datetime.now()
    log_entry = {
        "post_link": channel_link,
        "admin_username": f"@{user.username}" if user.username else user_info(user),
        "admin_id": user.id,
        "date": now.strftime("%d.%m.%Y"),
        "time": now.strftime("%H:%M"),
        "channel_message_id": sent.message_id,
        "published_by": "manual_publish",
    }
    logger.info("Записал в лог публикацию: %s", log_entry)
    add_log(log_entry)

    await message.reply_text(
        f"Сообщение успешно опубликовано в канал.\nСсылка: {channel_link}"
    )


async def private_message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    user = update.effective_user
    sender = user_info(user)
    # Проверка бана
    if is_banned(user.id):
        await message.reply_text("Вы были забанены.")
        return

    # Анти-спам: не чаще 15 секунд
    now_ts = datetime.now().timestamp()
    last_ts = get_last_message_time(user.id)
    if last_ts and (now_ts - last_ts) < 15:
        wait = int(15 - (now_ts - last_ts))
        await message.reply_text(f"Слишком часто. Подождите {wait} секунд.")
        return
    update_last_message_time(user.id, now_ts)

    forwarded = await bot_group_call_with_migrate_retry(
        context.bot.forward_message,
        chat_id=get_group_id(),
        from_chat_id=message.chat.id,
        message_id=message.message_id,
    )

    if forwarded:
        add_reply_mapping(forwarded.message_id, user.id)
        set_last_forwarded_group_message_id(forwarded.message_id)
        logger.info(
            "private_message_handler forwarded private message %s to group %s",
            message.message_id,
            forwarded.chat.id,
        )

    await message.reply_text("сообщение было отправленно")


async def group_reply_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    if not message.reply_to_message:
        return

    if not ensure_current_group_chat(message.chat):
        return

    original = message.reply_to_message
    target_user_id = get_reply_user(original.message_id)
    if not target_user_id:
        return

    if message.text:
        await context.bot.send_message(
            chat_id=target_user_id,
            text=message.text,
        )
    else:
        await context.bot.copy_message(
            chat_id=target_user_id,
            from_chat_id=message.chat.id,
            message_id=message.message_id,
        )

    await message.reply_text("Ответ отправлен пользователю.")


async def channel_post_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    post = update.channel_post
    if not post:
        return

    if post.message_id in published_channel_ids:
        return

    author = post.author_signature or user_info(post.from_user) if post.from_user else "Неизвестно"
    caption = f"Переслано от {author}"
    forwarded = await bot_group_call_with_migrate_retry(
        context.bot.copy_message,
        chat_id=get_group_id(),
        from_chat_id=post.chat.id,
        message_id=post.message_id,
        caption=caption,
    )

    if forwarded:
        set_last_forwarded_group_message_id(forwarded.message_id)

    now = datetime.now()
    log_entry = {
        "post_link": make_channel_link(config.CHANNEL_ID, post.message_id),
        "admin_username": post.author_signature or "Неизвестно",
        "admin_id": post.from_user.id if post.from_user else "Неизвестно",
        "date": now.strftime("%d.%m.%Y"),
        "time": now.strftime("%H:%M"),
        "channel_message_id": post.message_id,
        "published_by": "channel_post",
    }
    add_log(log_entry)
    logger.info("Forwarded channel post %s to group", post.message_id)


def main() -> None:
    print(f"Текущий BOT_TOKEN: {config.BOT_TOKEN[:10]}... | CHANNEL_ID={config.CHANNEL_ID} | GROUP_ID={config.GROUP_ID}")
    app = ApplicationBuilder().token(config.BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("status", status_command))
    app.add_handler(CommandHandler("warn", warn_command))
    app.add_handler(CommandHandler("warns", warns_command))
    app.add_handler(CommandHandler("clearwarns", clearwarns_command))
    app.add_handler(CommandHandler("ban", ban_command))
    app.add_handler(CommandHandler("unban", unban_command))
    app.add_handler(CommandHandler("banned", banned_list_command))
    app.add_handler(CommandHandler("allow", allow_command))
    app.add_handler(CommandHandler("revoke", revoke_command))
    app.add_handler(CommandHandler("logs", logs_command))
    app.add_handler(CommandHandler("logs_week", logs_week_command))
    app.add_handler(CommandHandler("logs_month", logs_month_command))
    app.add_handler(CommandHandler("allowed", allowed_command))
    app.add_handler(CommandHandler("publish", publish_command))
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & ~filters.COMMAND, private_message_handler))
    app.add_handler(MessageHandler(filters.ChatType.GROUPS & filters.REPLY, group_reply_handler))
    app.add_handler(MessageHandler(filters.ChatType.CHANNEL & filters.ALL, channel_post_handler))

    logger.info("Запуск бота")
    app.run_polling()


if __name__ == "__main__":
    main()
