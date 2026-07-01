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
import re
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
        openai_model=os.getenv("OPENAI_MODEL", "gpt-5.5").strip(),
        data_file=BASE_DIR / os.getenv("DATA_FILE", "clientpilot_data.json"),
        monthly_goal=int(os.getenv("MONTHLY_GOAL", "300000")),
        currency=os.getenv("CURRENCY", "руб.").strip(),
    )


SETTINGS: Settings
TELEGRAM_API = ""
LAST_OPENAI_ERROR = ""


def init_runtime() -> None:
    global SETTINGS, TELEGRAM_API
    SETTINGS = read_settings()
    TELEGRAM_API = f"https://api.telegram.org/bot{SETTINGS.telegram_token}"


def model_candidates() -> list[str]:
    preferred = SETTINGS.openai_model
    candidates = [preferred, "gpt-5.5", "gpt-5.4", "gpt-5.4-mini", "gpt-4.1"]
    unique: list[str] = []
    for model in candidates:
        if model and model not in unique:
            unique.append(model)
    return unique


def esc(value: Any) -> str:
    return html.escape(str(value), quote=False)


def now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M")


def next_id(data: dict[str, Any], kind: str) -> str:
    counters = data.setdefault("counters", {"staff": 0, "event": 0})
    counters[kind] = int(counters.get(kind, 0)) + 1
    prefix = "s" if kind == "staff" else "e"
    return f"{prefix}{counters[kind]}"


def normalize_staff(data: dict[str, Any]) -> None:
    max_id = 0
    for item in data.get("staff", []):
        if "id" not in item:
            item["id"] = next_id(data, "staff")
        if str(item["id"]).startswith("s") and str(item["id"])[1:].isdigit():
            max_id = max(max_id, int(str(item["id"])[1:]))

        item.setdefault("name", item.get("имя", item.get("name", "Без имени")))
        item.setdefault("age", item.get("возраст", item.get("age", "")))
        item.setdefault("telegram", item.get("telegram", item.get("тег", "")))
        item.setdefault("roles", item.get("роль", item.get("roles", "")))
        item.setdefault("status", item.get("статус", item.get("status", "")))
        item.setdefault("note", item.get("заметка", item.get("note", "")))
        item.setdefault("projects", item.get("projects", []))
        item.setdefault("created_at", item.get("дата", item.get("created_at", now())))

    data.setdefault("counters", {"staff": 0, "event": 0})
    data["counters"]["staff"] = max(int(data["counters"].get("staff", 0)), max_id)


def normalize_events(data: dict[str, Any]) -> None:
    max_id = 0
    for event in data.get("events", []):
        if "id" not in event:
            event["id"] = next_id(data, "event")
        if str(event["id"]).startswith("e") and str(event["id"])[1:].isdigit():
            max_id = max(max_id, int(str(event["id"])[1:]))

        event.setdefault("title", "Мероприятие")
        event.setdefault("date", "")
        event.setdefault("location", "")
        event.setdefault("task", "")
        event.setdefault("contact", "")
        event.setdefault("slots", {})
        event.setdefault("created_at", now())

    data.setdefault("counters", {"staff": 0, "event": 0})
    data["counters"]["event"] = max(int(data["counters"].get("event", 0)), max_id)


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
        "events": [],
        "wins": [],
        "notes": [],
        "custom_commands": [],
        "counters": {"staff": 0, "event": 0},
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
    data.setdefault("counters", {"staff": 0, "event": 0})
    normalize_staff(data)
    normalize_events(data)
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
    global LAST_OPENAI_ERROR
    LAST_OPENAI_ERROR = ""

    if not SETTINGS.openai_api_key:
        LAST_OPENAI_ERROR = "OPENAI_API_KEY пустой или не найден в .env"
        return None

    errors: list[str] = []
    for model in model_candidates():
        payload = {"model": model, "input": prompt, "max_output_tokens": 1000}
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
                text = extract_openai_text(json.loads(response.read().decode("utf-8")))
                if text:
                    return text
                errors.append(f"{model}: пустой ответ")
        except urllib.error.HTTPError as error:
            body = error.read().decode("utf-8", errors="replace")
            errors.append(f"{model}: HTTP {error.code} {body[:300]}")
            logger.warning("OpenAI model %s failed: HTTP %s %s", model, error.code, body[:300])
        except Exception as error:
            errors.append(f"{model}: {error}")
            logger.warning("OpenAI model %s failed: %s", model, error)

    LAST_OPENAI_ERROR = " | ".join(errors) or "OpenAI не ответил"
    return None


def format_ai_response(text: str) -> str:
    """Convert common Markdown from the model into Telegram-safe HTML."""
    text = text.strip()
    text = re.sub(r"```(?:\w+)?\n(.*?)```", r"<code>\1</code>", text, flags=re.DOTALL)
    text = re.sub(r"`([^`\n]+)`", r"<code>\1</code>", text)
    text = esc(text)

    for tag in ("b", "u", "i", "code"):
        text = text.replace(f"&lt;{tag}&gt;", f"<{tag}>")
        text = text.replace(f"&lt;/{tag}&gt;", f"</{tag}>")

    text = re.sub(r"&lt;code&gt;(.*?)&lt;/code&gt;", r"<code>\1</code>", text, flags=re.DOTALL)
    text = re.sub(r"(?m)^###\s+(.+)$", r"<u>\1</u>", text)
    text = re.sub(r"(?m)^##\s+(.+)$", r"<b><u>\1</u></b>", text)
    text = re.sub(r"(?m)^#\s+(.+)$", r"<b><u>\1</u></b>", text)
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text)
    text = re.sub(r"__(.+?)__", r"<u>\1</u>", text)
    text = re.sub(r"\*(.+?)\*", r"<i>\1</i>", text)
    text = re.sub(r"(?m)^\s*[-*]\s+", "• ", text)

    return text.replace("&amp;nbsp;", " ")


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
            [("Мероприятия", "events"), ("Персонал", "staff")],
            [("Мои команды", "custom_menu")],
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


def send_typing(chat_id: int) -> None:
    try:
        api_call("sendChatAction", {"chat_id": chat_id, "action": "typing"}, timeout=10)
    except Exception:
        logger.warning("Не получилось отправить typing action")


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
        f"Лидов: {len(data.get('leads', []))}; мероприятий: {len(data.get('events', []))}; кандидатов: {len(data.get('staff', []))}."
    )


def ai_prompt(task: str, data: dict[str, Any]) -> str:
    return (
        "Ты личный бизнес-ассистент владельца. Ты знаешь текущий бизнес-контекст "
        "и даешь практичные советы: что написать, кому написать, как закрыть заказ, "
        "как подобрать персонал и как не сорвать мероприятие.\n\n"
        "Формат ответа для Telegram: используй только HTML-теги <b>, <u>, <i>, <code>. "
        "Не используй Markdown: никаких ##, ###, **жирного**, __подчеркивания__, звездочек для списков. "
        "Для списков используй короткие строки с символом • или нумерацию 1., 2., 3. "
        "Пиши чисто и удобно для чтения в Telegram.\n\n"
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


def staff_label(person: dict[str, Any], index: int | None = None) -> str:
    age = str(person.get("age", "")).strip()
    tg = str(person.get("telegram", "")).strip()
    base = str(person.get("name", "Без имени")).strip()
    if age:
        base += f", {age}"
        if age.isdigit():
            base += " лет"
    if tg:
        base += f" | {tg}"
    return f"{index}. {base}" if index is not None else base


def staff_menu(data: dict[str, Any]) -> dict[str, Any]:
    rows: list[list[tuple[str, str]]] = []
    for index, person in enumerate(data.get("staff", [])[:30], start=1):
        rows.append([(staff_label(person, index)[:60], f"staff_card:{person['id']}")])
    rows.append([("Добавить: /staff", "staff_help")])
    rows.append([("Назад в меню", "menu")])
    return keyboard(rows)


def staff_list_text(data: dict[str, Any]) -> str:
    if not data.get("staff"):
        return (
            "<b>Персонал</b>\n\n"
            "Пока никого нет.\n\n"
            "Добавь человека так:\n"
            "<code>/staff Маша | 18 | @masha | промо, хелпер | свободна | опыт 3 смены</code>"
        )
    return "<b>Персонал</b>\n\nНажми на человека, чтобы открыть карточку."


def find_staff(data: dict[str, Any], staff_id: str) -> dict[str, Any] | None:
    for person in data.get("staff", []):
        if str(person.get("id")) == str(staff_id):
            return person
    return None


def staff_projects(data: dict[str, Any], staff_id: str) -> list[str]:
    projects: list[str] = []
    for event in data.get("events", []):
        for role, slot in event.get("slots", {}).items():
            if staff_id in slot.get("staff_ids", []):
                projects.append(f"{event.get('title', 'Мероприятие')} — {role}, приход {slot.get('arrival', '-')}")
    return projects


def staff_card_text(data: dict[str, Any], person: dict[str, Any]) -> str:
    projects = staff_projects(data, str(person.get("id")))
    project_text = "\n".join(f"• {esc(project)}" for project in projects) if projects else "пока нет"
    return (
        f"<b>{esc(person.get('name', 'Без имени'))}</b>\n\n"
        f"<b>Возраст:</b> {esc(person.get('age', '-'))}\n"
        f"<b>Telegram:</b> {esc(person.get('telegram', '-'))}\n"
        f"<b>Роли:</b> {esc(person.get('roles', '-'))}\n"
        f"<b>Статус:</b> {esc(person.get('status', '-'))}\n"
        f"<b>Заметка:</b> {esc(person.get('note', '-'))}\n\n"
        f"<b>Проекты:</b>\n{project_text}"
    )


def events_menu(data: dict[str, Any]) -> dict[str, Any]:
    rows: list[list[tuple[str, str]]] = []
    for index, event in enumerate(data.get("events", [])[:30], start=1):
        title = f"{index}. {event.get('title', 'Мероприятие')} | {event.get('date', '')}"
        rows.append([(title[:60], f"event_card:{event['id']}")])
    rows.append([("Добавить: /event", "event_help")])
    rows.append([("Назад в меню", "menu")])
    return keyboard(rows)


def events_list_text(data: dict[str, Any]) -> str:
    if not data.get("events"):
        return (
            "<b>Мероприятия</b>\n\n"
            "Пока мероприятий нет.\n\n"
            "Добавь карточку так:\n"
            "<code>/event Корпоратив Альфа | 12.07 | Москва, Loft Hall | ТЗ: 4 хелпера, 2 промо, черная форма | Иван @client</code>"
        )
    return "<b>Мероприятия</b>\n\nНажми на мероприятие, чтобы открыть карточку."


def find_event(data: dict[str, Any], event_id: str) -> dict[str, Any] | None:
    for event in data.get("events", []):
        if str(event.get("id")) == str(event_id):
            return event
    return None


def resolve_event(data: dict[str, Any], value: str) -> dict[str, Any] | None:
    value = value.strip()
    if value.isdigit():
        index = int(value) - 1
        events = data.get("events", [])
        if 0 <= index < len(events):
            return events[index]
    return find_event(data, value)


def resolve_staff(data: dict[str, Any], value: str) -> dict[str, Any] | None:
    value = value.strip()
    if value.isdigit():
        index = int(value) - 1
        staff = data.get("staff", [])
        if 0 <= index < len(staff):
            return staff[index]
    return find_staff(data, value)


def event_card_text(data: dict[str, Any], event: dict[str, Any]) -> str:
    lines = [
        f"<b>{esc(event.get('title', 'Мероприятие'))}</b>",
        "",
        f"<b>Дата:</b> {esc(event.get('date', '-'))}",
        f"<b>Адрес:</b> {esc(event.get('location', '-'))}",
        f"<b>Контакт:</b> {esc(event.get('contact', '-'))}",
        f"<b>ТЗ:</b> {esc(event.get('task', '-'))}",
        "",
        "<b>Команда по слотам:</b>",
    ]

    slots = event.get("slots", {})
    if not slots:
        lines.append("пока никого не назначено")
    else:
        for role, slot in slots.items():
            lines.append("")
            lines.append(f"<u>{esc(role)}</u> — приход <b>{esc(slot.get('arrival', '-'))}</b>")
            staff_ids = slot.get("staff_ids", [])
            if not staff_ids:
                lines.append("• пока пусто")
            for staff_id in staff_ids:
                person = find_staff(data, staff_id)
                if person:
                    tg = person.get("telegram", "")
                    age = person.get("age", "")
                    lines.append(f"• {esc(person.get('name', 'Без имени'))}, {esc(age)} | {esc(tg)}")

    lines.append("")
    lines.append("<b>Добавить человека:</b>")
    lines.append("<code>/assign номер_мероприятия | номер_персонала | роль | время прихода</code>")
    lines.append("<b>Добавить слот:</b>")
    lines.append("<code>/slot номер_мероприятия | роль | время прихода</code>")
    return "\n".join(lines)


def custom_commands_text(data: dict[str, Any]) -> str:
    commands = data.get("custom_commands", [])
    if not commands:
        return (
            "<b>Мои команды</b>\n\n"
            "Пока команд нет.\n\n"
            "Добавь команду обычным сообщением:\n"
            "<code>добавь команду Проверить заказ, чтобы бот задал вопросы по дате, людям, ставке и рискам</code>\n\n"
            "Или вручную:\n"
            "<code>/cmd Проверить заказ | задай мне вопросы по новой заявке и найди риски</code>"
        )

    rows = []
    for index, command in enumerate(commands, start=1):
        rows.append(f"{index}. <b>{esc(command.get('title', 'Команда'))}</b>\n{esc(command.get('prompt', ''))}")
    return (
        "<b>Мои команды</b>\n\n"
        + "\n\n".join(rows)
        + "\n\nУдалить: <code>/delcmd номер</code>"
    )


def custom_commands_menu(data: dict[str, Any]) -> dict[str, Any]:
    rows: list[list[tuple[str, str]]] = []
    for index, command in enumerate(data.get("custom_commands", [])[:8]):
        rows.append([(str(command.get("title", "Команда"))[:45], f"custom:{index}")])
    rows.append([("Назад в меню", "menu")])
    return keyboard(rows)


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
    elif text.startswith("/event"):
        add_event(chat_id, text)
    elif text.startswith("/slot"):
        add_slot(chat_id, text)
    elif text.startswith("/assign"):
        assign_staff_to_event(chat_id, text)
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
    elif text.startswith("/cmds"):
        data = load_data()
        send_message(chat_id, custom_commands_text(data), custom_commands_menu(data))
    elif text.startswith("/cmd"):
        add_custom_command(chat_id, text)
    elif text.startswith("/delcmd"):
        delete_custom_command(chat_id, text)
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


def add_event(chat_id: int, text: str) -> None:
    parts = [part.strip() for part in text.replace("/event", "", 1).strip().split("|")]
    if len(parts) < 5:
        send_message(
            chat_id,
            "Формат:\n"
            "<code>/event Название | дата | адрес | ТЗ мероприятия | контакт заказчика</code>\n\n"
            "Пример:\n"
            "<code>/event Корпоратив Альфа | 12.07 | Loft Hall | 4 хелпера, 2 промо, черная форма | Иван @client</code>",
        )
        return

    data = load_data()
    event = {
        "id": next_id(data, "event"),
        "title": parts[0],
        "date": parts[1],
        "location": parts[2],
        "task": parts[3],
        "contact": parts[4],
        "slots": {},
        "created_at": now(),
    }
    data["events"].append(event)
    save_data(data)
    send_message(
        chat_id,
        "Мероприятие добавлено.\n\n"
        "Теперь добавь слоты ролей и время прихода:\n"
        "<code>/slot номер_мероприятия | хелперы | 13:00</code>\n"
        "<code>/slot номер_мероприятия | промо | 14:00</code>",
        events_menu(data),
    )


def add_slot(chat_id: int, text: str) -> None:
    parts = [part.strip() for part in text.replace("/slot", "", 1).strip().split("|")]
    if len(parts) < 3:
        send_message(chat_id, "Формат:\n<code>/slot номер_мероприятия | роль | время прихода</code>")
        return

    data = load_data()
    event = resolve_event(data, parts[0])
    if not event:
        send_message(chat_id, "Не нашел мероприятие. Нажми «Мероприятия» и посмотри номер.")
        return

    role = parts[1]
    event.setdefault("slots", {}).setdefault(role, {"arrival": parts[2], "staff_ids": []})
    event["slots"][role]["arrival"] = parts[2]
    save_data(data)
    send_message(chat_id, f"Слот <b>{esc(role)}</b> добавлен.", events_menu(data))


def assign_staff_to_event(chat_id: int, text: str) -> None:
    parts = [part.strip() for part in text.replace("/assign", "", 1).strip().split("|")]
    if len(parts) < 4:
        send_message(
            chat_id,
            "Формат:\n"
            "<code>/assign номер_мероприятия | номер_персонала | роль | время прихода</code>\n\n"
            "Пример:\n"
            "<code>/assign 1 | 2 | хелперы | 13:00</code>",
        )
        return

    data = load_data()
    event = resolve_event(data, parts[0])
    person = resolve_staff(data, parts[1])
    if not event:
        send_message(chat_id, "Не нашел мероприятие. Нажми «Мероприятия» и посмотри номер.")
        return
    if not person:
        send_message(chat_id, "Не нашел человека. Нажми «Персонал» и посмотри номер.")
        return

    role = parts[2]
    slot = event.setdefault("slots", {}).setdefault(role, {"arrival": parts[3], "staff_ids": []})
    slot["arrival"] = parts[3]
    staff_id = str(person["id"])
    if staff_id not in slot.setdefault("staff_ids", []):
        slot["staff_ids"].append(staff_id)

    project = f"{event.get('title', 'Мероприятие')} — {role}, приход {parts[3]}"
    person.setdefault("projects", [])
    if project not in person["projects"]:
        person["projects"].append(project)

    save_data(data)
    send_message(chat_id, f"{esc(person.get('name', ''))} назначен(а) на <b>{esc(event.get('title', ''))}</b>.", events_menu(data))


def add_staff(chat_id: int, text: str) -> None:
    parts = [part.strip() for part in text.replace("/staff", "", 1).strip().split("|")]
    if len(parts) < 4:
        send_message(
            chat_id,
            "Формат:\n"
            "<code>/staff Имя | возраст | telegram | роли | статус | заметка</code>\n\n"
            "Пример:\n"
            "<code>/staff Маша | 18 | @masha | промо, хелпер | свободна | опыт 3 смены</code>",
        )
        return
    data = load_data()
    if len(parts) >= 6:
        person = {
            "id": next_id(data, "staff"),
            "name": parts[0],
            "age": parts[1],
            "telegram": parts[2],
            "roles": parts[3],
            "status": parts[4],
            "note": parts[5],
            "projects": [],
            "created_at": now(),
        }
    else:
        person = {
            "id": next_id(data, "staff"),
            "name": parts[0],
            "age": "",
            "telegram": "",
            "roles": parts[1],
            "status": parts[2],
            "note": parts[3],
            "projects": [],
            "created_at": now(),
        }
    data["staff"].append(person)
    save_data(data)
    send_message(chat_id, "Кандидат добавлен в базу.", staff_menu(data))


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


def add_custom_command(chat_id: int, text: str) -> None:
    raw = text.replace("/cmd", "", 1).strip()
    parts = [part.strip() for part in raw.split("|", 1)]
    if len(parts) < 2 or not parts[0] or not parts[1]:
        send_message(chat_id, "Формат:\n<code>/cmd Название | что должна делать команда</code>")
        return

    data = load_data()
    data.setdefault("custom_commands", []).append({"title": parts[0][:45], "prompt": parts[1], "created_at": now()})
    save_data(data)
    send_message(chat_id, f"Команда <b>{esc(parts[0])}</b> добавлена.", custom_commands_menu(data))


def delete_custom_command(chat_id: int, text: str) -> None:
    raw = text.replace("/delcmd", "", 1).strip()
    if not raw.isdigit():
        send_message(chat_id, "Напиши номер:\n<code>/delcmd 1</code>")
        return

    index = int(raw) - 1
    data = load_data()
    commands = data.get("custom_commands", [])
    if index < 0 or index >= len(commands):
        send_message(chat_id, "Такой команды нет.", custom_commands_menu(data))
        return

    removed = commands.pop(index)
    save_data(data)
    send_message(chat_id, f"Команда <b>{esc(removed.get('title', ''))}</b> удалена.", custom_commands_menu(data))


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
        send_typing(chat_id)
        text = openai_call(ai_prompt("Составь план на сегодня для роста бизнеса на персонале для мероприятий.", data))
        edit_message(chat_id, message_id, format_ai_response(text) if text else fallback_daily_plan(data), back_menu())
    elif action == "find_orders":
        send_typing(chat_id)
        text = openai_call(ai_prompt("Дай конкретный план поиска заказов в Telegram-группах и пример ответа заказчику.", data))
        edit_message(chat_id, message_id, format_ai_response(text) if text else fallback_find_orders(data), back_menu())
    elif action == "find_staff":
        send_typing(chat_id)
        text = openai_call(ai_prompt("Дай план набора базы персонала и текст сообщения кандидату.", data))
        edit_message(chat_id, message_id, format_ai_response(text) if text else fallback_find_staff(data), back_menu())
    elif action == "client_script":
        send_typing(chat_id)
        text = openai_call(ai_prompt("Напиши сильный скрипт первого сообщения заказчику и список вопросов для брифа.", data))
        edit_message(chat_id, message_id, format_ai_response(text) if text else fallback_client_script(data), back_menu())
    elif action == "vetting":
        send_typing(chat_id)
        text = openai_call(ai_prompt("Составь законный чек-лист проверки кандидата на мероприятие без незаконного пробива.", data))
        edit_message(chat_id, message_id, format_ai_response(text) if text else fallback_vetting(), back_menu())
    elif action == "events":
        edit_message(chat_id, message_id, events_list_text(data), events_menu(data))
    elif action.startswith("event_card:"):
        event = find_event(data, action.split(":", 1)[1])
        edit_message(chat_id, message_id, event_card_text(data, event) if event else "Мероприятие не найдено.", back_menu())
    elif action == "event_help":
        edit_message(
            chat_id,
            message_id,
            "<b>Добавить мероприятие</b>\n\n"
            "<code>/event Название | дата | адрес | ТЗ мероприятия | контакт заказчика</code>\n\n"
            "<b>Добавить слот:</b>\n"
            "<code>/slot номер_мероприятия | роль | время прихода</code>\n\n"
            "<b>Назначить человека:</b>\n"
            "<code>/assign номер_мероприятия | номер_персонала | роль | время прихода</code>",
            back_menu(),
        )
    elif action == "staff":
        edit_message(chat_id, message_id, staff_list_text(data), staff_menu(data))
    elif action.startswith("staff_card:"):
        person = find_staff(data, action.split(":", 1)[1])
        edit_message(chat_id, message_id, staff_card_text(data, person) if person else "Человек не найден.", back_menu())
    elif action == "staff_help":
        edit_message(
            chat_id,
            message_id,
            "<b>Добавить персонал</b>\n\n"
            "<code>/staff Имя | возраст | telegram | роли | статус | заметка</code>\n\n"
            "Пример:\n"
            "<code>/staff Маша | 18 | @masha | промо, хелпер | свободна | опыт 3 смены</code>",
            back_menu(),
        )
    elif action == "settings":
        edit_message(chat_id, message_id, settings_text(data), back_menu())
    elif action == "money":
        edit_message(chat_id, message_id, money_text(data), back_menu())
    elif action == "custom_menu":
        edit_message(chat_id, message_id, custom_commands_text(data), custom_commands_menu(data))
    elif action.startswith("custom:"):
        run_custom_command(chat_id, message_id, data, action)
    elif action == "help":
        edit_message(chat_id, message_id, help_text(), back_menu())


def run_custom_command(chat_id: int, message_id: int, data: dict[str, Any], action: str) -> None:
    raw_index = action.split(":", 1)[1]
    if not raw_index.isdigit():
        edit_message(chat_id, message_id, "Команда не найдена.", back_menu())
        return

    index = int(raw_index)
    commands = data.get("custom_commands", [])
    if index < 0 or index >= len(commands):
        edit_message(chat_id, message_id, "Команда не найдена.", back_menu())
        return

    command = commands[index]
    send_typing(chat_id)
    prompt = (
        f"Выполни мою пользовательскую команду «{command.get('title', 'Команда')}».\n"
        f"Инструкция команды: {command.get('prompt', '')}\n\n"
        "Если для выполнения не хватает данных, задай мне короткие уточняющие вопросы."
    )
    text = openai_call(ai_prompt(prompt, data))
    edit_message(
        chat_id,
        message_id,
        format_ai_response(text) if text else "<b>ChatGPT не ответил</b>\n\nПроверь OPENAI_API_KEY и доступ к API.",
        back_menu(),
    )


def looks_like_control_request(text: str) -> bool:
    lower = text.lower()
    triggers = [
        "добавь команду",
        "создай команду",
        "добавь кнопку",
        "создай кнопку",
        "удали команду",
        "измени бизнес",
        "поменяй бизнес",
        "запомни бизнес",
        "измени оффер",
        "поменяй оффер",
        "измени город",
        "измени географию",
        "добавь роль",
        "измени роли",
        "измени каналы",
    ]
    return any(trigger in lower for trigger in triggers)


def extract_json_object(text: str) -> dict[str, Any] | None:
    match = re.search(r"\{.*\}", text, flags=re.DOTALL)
    if not match:
        return None
    try:
        return json.loads(match.group(0))
    except json.JSONDecodeError:
        return None


def control_prompt(user_text: str, data: dict[str, Any]) -> str:
    return (
        "Ты модуль внутреннего управления Telegram-ботом. "
        "Верни только JSON без Markdown и без пояснений.\n\n"
        "Разрешенные действия:\n"
        "1. add_command: создать пользовательскую команду.\n"
        "JSON: {\"action\":\"add_command\",\"title\":\"короткое название\",\"prompt\":\"что команда должна делать\"}\n\n"
        "2. delete_command: удалить команду по названию или номеру.\n"
        "JSON: {\"action\":\"delete_command\",\"title\":\"название или номер\"}\n\n"
        "3. update_profile: изменить бизнес-контекст.\n"
        "JSON: {\"action\":\"update_profile\",\"fields\":{\"offer\":\"...\",\"geography\":\"...\",\"staff_roles\":\"...\",\"channels\":\"...\",\"business_type\":\"...\",\"clients\":\"...\",\"positioning\":\"...\"}}\n\n"
        "Если пользователь не просит менять настройки или команды, верни {\"action\":\"none\"}.\n\n"
        f"Текущий контекст:\n{context_text(data)}\n\n"
        f"Запрос владельца: {user_text}"
    )


def handle_control_request(chat_id: int, text: str, data: dict[str, Any]) -> bool:
    if not looks_like_control_request(text):
        return False

    send_typing(chat_id)
    raw = openai_call(control_prompt(text, data))
    action = extract_json_object(raw or "")

    if not action:
        if "команд" in text.lower() or "кнопк" in text.lower():
            data.setdefault("custom_commands", []).append(
                {
                    "title": "Новая команда",
                    "prompt": text,
                    "created_at": now(),
                }
            )
            save_data(data)
            send_message(
                chat_id,
                "Я добавил команду как <b>Новая команда</b>.\n\n"
                "Чтобы назвать точнее, используй формат:\n"
                "<code>/cmd Название | что должна делать команда</code>",
                custom_commands_menu(data),
            )
            return True

        send_message(chat_id, "Не понял, что именно изменить. Можно так:\n<code>/cmd Название | что должна делать команда</code>")
        return True

    name = str(action.get("action", "none"))

    if name == "add_command":
        title = str(action.get("title", "Новая команда")).strip()[:45]
        prompt = str(action.get("prompt", "")).strip()
        if not prompt:
            prompt = text
        data.setdefault("custom_commands", []).append({"title": title, "prompt": prompt, "created_at": now()})
        save_data(data)
        send_message(chat_id, f"Готово. Добавил команду <b>{esc(title)}</b>.", custom_commands_menu(data))
        return True

    if name == "delete_command":
        needle = str(action.get("title", "")).strip().lower()
        commands = data.get("custom_commands", [])
        remove_index: int | None = None
        if needle.isdigit():
            remove_index = int(needle) - 1
        else:
            for index, command in enumerate(commands):
                if needle and needle in str(command.get("title", "")).lower():
                    remove_index = index
                    break
        if remove_index is None or remove_index < 0 or remove_index >= len(commands):
            send_message(chat_id, "Не нашел такую команду.", custom_commands_menu(data))
            return True
        removed = commands.pop(remove_index)
        save_data(data)
        send_message(chat_id, f"Удалил команду <b>{esc(removed.get('title', ''))}</b>.", custom_commands_menu(data))
        return True

    if name == "update_profile":
        allowed = {"offer", "geography", "staff_roles", "channels", "business_type", "clients", "positioning", "business_name"}
        fields = action.get("fields", {})
        changed = []
        if isinstance(fields, dict):
            for key, value in fields.items():
                if key in allowed and str(value).strip():
                    data["profile"][key] = str(value).strip()
                    changed.append(key)
        if changed:
            save_data(data)
            send_message(chat_id, "Готово. Обновил бизнес-контекст.", main_menu())
        else:
            send_message(chat_id, "Не понял, какие поля бизнес-контекста изменить.", main_menu())
        return True

    return False


def handle_text(chat_id: int, text: str) -> None:
    data = load_data()
    if handle_control_request(chat_id, text, data):
        return

    data["state"]["mode"] = "gpt_chat"
    save_data(data)
    send_typing(chat_id)
    answer = openai_call(ai_prompt(text, data))
    if answer:
        send_message(chat_id, format_ai_response(answer), chat_menu())
        return

    send_message(
        chat_id,
        "<b>ChatGPT не ответил</b>\n\n"
        f"Причина: <code>{esc(LAST_OPENAI_ERROR or 'неизвестная ошибка')}</code>\n\n"
        "Проверь `.env`: OPENAI_API_KEY должен быть заполнен, а на аккаунте OpenAI должен быть доступ к API и включена оплата.",
        chat_menu(),
    )


def help_text() -> str:
    return (
        "<b>Команды</b>\n\n"
        "/menu - главное меню\n"
        "/chat - прямой ChatGPT-режим\n"
        "/cmd Название | что должна делать команда\n"
        "/cmds - показать мои команды\n"
        "/delcmd номер - удалить мою команду\n"
        "/business Название | чем занимаемся | кому продаем\n"
        "/offer что предлагаем\n"
        "/geo город/регион\n"
        "/roles роли персонала\n"
        "/channels где искать клиентов и персонал\n"
        "/lead Название | статус | заметка\n"
        "/event Название | дата | адрес | ТЗ мероприятия | контакт\n"
        "/slot номер_мероприятия | роль | время прихода\n"
        "/assign номер_мероприятия | номер_персонала | роль | время прихода\n"
        "/staff Имя | возраст | telegram | роли | статус | заметка\n"
        "/win сумма | описание\n"
        "/setgoal сумма\n\n"
        "Можно писать обычным сообщением:\n"
        "<code>добавь команду Разбор заявки, чтобы бот проверял дату, бюджет, роли и риски</code>\n"
        "<code>измени оффер: закрываем персонал на мероприятия за 24 часа</code>\n\n"
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
        "OPENAI_MODEL=gpt-5.5\n"
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
