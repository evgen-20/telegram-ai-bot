# Telegram AI Agent

[English version](README.md)

Telegram AI Agent - open-source Telegram-бот для управления Claude Code, Codex
CLI и Antigravity CLI на вашем VPS. Он превращает Telegram в удаленный интерфейс
для вайбкодинга: создаете топик под проект, пишете задачи с телефона,
прикладываете файлы или голосовые, смотрите прогресс, возвращаетесь к старым
сессиям и управляете живой терминальной TUI, когда агенту нужен ввод.

В репозитории лежит только публичный переиспользуемый runtime бота. Здесь нет
приватных данных ассистента, приватных промптов, runtime state, реальных ID,
токенов или машинно-специфичного деплоя.

## Что Можно Делать

- Запускать Claude Code, Codex или Antigravity из Telegram private chats и group
  forum topics.
- Держать отдельный Telegram-топик под каждый проект, workflow или долгий
  контекст агента.
- Привязать топик к папке на VPS, например `/home/user/projects/my-app`.
- Выбирать Claude Code, Codex или Antigravity отдельно для каждого топика и
  менять модель Antigravity через `/model`.
- Использовать постоянную `tmux`-сессию для полноценной разработки или короткий
  subprocess для простых разовых задач.
- Отправлять текст, фото, документы, пачки forwarded messages, Telegram rich
  messages и, опционально, voice messages.
- Настраивать bundled example prompt под второй workflow.
- Открывать live TUI snapshot через `/tui` и нажимать кнопки Enter, Esc,
  стрелки, цифры, refresh и close.
- Возобновлять старые сессии через reply на сообщения бота или через `/resume`.
- Перезапускать зависший runtime топика через `/recycle` и смотреть состояние
  MCP runtime processes через `/mcpstatus`.
- Давать агенту отправлять сообщения, картинки, галереи картинок и документы
  обратно в Telegram через встроенный bot MCP server.
- Показывать финальные ответы с Markdown-таблицами как Telegram rich messages,
  с автоматическим fallback на обычный текст.

## Как Это Устроено

Бот запускается на той же машине, где стоят Claude Code, Codex CLI и/или
Antigravity CLI. Telegram - только интерфейс управления. Когда вы отправляете
сообщение, бот:

1. проверяет, что ваш Telegram user ID разрешен;
2. скачивает приложенные медиа, если нужно;
3. находит настройки текущего чата или forum topic;
4. отправляет prompt в Claude Code, Codex или Antigravity в нужной рабочей
   папке;
5. стримит прогресс и финальный ответ обратно в Telegram.

## Rich Final Answers

Бот умеет читать входящие Telegram rich messages, включая forwarded rich posts.
Текстовые блоки нормализуются в Markdown-like текст для агента, таблицы и
сноски остаются видимыми в этом тексте, а rich photo blocks передаются как
image attachments, если Telegram отдает по ним файлы. Rich media без доступных
файлов остается явными placeholders.

Промежуточные сообщения всегда отправляются обычными Telegram-сообщениями. Если
в финальном ответе агента есть Markdown-таблица, бот преобразует этот ответ в
Telegram rich message blocks и отправляет его через Telegram RichText/RichMessage
API. Финальные ответы без таблиц остаются обычным текстом. Если Telegram
отклоняет rich payload или установленная Telegram-библиотека не умеет его
отправлять, бот делает fallback на обычный текстовый ответ.

Публичная схема Telegram для этой функции начинается здесь:
https://core.telegram.org/type/RichText.

Forum topics изолированы по Telegram `chat_id` и `thread_id`. У каждого топика
может быть свой:

- `cwd`: папка проекта на VPS;
- `mode`: prompt mode;
- `engine`: `claude`, `codex` или `antigravity`;
- `exec_mode`: `tmux` или `subprocess`;
- `stream_mode`: `verbose`, `live` или `minimal`;
- `mcp_config`: опциональный MCP config для агента;
- `model`: опциональный override модели.

## Требования

Нужен Linux-сервер или VPS, где будут работать бот и agent CLIs.

- Python 3.12+
- `uv`
- Telegram bot token из `@BotFather`
- ваш числовой Telegram user ID из `@userinfobot`
- Claude Code CLI и/или Codex CLI, установленные под тем же Linux-user, который
  запускает бота; Antigravity CLI (`agy`) — опциональный третий engine
- `tmux` для постоянных dev-сессий
- опционально: Deepgram API key для voice transcription

Бот может работать, если установлен только один agent CLI. По умолчанию он
предпочитает Claude Code, но если Claude Code нет, а Codex установлен, топик
может работать через Codex. Antigravity включается только явно: он никогда не
выбирается как автоматический fallback, и топик работает через него, только
если его `engine` равен `antigravity`.

Проверьте server user перед продолжением:

```bash
python3 --version
uv --version
command -v tmux
command -v claude || true
command -v codex || true
command -v agy || ls -l ~/.local/bin/agy || true
```

Должен существовать хотя бы один из `claude` или `codex`. Также убедитесь, что
CLI залогинен или настроен под тем же Linux-user, который будет запускать бота.

### Опционально: Antigravity CLI

Antigravity CLI (`agy`) — терминальный coding agent от Google. Это один
Go-бинарник без зависимости от Node. Установите его под Linux-user бота:

```bash
curl -fsSL https://antigravity.google/cli/install.sh | bash
```

Установщик кладет бинарник в `~/.local/bin/agy`. Один раз залогиньтесь внутри
TUI: запустите `agy` в терминале или tmux, выберите Google OAuth, откройте
показанный URL и вставьте код обратно. После этого может понадобиться
подтвердить аккаунт в браузере. Затем проверьте headless-режим:

```bash
agy -p "Reply with exactly: ok" --output-format json --dangerously-skip-permissions
```

Бот ищет бинарник так: `TELEGRAM_AGY_BIN` (абсолютный путь, владелец — service
user, без права записи для group/world), затем `~/.local/bin/agy`, затем `PATH`.

Правила для учетных данных:

- `agy` хранит OAuth-токен в
  `~/.gemini/antigravity-cli/antigravity-oauth-token`. Бот его никогда не
  читает. Не коммитьте, не печатайте и не копируйте его на другую машину.
- Для каждого хоста — свой Google-аккаунт. Вход тем же аккаунтом на втором
  хосте или из другой сети уже приводил к тому, что рабочий хост на время
  начинал падать с ошибкой "User location is not supported".
- У `agy` нет headless-команды для просмотра лимитов; квоту смотрите командой
  `/usage` внутри TUI `agy` (например, через `/tui`). Бот ее не показывает.

## Вариант Настройки A: Через Агента

Если на VPS уже есть Claude Code или Codex, это самый простой путь.

```bash
git clone https://github.com/pavel-molyanov/telegram-ai-agent.git
cd telegram-ai-agent
uv sync
```

Откройте Claude Code или Codex в этом репозитории и попросите:

```text
Настрой этот Telegram bot через bot-setup skill.
```

В репозитории есть setup skills для обоих runtime:

- `.claude/skills/bot-setup/SKILL.md`
- `.codex/skills/bot-setup/SKILL.md`

Для настройки forum topics попросите:

```text
Создай и настрой Telegram forum topics через topic-setup skill.
```

Topic setup skills лежат здесь:

- `.claude/skills/topic-setup/SKILL.md`
- `.codex/skills/topic-setup/SKILL.md`

Агент должен спросить язык UI, default engine, execution mode и нужен ли
systemd service.

## Вариант Настройки B: Руками

Склонируйте репозиторий и установите зависимости:

```bash
git clone https://github.com/pavel-molyanov/telegram-ai-agent.git
cd telegram-ai-agent
uv sync
cp .env.example .env
cp topic_config.example.json topic_config.json
chmod 600 .env topic_config.json
```

Отредактируйте `.env`:

```env
TELEGRAM_BOT_TOKEN=replace-with-botfather-token
ALLOWED_USER_IDS=[123456789]
BOT_LANG=ru
DEEPGRAM_API_KEY=
PROJECT_ROOT=.
APP_ROOT=
AGENT_WORKSPACE_ROOT=
DEFAULT_CWD=.
FILE_CACHE_DIR=./data
TOPIC_CONFIG_PATH=./topic_config.json
TMUX_SESSIONS_DIR=./tmux_sessions
CC_MAX_TURNS=100
CC_INACTIVITY_KILL_SEC=1800
CODEX_AUTO_UPDATE_ENABLED=true
CODEX_UPDATE_TIMEOUT_SEC=180
CODEX_UPDATE_COOLDOWN_SEC=86400
```

Пояснения:

- `TELEGRAM_BOT_TOKEN`: создайте бота в `@BotFather`.
- `ALLOWED_USER_IDS`: JSON array Telegram user IDs, которым можно пользоваться
  ботом.
- `BOT_LANG`: `en` или `ru`. После смены языка перезапустите бота.
- `PROJECT_ROOT`: стандартный корень единственного checkout. Для обычной
  установки оставьте `APP_ROOT` и `AGENT_WORKSPACE_ROOT` пустыми.
- `APP_ROOT` / `AGENT_WORKSPACE_ROOT`: необязательная схема с раздельными
  каталогами. В первом лежат установленный код и MCP launchers, во втором —
  редактируемые проекты, topic config, session mappings, tmux state и файлы.
- `DEFAULT_CWD`: рабочая папка по умолчанию для ненастроенных топиков.
- `DEEPGRAM_API_KEY`: оставьте пустым, если не нужны voice messages.
- `TELEGRAM_AGY_BIN`: опциональный абсолютный путь к бинарнику Antigravity CLI.
  Если не задан, используется `~/.local/bin/agy` или `PATH`.
- `CODEX_AUTO_UPDATE_ENABLED`: включает автоматические и ручные обновления
  Codex. Timeout ограничивает любое обновление, cooldown — только автоматические.

Запустите в foreground:

```bash
uv run telegram-bot
```

Откройте Telegram и отправьте `/start`. Перед установкой systemd отправьте одно
обычное сообщение и проверьте, что Claude Code, Codex или Antigravity отвечает.
Так проще поймать missing CLI auth, неправильный `PATH` и плохие project paths,
пока логи видны прямо в терминале.

## Настройка Telegram-Бота И Группы

Private chat подходит для простого использования. Для реальной проектной работы
удобнее Telegram supergroup с forum topics.

1. В `@BotFather` создайте бота и скопируйте token в `.env`.
2. В `@BotFather` используйте `/setprivacy` и отключите privacy, если хотите,
   чтобы бот получал обычные non-command messages в group topics.
3. Создайте Telegram group или supergroup.
4. Включите forum topics в настройках группы.
5. Добавьте бота в группу.
6. Сделайте бота админом с правами читать сообщения, отправлять сообщения,
   управлять topics и отправлять media/documents.
7. Создайте topics вручную или используйте `topic-setup` skill из Claude
   Code/Codex.

Каждый topic - отдельное рабочее пространство агента. Можно сделать один topic
для product repo, другой для лендинга, третий для задач, четвертый для постов и
так далее. В публичной версии есть общий prompt `free` и заменяемый пример
`task`. В своей установке можно добавить любые prompt modes под свои workflows.

Когда появляется новый forum topic, запущенный бот регистрирует его в
`topic_config.json` с настройками по умолчанию. Topic сразу принимает сообщения,
но работает на defaults, пока вы его не настроите.

Публичный `topic_config.json` хранит topics по Telegram `message_thread_id`,
поэтому самый простой и рекомендуемый setup - одна forum group на один bot
config. Чтобы узнать topic ID, запустите бота, создайте или переименуйте topic,
отправьте туда сообщение, затем откройте сгенерированный `topic_config.json` и
отредактируйте новую запись.

## Topic Configuration

Можно редактировать `topic_config.json` напрямую, использовать `/engine`,
`/mode`, `/stream` и `/model` (в Antigravity-топиках) внутри Telegram forum
topics или попросить `topic-setup` skill настроить topics.

Термины важны: поле config `mode` означает prompt mode. Telegram-команда
`/mode` меняет execution mode, который хранится как `exec_mode`.

Пример:

```json
{
  "topics": {
    "42": {
      "name": "My App",
      "type": "project",
      "mode": "free",
      "cwd": "/home/user/projects/my-app",
      "mcp_config": null,
      "stream_mode": "live",
      "exec_mode": "tmux",
      "engine": "codex"
    }
  }
}
```

Поля:

- `name`: понятное название.
- `type`: `assistant` или `project`.
- `mode`: prompt mode. Публичные modes - `free` и `task`.
- `cwd`: absolute path к проекту или `null`, чтобы использовать `DEFAULT_CWD`.
- `mcp_config`: absolute path к MCP config или `null` для bot-generated MCP
  config.
- `stream_mode`: `verbose`, `live` или `minimal`.
- `exec_mode`: `tmux` или `subprocess`.
- `engine`: `claude`, `codex` или `antigravity`. Antigravity работает в обоих
  execution modes: `tmux` и `subprocess`.
- `model`: legacy single override модели или `null`.
- `models`: опциональные per-engine overrides с ключами `claude`, `codex` и/или
  `antigravity`, например `{"antigravity": "gemini-3.1-pro-high"}`. Порядок
  выбора: `models[active_engine]`, затем `model`, затем default провайдера;
  ручной `/engine` сохраняет всю map. При automatic missing-CLI fallback первый
  запрос использует default нового провайдера, а сохранённый per-engine override
  применяется со следующего запроса. В Antigravity-топиках `/model` сам
  записывает `models.antigravity`.

Для `cwd` и `mcp_config` используйте absolute paths. По умолчанию оставляйте
`mcp_config` равным `null`. Project MCP config подключайте только после проверки
на secrets и private dependencies; дополнительные servers имеют собственные
provider/tool-policy ограничения. Не коммитьте настоящий `topic_config.json`.

## Prompt Modes

Prompt files лежат в `src/telegram_bot/prompts/`.

Публичные modes:

- `free`: дефолтный общий/project prompt, файл `default.md`.
- `task`: маленький заменяемый пример task-management prompt, файл
  `task-manager.md`.

Для no-code customization отредактируйте или замените `task-manager.md` и
сохраните `"mode": "task"` в нужных topics. Новый mode name требует изменений
runtime resolver, явной tool policy и тестов; одного prompt file недостаточно.
Не кладите в public repo приватные данные, секреты, личные workflows и реальный
customer context.

## Execution Modes

`/mode` выбирает, как запускается agent process.

### tmux

`tmux` нужен для полноценной разработки.

Бот поднимает постоянную terminal session и отправляет ваши Telegram-сообщения
напрямую в TUI Claude Code, Codex или Antigravity. Сессия хранит контекст,
может переживать рестарты бота, и ее можно смотреть через `/tui`.

Используйте это, когда:

- вы редактируете codebase;
- агенту нужен длинный контекст;
- агент может показывать permission questions или interactive menus;
- нужны `/resume` и reply-to-session behavior.

В Antigravity при первом запуске в новой рабочей папке `agy` показывает диалог
"trust this folder"; бот подтверждает его автоматически, потому что `cwd`
выбрал оператор. У `agy` нет флага для system prompt, поэтому prompt mode и
Telegram-контекст отправляются вместе с первым сообщением нового разговора.

`tmux` потребляет ресурсы, пока сессия жива. Если runtime топика застрял, но
resumable context надо сохранить, используйте `/recycle`; когда сессия больше
не нужна, используйте `/kill`.

### subprocess

`subprocess` нужен для коротких задач.

Каждое сообщение запускает свежий CLI process, получает ответ и завершает его.
Это удобно для простых вопросов, заметок, маленьких преобразований и задач, где
не хочется держать постоянную TUI-сессию в фоне.

Private chats используют настройки по умолчанию и подходят для простого
использования. Per-topic controls вроде `/mode`, `/stream`, `/engine` и
`/resume` работают в forum topics, потому что им нужна topic-specific config
entry.

## Stream Modes

`/stream` управляет тем, сколько прогресса бот отправляет в Telegram.

- `verbose`: каждый tool/status event приходит отдельным тихим сообщением.
- `live`: tool/status events собираются в один редактируемый progress message.
- `minimal`: tool/status events полностью скрываются.

Human-readable промежуточные обновления остаются отдельными сообщениями во всех
режимах. Нормализованный финальный ответ всегда приходит как один отдельный
логический ответ. `live` — лучший default для большинства проектных задач.

## TUI Mode

`/tui` открывает snapshot живой tmux pane и добавляет кнопки управления.

Это нужно, потому что Claude Code, Codex и Antigravity CLI - терминальные
приложения. Они могут показывать permission prompts, menus, confirmations, model
pickers и другой interactive UI. Бот умеет писать в терминал и читать
transcript, но иногда нужно увидеть и порулить TUI напрямую.

Используйте `/tui`, чтобы:

- посмотреть, что сейчас делает агент;
- нажать Enter, Esc, arrows, Tab, Backspace, Ctrl+C или цифры;
- обработать permission dialogs;
- закрыть startup modal (например update prompt у Codex или trust dialog) —
  если агент показал модал во время старта и бот не смог пробить ввод, в чате
  появится "engine started but input is blocked, use /tui"; открой `/tui` и
  закрой диалог кнопками;
- выйти из состояния, которое выглядит как зависшая TUI.

`/tail` - legacy alias для `/tui`.

## Reply, Resume И Sessions

Бот запоминает, какая agent session породила каждый ответ. Если вы отвечаете
reply на старое сообщение бота, он может направить новое сообщение обратно в
соответствующую сессию. Это удобно, когда в одном Telegram topic есть несколько
исторических сессий.

В tmux mode команда `/resume` показывает сохраненные sessions для рабочей папки
топика и позволяет переключиться обратно в одну из них. Если target session
относится к другому engine или execution mode, бот может переключить настройки
топика перед resume. Если live tmux session нужно заменить, она может быть
остановлена в рамках такого switch.

Slash commands устроены отдельно: в tmux topics non-bot commands вроде `/model`
или `/compact` отправляются в live TUI, а не в historical session из reply.
Исключение — `/model` в Antigravity-топиках: на него бот отвечает собственным
выбором модели.

`/clear` начинает свежий logical context для текущего topic. В tmux mode бот
сбрасывает или пересоздает tmux session в зависимости от текущего состояния.
`/new` все еще существует как legacy alias, но в меню показывается `/clear`.

## Команды

- `/start`: проверить, что бот отвечает, и показать базовую клавиатуру.
- `/clear`: сбросить сессию текущего topic.
- `/cancel`: отменить текущую обработку.
- `/language`: показать или сменить язык UI, например `/language ru`.
- `/mode`: только forum topics; выбрать `tmux` или `subprocess`. При
  переключении с `tmux` на `subprocess` активная tmux session останавливается.
- `/engine`: только forum topics; выбрать Claude Code, Codex или Antigravity.
  Смена engine сбрасывает активную session.
- `/model`: только forum topics; в Antigravity-топиках выбрать модель из
  `agy models` (список зависит от подписки залогиненного аккаунта) или
  "Default". Выбор сохраняется в map `models` топика, а разговор продолжается
  на новой модели. В топиках Claude Code и Codex команда объясняет, как задать
  `models` в `topic_config.json`.
- `/codex_update`: обновить Codex CLI вручную. Команда обходит automatic
  cooldown, но блокируется другим bot-managed обновлением в этом процессе или
  bot-managed активными Codex sessions. `/codex_update status` показывает
  последний redacted result.
- `/stream`: только forum topics; выбрать `verbose`, `live` или `minimal`.
- `/resume`: только forum topics; возобновить сохраненную tmux session для cwd
  текущего topic.
- `/tui`: показать и управлять живой tmux TUI.
- `/tail`: legacy alias для `/tui`.
- `/kill`: остановить активную tmux session и освободить ресурсы.
- `/recycle`: перезапустить активный tmux runtime и подчистить MCP processes
  текущего topic без намеренного сброса resumable context.
- `/mcpstatus`: показать redacted diagnostics MCP-процессов текущего topic.

Рекомендации:

- Настоящая разработка: `/mode` -> `tmux`, `/stream` -> `live`.
- Короткие разовые задачи: `/mode` -> `subprocess`, `/stream` -> `minimal` или
  `live`.
- Runtime выглядит зависшим, но session надо сохранить: `/recycle`.
- Старая session больше не нужна: `/kill`.

## MCP Bot Server

Встроенный MCP server позволяет агенту отправлять контент обратно в текущий
Telegram topic. Сессии, запущенные ботом, автоматически получают topic-scoped
MCP config.

Публичные prompt modes разрешают generic bot tools:

- `send_message`
- `send_image`
- `send_image_gallery`
- `send_document`

Tools для сообщений, картинок, галерей и документов принимают опциональный
Telegram `parse_mode`: `HTML` или `MarkdownV2`. Если Telegram отвергает
formatting, server повторяет отправку без `parse_mode` там, где это безопасно.

Публичные prompt modes также разрешают Context7 documentation tools, чтобы
агенты могли получать актуальную документацию библиотек/API из настроенных MCP
profiles.

Не коммитьте реальные `.mcp.json`: там могут быть токены или локальные пути.

Если `mcp_config` равен `null`, бот генерирует runtime MCP config с Telegram bot
server и routing текущего topic. Если `mcp_config` указывает на существующий
файл, бот использует его как base config и добавляет Telegram bot server в
runtime copy. Если path не существует, используйте `null` или исправьте path,
прежде чем полагаться на project-specific MCP tools.

У Antigravity нет флага для MCP на один запуск. При старте, если `agy`
установлен, бот регистрирует свой MCP server `bot` в глобальном MCP config
`agy` (существующую запись `bot` он не трогает). Routing в нужный topic каждый
процесс `agy` получает из своего environment, секреты в config `agy` не
пишутся. Картинки, созданные встроенным инструментом `agy` `generate_image`,
автоматически отправляются в чат.

## Автозапуск Через Systemd

На VPS лучше запускать бота как systemd service, чтобы он стартовал после
reboot и поднимался обратно после падений.

```bash
APP_DIR="$(pwd)"
UV_BIN="$(command -v uv)"
USER_NAME="$(whoami)"
tmp_unit="$(mktemp)"
sed \
  -e "s#REPLACE_WITH_LINUX_USER#${USER_NAME}#g" \
  -e "s#REPLACE_WITH_ABSOLUTE_REPO_PATH#${APP_DIR}#g" \
  -e "s#REPLACE_WITH_UV_PATH#${UV_BIN}#g" \
  docs/systemd/telegram-bot.service.template >"${tmp_unit}"
sudo install -m 0644 "${tmp_unit}" /etc/systemd/system/telegram-bot.service
rm -f "${tmp_unit}"

sudo systemctl daemon-reload
sudo systemctl enable telegram-bot
sudo systemctl start telegram-bot
sudo systemctl status telegram-bot
```

Логи:

```bash
journalctl -u telegram-bot -f
```

У systemd может быть более короткий `PATH`, чем в интерактивной shell. Перед
боевым запуском проверьте, что service user видит `uv`, `tmux` и хотя бы один из
`claude` или `codex` (плюс `agy`, если используете Antigravity-топики). Если
CLIs лежат в user-local directory, добавьте в unit строку `Environment=PATH=...`
или используйте absolute paths.

Если systemd перестал пробовать после частых падений:

```bash
sudo systemctl reset-failed telegram-bot
sudo systemctl start telegram-bot
```

## Runtime Files

Не коммитьте runtime files:

- `.env`
- `topic_config.json`
- `session_mapping.json`
- `channel_sessions.json`
- `tmux_sessions/`
- `data/`
- `.mcp*.json`
- `.venv/`, `.ruff_cache/`, `.mypy_cache/`, `.pytest_cache/`
- `__pycache__/`, `*.pyc`, `*.pyo`

Коммитьте только public-safe examples и docs: `.env.example`,
`topic_config.example.json`, README files.

## Security Model

Бот рассчитан на trusted personal или small-team use. `ALLOWED_USER_IDS` -
главный access control. Любой, кто может писать боту, может попросить
настроенный local agent CLI работать в заданной рабочей папке, включая file
edits и shell/tool actions, разрешенные этим CLI.

Не открывайте бота untrusted users. Не настраивайте project topics на папки,
которые агенту нельзя читать или менять. Лучше запускать бота под отдельным
low-privilege Linux user. Если bot token утек, перевыпустите его в
`@BotFather`.

Значения из `.env` читаются как непрозрачные строки, поэтому фрагменты вроде
`${NAME}` внутри токена не разворачиваются. Agent subprocesses запускаются с
ограниченным окружением и не наследуют bot tokens и посторонние credentials.
По умолчанию бот также использует отдельный workspace-local tmux server и при
обновлении переносит со старого сервера только собственные сохранённые сессии.

## Development

```bash
uv sync
uv run ruff check .
uv run ruff format --check .
uv run mypy src/ mcp-servers/bot/server.py
uv run pytest
PYTHONDONTWRITEBYTECODE=1 uv run python -c "import telegram_bot; import telegram_bot.__main__; print('ok')"
```

## Feedback

Issues, bug reports и идеи welcome. Откройте GitHub issue, если что-то
непонятно, сломано или не хватает важной возможности.

Бота сделал Паша Молянов. Я пишу про бизнес, AI-ассистентов, разработку и
запуск полезных сервисов в Telegram-канале:
[@molyanov_blog](https://t.me/+zJ5qmSsoYediYzdi). Мой сайт:
[molyanov.ru](https://molyanov.ru).

## License

MIT
