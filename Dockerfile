FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    TZ=Europe/Moscow

# tzdata — иначе zoneinfo не найдёт зону и время в сводках останется UTC.
# Значение TZ переопределяется через окружение (docker-compose/.env).
RUN apt-get update && apt-get install -y --no-install-recommends tzdata ca-certificates \
    && ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Сначала зависимости — для кэширования слоёв
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY config.py i18n.py vocab.py db.py report.py bot.py \
     smoke_test.py flow_test.py fuzz_test.py ./

# Офлайн-проверки при сборке: схема БД и форматирование (smoke_test), прогон
# сценариев через хендлеры с подменённым Telegram (flow_test) и фаззинг
# callback_data (fuzz_test) — payload кнопки управляется клиентом, и именно на
# нём жили два блокера.
# Сломанный образ падает на `docker build`, а не в проде.
RUN python smoke_test.py && python flow_test.py && python fuzz_test.py

# Каталог для SQLite (монтируется как volume из ./data).
RUN mkdir -p /app/data

# Процесс намеренно остаётся root — как и у остальных ботов этого VPS.
# Причина: ./data на хосте принадлежит root, а chown внутри образа монтирование
# перекрывает — под non-root бот падал бы на старте с «unable to open database
# file». Входящих портов у контейнера нет, так что выигрыш от non-root тут
# меньше, чем риск не запуститься после первого же деплоя.
CMD ["python", "bot.py"]
