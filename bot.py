import asyncio
import logging
from datetime import datetime, timedelta
import pytz
import aiohttp
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes

# ===== НАСТРОЙКИ =====
OZON_CLIENT_ID = "1360213"
OZON_API_KEY = "dd0e57bc-1497-4e70-a642-63266dbddcc7"
TELEGRAM_TOKEN = "8801888159:AAFJIece-JoNfGvg9PP5brVygU46_XdRbU0"
OZON_API_URL = "https://api-seller.ozon.ru"
MOSCOW_TZ = pytz.timezone("Europe/Moscow")

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

HEADERS = {
    "Client-Id": OZON_CLIENT_ID,
    "Api-Key": OZON_API_KEY,
    "Content-Type": "application/json",
}

# ===== OZON API =====

async def get_supply_orders(session: aiohttp.ClientSession) -> list:
    """Получить список заявок на поставку"""
    url = f"{OZON_API_URL}/v1/supply-order/list"
    payload = {
        "paging": {"from_supply_order_id": 0, "limit": 50},
        "filter": {}
    }
    async with session.post(url, json=payload, headers=HEADERS) as resp:
        data = await resp.json()
        logger.info(f"supply-order/list response: {data}")
        return data.get("supply_orders", [])


async def get_available_timeslots(session: aiohttp.ClientSession, supply_order_id: int, date_from: str, date_to: str) -> list:
    """Получить доступные таймслоты для заявки"""
    url = f"{OZON_API_URL}/v1/supply-order/timeslot/list"
    payload = {
        "supply_order_id": supply_order_id,
        "date_from": date_from,
        "date_to": date_to,
    }
    async with session.post(url, json=payload, headers=HEADERS) as resp:
        data = await resp.json()
        logger.info(f"timeslot/list response for {supply_order_id}: {data}")
        return data.get("timeslots", [])


async def update_timeslot(session: aiohttp.ClientSession, supply_order_id: int, timeslot_id: str) -> dict:
    """Установить новый таймслот для заявки"""
    url = f"{OZON_API_URL}/v1/supply-order/timeslot/update"
    payload = {
        "supply_order_id": supply_order_id,
        "timeslot_id": timeslot_id,
    }
    async with session.post(url, json=payload, headers=HEADERS) as resp:
        data = await resp.json()
        logger.info(f"timeslot/update response for {supply_order_id}: {data}")
        return data


# ===== ЛОГИКА ПЕРЕНОСА =====

def is_target_status(status: str) -> bool:
    """Трогаем только 'заполнение данных', пропускаем 'готово'"""
    skip_statuses = [
        "ready", "completed", "готово", "READY", "COMPLETED",
        "ready_to_supply", "READY_TO_SUPPLY"
    ]
    status_lower = status.lower()
    for s in skip_statuses:
        if s.lower() in status_lower:
            return False
    return True


def find_best_timeslot(timeslots: list) -> dict | None:
    """Ищем слот 19:00-20:00, если нет — берём первый доступный"""
    for slot in timeslots:
        try:
            slot_from = slot.get("from", "")
            dt = datetime.fromisoformat(slot_from.replace("Z", "+00:00"))
            dt_msk = dt.astimezone(MOSCOW_TZ)
            if dt_msk.hour == 19:
                return slot
        except Exception:
            pass
    if timeslots:
        return timeslots[0]
    return None


async def process_orders() -> str:
    """Основная логика: перенести все подходящие заявки"""
    now_msk = datetime.now(MOSCOW_TZ)
    tomorrow = now_msk + timedelta(days=1)
    date_from = tomorrow.strftime("%Y-%m-%dT00:00:00+03:00")
    date_to = (now_msk + timedelta(days=8)).strftime("%Y-%m-%dT23:59:59+03:00")

    results = []
    errors = []

    async with aiohttp.ClientSession() as session:
        orders = await get_supply_orders(session)

        if not orders:
            return "📭 Заявок на поставку не найдено."

        target_orders = [o for o in orders if is_target_status(o.get("status", ""))]

        if not target_orders:
            return (
                f"✅ Нет заявок для переноса (все в статусе «Готово»).\n"
                f"Всего заявок найдено: {len(orders)}"
            )

        for order in target_orders:
            order_id = order.get("supply_order_id") or order.get("order_id")
            order_number = order.get("supply_order_number") or order.get("order_number") or str(order_id)

            try:
                timeslots = await get_available_timeslots(session, order_id, date_from, date_to)

                # Оставляем только слоты начиная с завтра
                future_slots = []
                for slot in timeslots:
                    try:
                        dt = datetime.fromisoformat(slot["from"].replace("Z", "+00:00"))
                        if dt.astimezone(MOSCOW_TZ).date() >= tomorrow.date():
                            future_slots.append(slot)
                    except Exception:
                        future_slots.append(slot)

                if not future_slots:
                    errors.append(f"❌ {order_number}: нет доступных слотов на 7 дней вперёд")
                    continue

                best_slot = find_best_timeslot(future_slots)
                if not best_slot:
                    errors.append(f"❌ {order_number}: не удалось подобрать слот")
                    continue

                timeslot_id = best_slot.get("timeslot_id") or best_slot.get("id")

                # Форматируем для отчёта
                try:
                    dt_from = datetime.fromisoformat(best_slot["from"].replace("Z", "+00:00")).astimezone(MOSCOW_TZ)
                    dt_to = datetime.fromisoformat(best_slot["to"].replace("Z", "+00:00")).astimezone(MOSCOW_TZ)
                    slot_display = f"{dt_from.strftime('%d.%m.%Y %H:%M')}–{dt_to.strftime('%H:%M')}"
                except Exception:
                    slot_display = str(timeslot_id)

                update_result = await update_timeslot(session, order_id, timeslot_id)

                if update_result.get("operation_id") or not update_result.get("error"):
                    results.append(f"✅ {order_number} → {slot_display}")
                else:
                    err_msg = update_result.get("message") or update_result.get("error") or str(update_result)
                    errors.append(f"❌ {order_number}: {err_msg}")

            except Exception as e:
                logger.exception(f"Error processing order {order_number}")
                errors.append(f"❌ {order_number}: {str(e)}")

    lines = [f"📦 Найдено заявок для переноса: {len(target_orders)}\n"]
    if results:
        lines.append("Успешно перенесено:")
        lines.extend(results)
    if errors:
        if results:
            lines.append("")
        lines.append("Проблемы:")
        lines.extend(errors)

    return "\n".join(lines)


# ===== TELEGRAM =====

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [[InlineKeyboardButton("📅 Перенести заявки на день вперёд", callback_data="reschedule")]]
    await update.message.reply_text(
        "👋 Привет! Я бот для переноса заявок на поставку Ozon FBO.\n\n"
        "Переношу все заявки со статусом «Заполнение данных» на завтра (19:00–20:00).\n"
        "Если этот слот недоступен — нахожу ближайший.\n\n"
        "Заявки со статусом «Готово» не трогаю.",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )


async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "reschedule":
        await query.edit_message_text("⏳ Обрабатываю заявки, подождите...")
        try:
            result = await process_orders()
        except Exception as e:
            logger.exception("Error in process_orders")
            result = f"❗ Ошибка: {str(e)}"

        keyboard = [[InlineKeyboardButton("🔄 Запустить снова", callback_data="reschedule")]]
        await query.edit_message_text(result, reply_markup=InlineKeyboardMarkup(keyboard))


async def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(button_handler))
    logger.info("Бот запущен!")
    await app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    asyncio.run(main())
