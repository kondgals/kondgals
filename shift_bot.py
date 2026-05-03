import asyncio
import json
import logging
import os
import sqlite3
from datetime import datetime, time, timedelta
from pathlib import Path
from typing import Any

from aiogram import Bot, Dispatcher, types
from aiogram.client.default import DefaultBotProperties
from aiogram.filters import Command
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from dotenv import load_dotenv
from playwright.async_api import TimeoutError as PlaywrightTimeout
from playwright.async_api import async_playwright

load_dotenv()

EMAIL = os.getenv("EMAIL", "").strip()
PASSWORD = os.getenv("PASSWORD", "").strip()
TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
CHAT_ID_RAW = os.getenv("CHAT_ID", "").strip()
DEFAULT_CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "300"))

LOGIN_URL = "https://part-time.gymbeam.com/web/login"
SHIFT_URL = "https://part-time.gymbeam.com/news"

SENT_FILE = Path("sent_shifts.json")
SETTINGS_FILE = Path("user_settings.json")
LOG_FILE = Path("shift_log.txt")
DB_FILE = Path("shift_bot.db")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    handlers=[logging.FileHandler(LOG_FILE, encoding="utf-8"), logging.StreamHandler()],
)
logger = logging.getLogger(__name__)


def validate_config() -> int:
    missing = []
    for key, val in {
        "EMAIL": EMAIL,
        "PASSWORD": PASSWORD,
        "TELEGRAM_TOKEN": TOKEN,
        "CHAT_ID": CHAT_ID_RAW,
    }.items():
        if not val:
            missing.append(key)

    if missing:
        raise RuntimeError(f"Відсутні ENV-змінні: {', '.join(missing)}")

    try:
        return int(CHAT_ID_RAW)
    except ValueError as exc:
        raise RuntimeError("CHAT_ID має бути числом") from exc


CHAT_ID = validate_config()
bot = Bot(token=TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
dp = Dispatcher()
check_lock = asyncio.Lock()

state: dict[str, Any] = {
    "last_check": None,
    "last_success": None,
    "last_error": None,
    "last_total_found": 0,
    "last_sent": 0,
}


def load_json_file(path: Path, fallback: Any) -> Any:
    if not path.exists():
        return fallback
    try:
        with path.open(encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("Не вдалося прочитати %s: %s", path, exc)
        return fallback


def save_json_file(path: Path, payload: Any) -> None:
    with path.open("w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)


def normalize_names(names: list[str]) -> list[str]:
    out = []
    for n in names:
        n = n.strip()
        if n:
            out.append(n)
    return out


def load_settings() -> dict[str, Any]:
    default = {
        "notifications_enabled": True,
        "allowed_types": ["Експорт", "Прийом товару", "Вироба"],
        "allowed_periods": ["Ранок", "День", "Вечір"],
        "allowed_responsibles": [],
        "check_interval": DEFAULT_CHECK_INTERVAL,
    }
    data = load_json_file(SETTINGS_FILE, default)
    if not isinstance(data, dict):
        return default

    return {
        "notifications_enabled": bool(data.get("notifications_enabled", True)),
        "allowed_types": list(data.get("allowed_types", default["allowed_types"])),
        "allowed_periods": list(data.get("allowed_periods", default["allowed_periods"])),
        "allowed_responsibles": normalize_names(list(data.get("allowed_responsibles", []))),
        "check_interval": int(data.get("check_interval", DEFAULT_CHECK_INTERVAL)),
    }


def save_settings(s: dict[str, Any]) -> None:
    save_json_file(SETTINGS_FILE, s)


def save_sent_shifts() -> None:
    save_json_file(SENT_FILE, sorted(sent_shifts))


def init_db() -> None:
    with sqlite3.connect(DB_FILE) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS sent_shifts (
                shift_key TEXT PRIMARY KEY,
                created_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS check_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                check_time TEXT NOT NULL,
                found_count INTEGER NOT NULL,
                sent_count INTEGER NOT NULL,
                status TEXT NOT NULL,
                error_message TEXT
            )
            """
        )
        conn.commit()


def load_sent_shifts_db() -> set[str]:
    with sqlite3.connect(DB_FILE) as conn:
        rows = conn.execute("SELECT shift_key FROM sent_shifts").fetchall()
    return {r[0] for r in rows}


def save_shift_key_db(shift_key: str) -> None:
    with sqlite3.connect(DB_FILE) as conn:
        conn.execute(
            "INSERT OR IGNORE INTO sent_shifts(shift_key, created_at) VALUES (?, ?)",
            (shift_key, datetime.now().isoformat()),
        )
        conn.commit()


def reset_sent_shifts_db() -> None:
    with sqlite3.connect(DB_FILE) as conn:
        conn.execute("DELETE FROM sent_shifts")
        conn.commit()


def insert_check_history(found: int, sent: int, status: str, error: str | None = None) -> None:
    with sqlite3.connect(DB_FILE) as conn:
        conn.execute(
            """
            INSERT INTO check_history(check_time, found_count, sent_count, status, error_message)
            VALUES (?, ?, ?, ?, ?)
            """,
            (datetime.now().isoformat(), found, sent, status, error),
        )
        conn.commit()


def get_shift_type(responsible: str) -> str:
    responsible = responsible.strip()
    if any(name in responsible for name in ["Matúš Semanič", "Lukáš Bujnovský"]):
        return "Експорт"
    if any(name in responsible for name in ["Jakub Meliš", "Karol Matto"]):
        return "Прийом товару"
    return "Вироба"


def get_time_period(time_str: str) -> str:
    try:
        t = datetime.strptime(time_str, "%H:%M").time()
        if time(6, 0) <= t < time(14, 0):
            return "Ранок"
        if time(14, 0) <= t < time(22, 0):
            return "День"
        return "Вечір"
    except ValueError:
        return "День"


def get_emoji(shift_type: str, period: str) -> str:
    type_emoji = {"Експорт": "🚛", "Прийом товару": "📦", "Вироба": "⚙️"}.get(shift_type, "📌")
    period_emoji = {"Ранок": "🌅", "День": "☀️", "Вечір": "🌙"}.get(period, "")
    return f"{period_emoji}{type_emoji}"


def make_shift_key(shift: dict[str, str]) -> str:
    return f"{shift['date']}|{shift['time_from']}|{shift['time_to']}|{shift['responsible'].strip().lower()}"


def shift_duration_hours(time_from: str, time_to: str, break_minutes: int = 30) -> float:
    start = datetime.strptime(time_from, "%H:%M")
    end = datetime.strptime(time_to, "%H:%M")
    if end <= start:
        end = end + timedelta(days=1)
    minutes = int((end - start).total_seconds() // 60) - break_minutes
    return max(minutes, 0) / 60.0


def shift_passes_filters(shift: dict[str, str]) -> bool:
    if shift["type"] not in settings.get("allowed_types", []):
        return False

    period = get_time_period(shift["time_from"])
    if period not in settings.get("allowed_periods", []):
        return False

    allowed_resp = [n.lower() for n in settings.get("allowed_responsibles", [])]
    if allowed_resp and shift["responsible"].strip().lower() not in allowed_resp:
        return False

    return True


async def login(page) -> None:
    await page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=45000)

    login_selectors = ['input[name="login"]', 'input[type="email"]', 'input[autocomplete="username"]']
    pass_selectors = ['input[name="password"]', 'input[type="password"]', 'input[autocomplete="current-password"]']

    login_field = None
    for sel in login_selectors:
        loc = page.locator(sel).first
        if await loc.count() > 0:
            login_field = loc
            break

    pass_field = None
    for sel in pass_selectors:
        loc = page.locator(sel).first
        if await loc.count() > 0:
            pass_field = loc
            break

    if not login_field or not pass_field:
        raise PlaywrightTimeout("Не знайдено поля логіну/пароля")

    await login_field.fill(EMAIL, timeout=45000)
    await pass_field.fill(PASSWORD, timeout=45000)

    submit = page.locator('button[type="submit"], button:has-text("Sign in"), button:has-text("Login")').first
    if await submit.count() > 0:
        await submit.click()
    else:
        await pass_field.press("Enter")

    await page.wait_for_load_state("networkidle", timeout=45000)




async def get_section_by_heading(page, heading_text: str):
    heading = page.locator(f"h1:has-text('{heading_text}'), h2:has-text('{heading_text}'), h3:has-text('{heading_text}')").first
    if await heading.count() == 0:
        return None

    # Table is usually the next sibling container after heading.
    container = heading.locator("xpath=following::table[1]").first
    if await container.count() == 0:
        return None
    return container


async def parse_shift_rows(table_locator, today_only_future: bool = True) -> list[dict[str, str]]:
    shifts: list[dict[str, str]] = []
    today = datetime.now().date()
    rows = await table_locator.locator("tbody tr").all()

    for row in rows:
        cells = await row.locator("td").all()
        if len(cells) < 4:
            continue

        date_str = (await cells[0].inner_text()).strip()
        time_from = (await cells[1].inner_text()).strip()
        time_to = (await cells[2].inner_text()).strip()
        responsible = (await cells[3].inner_text()).strip() or "—"

        try:
            shift_date = datetime.strptime(date_str, "%d.%m.%Y").date()
        except ValueError:
            continue

        if today_only_future and shift_date < today:
            continue

        shifts.append(
            {
                "date": date_str,
                "time_from": time_from,
                "time_to": time_to,
                "responsible": responsible,
                "type": get_shift_type(responsible),
            }
        )

    return shifts

async def get_shifts(page) -> list[dict[str, str]]:
    await page.goto(SHIFT_URL, wait_until="domcontentloaded", timeout=30000)
    await page.wait_for_selector("table", timeout=30000)

    invitations_table = await get_section_by_heading(page, "My Invitations")
    if invitations_table is None:
        logger.warning("Блок 'My Invitations' не знайдено")
        return []

    shifts = await parse_shift_rows(invitations_table, today_only_future=True)
    logger.info("Зчитано %s змін саме з 'My Invitations'", len(shifts))
    return shifts


def format_shift_line(shift: dict[str, str]) -> str:
    period = get_time_period(shift["time_from"])
    emoji = get_emoji(shift["type"], period)
    return f"{emoji} <b>{shift['date']}</b> {shift['time_from']}–{shift['time_to']} | {shift['type']} | {shift['responsible']}"


async def send_grouped_shifts(shifts: list[dict[str, str]]) -> int:
    if not shifts:
        return 0

    lines = [format_shift_line(s) for s in shifts]
    chunks = []
    current = "📢 <b>Нові доступні зміни:</b>\n"
    for line in lines:
        if len(current) + len(line) + 1 > 3800:
            chunks.append(current)
            current = line + "\n"
        else:
            current += line + "\n"
    if current.strip():
        chunks.append(current)

    for chunk in chunks:
        await bot.send_message(CHAT_ID, chunk.strip())
        await asyncio.sleep(0.3)

    return len(chunks)


def classify_error(exc: Exception) -> str:
    text = str(exc).lower()
    if "no healthy upstream" in text:
        return "Сервер тимчасово недоступний (no healthy upstream)"
    if "timeout" in text:
        return "Таймаут підключення"
    return str(exc)


async def retry_async(coro_factory, attempts: int = 2, base_delay: float = 1.0, context: str = "operation"):
    last_exc = None
    for attempt in range(1, attempts + 1):
        try:
            return await coro_factory()
        except Exception as exc:
            last_exc = exc
            if attempt == attempts:
                break
            delay = base_delay * attempt
            logger.warning("%s failed (%s/%s): %s. Retry in %.1fs", context, attempt, attempts, exc, delay)
            await asyncio.sleep(delay)
    raise last_exc


async def check_site(is_manual: bool = False, message: types.Message | None = None) -> None:
    if not settings.get("notifications_enabled", True) and not is_manual:
        return

    if check_lock.locked() and is_manual and message:
        await message.edit_text("⏳ Перевірка вже виконується, зачекай кілька секунд.")
        return

    async with check_lock:
        logger.info("Починаю перевірку сайту...")
        state["last_check"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        browser = None
        try:
            async with async_playwright() as p:
                browser = await p.chromium.launch(headless=True)
                page = await browser.new_page()
                await retry_async(lambda: login(page), attempts=2, base_delay=1.2, context="login")
                shifts = await retry_async(lambda: get_shifts(page), attempts=2, base_delay=1.2, context="get_shifts")

                new_shifts = []
                for shift in shifts:
                    key = make_shift_key(shift)
                    if key in sent_shifts:
                        continue
                    if not shift_passes_filters(shift):
                        continue
                    sent_shifts.add(key)
                    save_shift_key_db(key)
                    new_shifts.append(shift)

                if new_shifts:
                    await send_grouped_shifts(new_shifts)
                    save_sent_shifts()

                state["last_success"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                state["last_error"] = None
                state["last_total_found"] = len(shifts)
                state["last_sent"] = len(new_shifts)
                insert_check_history(len(shifts), len(new_shifts), "ok")

                if is_manual and message:
                    await message.edit_text(
                        f"✅ Перевірка завершена.\nЗнайдено: {len(shifts)}\nНових: {len(new_shifts)}"
                    )
        except PlaywrightTimeout as exc:
            msg = classify_error(exc)
            logger.exception(msg)
            state["last_error"] = msg
            insert_check_history(state["last_total_found"], 0, "error", msg)
            if is_manual and message:
                await message.edit_text(f"⚠️ {msg}")
        except Exception as exc:
            msg = classify_error(exc)
            logger.exception("Помилка перевірки")
            state["last_error"] = msg
            insert_check_history(state["last_total_found"], 0, "error", msg)
            if is_manual and message:
                await message.edit_text(f"⚠️ {msg}")
        finally:
            if browser:
                await browser.close()


def main_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🔄 Перевірити зараз", callback_data="check_now")],
            [InlineKeyboardButton(text="📋 Показати всі зміни", callback_data="show_all")],
            [InlineKeyboardButton(text="📊 Статус", callback_data="status")],
            [InlineKeyboardButton(text="⚙️ Налаштування", callback_data="settings")],
            [InlineKeyboardButton(text="♻️ Очистити пам'ять", callback_data="reset_memory")],
        ]
    )




def settings_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="⚙️ Типи", callback_data="set_types")],
            [InlineKeyboardButton(text="🕒 Періоди", callback_data="set_periods")],
            [InlineKeyboardButton(text="👤 Ведучі", callback_data="resp_dev")],
            [InlineKeyboardButton(text="⏱ Інтервал", callback_data="set_interval")],
            [InlineKeyboardButton(text="← Назад", callback_data="back_main")],
        ]
    )


def interval_menu() -> InlineKeyboardMarkup:
    opts = [60, 120, 300, 600, 900]
    kb = []
    for v in opts:
        mark = "✅ " if settings.get("check_interval", DEFAULT_CHECK_INTERVAL) == v else ""
        kb.append([InlineKeyboardButton(text=f"{mark}{v} сек", callback_data=f"interval_{v}")])
    kb.append([InlineKeyboardButton(text="← Назад", callback_data="settings")])
    return InlineKeyboardMarkup(inline_keyboard=kb)
def types_menu() -> InlineKeyboardMarkup:
    kb = []
    for t in ["Експорт", "Прийом товару", "Вироба"]:
        on = "✅" if t in settings.get("allowed_types", []) else "☐"
        kb.append([InlineKeyboardButton(text=f"{on} {t}", callback_data=f"toggle_type_{t}")])
    kb.append([InlineKeyboardButton(text="← Назад", callback_data="back_main")])
    return InlineKeyboardMarkup(inline_keyboard=kb)


def periods_menu() -> InlineKeyboardMarkup:
    kb = []
    for p in ["Ранок", "День", "Вечір"]:
        on = "✅" if p in settings.get("allowed_periods", []) else "☐"
        kb.append([InlineKeyboardButton(text=f"{on} {p}", callback_data=f"toggle_period_{p}")])
    kb.append([InlineKeyboardButton(text="← Назад", callback_data="back_main")])
    return InlineKeyboardMarkup(inline_keyboard=kb)


@dp.message(Command("start"))
async def start_cmd(message: types.Message) -> None:
    if message.from_user.id != CHAT_ID:
        return
    await message.answer("✅ <b>GymBeam Shift Monitor</b> запущено", reply_markup=main_menu())


@dp.message(Command("help"))
async def help_cmd(message: types.Message) -> None:
    if message.from_user.id != CHAT_ID:
        return
    await message.answer(
        "ℹ️ <b>Команди</b>\n"
        "/start — головне меню\n"
        "/check — перевірити зараз\n"
        "/all — показати всі зміни\n"
        "/status — статус бота\n"
        "/mystats — статистика акаунта\n"
        "/selfcheck — технічна діагностика\n"
        "/type export|inbound|production|all\n"
        "/period morning|day|night|all\n"
        "/settings — налаштування",
        reply_markup=main_menu(),
    )


@dp.message(Command("type"))
async def type_cmd(message: types.Message) -> None:
    if message.from_user.id != CHAT_ID:
        return
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2:
        await message.answer("Використай: /type export|inbound|production|all")
        return
    value = parts[1].strip().lower()
    mapping = {"export": "Експорт", "inbound": "Прийом товару", "production": "Вироба"}
    if value == "all":
        settings["allowed_types"] = list(mapping.values())
    elif value in mapping:
        settings["allowed_types"] = [mapping[value]]
    else:
        await message.answer("Невідомий тип. Доступно: export, inbound, production, all")
        return
    save_settings(settings)
    await message.answer(f"✅ Фільтр типів: {', '.join(settings['allowed_types'])}")


@dp.message(Command("period"))
async def period_cmd(message: types.Message) -> None:
    if message.from_user.id != CHAT_ID:
        return
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2:
        await message.answer("Використай: /period morning|day|night|all")
        return
    value = parts[1].strip().lower()
    mapping = {"morning": "Ранок", "day": "День", "night": "Вечір"}
    if value == "all":
        settings["allowed_periods"] = list(mapping.values())
    elif value in mapping:
        settings["allowed_periods"] = [mapping[value]]
    else:
        await message.answer("Невідомий період. Доступно: morning, day, night, all")
        return
    save_settings(settings)
    await message.answer(f"✅ Фільтр періодів: {', '.join(settings['allowed_periods'])}")


@dp.message(Command("check"))
async def check_cmd(message: types.Message) -> None:
    if message.from_user.id != CHAT_ID:
        return
    progress = await message.answer("⏳ Перевіряю сайт...")
    await check_site(is_manual=True, message=progress)


@dp.message(Command("all"))
async def all_cmd(message: types.Message) -> None:
    if message.from_user.id != CHAT_ID:
        return
    browser = None
    progress = await message.answer("⏳ Завантажую всі доступні зміни...")
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            page = await browser.new_page()
            await retry_async(lambda: login(page), attempts=2, base_delay=1.0, context="all_login")
            shifts = await retry_async(lambda: get_shifts(page), attempts=2, base_delay=1.0, context="all_get_shifts")
        filtered = [s for s in shifts if shift_passes_filters(s)]
        if not filtered:
            await progress.edit_text("📭 Немає змін за поточними фільтрами.", reply_markup=main_menu())
            return
        lines = ["📋 <b>Доступні зміни:</b>"] + [format_shift_line(s) for s in filtered[:25]]
        if len(filtered) > 25:
            lines.append(f"\n...і ще {len(filtered) - 25} змін")
        await progress.edit_text("\n".join(lines), reply_markup=main_menu())
    except Exception as exc:
        await progress.edit_text(f"⚠️ {classify_error(exc)}", reply_markup=main_menu())
    finally:
        if browser:
            await browser.close()


@dp.message(Command("status"))
async def status_cmd(message: types.Message) -> None:
    if message.from_user.id != CHAT_ID:
        return
    resp = settings.get("allowed_responsibles", [])
    resp_text = ", ".join(resp) if resp else "всі"
    text = (
        "📊 <b>Статус бота</b>\n"
        f"• Інтервал: {settings.get('check_interval', DEFAULT_CHECK_INTERVAL)} сек\n"
        f"• Остання перевірка: {state['last_check'] or '—'}\n"
        f"• Останній успіх: {state['last_success'] or '—'}\n"
        f"• Знайдено (остання): {state['last_total_found']}\n"
        f"• Надіслано (остання): {state['last_sent']}\n"
        f"• Помилка: {state['last_error'] or 'немає'}\n"
        f"• Періоди: {', '.join(settings.get('allowed_periods', []))}\n"
        f"• Ведучі: {resp_text}"
    )
    await message.answer(text, reply_markup=main_menu())


@dp.message(Command("settings"))
async def settings_cmd(message: types.Message) -> None:
    if message.from_user.id != CHAT_ID:
        return
    await message.answer("⚙️ Налаштування:", reply_markup=settings_menu())


@dp.message(Command("setresp"))
async def set_resp_cmd(message: types.Message) -> None:
    if message.from_user.id != CHAT_ID:
        return
    await message.answer("👤 Функція фільтра ведучих зараз у розробці.")




@dp.message(Command("mystats"))
async def my_stats_cmd(message: types.Message) -> None:
    if message.from_user.id != CHAT_ID:
        return
    browser = None
    progress = await message.answer("⏳ Рахую розширену аналітику...")
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            page = await browser.new_page()
            await retry_async(lambda: login(page), attempts=2, base_delay=1.0, context="stats_login")
            shifts = await retry_async(lambda: get_shifts(page), attempts=2, base_delay=1.0, context="stats_get_shifts")

        with sqlite3.connect(DB_FILE) as conn:
            rows = conn.execute(
                """
                SELECT substr(check_time, 1, 7) as ym, COUNT(*), SUM(found_count), SUM(sent_count)
                FROM check_history
                GROUP BY ym
                ORDER BY ym DESC
                LIMIT 3
                """
            ).fetchall()

        now = datetime.now()
        current_month = now.strftime("%m.%Y")
        durations = [shift_duration_hours(s["time_from"], s["time_to"]) for s in shifts] if shifts else []
        avg_duration = sum(durations) / len(durations) if durations else 0.0
        forecast_hours = len(shifts) * 7.5
        monthly_text = "\n".join(
            [f"• {ym.replace('-', '.')} | checks:{cnt} found:{found} sent:{sent}" for ym, cnt, found, sent in rows]
        ) or "• Немає історії перевірок"

        await progress.edit_text(
            "📈 <b>Розширена аналітика</b>\n"
            f"• Місяць: {current_month}\n"
            f"• Доступних змін зараз: {len(shifts)}\n"
            f"• Середня тривалість зміни: {avg_duration:.2f} год\n"
            f"• Forecast годин (доступні * 7.5): {forecast_hours:.1f}\n\n"
            f"🗓 <b>Останні 3 місяці (історія перевірок)</b>\n{monthly_text}"
        )
    except Exception as exc:
        await progress.edit_text(f"⚠️ Не вдалося порахувати аналітику: {classify_error(exc)}")
    finally:
        if browser:
            await browser.close()


@dp.message(Command("selfcheck"))
async def selfcheck_cmd(message: types.Message) -> None:
    if message.from_user.id != CHAT_ID:
        return
    browser = None
    progress = await message.answer("🛠 Виконую self-check...")
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            page = await browser.new_page()
            await retry_async(lambda: login(page), attempts=2, base_delay=1.0, context="selfcheck_login")
            shifts = await retry_async(lambda: get_shifts(page), attempts=2, base_delay=1.0, context="selfcheck_shifts")
        await progress.edit_text(
            "✅ <b>Self-check успішний</b>\n"
            "• Login: OK\n"
            "• Блок My Invitations: знайдено\n"
            f"• Рядків у вибірці: {len(shifts)}"
        )
    except Exception as exc:
        await progress.edit_text(f"❌ Self-check помилка: {classify_error(exc)}")
    finally:
        if browser:
            await browser.close()
@dp.callback_query()
async def callback_handler(callback: types.CallbackQuery) -> None:
    try:
        await callback.answer()
    except Exception:
        pass

    if callback.from_user.id != CHAT_ID:
        return

    data = callback.data
    global sent_shifts

    if data == "check_now":
        await callback.message.edit_text("⏳ Перевіряю сайт...")
        asyncio.create_task(check_site(is_manual=True, message=callback.message))

    elif data == "show_all":
        browser = None
        await callback.message.edit_text("⏳ Завантажую всі доступні зміни...")
        try:
            async with async_playwright() as p:
                browser = await p.chromium.launch(headless=True)
                page = await browser.new_page()
                await login(page)
                shifts = await get_shifts(page)
            filtered = [s for s in shifts if shift_passes_filters(s)]
            if not filtered:
                await callback.message.edit_text("📭 Немає змін за поточними фільтрами.", reply_markup=main_menu())
                return

            lines = ["📋 <b>Доступні зміни:</b>"] + [format_shift_line(s) for s in filtered[:25]]
            if len(filtered) > 25:
                lines.append(f"\n...і ще {len(filtered) - 25} змін")
            await callback.message.edit_text("\n".join(lines), reply_markup=main_menu())
        except Exception as exc:
            await callback.message.edit_text(f"⚠️ {classify_error(exc)}", reply_markup=main_menu())
        finally:
            if browser:
                await browser.close()

    elif data == "status":
        resp = settings.get("allowed_responsibles", [])
        resp_text = ", ".join(resp) if resp else "всі"
        text = (
            "📊 <b>Статус бота</b>\n"
            f"• Інтервал: {settings.get('check_interval', DEFAULT_CHECK_INTERVAL)} сек\n"
            f"• Остання перевірка: {state['last_check'] or '—'}\n"
            f"• Останній успіх: {state['last_success'] or '—'}\n"
            f"• Знайдено (остання): {state['last_total_found']}\n"
            f"• Надіслано (остання): {state['last_sent']}\n"
            f"• Помилка: {state['last_error'] or 'немає'}\n"
            f"• Періоди: {', '.join(settings.get('allowed_periods', []))}\n"
            f"• Ведучі: {resp_text}"
        )
        await callback.message.edit_text(text, reply_markup=main_menu())

    elif data == "settings":
        await callback.message.edit_text("⚙️ Налаштування:", reply_markup=settings_menu())

    elif data == "set_types":
        await callback.message.edit_text("🔹 Обери типи змін:", reply_markup=types_menu())

    elif data == "set_periods":
        await callback.message.edit_text("🕒 Обери періоди:", reply_markup=periods_menu())

    elif data and data.startswith("toggle_type_"):
        t = data.replace("toggle_type_", "")
        allowed = settings.setdefault("allowed_types", ["Експорт", "Прийом товару", "Вироба"])
        if t in allowed:
            allowed.remove(t)
        else:
            allowed.append(t)
        save_settings(settings)
        await callback.message.edit_text("🔹 Обери типи змін:", reply_markup=types_menu())

    elif data and data.startswith("toggle_period_"):
        period = data.replace("toggle_period_", "")
        allowed = settings.setdefault("allowed_periods", ["Ранок", "День", "Вечір"])
        if period in allowed:
            allowed.remove(period)
        else:
            allowed.append(period)
        save_settings(settings)
        await callback.message.edit_text("🕒 Обери періоди:", reply_markup=periods_menu())

    elif data == "resp_dev":
        await callback.message.edit_text("👤 Ведучі: функція в розробці.", reply_markup=settings_menu())

    elif data == "set_interval":
        await callback.message.edit_text("⏱ Обери інтервал перевірки:", reply_markup=interval_menu())

    elif data and data.startswith("interval_"):
        new_interval = int(data.replace("interval_", ""))
        settings["check_interval"] = new_interval
        save_settings(settings)
        await callback.message.edit_text(
            f"✅ Інтервал оновлено: {new_interval} сек",
            reply_markup=settings_menu(),
        )

    elif data == "back_main":
        await callback.message.edit_text("Головне меню:", reply_markup=main_menu())

    elif data == "reset_memory":
        sent_shifts = set()
        if SENT_FILE.exists():
            SENT_FILE.unlink()
        reset_sent_shifts_db()
        await callback.message.edit_text("♻️ Пам'ять очищена!", reply_markup=main_menu())


async def background_monitor() -> None:
    await asyncio.sleep(10)
    logger.info("Фоновий моніторинг запущено")
    while True:
        await check_site()
        await asyncio.sleep(settings.get("check_interval", DEFAULT_CHECK_INTERVAL))


async def main() -> None:
    logger.info("=== GymBeam Shift Bot запущено ===")
    settings.update(load_settings())
    init_db()
    global sent_shifts
    sent_shifts = load_sent_shifts_db() | set(load_json_file(SENT_FILE, []))
    for key in sent_shifts:
        save_shift_key_db(key)
    await bot.set_my_commands(
        [
            types.BotCommand(command="start", description="Головне меню"),
            types.BotCommand(command="help", description="Список команд"),
            types.BotCommand(command="check", description="Перевірити зараз"),
            types.BotCommand(command="all", description="Показати всі зміни"),
            types.BotCommand(command="status", description="Статус бота"),
            types.BotCommand(command="selfcheck", description="Технічна діагностика"),
            types.BotCommand(command="mystats", description="Статистика акаунта"),
            types.BotCommand(command="settings", description="Налаштування"),
        ]
    )
    asyncio.create_task(background_monitor())
    await dp.start_polling(bot)


if __name__ == "__main__":
    settings = load_settings()
    sent_shifts = set(load_json_file(SENT_FILE, []))
    asyncio.run(main())
