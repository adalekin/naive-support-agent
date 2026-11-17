# Why

Установка должна вызывать Claude через выбранное подключение: прямой Anthropic API или Anthropic на Google Cloud. Владелец согласовал Google Cloud по образцу chats.

# What Changes

- Установка явно задаёт MODEL_PROVIDER.
- Google Cloud использует ADC, project и workspace без Anthropic API key и SOCKS.
- Цикл Tool Runner, модель, инструменты и бюджет остаются прежними.

# Impact

Настройки сервера, Helm-чарт и инструкции установки. Миграции базы не нужны.
