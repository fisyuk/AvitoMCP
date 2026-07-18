# Avito Monitor MCP

Удалённый MCP-сервер с одним инструментом `search_avito`. Он ищет объявления,
фильтрует цену и возвращает только объявления, которых не было в предыдущих
вызовах того же поиска. Рассчитан на ChatGPT Scheduled Tasks.

## Важное ограничение

У Avito нет публичного buyer API для такого поиска. Сервер читает публичную
HTML-страницу. Avito может изменить разметку или защитить выдачу на уровне
провайдера. Текущая реализация не гарантирует прохождение QRATOR/CAPTCHA, поэтому
выбранный хостинг нужно проверять реальным запросом.

Обычные `httpx` и Linux `curl` из Docker получали `HTTP 429` от QRATOR, хотя
браузер с того же внешнего IP открывал поиск. Поэтому запросы к Avito выполняются
через `curl_cffi` с браузерным TLS/JA3/HTTP2 fingerprint. Это повысило шанс
успешного ответа в тестах, но не устранило блокировку стабильно: Avito всё равно
может вернуть `429`. При `401/403/429` активный профиль делает не более трёх
повторов в той же сессии с backoff, jitter и поддержкой `Retry-After`, сохраняя
выданные QRATOR cookies. Если это не помогло, клиент последовательно создаёт
чистые сессии Chrome, Firefox, Brave-compatible Chromium, Safari и
Opera-compatible Chromium. Ошибка возвращается только после проверки всех пяти
профилей. Cookies существуют только в памяти процесса и на диск не записываются.

## Что хранится

В SQLite сохраняется только история поиска:

- запрос, ценовой предел и область поиска;
- время и счётчики запусков;
- непрозрачные ID уже встреченных объявлений.

Заголовки, цены, описания и HTML Avito не сохраняются. `ACCESS_CODE` и
`TOKEN_SECRET` существуют как deployment secrets/environment, но не попадают в
SQLite. OAuth-коды живут в памяти до пяти минут, а bearer-токены самодостаточны
и на сервере не сохраняются. По умолчанию bearer действует 365 дней.

## Почему не обычный API key

ChatGPT не передаёт произвольные API keys удалённому MCP. Поэтому подключение
использует стандартный OAuth 2.1 Authorization Code + PKCE: вы один раз вводите
общий `ACCESS_CODE`, после чего сервер выдаёт долгоживущий bearer. Это не система
пользователей; постоянных auth-данных в БД нет.

## Локальный запуск

```bash
cp .env.example .env
python -c 'import secrets; print(secrets.token_urlsafe(24))'  # ACCESS_CODE
python -c 'import secrets; print(secrets.token_urlsafe(48))'  # TOKEN_SECRET
python -m venv .venv
.venv/bin/pip install -e '.[dev]'
set -a; source .env; set +a
.venv/bin/python -m avito_mcp
```

## Docker Compose

Минимальный Compose запускает только MCP-сервис, публикует его порт `8000` и
хранит SQLite в именованном Docker volume. Reverse proxy и TLS в него не входят.

```bash
cp .env.example .env
# Замените ACCESS_CODE, TOKEN_SECRET и PUBLIC_BASE_URL в .env.
docker compose up -d --build
curl http://127.0.0.1:8000/healthz
```

Сервис доступен на host-порту `8000`; внутри контейнера он также слушает `8000`.
`PUBLIC_BASE_URL` должен содержать фактический внешний HTTPS origin без `/mcp`.
Если TLS и домен обслуживаются отдельным проектом, он должен проксировать трафик
на `http://127.0.0.1:8000`.

Если IP хостинга отклоняется, можно задать `AVITO_PROXY_URL` и вывести только
Avito-трафик через контролируемый вами HTTP proxy, например домашний egress.
Сторонний residential proxy становится отдельной стороной, видящей факт трафика,
и может нарушать правила площадки — не подключайте его автоматически.

Используйте один worker. OAuth handshake находится в памяти, а SQLite рассчитан
на один экземпляр приложения. Для этого персонального hourly monitor несколько
worker-ов не дают пользы.

## Подключение к ChatGPT

1. Откройте **Settings → Security and login** и включите **Developer mode**.
2. В **Settings → Plugins** нажмите `+` и создайте developer-mode app.
3. Укажите MCP URL: `https://ваш-домен/mcp`.
4. При первом подключении введите `ACCESS_CODE`.
5. Сначала выполните ручной вызов и убедитесь, что облачный IP не блокируется
   Avito. Затем создавайте Scheduled Task.

Пример задачи:

> Каждый час используй Avito Monitor и вызывай `search_avito` с запросом
> «книга невероятная история о гигантской груше», `max_price_rub=2000` и
> `only_new=true`. Сообщай только элементы из `items`. Если
> `should_notify=false`, не присылай объявлений.

Первый успешный вызов считает все текущие подходящие объявления новыми. После
этого сервер возвращает только ранее не встречавшиеся ID. Область по умолчанию
задаётся через `AVITO_DEFAULT_SEARCH_URL`; туда можно положить URL сохранённого
поиска, а `q`, `pmax` и сортировку сервер заменит параметрами инструмента.

## API и эксплуатация

- MCP: `/mcp`
- Health check: `/healthz`
- Privacy statement: `/privacy`
- OAuth metadata: `/.well-known/oauth-authorization-server`
- Protected resource metadata: `/.well-known/oauth-protected-resource`

Ротация `TOKEN_SECRET` немедленно инвалидирует все bearer-токены. Ротация только
`ACCESS_CODE` запрещает новые подключения, но уже выданные токены продолжат
работать до истечения срока.

## Проверка

```bash
.venv/bin/ruff check .
.venv/bin/pytest
```

Для полного smoke-теста контейнера используйте `scripts/smoke_remote.py`: он
проверяет health, OAuth, MCP initialize, tools/list и реальный вызов
`search_avito` через HTTP.
