import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
env_path = BASE_DIR / ".env"

def load_env_file(path):
    if not path.exists():
        return {}
    env_vars = {}
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip().lstrip('\ufeff')
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                key, value = line.split("=", 1)
                env_vars[key.strip()] = value.strip()
    return env_vars

env_vars = load_env_file(env_path)
for key, value in env_vars.items():
    os.environ[key] = value

def parse_chat_id(value):
    if value is None:
        return None
    value = value.strip()
    if not value:
        return None
    if value.startswith("@"):
        return value
    try:
        return int(value)
    except ValueError:
        return value


def save_env_file(path, env_vars):
    with open(path, "w", encoding="utf-8") as f:
        for key, value in env_vars.items():
            f.write(f"{key}={value}\n")


def set_env_value(key, value):
    env_vars = load_env_file(env_path)
    env_vars[key] = str(value)
    save_env_file(env_path, env_vars)
    os.environ[key] = str(value)


BOT_TOKEN = os.getenv("BOT_TOKEN")
OWNER_ID = int(os.getenv("OWNER_ID", "0"))
CHANNEL_ID = parse_chat_id(os.getenv("CHANNEL_ID"))
GROUP_ID = parse_chat_id(os.getenv("GROUP_ID"))
AUTO_UPDATE_GROUP_ID = os.getenv("AUTO_UPDATE_GROUP_ID", "1") in ("1", "true", "True")
LOGS_FILE = BASE_DIR / os.getenv("LOGS_FILE", "logs.json")
ALLOWED_FILE = BASE_DIR / os.getenv("ALLOWED_FILE", "allowed_users.json")

print(
    f"Загружено: BOT_TOKEN={BOT_TOKEN[:10]}..., OWNER_ID={OWNER_ID}, CHANNEL_ID={CHANNEL_ID}, "
    f"GROUP_ID={GROUP_ID}, AUTO_UPDATE_GROUP_ID={AUTO_UPDATE_GROUP_ID}"
)

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN не задан в файле .env")
if OWNER_ID == 0:
    raise RuntimeError("OWNER_ID не задан или неверен в файле .env")
if not CHANNEL_ID:
    raise RuntimeError("CHANNEL_ID не задан в файле .env")
if not GROUP_ID:
    raise RuntimeError("GROUP_ID не задан в файле .env")
