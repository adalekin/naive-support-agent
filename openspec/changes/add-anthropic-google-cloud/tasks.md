## 1. Implementation

- [x] Явный MODEL_PROVIDER и проверка обязательных настроек.
- [x] AsyncAnthropicGoogleCloud в существующей точке сборки.
- [x] Google ADC dependency и read-only mount в Helm.
- [x] Тест сначала падает без поддержки Google Cloud, затем проходит.
- [x] Все 162 теста и ruff проходят.
- [x] Helm проверен для обоих подключений.

## 2. Delivery verification

- [x] Образ опубликован с отдельным неизменяемым тегом.
- [x] Чарт 0.1.7-google-cloud.1 опубликован.
- [x] Изолированный контейнер выполнил Tool Runner с выбранной моделью, beta-параметрами и тестовым инструментом.
- [x] Установка переключена на Google Cloud. Секреты и данные кейсов не выведены.
