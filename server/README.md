# Server - Документация API

Cервер для Telegram бота и ML приложением обработки сценариев.

### Локальный запуск

```bash
# Установка зависимостей
pip install -r requirements.txt

# Запуск
python app.py

### Или через докер(не проверял):

# Сборка образа
docker build -t server .

# Запуск контейнера
docker run -d --name server -p 8123:8123 --env-file .env server
