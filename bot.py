import logging
from aiogram import Bot, Dispatcher, types, executor
from aiogram.dispatcher.filters import Command
from aiogram.contrib.fsm_storage.memory import MemoryStorage
from aiogram.dispatcher import FSMContext
from aiogram.dispatcher.filters.state import State, StatesGroup
import sqlite3
import datetime
import pandas as pd
from io import BytesIO
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

# Настройка логирования
logging.basicConfig(level=logging.INFO)

# Инициализация бота
API_TOKEN = 'YOUR_TELEGRAM_BOT_TOKEN'
bot = Bot(token=API_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(bot, storage=storage)

# Подключение к базе данных
conn = sqlite3.connect('proregion.db')
cursor = conn.cursor()

# Создание таблиц
cursor.execute('''
CREATE TABLE IF NOT EXISTS workshops (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    date TEXT NOT NULL,
    time TEXT NOT NULL,
    max_participants INTEGER NOT NULL,
    is_active INTEGER DEFAULT 0
)
''')

cursor.execute('''
CREATE TABLE IF NOT EXISTS registrations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    workshop_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    full_name TEXT NOT NULL,
    phone TEXT NOT NULL,
    registration_date TEXT NOT NULL,
    FOREIGN KEY (workshop_id) REFERENCES workshops (id)
)
''')

cursor.execute('''
CREATE TABLE IF NOT EXISTS admin_users (
    user_id INTEGER PRIMARY KEY
)
''')

conn.commit()

# Состояния для FSM
class RegistrationStates(StatesGroup):
    waiting_for_full_name = State()
    waiting_for_phone = State()

class AdminStates(StatesGroup):
    waiting_for_workshop_name = State()
    waiting_for_workshop_date = State()
    waiting_for_workshop_time = State()
    waiting_for_max_participants = State()
    waiting_for_announcement = State()

# Команда старт
@dp.message_handler(commands=['start'])
async def send_welcome(message: types.Message):
    if is_admin(message.from_user.id):
        markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
        markup.add("Управление мастер-классами", "Выгрузка данных", "Рассылка")
        await message.answer("Добро пожаловать, администратор!", reply_markup=markup)
    else:
        markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
        markup.add("Записаться на мастер-класс", "Мои записи")
        await message.answer("Добро пожаловать на форум 'ПРОрегион'! Выберите действие:", reply_markup=markup)

# Показать доступные мастер-классы
@dp.message_handler(text="Записаться на мастер-класс")
async def show_workshops(message: types.Message):
    today = datetime.date.today().strftime("%Y-%m-%d")
    cursor.execute("SELECT id, name, time, max_participants, (SELECT COUNT(*) FROM registrations WHERE workshop_id = workshops.id) as registered FROM workshops WHERE date = ? AND is_active = 1", (today,))
    workshops = cursor.fetchall()
    
    if not workshops:
        await message.answer("На сегодня мастер-классов нет.")
        return
    
    markup = types.InlineKeyboardMarkup()
    for workshop in workshops:
        workshop_id, name, time, max_participants, registered = workshop
        available = max_participants - registered
        markup.add(types.InlineKeyboardButton(
            text=f"{name} ({time}) - мест: {available}/{max_participants}",
            callback_data=f"workshop_{workshop_id}"
        ))
    
    await message.answer("Выберите мастер-класс:", reply_markup=markup)

# Обработка выбора мастер-класса
@dp.callback_query_handler(lambda c: c.data.startswith('workshop_'))
async def process_workshop_selection(callback_query: types.CallbackQuery, state: FSMContext):
    workshop_id = int(callback_query.data.split('_')[1])
    
    # Проверка доступности мест
    cursor.execute("SELECT max_participants, (SELECT COUNT(*) FROM registrations WHERE workshop_id = ?) as registered FROM workshops WHERE id = ?", (workshop_id, workshop_id))
    max_participants, registered = cursor.fetchone()
    
    if registered >= max_participants:
        await bot.answer_callback_query(callback_query.id, "Извините, все места заняты!")
        return
    
    # Проверка, не записан ли уже пользователь
    cursor.execute("SELECT 1 FROM registrations WHERE workshop_id = ? AND user_id = ?", (workshop_id, callback_query.from_user.id))
    if cursor.fetchone():
        await bot.answer_callback_query(callback_query.id, "Вы уже записаны на этот мастер-класс!")
        return
    
    await bot.answer_callback_query(callback_query.id)
    await state.update_data(workshop_id=workshop_id)
    await RegistrationStates.waiting_for_full_name.set()
    await bot.send_message(callback_query.from_user.id, "Введите ваше ФИО:")

# Обработка ФИО
@dp.message_handler(state=RegistrationStates.waiting_for_full_name)
async def process_full_name(message: types.Message, state: FSMContext):
    await state.update_data(full_name=message.text)
    await RegistrationStates.next()
    await message.answer("Введите ваш номер телефона:")

# Обработка телефона и завершение регистрации
@dp.message_handler(state=RegistrationStates.waiting_for_phone)
async def process_phone(message: types.Message, state: FSMContext):
    phone = message.text
    data = await state.get_data()
    
    # Запись в базу данных
    cursor.execute(
        "INSERT INTO registrations (workshop_id, user_id, full_name, phone, registration_date) VALUES (?, ?, ?, ?, datetime('now'))",
        (data['workshop_id'], message.from_user.id, data['full_name'], phone)
    )
    conn.commit()
    
    # Получаем информацию о мастер-классе
    cursor.execute("SELECT name, time FROM workshops WHERE id = ?", (data['workshop_id'],))
    workshop_name, workshop_time = cursor.fetchone()
    
    await state.finish()
    await message.answer(f"Вы успешно записаны на мастер-класс '{workshop_name}' в {workshop_time}!")

# Показать свои записи
@dp.message_handler(text="Мои записи")
async def show_my_registrations(message: types.Message):
    cursor.execute('''
    SELECT w.name, w.date, w.time 
    FROM registrations r
    JOIN workshops w ON r.workshop_id = w.id
    WHERE r.user_id = ?
    ORDER BY w.date, w.time
    ''', (message.from_user.id,))
    
    registrations = cursor.fetchall()
    
    if not registrations:
        await message.answer("У вас нет записей на мастер-классы.")
        return
    
    response = "Ваши записи:\n\n"
    for reg in registrations:
        name, date, time = reg
        response += f"{name} - {date} в {time}\n"
    
    await message.answer(response)

# Административные функции
def is_admin(user_id):
    cursor.execute("SELECT 1 FROM admin_users WHERE user_id = ?", (user_id,))
    return cursor.fetchone() is not None

@dp.message_handler(text="Управление мастер-классами")
async def manage_workshops(message: types.Message):
    if not is_admin(message.from_user.id):
        await message.answer("Доступ запрещен")
        return
    
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    markup.add("Добавить мастер-класс", "Активировать/деактивировать", "Назад")
    await message.answer("Управление мастер-классами:", reply_markup=markup)

@dp.message_handler(text="Добавить мастер-класс")
async def add_workshop_start(message: types.Message):
    if not is_admin(message.from_user.id):
        await message.answer("Доступ запрещен")
        return
    
    await AdminStates.waiting_for_workshop_name.set()
    await message.answer("Введите название мастер-класса:")

@dp.message_handler(state=AdminStates.waiting_for_workshop_name)
async def process_workshop_name(message: types.Message, state: FSMContext):
    await state.update_data(name=message.text)
    await AdminStates.next()
    await message.answer("Введите дату мастер-класса (ГГГГ-ММ-ДД):")

@dp.message_handler(state=AdminStates.waiting_for_workshop_date)
async def process_workshop_date(message: types.Message, state: FSMContext):
    await state.update_data(date=message.text)
    await AdminStates.next()
    await message.answer("Введите время мастер-класса (ЧЧ:ММ):")

@dp.message_handler(state=AdminStates.waiting_for_workshop_time)
async def process_workshop_time(message: types.Message, state: FSMContext):
    await state.update_data(time=message.text)
    await AdminStates.next()
    await message.answer("Введите максимальное количество участников:")

@dp.message_handler(state=AdminStates.waiting_for_max_participants)
async def process_max_participants(message: types.Message, state: FSMContext):
    try:
        max_participants = int(message.text)
        data = await state.get_data()
        
        cursor.execute(
            "INSERT INTO workshops (name, date, time, max_participants) VALUES (?, ?, ?, ?)",
            (data['name'], data['date'], data['time'], max_participants)
        )
        conn.commit()
        
        await state.finish()
        await message.answer("Мастер-класс успешно добавлен!")
    except ValueError:
        await message.answer("Пожалуйста, введите число.")

@dp.message_handler(text="Активировать/деактивировать")
async def activate_deactivate_workshops(message: types.Message):
    if not is_admin(message.from_user.id):
        await message.answer("Доступ запрещен")
        return
    
    cursor.execute("SELECT id, name, date, time, is_active FROM workshops ORDER BY date, time")
    workshops = cursor.fetchall()
    
    if not workshops:
        await message.answer("Нет мастер-классов в базе.")
        return
    
    markup = types.InlineKeyboardMarkup()
    for workshop in workshops:
        workshop_id, name, date, time, is_active = workshop
        status = "✅" if is_active else "❌"
        markup.add(types.InlineKeyboardButton(
            text=f"{status} {name} ({date} {time})",
            callback_data=f"toggle_{workshop_id}"
        ))
    
    await message.answer("Выберите мастер-класс для активации/деактивации:", reply_markup=markup)

@dp.callback_query_handler(lambda c: c.data.startswith('toggle_'))
async def toggle_workshop_status(callback_query: types.CallbackQuery):
    workshop_id = int(callback_query.data.split('_')[1])
    
    cursor.execute("UPDATE workshops SET is_active = NOT is_active WHERE id = ?", (workshop_id,))
    conn.commit()
    
    await bot.answer_callback_query(callback_query.id, "Статус изменен!")
    await bot.delete_message(callback_query.from_user.id, callback_query.message.message_id)
    await activate_deactivate_workshops(callback_query.message)

@dp.message_handler(text="Выгрузка данных")
async def export_data(message: types.Message):
    if not is_admin(message.from_user.id):
        await message.answer("Доступ запрещен")
        return
    
    cursor.execute('''
    SELECT w.name, w.date, w.time, r.full_name, r.phone, r.registration_date
    FROM registrations r
    JOIN workshops w ON r.workshop_id = w.id
    ORDER BY w.date, w.time, r.registration_date
    ''')
    
    data = cursor.fetchall()
    df = pd.DataFrame(data, columns=['Мастер-класс', 'Дата', 'Время', 'ФИО', 'Телефон', 'Дата регистрации'])
    
    output = BytesIO()
    with pd.ExcelWriter(output, engine='xlsxwriter') as writer:
        df.to_excel(writer, sheet_name='Регистрации', index=False)
    output.seek(0)
    
    await bot.send_document(message.chat.id, ('registrations.xlsx', output.getvalue()))

@dp.message_handler(text="Рассылка")
async def start_announcement(message: types.Message):
    if not is_admin(message.from_user.id):
        await message.answer("Доступ запрещен")
        return
    
    await AdminStates.waiting_for_announcement.set()
    await message.answer("Введите сообщение для рассылки:")

@dp.message_handler(state=AdminStates.waiting_for_announcement)
async def send_announcement(message: types.Message, state: FSMContext):
    cursor.execute("SELECT DISTINCT user_id FROM registrations")
    users = cursor.fetchall()
    
    for user in users:
        try:
            await bot.send_message(user[0], message.text)
        except Exception as e:
            logging.error(f"Не удалось отправить сообщение пользователю {user[0]}: {e}")
    
    await state.finish()
    await message.answer(f"Рассылка завершена. Сообщение отправлено {len(users)} пользователям.")

@dp.message_handler(text="Назад")
async def back_to_main_menu(message: types.Message):
    if is_admin(message.from_user.id):
        markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
        markup.add("Управление мастер-классами", "Выгрузка данных", "Рассылка")
        await message.answer("Главное меню администратора:", reply_markup=markup)
    else:
        markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
        markup.add("Записаться на мастер-класс", "Мои записи")
        await message.answer("Главное меню:", reply_markup=markup)

if __name__ == '__main__':
    executor.start_polling(dp, skip_updates=True)

scheduler = AsyncIOScheduler(timezone="Europe/Moscow")

def get_admin_ids():
    cursor.execute("SELECT user_id FROM admin_users")
    return [row[0] for row in cursor.fetchall()]

async def activate_workshops_by_schedule():
    today = datetime.date.today().strftime("%Y-%m-%d")
    cursor.execute(
        "UPDATE workshops SET is_active = 1 WHERE date = ? AND is_active = 0",
        (today,)
    )
    conn.commit()
    await notify_admin_about_activation()

async def notify_admin_about_activation():
    cursor.execute("SELECT name, time FROM workshops WHERE date = ?", (datetime.date.today().strftime("%Y-%m-%d"),))
    workshops = cursor.fetchall()
    if workshops:
        text = "🔔 Автоматически активированы мастер-классы:\n\n" + \
               "\n".join([f"• {name} ({time})" for name, time in workshops])
        for admin_id in get_admin_ids():
            await bot.send_message(admin_id, text)

def schedule_jobs():
    # Активация в 00:01
    scheduler.add_job(
        activate_workshops_by_schedule,
        CronTrigger(hour=0, minute=1),
    )
    
    # Деактивация в 23:59
    scheduler.add_job(
        deactivate_past_workshops,
        CronTrigger(hour=23, minute=59),
    )

if __name__ == '__main__':
    schedule_jobs()
    scheduler.start()
    executor.start_polling(dp, skip_updates=True)
