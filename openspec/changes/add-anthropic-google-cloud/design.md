# Context

SDK предоставляет AsyncAnthropicGoogleCloud с ADC и workspace. Проверка 2026-11-17: beta Messages с моделью claude-opus-5-5, server-side-fallback-2026-07-01 и compact-2026-09-04 вернул ответ. Изолированная проверка опубликованного образа также выполнила полный цикл Tool Runner с тестовым инструментом.

# Decisions

Выбор SDK выполняется в существующей точке сборки Assistant. Для Google Cloud используется AsyncAnthropicGoogleCloud с ADC и адресом claude.googleapis.com/v1/projects/{project}/locations/global/workspaces/{workspace}/invoke. Для прямого Anthropic сохраняется AsyncAnthropic. MODEL_PROVIDER обязателен; неизвестное подключение и неполная конфигурация запрещают запуск. Автоматического переключения подключений нет.

Секрет ADC монтируется только в контейнер ассистента. Telegram сохраняет отдельный прокси. Существующие кейсы и цикл Tool Runner не меняются.

# Non-Goals

Изменение инструментов, промптов, прав доступа и схемы базы.
