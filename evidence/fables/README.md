# История выпуска FABLES (Robinhood Chain)

Контракт: `0xb63d07CEf74bE747231d601bb2FAa6B252c68106` · decimals 18 · экспорт 2026-11-05T02:26:51Z

- Событий: **20587** (mint/burn), блоки 63731771 → 106387177
- Σ mint − Σ burn = `146827769516183445362352418` мин. единиц
- totalSupply() = `146827769516183445362352418` (на блоке 106387176)
- Совпадение: **точное** (разница `0`)
- SHA-256 supply_events.csv: `7c62c57e14b8aecbbb1f167e6728f94fc5bc23df72cce6ecbab387d3ca707b10`

Проверка без программы: сложите столбец `amount_raw` для kind=mint и вычтите kind=burn (целыми числами, не float) — результат должен совпасть с totalSupply из summary.json. Любую транзакцию можно открыть по ссылке explorer_tx.
Скрипт проверки: `python scripts/verify_supply_export.py <папка>`.
