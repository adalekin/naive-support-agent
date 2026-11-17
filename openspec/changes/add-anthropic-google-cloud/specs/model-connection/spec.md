## ADDED Requirements

### Requirement: Explicit model connection
Установка SHALL явно выбирать anthropic или anthropic_google_cloud через MODEL_PROVIDER.

#### Scenario: Google Cloud connection
- **WHEN** установка выбирает anthropic_google_cloud
- **THEN** ассистент использует ADC, заданные project и workspace и не требует Anthropic API key
- **AND** обращается к Google Cloud напрямую

#### Scenario: Incomplete connection
- **WHEN** отсутствует обязательная настройка выбранного подключения
- **THEN** запуск запрещён с именем настройки без её значения

#### Scenario: Direct Anthropic connection
- **WHEN** установка выбирает anthropic
- **THEN** ассистент использует Anthropic API key и заданный режим доступа direct или proxy
