# Используем официальный образ Python
FROM python:3.11-slim-bullseye

# Устанавливаем системные зависимости
RUN apt-get update && \
    apt-get install -y \
        tesseract-ocr \
        tesseract-ocr-rus \
        poppler-utils \
        libgl1-mesa-glx \
        unzip \
        wget && \
    rm -rf /var/lib/apt/lists/*

# Устанавливаем gdown для загрузки с Google Drive
RUN pip install --no-cache-dir gdown

# Рабочая директория
WORKDIR /app

# Создаем директории для моделей и временных файлов
RUN mkdir -p models temp_bot_files

# Копируем только requirements.txt для кэширования слоёв
COPY requirements.txt .

# Устанавливаем Python-зависимости
RUN pip install --no-cache-dir -U pip && \
    pip install --no-cache-dir -r requirements.txt

# Копируем исходный код
COPY . .

# Скачиваем и распаковываем модели
RUN gdown --fuzzy "https://drive.google.com/uc?id=1y1HEjacQjgxP6650Mt5_soHGPOMxArm3" -O /app/models.zip && \
    rm /app/models_v5.zip && \
    chmod -R a+rw /app/models_v5

# Переменные окружения
ENV WINK_MODELS_DIR="/app/models_v5" \
    TEMP_DIR="/app/temp_bot_files" \
    PYTHONUNBUFFERED=1 \
    TELEGRAM_BOT_TOKEN="<your_token>"

# Очистка кэша apt
RUN apt-get clean && rm -rf /var/lib/apt/lists/*

# Старт бота
CMD ["python", "bot.py"]
