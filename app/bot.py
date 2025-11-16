import asyncio
import os
from pathlib import Path
from datetime import datetime
from typing import Optional, Dict, List, Set
from collections import defaultdict, deque
from concurrent.futures import ThreadPoolExecutor
import time

import pandas as pd
from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    Message,
    FSInputFile,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    CallbackQuery
)

from wink_v4 import (
    SceneSegmenter,
    SceneNER,
    ProductionTableCreator,
    FileProcessor
)

# ======================
# Конфигурация
# ======================

from dotenv import load_dotenv
load_dotenv()

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")
MODELS_PATH = Path(os.getenv("WINK_MODELS_DIR", "./models_v5"))
TEMP_DIR = Path("./temp_bot_files")
TEMP_DIR.mkdir(exist_ok=True)

MEDIA_GROUP_TIMEOUT = 5
executor = ThreadPoolExecutor(max_workers=2)

# ======================
# Глобальный Rate Limiter
# ======================

class GlobalRateLimiter:
    """Глобальный ограничитель частоты запросов к Telegram API"""
    
    def __init__(self, max_calls: int = 12, period: float = 1.0):
        self.max_calls = max_calls
        self.period = period
        self.calls = deque()
        self.lock = asyncio.Lock()
    
    async def wait_if_needed(self):
        """Ждет если достигнут лимит запросов"""
        async with self.lock:
            now = time.time()
            
            # Удаляем старые вызовы
            while self.calls and self.calls[0] < now - self.period:
                self.calls.popleft()
            
            # Если достигли лимита - ждем
            if len(self.calls) >= self.max_calls:
                sleep_time = self.period - (now - self.calls[0]) + 0.2
                if sleep_time > 0:
                    await asyncio.sleep(sleep_time)
            
            self.calls.append(now)

rate_limiter = GlobalRateLimiter(max_calls=12, period=1.0)

# ======================
# Helper функции
# ======================

async def safe_edit_text(message: Message, text: str, **kwargs):
    """Безопасное редактирование с rate limiting"""
    await rate_limiter.wait_if_needed()
    
    max_retries = 2
    for attempt in range(max_retries):
        try:
            await message.edit_text(text, **kwargs)
            return True
        except Exception as e:
            if "message is not modified" in str(e).lower():
                return True
            if "retryafter" in str(e).lower():
                await asyncio.sleep(10)
                continue
            if attempt < max_retries - 1:
                await asyncio.sleep(3 ** attempt)
            else:
                print(f"Не удалось отредактировать: {e}")
                return False
    return False

async def safe_answer(message: Message, text: str, **kwargs):
    """Безопасная отправка с rate limiting"""
    await rate_limiter.wait_if_needed()
    
    max_retries = 2
    for attempt in range(max_retries):
        try:
            return await message.answer(text, **kwargs)
        except Exception as e:
            if "retryafter" in str(e).lower():
                await asyncio.sleep(10)
                continue
            if attempt < max_retries - 1:
                await asyncio.sleep(3 ** attempt)
            else:
                raise
    return None

# ======================
# Определение колонок
# ======================

MANDATORY_COLUMNS = [
    "Источник", "Серия", "Сцена", "Режим", "Инт / нат", "Объект / Подобъект / Синопсис"
]

OPTIONAL_COLUMNS = {
    "time": {"name": "Время года / Примечание", "emoji": "🌤️"},
    "chars": {"name": "Персонажи", "emoji": "👥"},
    "crowd": {"name": "Массовка", "emoji": "👫"},
    "group": {"name": "Групповка", "emoji": "👨‍👩‍👧"},
    "makeup": {"name": "Грим", "emoji": "💄"},
    "costume": {"name": "Костюм", "emoji": "👔"},
    "props": {"name": "Реквизит", "emoji": "🎭"},
    "transport": {"name": "Игровой транспорт", "emoji": "🚗"},
    "decor": {"name": "Декорация", "emoji": "🏛️"},
    "pyro": {"name": "Пиротехника", "emoji": "🎆"},
    "stunt": {"name": "Каскадер / Трюк", "emoji": "🤸"},
    "music": {"name": "Музыка", "emoji": "🎵"},
    "sfx": {"name": "Спецэффект", "emoji": "✨"},
    "equip": {"name": "Спец. оборудование", "emoji": "🎬"}
}

# ======================
# FSM States
# ======================

class ProcessingStates(StatesGroup):
    waiting_for_file = State()
    choosing_columns = State()
    choosing_format = State()

# ======================
# MediaGroupCollector
# ======================

class MediaGroupCollector:
    def __init__(self):
        self.groups: Dict[str, List[Message]] = defaultdict(list)
        self.timers: Dict[str, asyncio.Task] = {}
        self.locks: Dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
    
    async def add_message(self, media_group_id: str, message: Message, callback):
        async with self.locks[media_group_id]:
            self.groups[media_group_id].append(message)
            
            if media_group_id in self.timers:
                self.timers[media_group_id].cancel()
            
            async def delayed_callback():
                try:
                    await asyncio.sleep(MEDIA_GROUP_TIMEOUT)
                    async with self.locks[media_group_id]:
                        messages = self.groups.get(media_group_id, [])
                        if messages:
                            await callback(messages)
                        self.clear_group(media_group_id)
                except asyncio.CancelledError:
                    pass
                except Exception as e:
                    print(f"Ошибка в delayed_callback: {e}")
            
            self.timers[media_group_id] = asyncio.create_task(delayed_callback())
    
    def clear_group(self, media_group_id: str):
        if media_group_id in self.groups:
            del self.groups[media_group_id]
        if media_group_id in self.timers:
            if not self.timers[media_group_id].done():
                self.timers[media_group_id].cancel()
            del self.timers[media_group_id]
        if media_group_id in self.locks:
            del self.locks[media_group_id]

media_collector = MediaGroupCollector()

# ======================
# ModelsContainer
# ======================

class ModelsContainer:
    def __init__(self):
        self.segmenter: Optional[SceneSegmenter] = None
        self.ner: Optional[SceneNER] = None
        self.table_creator: Optional[ProductionTableCreator] = None
        self.file_processor = FileProcessor()
        self.loaded = False
    
    def load_models(self):
        if self.loaded:
            return
        try:
            print("🔄 Загрузка моделей...")
            seg_path = MODELS_PATH / "segmentation_ruBERT"
            ner_path = MODELS_PATH / "ner_ruBERT"
            
            self.segmenter = SceneSegmenter()
            self.segmenter.load_segmentation_model(str(seg_path))
            
            self.ner = SceneNER()
            self.ner.load_ner_model(str(ner_path))
            
            self.table_creator = ProductionTableCreator()
            self.table_creator.segmenter = self.segmenter
            self.table_creator.ner = self.ner
            
            self.loaded = True
            print("✅ Модели загружены")
        except Exception as e:
            print(f"❌ Ошибка загрузки моделей: {e}")
            raise

models = ModelsContainer()

# ======================
# Клавиатуры
# ======================

def get_columns_keyboard(selected_columns: Set[str]) -> InlineKeyboardMarkup:
    buttons = []
    row = []
    for col_id, col_info in OPTIONAL_COLUMNS.items():
        is_selected = col_id in selected_columns
        checkbox = "✅" if is_selected else "☐"
        button = InlineKeyboardButton(
            text=f"{checkbox} {col_info['emoji']} {col_info['name'][:15]}...",
            callback_data=f"col_{col_id}"
        )
        row.append(button)
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    
    buttons.append([
        InlineKeyboardButton(text="✅ Выбрать все", callback_data="col_select_all"),
        InlineKeyboardButton(text="☐ Снять все", callback_data="col_deselect_all")
    ])
    buttons.append([
        InlineKeyboardButton(text="📋 Только основное", callback_data="col_minimal")
    ])
    buttons.append([
        InlineKeyboardButton(
            text=f"➡️ Продолжить ({len(selected_columns)} выбрано)",
            callback_data="col_confirm"
        )
    ])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_format_keyboard() -> InlineKeyboardMarkup:
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="📊 Excel (.xlsx)", callback_data="format_xlsx"),
            InlineKeyboardButton(text="📄 CSV (.csv)", callback_data="format_csv")
        ],
        [InlineKeyboardButton(text="📦 Оба формата", callback_data="format_both")]
    ])
    return keyboard

def get_main_menu_keyboard() -> InlineKeyboardMarkup:
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📝 Отправить сценарий", callback_data="new_script")],
        [InlineKeyboardButton(text="ℹ️ Помощь", callback_data="help")]
    ])
    return keyboard

# ======================
# Утилиты
# ======================

def create_progress_bar(current: int, total: int, length: int = 15) -> str:
    filled = int(length * current / total)
    bar = '█' * filled + '░' * (length - filled)
    percent = int(100 * current / total)
    return f"[{bar}] {percent}%"

def create_progress_bar_percent(percent: int, length: int = 15) -> str:
    filled = int(length * percent / 100)
    bar = '█' * filled + '░' * (length - filled)
    return f"[{bar}] {percent}%"

def extract_episode_from_filename(filename: str) -> Optional[str]:
    import re
    patterns = [
        r'[Сс]ерия[_\s-]*(\d+)', r'[Ee]pisode[_\s-]*(\d+)', r'[Ee]p[_\s-]*(\d+)',
        r'[Сс][_\s-]*(\d+)', r'_(\d+)_', r'-(\d+)-', r'\.(\d+)\.',
    ]
    for pattern in patterns:
        match = re.search(pattern, filename)
        if match:
            return match.group(1)
    return None

async def cleanup_temp_file(file_path: str):
    try:
        path = Path(file_path)
        if path.exists():
            path.unlink()
    except Exception as e:
        print(f"Не удалось удалить файл {file_path}: {e}")

async def cleanup_files(file_infos: List[Dict]):
    for info in file_infos:
        await cleanup_temp_file(info['path'])

def filter_dataframe_columns(df: pd.DataFrame, selected_columns: Set[str]) -> pd.DataFrame:
    columns_to_keep = MANDATORY_COLUMNS.copy()
    for col_id in selected_columns:
        if col_id in OPTIONAL_COLUMNS:
            col_name = OPTIONAL_COLUMNS[col_id]["name"]
            if col_name in df.columns:
                columns_to_keep.append(col_name)
    available_columns = [col for col in columns_to_keep if col in df.columns]
    return df[available_columns]

def create_excel_file(filtered_df, file_infos, total_files, xlsx_path):
    try:
        with pd.ExcelWriter(xlsx_path, engine='xlsxwriter') as writer:
            filtered_df.to_excel(writer, sheet_name='Все сцены', index=False)
            if total_files > 1:
                for info in file_infos:
                    file_df = filtered_df[filtered_df['Источник'] == info['name']]
                    if not file_df.empty:
                        sheet_name = Path(info['name']).stem[:31]
                        file_df.to_excel(writer, sheet_name=sheet_name, index=False)
            stats_data = {
                'Показатель': ['Всего файлов', 'Всего сцен', 'Уникальных локаций', 'Персонажей упомянуто'],
                'Значение': [
                    len(file_infos),
                    len(filtered_df),
                    filtered_df['Объект / Подобъект / Синопсис'].nunique(),
                    filtered_df['Персонажи'].str.len().sum() if 'Персонажи' in filtered_df.columns else 0
                ]
            }
            stats_df = pd.DataFrame(stats_data)
            stats_df.to_excel(writer, sheet_name='Статистика', index=False)
    except Exception as e:
        print(f"❌ Ошибка создания Excel: {e}")
        raise

def create_csv_file(df, csv_path):
    try:
        df.to_csv(csv_path, index=False, encoding='utf-8-sig')
    except Exception as e:
        print(f"❌ Ошибка создания CSV: {e}")
        raise

async def animate_processing(message: Message, file_name: str, idx: int, total: int):
    """Анимация с редкими обновлениями (каждые 5 сек)"""
    frames = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]
    stages = [
        (0, 10, "🔍 Анализирую структуру..."),
        (10, 25, "📄 Разбиваю на предложения..."),
        (20, 45, "🔪 Сегментирую на сцены..."),
        (40, 70, "🏷️ Извлекаю сущности..."),
        (60, 90, "🎭 Финальная обработка..."),
    ]
    
    frame_idx = 0
    elapsed_seconds = 0
    update_interval = 5  # 5 секунд между обновлениями
    failed_updates = 0
    max_failed = 2
    last_message = ""
    
    while True:
        frame = frames[frame_idx % len(frames)]
        current_stage_text = stages[0][2]
        progress = stages[0][1]
        
        for stage_time, stage_progress, stage_text in stages:
            if elapsed_seconds >= stage_time:
                current_stage_text = stage_text
                progress = stage_progress
        
        progress_bar = create_progress_bar_percent(progress)
        new_message = (
            f"{frame} <b>Обработка {idx}/{total}</b>\n\n"
            f"📄 <code>{file_name[:30]}...</code>\n\n"
            f"{progress_bar}\n\n"
            f"{current_stage_text}"
        )
        
        # Обновляем только если текст изменился
        if new_message != last_message:
            success = await safe_edit_text(message, new_message, parse_mode="HTML")
            if not success:
                failed_updates += 1
                if failed_updates >= max_failed:
                    break
            else:
                failed_updates = 0
                last_message = new_message
        
        await asyncio.sleep(update_interval)
        frame_idx += 1
        elapsed_seconds += update_interval

# ======================
# Обработчики команд
# ======================

router = Router()

@router.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    welcome_text = (
        "👋 <b>Добро пожаловать в Cinematiz Wizard!</b>\n\n"
        "Я помогу создать продакшн-таблицу из сценариев.\n\n"
        "📤 <b>Как использовать:</b>\n"
        "1️⃣ Отправьте файл(ы) сценария (PDF/DOCX/TXT)\n"
        "2️⃣ Выберите нужные колонки\n"
        "3️⃣ Выберите формат (Excel/CSV)\n"
        "4️⃣ Получите готовую таблицу!\n\n"
        "💡 <b>Совет:</b> Можно отправить несколько файлов одновременно.\n\n"
        "Отправьте файл прямо сейчас! 🚀"
    )
    await safe_answer(message, welcome_text, reply_markup=get_main_menu_keyboard(), parse_mode="HTML")
    await state.set_state(ProcessingStates.waiting_for_file)

@router.message(Command("help"))
async def cmd_help(message: Message):
    help_text = (
        "📖 <b>Подробная инструкция</b>\n\n"
        "<b>1️⃣ Отправка файлов:</b>\n"
        "• Один файл: обработается отдельно\n"
        "• Несколько файлов: объединятся в одну таблицу\n\n"
        "<b>2️⃣ Выбор колонок:</b>\n"
        "Выберите нужные данные для таблицы\n\n"
        "<b>3️⃣ Формат:</b>\n"
        "• Excel: многолистовой документ\n"
        "• CSV: простая таблица\n\n"
        "❓ Вопросы? Напишите /start"
    )
    await safe_answer(message, help_text, parse_mode="HTML")

# ======================
# ОПТИМИЗИРОВАННАЯ ЗАГРУЗКА ФАЙЛОВ
# ======================

async def process_collected_files(messages: List[Message], state: FSMContext, bot: Bot):
    """ОПТИМИЗИРОВАННАЯ обработка файлов - минимум обращений к API"""
    
    if not messages:
        return
    
    first_message = messages[0]
    user_id = first_message.from_user.id
    
    # ОДНО сообщение о начале
    status_msg = await safe_answer(
        first_message,
        f"📦 Получено: <b>{len(messages)}</b> файл(ов)\n⏳ Загружаю...",
        parse_mode="HTML"
    )
    
    file_infos = []
    errors = []
    
    # Загружаем ВСЕ файлы БЕЗ промежуточных обновлений
    for idx, msg in enumerate(messages, 1):
        document = msg.document
        file_name = document.file_name
        file_ext = Path(file_name).suffix.lower()
        
        # Валидация
        if file_ext not in ['.pdf', '.docx', '.txt']:
            errors.append(f"Файл #{idx} - неподдерживаемый формат")
            continue
        
        if document.file_size > 50 * 1024 * 1024:
            errors.append(f"Файл #{idx} - слишком большой")
            continue
        
        try:
            file = await bot.get_file(document.file_id)
            temp_file_path = TEMP_DIR / f"{user_id}_{datetime.now().timestamp()}_{idx}_{file_name}"
            await bot.download_file(file.file_path, temp_file_path)
            
            file_infos.append({
                'path': str(temp_file_path),
                'name': file_name,
                'ext': file_ext,
                'index': idx
            })
        except Exception as e:
            errors.append(f"Ошибка загрузки #{idx}: {str(e)[:50]}")
    
    # Если есть ошибки - показываем
    if errors and not file_infos:
        await safe_edit_text(status_msg, "❌ " + "\n".join(errors), parse_mode="HTML")
        return
    
    # ОДНО обновление после загрузки всех
    await asyncio.sleep(1) 
    
    files_list = "\n".join([f"  ✓ {info['name'][:30]}..." for info in file_infos])
    
    success_text = f"✅ Загружено: <b>{len(file_infos)}</b>\n\n{files_list}\n\n📋 Выберите колонки:"
    if errors:
        success_text += f"\n\n⚠️ Пропущено: {len(errors)}"
    
    await safe_edit_text(status_msg, success_text, parse_mode="HTML")
    
    await state.update_data(
        file_infos=file_infos,
        total_files=len(file_infos),
        selected_columns=set()
    )
    
    await asyncio.sleep(1) 
    await show_columns_selection(first_message, state)
    await state.set_state(ProcessingStates.choosing_columns)

async def show_columns_selection(message: Message, state: FSMContext):
    data = await state.get_data()
    selected_columns = data.get("selected_columns", set())
    
    text = (
        "📋 <b>Выбор колонок</b>\n\n"
        "Обязательные (\"Источник\", \"Серия\", \"Сцена\", \"Режим\", \"Инт / нат\", \"Объект / Подобъект / Синопсис\" - всегда включены):\n"
    )
    for col in MANDATORY_COLUMNS:
        text += f"  ✓ {col}\n"
    text += "\n<b>Выберите дополнительные:</b>"
    
    keyboard = get_columns_keyboard(selected_columns)
    await safe_answer(message, text, reply_markup=keyboard, parse_mode="HTML")

# Словарь для отслеживания обрабатываемых пользователей
processing_users: Dict[int, bool] = {}

@router.message(F.document, StateFilter(ProcessingStates.waiting_for_file))
async def handle_document(message: Message, state: FSMContext, bot: Bot):
    user_id = message.from_user.id
    media_group_id = message.media_group_id
    
    if media_group_id:
        async def process_group_callback(messages: List[Message]):
            if processing_users.get(user_id, False):
                return
            processing_users[user_id] = True
            try:
                await process_collected_files(messages, state, bot)
            finally:
                processing_users[user_id] = False
        
        await media_collector.add_message(media_group_id, message, process_group_callback)
    else:
        if processing_users.get(user_id, False):
            await safe_answer(message, "⏳ Предыдущий файл обрабатывается...")
            return
        
        processing_users[user_id] = True
        try:
            await process_collected_files([message], state, bot)
        finally:
            processing_users[user_id] = False

@router.message(F.document)
async def handle_document_fallback(message: Message, state: FSMContext, bot: Bot):
    await state.set_state(ProcessingStates.waiting_for_file)
    await handle_document(message, state, bot)

# ======================
# Обработчики выбора колонок
# ======================

@router.callback_query(F.data.startswith("col_"), StateFilter(ProcessingStates.choosing_columns))
async def handle_column_selection(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    
    data = await state.get_data()
    selected_columns = data.get("selected_columns", set())
    action = callback.data[4:]
    
    if action == "select_all":
        selected_columns = set(OPTIONAL_COLUMNS.keys())
    elif action == "deselect_all":
        selected_columns = set()
    elif action == "minimal":
        selected_columns = {"chars", "props", "time"}
    elif action == "confirm":
        if not selected_columns:
            await callback.answer("⚠️ Выберите хотя бы одну колонку", show_alert=True)
            return
        
        await state.update_data(selected_columns=selected_columns)
        
        col_list = "\n".join([
            f"  • {OPTIONAL_COLUMNS[col]['emoji']} {OPTIONAL_COLUMNS[col]['name']}"
            for col in sorted(selected_columns)
        ])
        
        await safe_edit_text(
            callback.message,
            f"✅ <b>Колонки выбраны!</b>\n\n"
            f"Обязательные:\n  • Источник, Серия, Сцена\n  • Режим, Инт/нат, Объект\n\n"
            f"Дополнительные:\n{col_list}\n\n"
            f"📊 Выберите формат:",
            reply_markup=get_format_keyboard(),
            parse_mode="HTML"
        )
        await state.set_state(ProcessingStates.choosing_format)
        return
    else:
        if action in selected_columns:
            selected_columns.remove(action)
        else:
            selected_columns.add(action)
    
    await state.update_data(selected_columns=selected_columns)
    keyboard = get_columns_keyboard(selected_columns)
    
    try:
        await callback.message.edit_reply_markup(reply_markup=keyboard)
    except:
        pass

# ======================
# Обработчик выбора формата
# ======================

@router.callback_query(F.data.startswith("format_"), StateFilter(ProcessingStates.choosing_format))
async def handle_format_choice(callback: CallbackQuery, state: FSMContext, bot: Bot):
    await callback.answer()
    
    format_choice = callback.data.split("_")[1]
    data = await state.get_data()
    file_infos = data.get("file_infos", [])
    total_files = data.get("total_files", 0)
    selected_columns = data.get("selected_columns", set())
    
    if not file_infos:
        await safe_edit_text(callback.message, "❌ Файлы не найдены.")
        await state.clear()
        return
    
    # ОДНО сообщение о начале
    await safe_edit_text(callback.message, f"⚙️ Обрабатываю {total_files} файл(ов)...", parse_mode="HTML")
    await asyncio.sleep(1)
    
    try:
        if not models.loaded:
            models.load_models()
        
        all_dataframes = []
        
        # Обработка файлов
        for idx, file_info in enumerate(file_infos, 1):
            file_path = file_info['path']
            file_name = file_info['name']
            
            if not Path(file_path).exists():
                continue
            
            try:
                loop = asyncio.get_event_loop()
                text = await loop.run_in_executor(executor, models.file_processor.process_file, file_path)
            except Exception as e:
                print(f"Ошибка чтения: {e}")
                continue
            
            if not text or len(text.strip()) < 100:
                continue
            
            # Анимация
            animation_task = asyncio.create_task(
                animate_processing(callback.message, file_name, idx, total_files)
            )
            
            try:
                episode_number = extract_episode_from_filename(file_name) or str(idx)
                loop = asyncio.get_event_loop()
                df = await loop.run_in_executor(
                    executor,
                    models.table_creator.process_script_file,
                    text,
                    episode_number
                )
            finally:
                animation_task.cancel()
                try:
                    await animation_task
                except asyncio.CancelledError:
                    pass
            
            if not df.empty:
                df.insert(0, 'Источник', file_name)
                all_dataframes.append(df)
            
            await asyncio.sleep(1)
        
        if not all_dataframes:
            await safe_edit_text(callback.message, "❌ Не удалось обработать файлы.")
            await cleanup_files(file_infos)
            await state.clear()
            return
        
        # Объединение и экспорт
        await safe_edit_text(callback.message, "📊 Создаю таблицу...")
        await asyncio.sleep(1)
        
        combined_df = pd.concat(all_dataframes, ignore_index=True)
        filtered_df = filter_dataframe_columns(combined_df, selected_columns)
        
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        base_name = f"result_{total_files}files" if total_files > 1 else Path(file_infos[0]['name']).stem
        
        files_to_send = []
        loop = asyncio.get_event_loop()
        
        if format_choice in ["xlsx", "both"]:
            xlsx_path = TEMP_DIR / f"{base_name}_{timestamp}.xlsx"
            await loop.run_in_executor(executor, create_excel_file, filtered_df, file_infos, total_files, xlsx_path)
            files_to_send.append(("Excel", xlsx_path))
        
        if format_choice in ["csv", "both"]:
            csv_path = TEMP_DIR / f"{base_name}_{timestamp}.csv"
            await loop.run_in_executor(executor, create_csv_file, filtered_df, csv_path)
            files_to_send.append(("CSV", csv_path))
        
        # Статистика
        total_scenes = len(filtered_df)
        stats_text = f"✅ <b>Готово!</b>\n\n📊 Сцен: <b>{total_scenes}</b>\n📁 Файлов: <b>{len(all_dataframes)}</b>"
        
        await safe_edit_text(callback.message, stats_text, parse_mode="HTML")
        await asyncio.sleep(1)
        
        # Отправка файлов
        for file_type, file_path_export in files_to_send:
            file_to_send = FSInputFile(file_path_export)
            await callback.message.answer_document(file_to_send, caption=f"📊 Продакшн-таблица ({file_type})")
            await cleanup_temp_file(file_path_export)
        
        await safe_answer(
            callback.message,
            "🎉 Готово! Отправьте новый файл.",
            reply_markup=get_main_menu_keyboard()
        )
        
    except Exception as e:
        print(f"Ошибка: {e}")
        await safe_edit_text(callback.message, "❌ Ошибка обработки.", reply_markup=get_main_menu_keyboard())
    
    finally:
        await cleanup_files(file_infos)
        await state.set_state(ProcessingStates.waiting_for_file)

# ======================
# Обработчики кнопок меню
# ======================

@router.callback_query(F.data == "new_script")
async def handle_new_script(callback: CallbackQuery, state: FSMContext):
    await callback.answer("Жду файлы! 📤")
    await state.set_state(ProcessingStates.waiting_for_file)
    await safe_answer(
        callback.message,
        "📤 <b>Отправьте файл(ы)</b>\n\n💡 Можно несколько файлов",
        parse_mode="HTML"
    )

@router.callback_query(F.data == "help")
async def handle_help_button(callback: CallbackQuery):
    await callback.answer()
    await cmd_help(callback.message)

# ======================
# Запуск бота
# ======================

async def on_startup():
    print("🤖 Бот запускается...")
    try:
        models.load_models()
        print("✅ Модели загружены")
    except Exception as e:
        print(f"⚠️ Не удалось загрузить модели: {e}")

async def main():
    """Главная функция"""
    from aiogram.client.session.aiohttp import AiohttpSession
    
    session = AiohttpSession(timeout=90)
    
    bot = Bot(token=BOT_TOKEN, session=session)
    dp = Dispatcher(storage=MemoryStorage())
    
    dp.include_router(router)
    await on_startup()
    
    print("✅ Бот запущен!")
    await dp.start_polling(
        bot, 
        allowed_updates=dp.resolve_used_update_types(),
        polling_timeout=45
    )

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n👋 Бот остановлен")
