# Avito Monitor MCP

Удалённый MCP-сервер с одним инструментом `search_avito`. Он ищет объявления,
фильтрует цену и возвращает только объявления, которых не было в предыдущих
вызовах того же поиска. Рассчитан на ChatGPT Scheduled Tasks.

## Важное ограничение

У Avito нет публичного buyer API для такого поиска. Сервер читает публичную
HTML-страницу. Avito может изменить разметку или отклонить запрос с IP облачного
провайдера. Сервер намеренно не обходит CAPTCHA и блокировки. Поэтому сначала
проверьте выбранный хостинг реальным запросом; абсолютной надёжности здесь нет.

При разработке прямой запрос из текущего окружения уже получил `HTTP 429` с
сообщением Avito «Доступ ограничен: проблема с IP». Это не означает, что любой
хостинг будет заблокирован, но деплой без проверки конкретного egress — плохая
ставка.

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

## Docker image

Этот репозиторий собирает только образ MCP-сервиса:

```bash
docker build -t avito-mcp .
```

Production orchestration намеренно находится вне этого проекта. Внешний проект
должен передать переменные из `.env`, подключить постоянное хранилище к `/data`,
направить HTTPS-трафик на порт `8000` и запускать ровно один экземпляр сервиса.
`PUBLIC_BASE_URL` должен содержать внешний HTTPS origin без `/mcp`.

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

Для полного smoke-теста локального контейнера с `DEVELOPMENT_OAUTH=1` используйте
`scripts/smoke_remote.py`: он проверяет health, OAuth, MCP initialize, tools/list
и реальный вызов `search_avito` через HTTP.
