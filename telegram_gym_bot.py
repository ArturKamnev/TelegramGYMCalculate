"""
telegram_gym_bot.py
====================

This module defines a Telegram bot that uses a large language model via
the OpenRouter API to construct personalised gym workout plans.
OpenRouter provides access to many models with generous free tiers【841123382249507†L280-L334】.
The bot stores user profiles in a SQLite database so that data
persists between sessions.  When a user first sends `/start` they are
prompted to enter their name and surname.  Thereafter a main menu is
presented showing their profile and offering buttons to edit height,
weight, training days, experience level and goal, as well as to view
or generate a workout plan, review past workouts and mark a training
as completed.  Once all required profile data is filled, the bot
builds a prompt and sends it to a selected model via the OpenRouter
API to generate a structured training plan.  The plan includes a
weekly schedule, exercise selection and guidance on repetitions, sets
and rest intervals.  For convenience and security the API keys are
read from environment variables rather than being hard‑coded in the
source.

Usage:
  1. Install the dependencies:
     pip install -r requirements.txt

  2. Create a ``.env`` file in the same directory as this script and
     define the following environment variables:

     OPENROUTER_API_KEY=or-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
     OPENROUTER_MODEL=deepseek-ai/deepseek-v3-0324    # Optional. Any supported model name【841123382249507†L280-L334】
     TELEGRAM_BOT_TOKEN=123456:ABC‑defGhIJKLmnoPQrstUVwxyZ

     For security reasons you should never commit your real API keys to
     version control or share them publicly.

  3. Run the bot:
     python telegram_gym_bot.py

The bot will start polling for new messages.  After `/start` it
registers the user if necessary and then presents a menu-based
interface.  Once the profile is complete a tailored workout plan can
be generated and stored for future reference.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Dict
import sqlite3
from datetime import datetime, date

# If python-dotenv is available we can load environment variables from
# a .env file.  This is optional but makes local development easier.
try:
    from dotenv import load_dotenv  # type: ignore
    load_dotenv()
except Exception:
    # It's okay if dotenv isn't installed; environment variables can
    # still be provided by the hosting environment.
    pass

import requests
from telegram import Update, ReplyKeyboardMarkup, ReplyKeyboardRemove
from telegram.constants import ParseMode
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

# Conversation states.  Each state corresponds to a step in the user
# interaction flow.  The integer values themselves are arbitrary; only
# their ordering matters.  New states are defined below for registration
# and menu-driven interactions.  Additional states are added for editing
# the user's name and confirming profile deletion.  Note: legacy states
# for the original questionnaire are no longer used.
REGISTRATION, MAIN_MENU, EDIT_HEIGHT, EDIT_WEIGHT, EDIT_DAYS, EDIT_EXPERIENCE, EDIT_GOAL, EDIT_NAME, CONFIRM_DELETE = range(9)

# Legacy state constants retained for backward compatibility with old
# questionnaire functions.  They are not used in the new flow but
# defined here to avoid NameError if referenced inadvertently.
GOAL = EXPERIENCE = WEIGHT = HEIGHT = DAYS = CONFIRM = -1

# Path to the SQLite database.  This can be overridden via the
# GYM_BOT_DB environment variable.
DB_PATH = os.environ.get("GYM_BOT_DB", "gym_bot.db")

def init_db() -> None:
    """Initialize the SQLite database with required tables.

    This function creates the `users` and `workouts` tables if they do
    not already exist.  The `users` table stores profile information
    and the most recently generated plan, while the `workouts` table
    records completion of training sessions by date.
    """
    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.cursor()
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                first_name TEXT NOT NULL,
                last_name TEXT NOT NULL,
                goal TEXT DEFAULT '',
                experience TEXT DEFAULT '',
                weight REAL DEFAULT 0.0,
                height REAL DEFAULT 0.0,
                days INTEGER DEFAULT 0,
                plan TEXT,
                plan_date TEXT
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS workouts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                date TEXT NOT NULL,
                FOREIGN KEY(user_id) REFERENCES users(user_id)
            )
            """
        )
        conn.commit()

def get_user(user_id: int) -> Dict[str, any] | None:
    """Fetch a user's record from the database.

    Returns a dictionary of column names to values or ``None`` if the
    user does not exist.
    """
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cur.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
        row = cur.fetchone()
        return dict(row) if row else None

def create_user(user_id: int, first_name: str, last_name: str) -> None:
    """Insert a new user into the database with minimal fields."""
    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO users (user_id, first_name, last_name) VALUES (?, ?, ?)",
            (user_id, first_name, last_name),
        )
        conn.commit()

def update_user_field(user_id: int, field: str, value: any) -> None:
    """Generic helper to update a single field of a user record."""
    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.cursor()
        cur.execute(
            f"UPDATE users SET {field} = ? WHERE user_id = ?",
            (value, user_id),
        )
        conn.commit()

def save_plan(user_id: int, plan: str) -> None:
    """Save the generated workout plan and timestamp for the user."""
    now = datetime.now().isoformat(timespec='seconds')
    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.cursor()
        cur.execute(
            "UPDATE users SET plan = ?, plan_date = ? WHERE user_id = ?",
            (plan, now, user_id),
        )
        conn.commit()

def get_workouts(user_id: int) -> list[str]:
    """Return a list of dates on which the user performed workouts."""
    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT date FROM workouts WHERE user_id = ? ORDER BY date DESC",
            (user_id,),
        )
        rows = cur.fetchall()
        return [r[0] for r in rows]

def add_workout(user_id: int, workout_date: str) -> None:
    """Record a workout completion for the user on a specific date."""
    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO workouts (user_id, date) VALUES (?, ?)",
            (user_id, workout_date),
        )
        conn.commit()


def delete_user(user_id: int) -> None:
    """Completely remove a user and all associated workouts from the database.

    This helper can be used when the user chooses to delete their profile.
    """
    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.cursor()
        # Remove workouts first to satisfy foreign key constraints
        cur.execute("DELETE FROM workouts WHERE user_id = ?", (user_id,))
        cur.execute("DELETE FROM users WHERE user_id = ?", (user_id,))
        conn.commit()


def compute_bmi(weight: float, height: float) -> tuple[float, str] | None:
    """Compute BMI and return a tuple of (value, classification).

    If weight or height is zero or negative, return ``None``.  The
    classification is based on World Health Organisation categories.
    """
    if weight <= 0 or height <= 0:
        return None
    height_m = height / 100.0
    bmi = weight / (height_m ** 2)
    if bmi < 18.5:
        category = "недостаток веса"
    elif bmi < 25:
        category = "нормальный вес"
    elif bmi < 30:
        category = "избыточный вес"
    elif bmi < 35:
        category = "ожирение I степени"
    elif bmi < 40:
        category = "ожирение II степени"
    else:
        category = "ожирение III степени"
    return round(bmi, 1), category


def workout_stats(user_id: int) -> str:
    """Generate a textual summary of the user's recent workout activity.

    Returns a string describing the total number of workouts, the number
    of workouts in the last 7 days and the last 30 days.  If there are
    no workouts, a corresponding message is returned.
    """
    dates = get_workouts(user_id)
    if not dates:
        return "У вас ещё нет завершённых тренировок."
    total = len(dates)
    today = date.today()
    last_7 = 0
    last_30 = 0
    for d in dates:
        try:
            workout_date = datetime.fromisoformat(d).date()
        except Exception:
            # Fallback if stored as simple string YYYY-MM-DD
            workout_date = datetime.strptime(d, "%Y-%m-%d").date()
        delta = (today - workout_date).days
        if delta < 7:
            last_7 += 1
        if delta < 30:
            last_30 += 1
    return (
        f"Всего завершённых тренировок: {total}\n"
        f"За последние 7 дней: {last_7}\n"
        f"За последние 30 дней: {last_30}"
    )


@dataclass
class UserProfile:
    """Simple container for storing a user's fitness data.

    Attributes:
        goal (str): The user's primary objective (e.g. muscle gain,
            fat loss, endurance).
        experience (str): The user's training experience level
            (beginner, intermediate, advanced).
        weight (float): The user's weight in kilograms.
        height (float): The user's height in centimetres.
        days (int): Number of training days per week the user can
            commit to.
    """

    goal: str = ""
    experience: str = ""
    weight: float = 0.0
    height: float = 0.0
    days: int = 0

    def to_prompt(self) -> str:
        """Render the profile as a textual description for the AI prompt."""
        return (
            f"Goal: {self.goal}. "
            f"Experience: {self.experience}. "
            f"Weight: {self.weight:.1f} kg. "
            f"Height: {self.height:.1f} cm. "
            f"Training days per week: {self.days}."
        )


# -----------------------------------------------------------------------------
# Conversation logic
# -----------------------------------------------------------------------------

async def show_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Display the main menu and the user's profile information.

    This helper reads the current user's data from the database and
    composes a message showing their profile.  It then sends a reply
    keyboard with available actions.
    """
    user_id = context.user_data.get('user_id')
    if user_id is None:
        await update.message.reply_text(
            "Произошла ошибка: пользователь не найден. Попробуйте ещё раз командой /start."
        )
        return
    user = get_user(user_id)
    if user is None:
        await update.message.reply_text(
            "Пользователь не найден в базе данных. Попробуйте зарегистрироваться заново."
        )
        return
    # Compose profile text with HTML formatting
    profile_lines = [
        f"<b>Имя:</b> {user['first_name']}",
        f"<b>Фамилия:</b> {user['last_name']}",
        f"<b>Цель:</b> {user['goal'] or 'не указана'}",
        f"<b>Уровень подготовки:</b> {user['experience'] or 'не указан'}",
        f"<b>Вес:</b> {user['weight'] if user['weight'] > 0 else 'не указан'} кг",
        f"<b>Рост:</b> {user['height'] if user['height'] > 0 else 'не указан'} см",
        f"<b>Дней тренировок в неделю:</b> {user['days'] if user['days'] > 0 else 'не указано'}",
    ]
    profile_text = "<b>Профиль пользователя:</b>\n" + "\n".join(profile_lines)
    # Define menu buttons with additional options
    menu_keyboard = [
        ["Редактировать рост", "Редактировать вес"],
        ["Редактировать количество дней", "Редактировать уровень подготовки"],
        ["Редактировать цель", "Изменить имя"],
        ["Показать план", "Сгенерировать новый план"],
        ["Прошлые тренировки", "Статистика"],
        ["Показать ИМТ", "Выполнить тренировку"],
        ["Удалить профиль"],
    ]
    await update.message.reply_text(
        profile_text,
        reply_markup=ReplyKeyboardMarkup(menu_keyboard, resize_keyboard=True),
        parse_mode=ParseMode.HTML,
    )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Entry point: register a new user or load existing data.

    When the user sends /start, we check if they already exist in the
    database.  If so, we greet them and show the menu.  Otherwise we
    prompt them for their name and surname.
    """
    user_id = update.effective_user.id
    user = get_user(user_id)
    if user:
        # User exists; store id in context and show menu
        context.user_data['user_id'] = user_id
        await update.message.reply_text(
            f"С возвращением, {user['first_name']}!", reply_markup=ReplyKeyboardRemove()
        )
        await show_menu(update, context)
        return MAIN_MENU
    else:
        # New user registration
        context.user_data['register_user_id'] = user_id
        await update.message.reply_text(
            "Добро пожаловать! Пожалуйста, введите ваше имя и фамилию через пробел:",
            reply_markup=ReplyKeyboardRemove(),
        )
        return REGISTRATION


async def handle_registration(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle input of name and surname during registration."""
    user_id = context.user_data.get('register_user_id')
    text = update.message.text.strip()
    parts = text.split()
    if len(parts) < 2:
        await update.message.reply_text("Пожалуйста, введите имя и фамилию через пробел.")
        return REGISTRATION
    first_name = parts[0]
    last_name = " ".join(parts[1:])
    create_user(user_id, first_name, last_name)
    context.user_data['user_id'] = user_id
    await update.message.reply_text(
        f"Спасибо, {first_name}! Ваш профиль создан.", reply_markup=ReplyKeyboardRemove()
    )
    await show_menu(update, context)
    return MAIN_MENU


async def handle_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Respond to menu selections and route to appropriate state."""
    text = update.message.text.strip().lower()
    # Map the Russian menu text to actions
    if text.startswith("редактировать рост"):
        await update.message.reply_text("Введите новый рост в сантиметрах:", reply_markup=ReplyKeyboardRemove())
        return EDIT_HEIGHT
    if text.startswith("редактировать вес"):
        await update.message.reply_text("Введите новый вес в килограммах:", reply_markup=ReplyKeyboardRemove())
        return EDIT_WEIGHT
    if text.startswith("редактировать количество дней"):
        await update.message.reply_text("Введите новое количество тренировочных дней (1–7):", reply_markup=ReplyKeyboardRemove())
        return EDIT_DAYS
    if text.startswith("редактировать уровень подготовки"):
        # Provide simple keyboard for experience levels
        reply_keyboard = [["Начинающий", "Средний", "Продвинутый"]]
        await update.message.reply_text(
            "Выберите новый уровень подготовки:",
            reply_markup=ReplyKeyboardMarkup(reply_keyboard, one_time_keyboard=True),
        )
        return EDIT_EXPERIENCE
    if text.startswith("редактировать цель"):
        await update.message.reply_text(
            "Введите новую цель (например: набор массы, похудение, выносливость):",
            reply_markup=ReplyKeyboardRemove(),
        )
        return EDIT_GOAL
    if text.startswith("изменить имя"):
        await update.message.reply_text(
            "Введите новое имя и фамилию через пробел:",
            reply_markup=ReplyKeyboardRemove(),
        )
        return EDIT_NAME
    if text.startswith("показать план"):
        await show_plan_handler(update, context)
        return MAIN_MENU
    if text.startswith("сгенерировать новый план"):
        await generate_plan_handler(update, context)
        return MAIN_MENU
    if text.startswith("прошлые тренировки"):
        await past_workouts_handler(update, context)
        return MAIN_MENU
    if text.startswith("выполнить тренировку"):
        await perform_workout_handler(update, context)
        return MAIN_MENU
    if text.startswith("статистика"):
        await stats_handler(update, context)
        return MAIN_MENU
    if text.startswith("показать имт"):
        await bmi_handler(update, context)
        return MAIN_MENU
    if text.startswith("удалить профиль"):
        await confirm_delete_handler(update, context)
        return CONFIRM_DELETE
    # Unknown selection
    await update.message.reply_text("Неизвестная команда. Пожалуйста, выберите пункт из меню.")
    return MAIN_MENU


async def edit_height(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Update the user's height."""
    user_id = context.user_data.get('user_id')
    text = update.message.text.strip().replace(',', '.')
    try:
        height_value = float(text)
        if height_value <= 0:
            raise ValueError
        update_user_field(user_id, 'height', height_value)
        await update.message.reply_text(f"Рост обновлен: {height_value} см.")
    except ValueError:
        await update.message.reply_text("Пожалуйста, введите корректное число для роста.")
        return EDIT_HEIGHT
    await show_menu(update, context)
    return MAIN_MENU


async def edit_weight(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Update the user's weight."""
    user_id = context.user_data.get('user_id')
    text = update.message.text.strip().replace(',', '.')
    try:
        weight_value = float(text)
        if weight_value <= 0:
            raise ValueError
        update_user_field(user_id, 'weight', weight_value)
        await update.message.reply_text(f"Вес обновлен: {weight_value} кг.")
    except ValueError:
        await update.message.reply_text("Пожалуйста, введите корректное число для веса.")
        return EDIT_WEIGHT
    await show_menu(update, context)
    return MAIN_MENU


async def edit_days(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Update the user's training days per week."""
    user_id = context.user_data.get('user_id')
    text = update.message.text.strip()
    if not text.isdigit():
        await update.message.reply_text("Введите целое число от 1 до 7.")
        return EDIT_DAYS
    days_value = int(text)
    if not 1 <= days_value <= 7:
        await update.message.reply_text("Количество дней должно быть от 1 до 7.")
        return EDIT_DAYS
    update_user_field(user_id, 'days', days_value)
    await update.message.reply_text(f"Количество дней обновлено: {days_value}.")
    await show_menu(update, context)
    return MAIN_MENU


async def edit_experience(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Update the user's experience level."""
    user_id = context.user_data.get('user_id')
    experience_value = update.message.text.strip()
    update_user_field(user_id, 'experience', experience_value)
    await update.message.reply_text(f"Уровень подготовки обновлён: {experience_value}.")
    await show_menu(update, context)
    return MAIN_MENU


async def edit_goal(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Update the user's fitness goal."""
    user_id = context.user_data.get('user_id')
    goal_value = update.message.text.strip()
    update_user_field(user_id, 'goal', goal_value)
    await update.message.reply_text(f"Цель обновлена: {goal_value}.")
    await show_menu(update, context)
    return MAIN_MENU


async def edit_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Update the user's first and last name.

    The user is expected to provide a new first name and surname
    separated by whitespace.  If the input cannot be parsed, the
    function prompts the user again.
    """
    user_id = context.user_data.get('user_id')
    text = update.message.text.strip()
    parts = text.split()
    if len(parts) < 2:
        await update.message.reply_text(
            "Пожалуйста, введите имя и фамилию через пробел.",
        )
        return EDIT_NAME
    first_name = parts[0]
    last_name = " ".join(parts[1:])
    update_user_field(user_id, 'first_name', first_name)
    update_user_field(user_id, 'last_name', last_name)
    await update.message.reply_text(
        f"Имя и фамилия обновлены: {first_name} {last_name}."
    )
    await show_menu(update, context)
    return MAIN_MENU


async def stats_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send the user's workout statistics and return to the main menu."""
    user_id = context.user_data.get('user_id')
    msg = workout_stats(user_id)
    await update.message.reply_text(msg)
    await show_menu(update, context)


async def bmi_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Calculate and display the user's BMI and classification."""
    user_id = context.user_data.get('user_id')
    user = get_user(user_id)
    weight = float(user.get('weight', 0) or 0)
    height = float(user.get('height', 0) or 0)
    bmi_result = compute_bmi(weight, height)
    if bmi_result is None:
        await update.message.reply_text(
            "Для расчёта ИМТ заполните ваш вес и рост в профиле."
        )
    else:
        bmi, category = bmi_result
        await update.message.reply_text(
            f"Ваш ИМТ: {bmi} (категория: {category})."
        )
    await show_menu(update, context)


async def confirm_delete_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Ask the user to confirm deletion of their profile."""
    await update.message.reply_text(
        "Вы уверены, что хотите удалить профиль и все данные? (да/нет)"
    )


async def handle_confirm_delete(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle the user's confirmation for deleting their profile."""
    user_id = context.user_data.get('user_id')
    response = update.message.text.strip().lower()
    if response in {"да", "д", "yes", "y"}:
        delete_user(user_id)
        await update.message.reply_text(
            "Ваш профиль удалён. Если захотите создать новый, отправьте /start.",
            reply_markup=ReplyKeyboardRemove(),
        )
        # Clear user context
        context.user_data.clear()
        return ConversationHandler.END
    else:
        await update.message.reply_text("Удаление отменено.")
        await show_menu(update, context)
        return MAIN_MENU


async def show_plan_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send the existing workout plan to the user if available."""
    user_id = context.user_data.get('user_id')
    user = get_user(user_id)
    # Check completeness of required data
    required_fields = ['goal', 'experience', 'weight', 'height', 'days']
    incomplete = [f for f in required_fields if not user.get(f) or (isinstance(user[f], (int, float)) and user[f] == 0)]
    if incomplete:
        await update.message.reply_text(
            "Пожалуйста, заполните все данные профиля, прежде чем просматривать план."
        )
        return
    plan = user.get('plan')
    if not plan:
        await update.message.reply_text(
            "План ещё не был создан. Нажмите 'Сгенерировать новый план' чтобы создать его."
        )
        return
    await update.message.reply_text(plan)


async def generate_plan_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Generate a new workout plan, store it in the database, and send it."""
    user_id = context.user_data.get('user_id')
    user = get_user(user_id)
    # Ensure all fields are filled
    required_fields = ['goal', 'experience', 'weight', 'height', 'days']
    incomplete = [f for f in required_fields if not user.get(f) or (isinstance(user[f], (int, float)) and user[f] == 0)]
    if incomplete:
        await update.message.reply_text(
            "Для генерации плана заполните все данные профиля (цель, уровень, вес, рост, дни)."
        )
        return
    profile = UserProfile(
        goal=user['goal'],
        experience=user['experience'],
        weight=float(user['weight']),
        height=float(user['height']),
        days=int(user['days']),
    )
    try:
        plan_text = await generate_workout_plan(profile)
        save_plan(user_id, plan_text)
        await update.message.reply_text("План успешно сгенерирован:\n" + plan_text)
    except Exception as exc:
        await update.message.reply_text(
            f"Ошибка при генерации плана: {exc}. Попробуйте позже."
        )


async def past_workouts_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Display the list of past completed workouts for the user."""
    user_id = context.user_data.get('user_id')
    dates = get_workouts(user_id)
    if not dates:
        await update.message.reply_text("У вас ещё нет завершённых тренировок.")
        return
    # Show up to the last 10 workouts
    recent = dates[:10]
    msg = "Последние тренировки:\n" + "\n".join(recent)
    await update.message.reply_text(msg)


async def perform_workout_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Mark the current date as a completed workout for the user."""
    user_id = context.user_data.get('user_id')
    user = get_user(user_id)
    if not user.get('plan'):
        await update.message.reply_text(
            "Сначала сгенерируйте тренировочный план, чтобы отмечать тренировки."
        )
        return
    today_str = date.today().isoformat()
    add_workout(user_id, today_str)
    await update.message.reply_text(
        f"Отлично! Тренировка за {today_str} отмечена как выполненная."
    )




async def generate_workout_plan(profile: UserProfile) -> str:
    """Generate a workout plan using a free LLM provider (OpenRouter).

    This function builds a prompt based on the user's profile and sends
    it to the OpenRouter API.  OpenRouter provides access to a variety
    of large language models with generous free quotas【841123382249507†L280-L334】.  The
    API key and model name must be provided via the environment
    variables ``OPENROUTER_API_KEY`` and ``OPENROUTER_MODEL``.  If
    ``OPENROUTER_MODEL`` is unset, a sensible default will be used.

    The function returns the model's response as plain text.  Any HTTP
    errors or missing configuration will raise exceptions that are
    handled upstream.
    """
    # Read API key for OpenRouter.  This key can be obtained for free from
    # openrouter.ai.  See the documentation for rate limits and free
    # models【841123382249507†L280-L334】.
    api_key = os.environ.get("OPENROUTER_API_KEY")
    if not api_key:
        raise RuntimeError(
            "OPENROUTER_API_KEY не найден. Пожалуйста, укажите ключ OpenRouter в файле .env."
        )

    # Determine which model to use.  Users can override this by setting
    # OPENROUTER_MODEL in their environment.  Popular free models include
    # ``deepseek-ai/deepseek-r1-0528``, ``google/gemma-3n-e2b-it`` and
    # ``deepseek-ai/deepseek-v3-0324``【841123382249507†L280-L334】.  The default below is
    # deepseek-ai's v3 model.
    model_name = os.environ.get("OPENROUTER_MODEL", "deepseek-ai/deepseek-v3-0324")

    # Simple heuristic: recommended total sets per week for major muscle groups.
    experience_map: Dict[str, int] = {
        "начинающий": 10,
        "средний": 15,
        "продвинутый": 20,
    }
    sets_per_week = experience_map.get(profile.experience.lower(), 12)
    sets_per_session = max(1, round(sets_per_week / profile.days))

    # Build the prompts for the chat model
    system_prompt = (
        "Вы — профессиональный тренер по фитнесу. Ваша задача — составить "
        "персональный план тренировок для клиента на основе его целей и "
        "данных. План должен включать упражнения для основных групп мышц, "
        "распределённые по дням недели, количество подходов и повторений, "
        "рекомендации по отдыху между подходами, а также советы по разминке "
        "и заминке. Не указывайте лишнюю теорию — только практические "
        "рекомендации."
    )
    user_prompt = (
        f"Составь план тренировок. {profile.to_prompt()}\n"
        f"Рекомендуемое количество подходов на каждую группу мышц в неделю: {sets_per_week}. "
        f"Поскольку пользователь тренируется {profile.days} раз(а) в неделю, "
        f"предложи примерно {sets_per_session} подходов на сессию для крупных мышц. "
        "Используй таблицу или маркированный список для структуры."
    )
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]

    # Prepare the HTTP request to OpenRouter.  We use requests.post with
    # JSON payload.  Note: this is a synchronous call inside an async
    # function; for production workloads you may wish to use an async
    # HTTP client such as aiohttp or httpx.
    url = "https://openrouter.ai/api/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": model_name,
        "messages": messages,
        "max_tokens": 800,
        "temperature": 0.7,
    }

    # Perform the HTTP request
    response = requests.post(url, headers=headers, json=payload, timeout=60)
    response.raise_for_status()
    data = response.json()

    # Extract the assistant's reply.  The structure of the response matches
    # the OpenAI API: each choice contains a message with a content field.
    return data["choices"][0]["message"]["content"]


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Allow the user to cancel the conversation at any time."""
    await update.message.reply_text(
        "Диалог отменён. Если захотите ещё раз воспользоваться ботом, напишите /start.",
        reply_markup=ReplyKeyboardRemove(),
    )
    return ConversationHandler.END


def main() -> None:
    """Run the Telegram bot.  Reads configuration from environment variables."""
    # Initialize the SQLite database
    init_db()

    telegram_token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not telegram_token:
        raise RuntimeError(
            "TELEGRAM_BOT_TOKEN не установлен. Укажите токен бота в переменных окружения."
        )

    application = ApplicationBuilder().token(telegram_token).build()

    # Define the conversation handler with the new registration and menu states
    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            REGISTRATION: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_registration)],
            MAIN_MENU: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_menu)],
            EDIT_HEIGHT: [MessageHandler(filters.TEXT & ~filters.COMMAND, edit_height)],
            EDIT_WEIGHT: [MessageHandler(filters.TEXT & ~filters.COMMAND, edit_weight)],
            EDIT_DAYS: [MessageHandler(filters.TEXT & ~filters.COMMAND, edit_days)],
            EDIT_EXPERIENCE: [MessageHandler(filters.TEXT & ~filters.COMMAND, edit_experience)],
            EDIT_GOAL: [MessageHandler(filters.TEXT & ~filters.COMMAND, edit_goal)],
            EDIT_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, edit_name)],
            CONFIRM_DELETE: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_confirm_delete)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    application.add_handler(conv_handler)
    application.add_handler(CommandHandler("help", start))  # alias /help to restart

    # Start the bot.  run_polling() will block until the bot is shut down.
    print("Бот запущен. Нажмите Ctrl+C для остановки.")
    application.run_polling()


if __name__ == "__main__":
    # Only run the bot if this module is executed directly.
    main()