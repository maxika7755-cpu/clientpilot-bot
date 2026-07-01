#!/usr/bin/env python3
"""
ClientPilot: личный Telegram-бизнес-ассистент в одном файле.

Фокус по умолчанию: предоставление персонала на мероприятия.
Зависимости не нужны, только Python 3.10+.

Запуск на Ubuntu:
1. sudo apt update
2. sudo apt install -y python3 git
3. python3 clientpilot_bot.py --env-template > .env
4. nano .env
5. python3 clientpilot_bot.py
"""

from __future__ import annotations

import html
import json
import logging
import os
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any


BASE_DIR = Path(__file__).resolve().parent

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("clientpilot")


def load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


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
        openai_model=os.getenv("OPENAI_MODEL", "gpt-5.4").strip(),
        data_file=BASE_DIR / os.getenv("DATA_FILE", "clientpilot_data.json"),
        monthly_goal=int(os.getenv("MONTHLY_GOAL", "300000")),
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


def now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M")


def default_data() -> dict[str, Any]:
    return {
        "profile": {
            "goal": SETTINGS.monthly_goal,
            "currency": SETTINGS.currency,
            "business_name": "EventStaff",
            "business_type": "предоставление персонала на мероприятия",
            "geography": "город и область, можно изменить командой /geo",
            "offer": "быстро подбираем промоутеров, хостес, официантов, администраторов, грузчиков и разнорабочих на мероприятия",
            "clients": "организаторы мероприятий, event-агентства, заведения, выставки, промо-акции, частные заказчики",
            "channels": "Telegram-группы, чаты организаторов, чаты вакансий, личные рекомендации",
            "staff_roles": "промоутер, хостес, официант, администратор, координатор, грузчик, разнорабочий",
            "positioning": "закрываем срочные смены, держим базу проверенных людей, помогаем заказчику не сорвать мероприятие",
            "rules": "проверка кандидатов только законно: согласие, документы, опыт, рекомендации, договоренности, без незаконного сбора персональных данных",
        },
        "state": {"mode": "menu"},
        "leads": [],
        "orders": [],
        "staff": [],
        "wins": [],
        "notes": [],
    }


def migrate_data(data: dict[str, Any]) -> dict[str, Any]:
    base = default_data()
    data.setdefault("profile", {})
    data.setdefault("state", {})
    for key, value in base["profile"].items():
        data["profile"].setdefault(key, value)
    for key, value in base.items():
        if key not in ("profile", "state"):
            data.setdefault(key, value)
    data["state"].setdefault("mode", "menu")
    return data


def load_data() -> dict[str, Any]:
    if not SETTINGS.data_file.exists():
        return default_data()
    try:
        return migrate_data(json.loads(SETTINGS.data_file.read_text(encoding="utf-8")))
    except (json.JSONDecodeError, OSError):
        logger.exception("Не получилось прочитать базу. Создаю новую.")
        return default_data()


def save_data(data: dict[str, Any]) -> None:
    SETTINGS.data_file.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def api_call(method: str, payload: dict[str, Any] | None = None, timeout: int = 35) -> dict[str, Any]:
    request = urllib.request.Request(
        f"{TELEGRAM_API}/{method}",
        data=json.dumps(payload or {}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        result = json.loads(response.read().decode("utf-8"))
    if not result.get("ok"):
        raise RuntimeError(f"Telegram API error: {result}")
    return result


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


def openai_call(prompt: str) -> str | None:
    if not SETTINGS.openai_api_key:
        return None

    payload = {"model": SETTINGS.openai_model, "input": prompt, "max_output_tokens": 1000}
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
        with urllib.request.urlopen(request, timeout=60) as response:
            return extract_openai_text(json.loads(response.read().decode("utf-8")))
    except Exception:
        logger.exception("OpenAI не ответил, использую шаблон.")
        return None


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
            [("ChatGPT по бизнесу", "chat_gpt"), ("План на сегодня", "daily_plan")],
            [("Найти заказы", "find_orders"), ("Найти персонал", "find_staff")],
            [("Скрипт клиенту", "client_script"), ("Проверка кандидата", "vetting")],
            [("Заказы", "orders"), ("Персонал", "staff")],
            [("Бизнес-контекст", "settings"), ("Деньги", "money")],
            [("Помощь", "help")],
        ]
    )


def back_menu() -> dict[str, Any]:
    return keyboard([[("Назад в меню", "menu")]])


def chat_menu() -> dict[str, Any]:
    return keyboard([[("Выйти из ChatGPT", "exit_chat")], [("Назад в меню", "menu")]])


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


def is_owner(user_id: int | None) -> bool:
    return user_id == SETTINGS.owner_id


def deny(chat_id: int) -> None:
    send_message(chat_id, "Этот ассистент закрыт. Доступ есть только у владельца.")


def context_text(data: dict[str, Any]) -> str:
    p = data["profile"]
    return (
        f"Название: {p['business_name']}\n"
        f"Бизнес: {p['business_type']}\n"
        f"География: {p['geography']}\n"
        f"Оффер: {p['offer']}\n"
        f"Клиенты: {p['clients']}\n"
        f"Каналы поиска: {p['channels']}\n"
        f"Роли персонала: {p['staff_roles']}\n"
        f"Позиционирование: {p['positioning']}\n"
        f"Правила проверок: {p['rules']}\n"
        f"Лидов: {len(data.get('leads', []))}; заказов: {len(data.get('orders', []))}; кандидатов: {len(data.get('staff', []))}."
    )


def ai_prompt(task: str, data: dict[str, Any]) -> str:
    return (
        "Ты личный бизнес-ассистент владельца. Ты знаешь текущий бизнес-контекст "
        "и даешь практичные советы: что написать, кому написать, как закрыть заказ, "
        "как подобрать персонал и как не сорвать мероприятие.\n\n"
        "Важное правило безопасности: не помогай с незаконным пробивом людей, "
        "доксингом, покупкой персональных данных, скрытым сбором информации или "
        "обходом согласия. Вместо этого предлагай законную проверку кандидатов: "
        "согласие, документы, опыт, рекомендации, договор, чек-лист рисков и фиксацию договоренностей.\n\n"
        "Контекст бизнеса:\n"
        f"{context_text(data)}\n\n"
        f"Задача владельца: {task}"
    )


def start_text() -> str:
    return (
        "<b>ClientPilot</b>\n\n"
        "Я твой личный бизнес-ассистент для агентства персонала на мероприятия.\n"
        "Могу помогать с заказами, кандидатами, сообщениями, планом дня и прямым ChatGPT-режимом."
    )


def settings_text(data: dict[str, Any]) -> str:
    p = data["profile"]
    return (
        "<b>Бизнес-контекст</b>\n\n"
        f"<b>Название:</b> {esc(p['business_name'])}\n"
        f"<b>Бизнес:</b> {esc(p['business_type'])}\n"
        f"<b>География:</b> {esc(p['geography'])}\n"
        f"<b>Оффер:</b> {esc(p['offer'])}\n"
        f"<b>Клиенты:</b> {esc(p['clients'])}\n"
        f"<b>Каналы:</b> {esc(p['channels'])}\n"
        f"<b>Персонал:</b> {esc(p['staff_roles'])}\n\n"
        "Меняй контекст командами:\n"
        "<code>/business Название | чем занимаемся | кому продаем</code>\n"
        "<code>/offer что именно предлагаем</code>\n"
        "<code>/geo город/регион</code>\n"
        "<code>/roles роли персонала через запятую</code>\n"
        "<code>/channels где искать клиентов и персонал</code>"
    )


def fallback_daily_plan(data: dict[str, Any]) -> str:
    return (
        "<b>План на сегодня</b>\n\n"
        "1. Найди 20 свежих сообщений в Telegram-группах, где ищут персонал на мероприятия.\n"
        "2. Ответь 10 заказчикам коротким сообщением: кто ты, кого можешь закрыть, за сколько времени.\n"
        "3. Добавь всех в /order или /lead.\n"
        "4. Найди 10 кандидатов под самые частые роли: промоутер, хостес, официант, грузчик.\n"
        "5. Проверь кандидатов законно: согласие, опыт, документы, отзывы, готовность к смене.\n"
        "6. Вечером сделай повторное касание всем, кто не ответил.\n\n"
        "Цель дня: 1 реальный заказ или 3 теплых диалога."
    )


def fallback_find_orders(data: dict[str, Any]) -> str:
    return (
        "<b>Как искать заказы</b>\n\n"
        "Ищи в Telegram по запросам: персонал на мероприятие, нужны промоутеры, нужны хостес, "
        "официанты на банкет, грузчики на мероприятие, персонал срочно, event вакансии.\n\n"
        "<b>Ответ в группу:</b>\n"
        "Здравствуйте. Могу помочь с персоналом на мероприятие: промоутеры, хостес, официанты, "
        "администраторы, грузчики. Напишите дату, город, часы, количество людей и ставку - быстро скажу, кого сможем закрыть."
    )


def fallback_find_staff(data: dict[str, Any]) -> str:
    return (
        "<b>Как собирать базу персонала</b>\n\n"
        "Пиши кандидатам коротко: роль, дата, часы, ставка, требования, форма оплаты. "
        "Сразу спрашивай опыт, район, возраст 18+, фото/резюме по желанию, готовность к договоренностям.\n\n"
        "Команда для базы:\n"
        "<code>/staff Имя | роль | статус | заметка</code>"
    )


def fallback_client_script(data: dict[str, Any]) -> str:
    return (
        "<b>Сообщение заказчику</b>\n\n"
        "Здравствуйте. Я занимаюсь подбором персонала на мероприятия. Можем закрыть промоутеров, "
        "хостес, официантов, администраторов, грузчиков и разнорабочих.\n\n"
        "Чтобы быстро сориентировать вас, напишите, пожалуйста:\n"
        "1. дата и город;\n"
        "2. сколько людей нужно;\n"
        "3. часы работы;\n"
        "4. обязанности;\n"
        "5. ставка и формат оплаты.\n\n"
        "После этого скажу, кого реально вывести и на каких условиях."
    )


def fallback_vetting() -> str:
    return (
        "<b>Проверка кандидата законно</b>\n\n"
        "Я не помогаю с незаконным пробивом людей. Для бизнеса лучше использовать чистую схему:\n\n"
        "1. Получи согласие кандидата на проверку данных.\n"
        "2. Проверь ФИО, возраст 18+, город, контакт, опыт.\n"
        "3. Попроси фото/резюме только если это уместно для роли.\n"
        "4. Проверь рекомендации или прошлые смены.\n"
        "5. Зафиксируй ставку, часы, штрафы, форму одежды, адрес, контакт координатора.\n"
        "6. Для важных ролей делай короткий созвон.\n\n"
        "Команда для записи:\n"
        "<code>/staff Иван | промоутер | проверен | опыт 5 смен, готов завтра</code>"
    )


def list_items(title: str, items: list[dict[str, Any]], fields: list[str], empty: str) -> str:
    if not items:
        return f"<b>{title}</b>\n\n{empty}"
    rows = []
    for item in items[-10:]:
        row = []
        for field in fields:
            row.append(f"<b>{esc(field)}:</b> {esc(item.get(field, '-'))}")
        rows.append("\n".join(row))
    return f"<b>{title}</b>\n\n" + "\n\n".join(rows)


def money_text(data: dict[str, Any]) -> str:
    p = data["profile"]
    wins = data.get("wins", [])
    total = sum(int(win.get("amount", 0)) for win in wins)
    goal = int(p.get("goal", SETTINGS.monthly_goal))
    left = max(goal - total, 0)
    currency = p.get("currency", SETTINGS.currency)
    return (
        "<b>Деньги</b>\n\n"
        f"Цель на месяц: <b>{goal} {esc(currency)}</b>\n"
        f"Записано продаж: <b>{total} {esc(currency)}</b>\n"
        f"Осталось: <b>{left} {esc(currency)}</b>\n\n"
        "Записать оплату: <code>/win 15000 | заказ на промоутеров</code>"
    )


def set_profile_value(chat_id: int, key: str, value: str, label: str) -> None:
    data = load_data()
    data["profile"][key] = value.strip()
    save_data(data)
    send_message(chat_id, f"{label} обновлено.", main_menu())


def handle_start(chat_id: int) -> None:
    send_message(chat_id, start_text(), main_menu())


def handle_command(chat_id: int, text: str) -> None:
    if text.startswith("/start") or text.startswith("/menu"):
        data = load_data()
        data["state"]["mode"] = "menu"
        save_data(data)
        handle_start(chat_id)
    elif text.startswith("/lead"):
        add_lead(chat_id, text)
    elif text.startswith("/order"):
        add_order(chat_id, text)
    elif text.startswith("/staff"):
        add_staff(chat_id, text)
    elif text.startswith("/win"):
        add_win(chat_id, text)
    elif text.startswith("/setgoal"):
        set_goal(chat_id, text)
    elif text.startswith("/business"):
        set_business(chat_id, text)
    elif text.startswith("/offer"):
        set_profile_value(chat_id, "offer", text.replace("/offer", "", 1), "Оффер")
    elif text.startswith("/geo"):
        set_profile_value(chat_id, "geography", text.replace("/geo", "", 1), "География")
    elif text.startswith("/roles"):
        set_profile_value(chat_id, "staff_roles", text.replace("/roles", "", 1), "Роли персонала")
    elif text.startswith("/channels"):
        set_profile_value(chat_id, "channels", text.replace("/channels", "", 1), "Каналы поиска")
    elif text.startswith("/chat"):
        enter_chat(chat_id)
    else:
        send_message(chat_id, "Не знаю такую команду. Нажми /menu.", main_menu())


def add_lead(chat_id: int, text: str) -> None:
    parts = [part.strip() for part in text.replace("/lead", "", 1).strip().split("|")]
    if len(parts) < 3:
        send_message(chat_id, "Формат:\n<code>/lead Название | статус | заметка</code>")
        return
    data = load_data()
    data["leads"].append({"название": parts[0], "статус": parts[1], "заметка": parts[2], "дата": now()})
    save_data(data)
    send_message(chat_id, "Лид добавлен.", main_menu())


def add_order(chat_id: int, text: str) -> None:
    parts = [part.strip() for part in text.replace("/order", "", 1).strip().split("|")]
    if len(parts) < 5:
        send_message(chat_id, "Формат:\n<code>/order Клиент | дата | кого нужно | бюджет | статус</code>")
        return
    data = load_data()
    data["orders"].append(
        {"клиент": parts[0], "дата": parts[1], "персонал": parts[2], "бюджет": parts[3], "статус": parts[4], "создано": now()}
    )
    save_data(data)
    send_message(chat_id, "Заказ добавлен.", main_menu())


def add_staff(chat_id: int, text: str) -> None:
    parts = [part.strip() for part in text.replace("/staff", "", 1).strip().split("|")]
    if len(parts) < 4:
        send_message(chat_id, "Формат:\n<code>/staff Имя | роль | статус | заметка</code>")
        return
    data = load_data()
    data["staff"].append({"имя": parts[0], "роль": parts[1], "статус": parts[2], "заметка": parts[3], "дата": now()})
    save_data(data)
    send_message(chat_id, "Кандидат добавлен в базу.", main_menu())


def add_win(chat_id: int, text: str) -> None:
    parts = [part.strip() for part in text.replace("/win", "", 1).strip().split("|")]
    if len(parts) < 2 or not parts[0].isdigit():
        send_message(chat_id, "Формат:\n<code>/win 15000 | заказ на промоутеров</code>")
        return
    data = load_data()
    data["wins"].append({"amount": int(parts[0]), "описание": parts[1], "дата": now()})
    save_data(data)
    send_message(chat_id, "Деньги записаны.", main_menu())


def set_goal(chat_id: int, text: str) -> None:
    raw = text.replace("/setgoal", "", 1).strip()
    if not raw.isdigit():
        send_message(chat_id, "Напиши так:\n<code>/setgoal 300000</code>")
        return
    data = load_data()
    data["profile"]["goal"] = int(raw)
    save_data(data)
    send_message(chat_id, "Цель обновлена.", main_menu())


def set_business(chat_id: int, text: str) -> None:
    parts = [part.strip() for part in text.replace("/business", "", 1).strip().split("|")]
    if len(parts) < 3:
        send_message(chat_id, "Формат:\n<code>/business Название | чем занимаемся | кому продаем</code>")
        return
    data = load_data()
    data["profile"]["business_name"] = parts[0]
    data["profile"]["business_type"] = parts[1]
    data["profile"]["clients"] = parts[2]
    save_data(data)
    send_message(chat_id, "Бизнес-контекст обновлен.", main_menu())


def enter_chat(chat_id: int) -> None:
    data = load_data()
    data["state"]["mode"] = "gpt_chat"
    save_data(data)
    send_message(
        chat_id,
        "<b>ChatGPT-режим включен</b>\n\n"
        "Пиши любой вопрос по бизнесу. Я буду отвечать с учетом твоего направления, заказов, персонала и правил проверки кандидатов.",
        chat_menu(),
    )


def handle_callback(chat_id: int, message_id: int, callback_id: str, action: str) -> None:
    answer_callback(callback_id)
    data = load_data()

    if action == "menu":
        data["state"]["mode"] = "menu"
        save_data(data)
        edit_message(chat_id, message_id, start_text(), main_menu())
    elif action == "exit_chat":
        data["state"]["mode"] = "menu"
        save_data(data)
        edit_message(chat_id, message_id, "ChatGPT-режим выключен.", main_menu())
    elif action == "chat_gpt":
        data["state"]["mode"] = "gpt_chat"
        save_data(data)
        edit_message(chat_id, message_id, "<b>ChatGPT-режим включен</b>\n\nНапиши вопрос следующим сообщением.", chat_menu())
    elif action == "daily_plan":
        text = openai_call(ai_prompt("Составь план на сегодня для роста бизнеса на персонале для мероприятий.", data))
        edit_message(chat_id, message_id, text or fallback_daily_plan(data), back_menu())
    elif action == "find_orders":
        text = openai_call(ai_prompt("Дай конкретный план поиска заказов в Telegram-группах и пример ответа заказчику.", data))
        edit_message(chat_id, message_id, text or fallback_find_orders(data), back_menu())
    elif action == "find_staff":
        text = openai_call(ai_prompt("Дай план набора базы персонала и текст сообщения кандидату.", data))
        edit_message(chat_id, message_id, text or fallback_find_staff(data), back_menu())
    elif action == "client_script":
        text = openai_call(ai_prompt("Напиши сильный скрипт первого сообщения заказчику и список вопросов для брифа.", data))
        edit_message(chat_id, message_id, text or fallback_client_script(data), back_menu())
    elif action == "vetting":
        text = openai_call(ai_prompt("Составь законный чек-лист проверки кандидата на мероприятие без незаконного пробива.", data))
        edit_message(chat_id, message_id, text or fallback_vetting(), back_menu())
    elif action == "orders":
        edit_message(chat_id, message_id, list_items("Заказы", data["orders"], ["клиент", "дата", "персонал", "бюджет", "статус"], "Пока заказов нет.\n\n<code>/order Клиент | дата | кого нужно | бюджет | статус</code>"), back_menu())
    elif action == "staff":
        edit_message(chat_id, message_id, list_items("Персонал", data["staff"], ["имя", "роль", "статус", "заметка"], "Пока кандидатов нет.\n\n<code>/staff Имя | роль | статус | заметка</code>"), back_menu())
    elif action == "settings":
        edit_message(chat_id, message_id, settings_text(data), back_menu())
    elif action == "money":
        edit_message(chat_id, message_id, money_text(data), back_menu())
    elif action == "help":
        edit_message(chat_id, message_id, help_text(), back_menu())


def handle_text(chat_id: int, text: str) -> None:
    data = load_data()
    if data.get("state", {}).get("mode") == "gpt_chat":
        answer = openai_call(ai_prompt(text, data))
        send_message(chat_id, answer or "OpenAI-ключ не подключен или модель не ответила. Я могу работать кнопками и шаблонами.", chat_menu())
        return

    answer = openai_call(ai_prompt(f"Ответь коротко и предложи следующее действие. Сообщение владельца: {text}", data))
    send_message(chat_id, answer or "Я понял. Нажми кнопку в меню или включи ChatGPT-режим.", main_menu())


def help_text() -> str:
    return (
        "<b>Команды</b>\n\n"
        "/menu - главное меню\n"
        "/chat - прямой ChatGPT-режим\n"
        "/business Название | чем занимаемся | кому продаем\n"
        "/offer что предлагаем\n"
        "/geo город/регион\n"
        "/roles роли персонала\n"
        "/channels где искать клиентов и персонал\n"
        "/lead Название | статус | заметка\n"
        "/order Клиент | дата | кого нужно | бюджет | статус\n"
        "/staff Имя | роль | статус | заметка\n"
        "/win сумма | описание\n"
        "/setgoal сумма\n\n"
        "Бот закрыт: отвечает только владельцу по TELEGRAM_OWNER_ID."
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
        handle_callback(message["chat"]["id"], message["message_id"], callback_id, callback.get("data", ""))


def poll_forever() -> None:
    logger.info("ClientPilot запущен. Владелец Telegram ID: %s", SETTINGS.owner_id)
    offset = 0
    while True:
        try:
            result = api_call(
                "getUpdates",
                {"offset": offset, "timeout": 30, "allowed_updates": ["message", "callback_query"]},
                timeout=40,
            )
            for update in result.get("result", []):
                offset = max(offset, update["update_id"] + 1)
                process_update(update)
        except urllib.error.HTTPError as error:
            logger.error("HTTP ошибка: %s %s", error.code, error.read().decode("utf-8", errors="replace"))
            time.sleep(5)
        except Exception:
            logger.exception("Ошибка в цикле бота")
            time.sleep(5)


def print_env_template() -> None:
    print(
        "TELEGRAM_BOT_TOKEN=\n"
        "TELEGRAM_OWNER_ID=\n"
        "OPENAI_API_KEY=\n"
        "OPENAI_MODEL=gpt-5.4\n"
        "DATA_FILE=clientpilot_data.json\n"
        "MONTHLY_GOAL=300000\n"
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
