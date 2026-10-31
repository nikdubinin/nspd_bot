FROM python:3.13-slim

# Время в журнале — московское (в контейнере по умолчанию UTC).
# PYTHONUNBUFFERED: строки журнала сразу попадают в логи Docker, без задержки буфера.
ENV TZ=Europe/Moscow \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 1000 bot

WORKDIR /app

# Зависимости отдельным слоем — при правке кода они не переустанавливаются
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY bot.py ./
COPY nspdbot ./nspdbot
COPY tests ./tests

RUN mkdir -p data logs && chown -R bot:bot /app
USER bot

# exec-форма: бот получает SIGTERM от «docker stop» и успевает напечатать ОСТАНОВКА
CMD ["python", "bot.py"]
