#!/usr/bin/env python3
"""
ClientPilot: личный Telegram-бот для поиска клиентов и первых продаж.

Это one-file версия без внешних Python-зависимостей.
Нужен только Python 3.10+.

Быстрый запуск на Ubuntu:

1. Установи Python:
   sudo apt update
   sudo apt install -y python3 git

2. Создай .env рядом с этим файлом:
   TELEGRAM_BOT_TOKEN=токен_от_BotFather
   TELEGRAM_OWNER_ID=твой_telegram_id
   OPENAI_API_KEY=
   OPENAI_MODEL=gpt-4.1-mini

3. Запусти:
   python3 clientpilot_bot.py

Если OPENAI_API_KEY пустой, бот все равно работает на встроенных шаблонах.
"""

from __future__ import annotations

import html
import json
import logging
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any


BASE_DIR = Path(__file__).resolve().parent


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger("clientpilot")


def load_dotenv(path: Path) -> None:
    if not path.exists():
        return

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


load_dotenv(BASE_DIR / ".env")


@dataclass(frozen=True)
class Settings:
    telegram_token: str
    owner_id: int
    openai_api_key: str
    openai_model: str
    data_file: Path
    monthly_goal: int
    currency: str


def read_settings() -> Settings:
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    owner = os.getenv("TELEGRAM_OWNER_ID", "").strip()

    if not token:
        raise RuntimeError("Не заполнен TELEGRAM_BOT_TOKEN. Добавь его в .env")
    if not owner.isdigit():
        raise RuntimeError("TELEGRAM_OWNER_ID должен быть числом. Добавь его в .env")

    return Settings(
        telegram_token=token,
        owner_id=int(owner),
        openai_api_key=os.getenv("OPENAI_API_KEY", "").strip(),
        openai_model=os.getenv("OPENAI_MODEL", "gpt-4.1-mini").strip(),
        data_file=BASE_DIR / os.getenv("DATA_FILE", "clientpilot_data.json"),
        monthly_goal=int(os.getenv("MONTHLY_GOAL", "50000")),
        currency=os.getenv("CURRENCY", "руб.").strip(),
    )


SETTINGS: Settings
TELEGRAM_API = ""


def init_runtime() -> None:
    global SETTINGS, TELEGRAM_API
    SETTINGS = read_settings()
    TELEGRAM_API = f"https://api.telegram.org/bot{SETTINGS.telegram_token}"


def esc(value: Any) -> str:
    return html.escape(str(value), quote=False)


def default_data() -> dict[str, Any]:
    return {
        "profile": {
            "goal": SETTINGS.monthly_goal,
            "currency": SETTINGS.currency,
            "service": "10 постов для Telegram-канала малого бизнеса",
            "price": "3 000-7 000 руб.",
            "niche": "салоны красоты, кафе, мастера услуг",
        },
        "leads": [],
        "wins": [],
    }


def load_data() -> dict[str, Any]:
    if not SETTINGS.data_file.exists():
        return default_data()

    try:
        return json.loads(SETTINGS.data_file.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        logger.exception("Не получилось прочитать базу. Создаю новую.")
        return default_data()


def save_data(data: dict[str, Any]) -> None:
    SETTINGS.data_file.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def api_call(method: str, payload: dict[str, Any] | None = None, timeout: int = 35) -> dict[str, Any]:
    url = f"{TELEGRAM_API}/{method}"
    body = json.dumps(payload or {}).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    with urllib.request.urlopen(request, timeout=timeout) as response:
        result = json.loads(response.read().decode("utf-8"))
    if not result.get("ok"):
        raise RuntimeError(f"Telegram API error: {result}")
    return result


def openai_call(prompt: str) -> str | None:
    if not SETTINGS.openai_api_key:
        return None

    payload = {
        "model": SETTINGS.openai_model,
        "input": prompt,
        "max_output_tokens": 700,
    }
    request = urllib.request.Request(
        "https://api.openai.com/v1/responses",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {SETTINGS.openai_api_key}",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=45) as response:
            data = json.loads(response.read().decode("utf-8"))
        return extract_openai_text(data)
    except Exception:
        logger.exception("OpenAI не ответил, использую шаблон.")
        return None


def extract_openai_text(data: dict[str, Any]) -> str | None:
    if isinstance(data.get("output_text"), str):
        return data["output_text"]

    parts: list[str] = []
    for item in data.get("output", []):
        for content in item.get("content", []):
            text = content.get("text")
            if isinstance(text, str):
                parts.append(text)

    return "\n".join(parts).strip() or None


def keyboard(rows: list[list[tuple[str, str]]]) -> dict[str, Any]:
    return {
        "inline_keyboard": [
            [{"text": text, "callback_data": callback} for text, callback in row]
            for row in rows
        ]
    }


def main_menu() -> dict[str, Any]:
    return keyboard(
        [
            [("План на сегодня", "daily_plan"), ("Моя услуга", "offer")],
            [("Сообщение клиенту", "client_message"), ("Ответ на возражение", "objection")],
            [("CRM", "crm"), ("Цель и прогресс", "money")],
            [("Добавить клиента", "add_lead_help"), ("Помощь", "help")],
        ]
    )


def back_menu() -> dict[str, Any]:
    return keyboard([[("Назад в меню", "menu")]])


def send_message(chat_id: int, text: str, reply_markup: dict[str, Any] | None = None) -> None:
    api_call(
        "sendMessage",
        {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "reply_markup": reply_markup,
            "disable_web_page_preview": True,
        },
    )


def edit_message(chat_id: int, message_id: int, text: str, reply_markup: dict[str, Any] | None = None) -> None:
    api_call(
        "editMessageText",
        {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": text,
            "parse_mode": "HTML",
            "reply_markup": reply_markup,
            "disable_web_page_preview": True,
        },
    )


def answer_callback(callback_id: str, text: str = "") -> None:
    api_call("answerCallbackQuery", {"callback_query_id": callback_id, "text": text})


def deny(chat_id: int) -> None:
    send_message(chat_id, "Этот ассистент закрыт. Доступ есть только у владельца.")


def is_owner(user_id: int | None) -> bool:
    return user_id == SETTINGS.owner_id


def profile_text(data: dict[str, Any]) -> str:
    profile = data["profile"]
    return (
        "<b>Твоя стартовая услуга</b>\n\n"
        f"<b>Услуга:</b> {esc(profile['service'])}\n"
        f"<b>Цена:</b> {esc(profile['price'])}\n"
        f"<b>Кому продавать:</b> {esc(profile['niche'])}\n\n"
        "Ты продаешь не AI, а понятный результат: готовые посты, идеи акций "
        "и тексты, которые бизнес может сразу выложить."
    )


def plan_text(data: dict[str, Any]) -> str:
    profile = data["profile"]
    return (
        "<b>План на сегодня</b>\n\n"
        "1. Найди 20 бизнесов в одной нише.\n"
        "2. Выбери 10, у кого слабый Telegram или давно не было постов.\n"
        "3. Напиши каждому короткое личное сообщение.\n"
        "4. Сохрани ответы через команду /lead.\n"
        "5. Вечером сделай 3 повторных касания тем, кто не ответил.\n\n"
        f"<b>Сегодня продаем:</b> {esc(profile['service'])} за {esc(profile['price'])}."
    )


def fallback_client_message(data: dict[str, Any]) -> str:
    profile = data["profile"]
    return (
        "<b>Сообщение клиенту</b>\n\n"
        "Здравствуйте. Посмотрел ваш Telegram-канал и вижу, что его можно усилить "
        "постами, которые ведут к заявкам и повторным покупкам.\n\n"
        f"Я могу подготовить для вас {esc(profile['service']).lower()}: идеи, тексты, "
        "акции и спокойные продающие формулировки без навязчивости.\n\n"
        "Могу бесплатно прислать один пример поста под ваш бизнес. Если понравится, "
        f"сделаем полный пакет за {esc(profile['price'])}."
    )


def fallback_objection() -> str:
    return (
        "<b>Ответ на возражение</b>\n\n"
        "<b>Клиент:</b> Нам пока не нужно.\n\n"
        "<b>Ответ:</b> Понимаю. Тогда предлагаю без обязательств: я сделаю один "
        "пример поста именно под ваш бизнес. Если он покажется полезным, обсудим "
        "пакет. Если нет, просто оставите себе идею."
    )


def crm_text(data: dict[str, Any]) -> str:
    leads = data.get("leads", [])
    if not leads:
        return (
            "<b>CRM</b>\n\n"
            "Пока клиентов нет.\n\n"
            "Добавь первого:\n"
            "<code>/lead Салон Лилия | написал | завтра отправить пример поста</code>"
        )

    rows = []
    for lead in leads[-10:]:
        rows.append(
            f"<b>{esc(lead['name'])}</b>\n"
            f"Статус: {esc(lead['status'])}\n"
            f"Заметка: {esc(lead['note'])}\n"
            f"Дата: {esc(lead['created_at'])}"
        )
    return "<b>CRM: последние клиенты</b>\n\n" + "\n\n".join(rows)


def money_text(data: dict[str, Any]) -> str:
    profile = data["profile"]
    wins = data.get("wins", [])
    total = sum(int(win.get("amount", 0)) for win in wins)
    goal = int(profile.get("goal", SETTINGS.monthly_goal))
    left = max(goal - total, 0)
    currency = profile.get("currency", SETTINGS.currency)

    return (
        "<b>Цель и прогресс</b>\n\n"
        f"Цель на месяц: <b>{goal} {esc(currency)}</b>\n"
        f"Уже записано: <b>{total} {esc(currency)}</b>\n"
        f"Осталось: <b>{left} {esc(currency)}</b>\n\n"
        "Фокус на сегодня: не идеальный продукт, а 10 честных касаний с потенциальными клиентами."
    )


def ai_prompt(task: str, data: dict[str, Any]) -> str:
    profile = data["profile"]
    return (
        "Ты личный Telegram-ассистент для заработка на простых freelance-услугах. "
        "Пиши по-русски, конкретно, без обещаний гарантированного дохода. "
        "Помогай владельцу каждый день находить клиентов и продавать услугу.\n\n"
        f"Услуга: {profile['service']}\n"
        f"Цена: {profile['price']}\n"
        f"Ниша: {profile['niche']}\n\n"
        f"Задача: {task}"
    )


def handle_start(chat_id: int) -> None:
    send_message(
        chat_id,
        "<b>ClientPilot</b>\n\n"
        "Я твой закрытый ассистент для поиска клиентов и первых продаж.\n"
        "Выбери действие ниже.",
        main_menu(),
    )


def handle_command(chat_id: int, text: str) -> None:
    if text.startswith("/start") or text.startswith("/menu"):
        handle_start(chat_id)
        return

    if text.startswith("/lead"):
        add_lead(chat_id, text)
        return

    if text.startswith("/win"):
        add_win(chat_id, text)
        return

    if text.startswith("/setgoal"):
        set_goal(chat_id, text)
        return

    send_message(chat_id, "Не знаю такую команду. Нажми /menu.", main_menu())


def add_lead(chat_id: int, text: str) -> None:
    raw = text.replace("/lead", "", 1).strip()
    parts = [part.strip() for part in raw.split("|")]
    if len(parts) < 3:
        send_message(chat_id, "Формат такой:\n/lead Название бизнеса | статус | заметка")
        return

    data = load_data()
    data.setdefault("leads", []).append(
        {
            "name": parts[0],
            "status": parts[1],
            "note": parts[2],
            "created_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        }
    )
    save_data(data)
    send_message(chat_id, "Клиент добавлен в CRM.", main_menu())


def add_win(chat_id: int, text: str) -> None:
    raw = text.replace("/win", "", 1).strip()
    parts = [part.strip() for part in raw.split("|")]
    if len(parts) < 2 or not parts[0].isdigit():
        send_message(chat_id, "Формат такой:\n/win 5000 | Салон Лилия")
        return

    data = load_data()
    data.setdefault("wins", []).append(
        {
            "amount": int(parts[0]),
            "client": parts[1],
            "created_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        }
    )
    save_data(data)
    send_message(chat_id, "Продажа записана. Двигаемся дальше.", main_menu())


def set_goal(chat_id: int, text: str) -> None:
    raw = text.replace("/setgoal", "", 1).strip()
    if not raw.isdigit():
        send_message(chat_id, "Напиши так:\n/setgoal 50000")
        return

    data = load_data()
    data["profile"]["goal"] = int(raw)
    save_data(data)
    send_message(chat_id, "Цель обновлена.", main_menu())


def handle_callback(chat_id: int, message_id: int, callback_id: str, action: str) -> None:
    answer_callback(callback_id)
    data = load_data()

    if action == "menu":
        edit_message(chat_id, message_id, "<b>ClientPilot</b>\n\nВыбери следующее действие.", main_menu())
        return

    if action == "daily_plan":
        ai_text = openai_call(ai_prompt("Составь план продаж на сегодня на 60-90 минут.", data))
        edit_message(chat_id, message_id, ai_text or plan_text(data), back_menu())
        return

    if action == "offer":
        edit_message(chat_id, message_id, profile_text(data), back_menu())
        return

    if action == "client_message":
        ai_text = openai_call(
            ai_prompt(
                "Напиши короткое первое сообщение потенциальному клиенту. "
                "Тон: уверенно, спокойно, без давления. Дай 2 варианта.",
                data,
            )
        )
        edit_message(chat_id, message_id, ai_text or fallback_client_message(data), back_menu())
        return

    if action == "objection":
        ai_text = openai_call(
            ai_prompt("Дай ответы на 4 возражения: дорого, нам не нужно, пришлите примеры, мы подумаем.", data)
        )
        edit_message(chat_id, message_id, ai_text or fallback_objection(), back_menu())
        return

    if action == "crm":
        edit_message(chat_id, message_id, crm_text(data), back_menu())
        return

    if action == "money":
        edit_message(chat_id, message_id, money_text(data), back_menu())
        return

    if action == "add_lead_help":
        edit_message(
            chat_id,
            message_id,
            "<b>Добавить клиента</b>\n\n"
            "Отправь команду в таком формате:\n\n"
            "<code>/lead Название бизнеса | статус | заметка</code>\n\n"
            "Пример:\n"
            "<code>/lead Салон Лилия | написал | ждут пример поста завтра</code>",
            back_menu(),
        )
        return

    if action == "help":
        edit_message(
            chat_id,
            message_id,
            "<b>Команды</b>\n\n"
            "/start - открыть ассистента\n"
            "/menu - главное меню\n"
            "/lead Название | статус | заметка - добавить клиента\n"
            "/win сумма | клиент - записать продажу\n"
            "/setgoal сумма - изменить цель на месяц",
            back_menu(),
        )


def handle_text(chat_id: int, text: str) -> None:
    data = load_data()
    ai_text = openai_call(
        ai_prompt(
            "Ответь владельцу как личный ассистент по заработку. "
            f"Сообщение владельца: {text}",
            data,
        )
    )
    send_message(
        chat_id,
        ai_text or "Я понял. Выбери действие в меню, чтобы сразу перейти к практике.",
        main_menu(),
    )


def process_update(update: dict[str, Any]) -> None:
    if "message" in update:
        message = update["message"]
        chat_id = message["chat"]["id"]
        user_id = message.get("from", {}).get("id")
        text = message.get("text", "")

        if not is_owner(user_id):
            deny(chat_id)
            return

        if text.startswith("/"):
            handle_command(chat_id, text)
        elif text:
            handle_text(chat_id, text)
        return

    if "callback_query" in update:
        callback = update["callback_query"]
        user_id = callback.get("from", {}).get("id")
        callback_id = callback["id"]

        if not is_owner(user_id):
            answer_callback(callback_id, "Доступ закрыт")
            return

        message = callback["message"]
        handle_callback(
            chat_id=message["chat"]["id"],
            message_id=message["message_id"],
            callback_id=callback_id,
            action=callback.get("data", ""),
        )


def poll_forever() -> None:
    logger.info("ClientPilot запущен. Владелец Telegram ID: %s", SETTINGS.owner_id)
    offset = 0

    while True:
        try:
            result = api_call(
                "getUpdates",
                {
                    "offset": offset,
                    "timeout": 30,
                    "allowed_updates": ["message", "callback_query"],
                },
                timeout=40,
            )

            for update in result.get("result", []):
                offset = max(offset, update["update_id"] + 1)
                process_update(update)

        except urllib.error.HTTPError as error:
            body = error.read().decode("utf-8", errors="replace")
            logger.error("HTTP ошибка: %s %s", error.code, body)
            time.sleep(5)
        except Exception:
            logger.exception("Ошибка в цикле бота")
            time.sleep(5)


def print_env_template() -> None:
    print(
        "TELEGRAM_BOT_TOKEN=\n"
        "TELEGRAM_OWNER_ID=\n"
        "OPENAI_API_KEY=\n"
        "OPENAI_MODEL=gpt-4.1-mini\n"
        "DATA_FILE=clientpilot_data.json\n"
        "MONTHLY_GOAL=50000\n"
        "CURRENCY=руб.\n"
    )


def print_service_template() -> None:
    current_file = Path(__file__).resolve()
    workdir = current_file.parent
    print(
        "[Unit]\n"
        "Description=ClientPilot Telegram bot\n"
        "After=network-online.target\n"
        "Wants=network-online.target\n\n"
        "[Service]\n"
        "Type=simple\n"
        f"WorkingDirectory={workdir}\n"
        f"ExecStart=/usr/bin/python3 {current_file}\n"
        "Restart=always\n"
        "RestartSec=5\n"
        "Environment=PYTHONUNBUFFERED=1\n\n"
        "[Install]\n"
        "WantedBy=multi-user.target\n"
    )


if __name__ == "__main__":
    if "--env-template" in sys.argv:
        print_env_template()
    elif "--service-template" in sys.argv:
        print_service_template()
    else:
        init_runtime()
        poll_forever()
