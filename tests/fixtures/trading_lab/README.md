# Записанные реальные данные FABLES/USDG (Uniswap v4, Robinhood Chain)

Получены 2026-11-05 ~13:27 UTC одним запросом на файл (не синтетика, не дополнялись):
- `gt_ohlcv_h1.json` — GeckoTerminal `/pools/0xcce8…869e/ohlcv/hour?aggregate=1&limit=1000&currency=usd&token=FABLES`
- `gt_ohlcv_h4.json` — то же, `hour?aggregate=4`
- `gt_ohlcv_d1.json` — то же, `day?aggregate=1`
- `ds_pair.json` — DexScreener `/latest/dex/pairs/robinhood/0xcce8…869e`

Последняя свеча каждого ряда на момент записи НЕ закрыта — код обязан её отбросить.
Используются только как тестовые данные для детерминированных проверок; результаты на них не являются forward-тестом.
