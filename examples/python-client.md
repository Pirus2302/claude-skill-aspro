# Эталонный Python-клиент Aspro

Боевой паттерн HTTP-клиента к Aspro API: rate-limit, exponential backoff
retry на 429/5xx, пагинация по 100 без доверия к `total`.

Это **референс**, не библиотека. Скопируй в свой проект, адаптируй под
свои нужды (логирование, метрики, фреймворк).

## Зависимости

- Python 3.10+
- `requests` (или `httpx` — паттерн тот же)

## Конфигурация через env

```
ASPRO_COMPANY=mycompany          # поддомен в Aspro Cloud
ASPRO_API_KEY=...                # из настроек Aspro → API
ASPRO_MIN_INTERVAL=1.5           # пауза между запросами (сек)
ASPRO_RETRY_MAX_ATTEMPTS=4
ASPRO_RETRY_BASE_DELAY=2.0       # удваивается каждую попытку
ASPRO_RETRY_MAX_DELAY=60.0       # cap на одну паузу
ASPRO_REQUEST_TIMEOUT=30         # сек
```

## Клиент целиком

```python
"""HTTP-клиент Aspro API с retry на 429 и сетевые ошибки.

Архитектура:
- _rate_limit: пауза между запросами (per-process). Интервал — env
  ASPRO_MIN_INTERVAL (default 1.5с) — чем больше, тем реже ловим 429.
  ВАЖНО: при нескольких воркерах (gunicorn workers>1) это per-worker,
  общий поток будет быстрее → Aspro может всё равно прислать 429.
  Поэтому retry ниже.
- aspro_get: один запрос с retry на 429/5xx/таймауты, exponential backoff.
- aspro_get_all: пагинация — несколько aspro_get подряд, по 100 на страницу.
"""
import logging
import os
import time
import requests

logger = logging.getLogger(__name__)

LAST_CALL = [0.0]

RETRY_MAX_ATTEMPTS = int(os.getenv('ASPRO_RETRY_MAX_ATTEMPTS', '4'))
RETRY_BASE_DELAY = float(os.getenv('ASPRO_RETRY_BASE_DELAY', '2.0'))
RETRY_MAX_DELAY = float(os.getenv('ASPRO_RETRY_MAX_DELAY', '60.0'))
REQUEST_TIMEOUT = int(os.getenv('ASPRO_REQUEST_TIMEOUT', '30'))
MIN_REQUEST_INTERVAL = float(os.getenv('ASPRO_MIN_INTERVAL', '1.5'))


def _rate_limit():
    """Минимальная пауза между запросами в этом процессе."""
    elapsed = time.time() - LAST_CALL[0]
    if elapsed < MIN_REQUEST_INTERVAL:
        time.sleep(MIN_REQUEST_INTERVAL - elapsed)
    LAST_CALL[0] = time.time()


def _retry_delay(attempt: int, server_hint: float | None = None) -> float:
    """attempt=0 → BASE, attempt=1 → BASE*2, ... cap MAX_DELAY.
    server_hint — значение из Retry-After, если сервер прислал. Берём max.
    """
    backoff = min(RETRY_BASE_DELAY * (2 ** attempt), RETRY_MAX_DELAY)
    if server_hint is not None:
        return max(backoff, min(server_hint, RETRY_MAX_DELAY))
    return backoff


def _parse_retry_after(value: str | None) -> float | None:
    """Поддерживаем только числовой Retry-After (типовой для rate limit API)."""
    if not value:
        return None
    try:
        return float(value.strip())
    except (ValueError, AttributeError):
        return None


def aspro_get(endpoint: str, params: dict | None = None) -> dict:
    """GET к Aspro API с retry.

    Повторяем при:
    - HTTP 429 (Too Many Requests) — обязательно
    - HTTP 5xx (server errors) — обычно временные
    - Timeout, ConnectionError — сеть тупит

    НЕ повторяем при:
    - HTTP 4xx кроме 429 (401, 403, 404, 422) — это наша ошибка
    - JSONDecodeError — сервер прислал мусор
    """
    base = f"https://{os.getenv('ASPRO_COMPANY')}.aspro.cloud/api/v1/module/"
    url = base + endpoint
    p = {'api_key': os.getenv('ASPRO_API_KEY'), **(params or {})}

    last_exception = None
    for attempt in range(RETRY_MAX_ATTEMPTS):
        _rate_limit()
        try:
            r = requests.get(url, params=p, timeout=REQUEST_TIMEOUT)
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
            last_exception = e
            if attempt < RETRY_MAX_ATTEMPTS - 1:
                delay = _retry_delay(attempt)
                logger.warning(
                    "Aspro %s: %s (attempt %d/%d), retry in %.1fs",
                    endpoint, type(e).__name__, attempt + 1,
                    RETRY_MAX_ATTEMPTS, delay
                )
                time.sleep(delay)
                continue
            raise

        if r.status_code == 429 or 500 <= r.status_code < 600:
            last_exception = requests.exceptions.HTTPError(
                f"{r.status_code} from Aspro {endpoint}", response=r
            )
            if attempt < RETRY_MAX_ATTEMPTS - 1:
                server_hint = _parse_retry_after(r.headers.get('Retry-After'))
                delay = _retry_delay(attempt, server_hint)
                logger.warning(
                    "Aspro %s: HTTP %d (attempt %d/%d), retry in %.1fs%s",
                    endpoint, r.status_code, attempt + 1,
                    RETRY_MAX_ATTEMPTS, delay,
                    f" (Retry-After={server_hint})" if server_hint else ""
                )
                time.sleep(delay)
                continue
            r.raise_for_status()

        r.raise_for_status()
        return r.json().get('response', {})

    if last_exception:
        raise last_exception
    raise RuntimeError(f"Aspro {endpoint}: exhausted retries without exception")


def aspro_get_all(endpoint: str, params: dict | None = None) -> list:
    """Пагинация по 100 записей до конца.

    Стоп-условие — страница вернула МЕНЬШЕ PAGE_LIMIT записей (она последняя).
    НЕ полагаемся на поле `total`: Aspro его присылает не всегда, а
    отсутствующее `total` (=0) обрывало бы цикл уже после первой страницы.

    Защита: max 1000 страниц (100k записей) — если Aspro отдаёт больше,
    что-то не так, пагинация зациклилась.
    """
    all_items = []
    page = 1
    PAGE_LIMIT = 100
    MAX_PAGES = 1000
    while page <= MAX_PAGES:
        p = {**(params or {}), 'limit': PAGE_LIMIT, 'page': page}
        resp = aspro_get(endpoint, p)
        items = resp.get('items', [])
        all_items.extend(items)
        if len(items) < PAGE_LIMIT:
            break
        # Если Aspro корректно прислал total и мы его достигли — экономим
        # один пустой запрос на ровно кратных случаях.
        total = resp.get('total')
        if isinstance(total, int) and total > 0 and len(all_items) >= total:
            break
        page += 1
    if page > MAX_PAGES:
        logger.warning("Aspro %s: hit MAX_PAGES=%d, got %d items",
                       endpoint, MAX_PAGES, len(all_items))
    return all_items
```

## POST: form-urlencoded (НЕ JSON!)

Это **самая частая ловушка**. Aspro не парсит `application/json` —
шли только form-urlencoded.

```python
def aspro_post(endpoint: str, body: dict) -> dict:
    """POST к Aspro API с retry. Тело сериализуем как form-urlencoded.

    Правила сериализации:
    - bool → '1' / '0'
    - None → отбрасываем
    - всё остальное → str()
    """
    base = f"https://{os.getenv('ASPRO_COMPANY')}.aspro.cloud/api/v1/module/"
    url = base + endpoint
    params = {'api_key': os.getenv('ASPRO_API_KEY')}

    data = {}
    for k, v in (body or {}).items():
        if v is None:
            continue
        if isinstance(v, bool):
            data[k] = '1' if v else '0'
        else:
            data[k] = str(v)

    last_exception = None
    for attempt in range(RETRY_MAX_ATTEMPTS):
        _rate_limit()
        try:
            r = requests.post(url, params=params, data=data,
                              timeout=REQUEST_TIMEOUT)
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
            last_exception = e
            if attempt < RETRY_MAX_ATTEMPTS - 1:
                time.sleep(_retry_delay(attempt))
                continue
            raise

        if r.status_code == 429 or 500 <= r.status_code < 600:
            if attempt < RETRY_MAX_ATTEMPTS - 1:
                server_hint = _parse_retry_after(r.headers.get('Retry-After'))
                time.sleep(_retry_delay(attempt, server_hint))
                continue
            r.raise_for_status()

        r.raise_for_status()
        return r.json().get('response', {})
```

## Кастомные поля в двух форматах сразу

```python
def attach_custom_fields(body: dict, custom: dict) -> dict:
    """Добавить кастомные поля в тело запроса.

    Aspro в разных модулях игнорирует то cf_, то cf. — безопасно слать оба.
    custom: {field_id: value}
    """
    for field_id, value in (custom or {}).items():
        body[f'cf_{field_id}'] = value
        body[f'cf.{field_id}'] = value
    return body
```

## Маппинг валют (НЕ ISO 4217)

```python
# Один раз при старте приложения — загрузить маппинг из Aspro и кэшировать.
# Aspro использует свои внутренние ID валют (например BYN=10 в одной из
# инсталляций), а НЕ стандарт ISO 4217 (BYN=933).

CURRENCY_TO_CODE: dict[int, str] = {}  # aspro_currency_id → 'USD'/'EUR'/...
CODE_TO_CURRENCY: dict[str, int] = {}  # обратный

def init_currency_mapping():
    """Грузить ОДИН РАЗ при старте приложения. Пустая карта → платежи
    с currency_id будут падать."""
    items = aspro_get_all('fin/currency/list')  # точный путь — свериться с openapiru.json
    for it in items:
        code = (it.get('code') or '').strip().upper()
        if code:
            CURRENCY_TO_CODE[it['id']] = code
            CODE_TO_CURRENCY[code] = it['id']
    if not CURRENCY_TO_CODE:
        raise RuntimeError("Currency mapping empty — Aspro currency/list returned nothing")
```

## Что не вошло

- Логирование/метрики — добавь свои.
- Идемпотентность POST — на стороне Aspro её нет, делай дедуп по
  `aspro_id` на ответ.
- Async-версия — паттерн ровно тот же на `httpx.AsyncClient`.
