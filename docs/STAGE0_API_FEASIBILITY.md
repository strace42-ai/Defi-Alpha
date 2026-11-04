# Этап 0. Проверка реализуемости — фактические ответы API

Дата проверки: **2026-11-04** (UTC). Все выводы ниже — по реальным запросам из рабочей среды разработки,
сохранённые ответы лежат в `tests/fixtures/real_2026-11-04/`.

## Итог

**Систему можно построить без платных подписок.** Все обязательные функции первой версии работают на бесплатных
API без ключей. Платные/ключевые сервисы нужны только для расширений (см. конец документа).

## Таблица возможностей и ограничений

| Источник | Метод | Результат запроса | Ограничения (измерено) |
|---|---|---|---|
| GeckoTerminal | `GET /api/v2/networks` | 200, 224 сети в 3 страницах; есть `robinhood`, `base`, `eth`, `arbitrum`, `bsc`, `optimism`, `solana`, `hyperevm`, `megaeth` | — |
| GeckoTerminal | `GET /networks/{net}/new_pools?page=N&include=base_token,quote_token,dex` | 200, **20 пулов на страницу** | 10 страниц максимум. Бесплатный лимит: подряд ~5 запросов → **429**; при равномерных запросах раз в 6.5 с (≈9/мин) — **ни одного 429 на 15 запросах** |
| GeckoTerminal | скорость появления новых пулов (страница 1) | Robinhood: 20 пулов за ~6–10 мин; Base: ~13–16 мин; BSC: ~6–10 мин; ETH: ~42 мин; Arbitrum: ~36 ч; Optimism: 2 мин окно (пакет); HyperEVM: 19 пулов за 2 дня; **Solana: 20 пулов за 40 секунд**; MegaETH: **0** пулов | Solana на бесплатном лимите покрывается лишь частично (минуты из каждого цикла) |
| GeckoTerminal | `GET /networks/{net}/tokens/{addr}` | 200: `total_supply`, `price_usd`, `fdv_usd`, `market_cap_usd` (FABLES: 2 131 798 — circulating по CoinGecko), `coingecko_coin_id` | `fdv_usd` = цена × **totalSupply**, а не × max supply |
| GeckoTerminal | `GET /networks/{net}/tokens/multi/{a,b,…}` | до 30 адресов за запрос | — |
| GeckoTerminal | `GET /tokens/{addr}/info` | 200: сайты, соцсети, описание, `holders` (FABLES: 4 794 держателя, топ-10 = 79.6%), `gt_score` | топ-10 включает контракты локов/пулов |
| GeckoTerminal | `GET /tokens/{addr}/pools` | 200: 20 пулов BOW, в т.ч. пулы, где BOW — **quote** (MASKS/BOW) и аномальный пул BOW/LIME с «ликвидностью» $7.09 млн | список ограничен страницей; нужны фильтры качества |
| GeckoTerminal | `GET /pools/{pool}/ohlcv/day?limit=1000&token={addr}` | 200: BOW — 89 дневных свечей с 2026-08-08; FABLES (4h) — с 2026-10-20 | бесплатно покрыта вся история этих молодых пулов |
| DEX Screener | `GET /token-pairs/v1/{chain}/{token}` | 200: FABLES — 7 пар, BOW — 12 пар | Пары, где токен — **quote**: `priceUsd` относится к ДРУГОМУ токену (BOW: 0.01925, 100.62 — цены чужих токенов). `marketCap == fdv` у BOW (circulating неизвестен) |
| DEX Screener | `GET /tokens/v1/{chain}/{a,b}` | 200, до 30 адресов, одна пара на токен; `info.websites/socials` | лимит 300/мин (используем 240) |
| DEX Screener | `GET /latest/dex/search?q=BOW` | 30 пар: **21 разных токенов с тикером BOW** (Solana «Save Bow», XRPL, Worldchain, Robinhood Longbow) | Подтверждает необходимость идентификации по chain+address |
| DEX Screener | `token-profiles/latest`, `token-boosts` | промотируемые токены (60/мин) | **Не репрезентативны** — в приложении выключены по умолчанию и помечены «ПРОМО» |
| DefiLlama | `GET /protocols` | 200, 8 927 протоколов (9.5 МБ). Поле `address`: Fables = `robinhood:0xb63d…`, Longbow = `robinhood:0x451b…` | `raises: []` — не доказательство отсутствия VC |
| DefiLlama | `GET /protocol/{slug}` | Fables: TVL $23.1 млн, история с 2026-08-26 (протокол старше токена!), `forkedFromIds`, `tags: ve(3,3)`. Longbow: TVL $3.05 млн, borrowed $1.10 млн | — |
| DefiLlama | `GET /summary/fees/{slug}?dataType=dailyFees/dailyRevenue/dailyHoldersRevenue` | Fables: fees 30д $2.61 млн; revenue 30д $342 тыс.; holders revenue 30д $358 тыс.; подробная методология | Longbow: **HTTP 400 «Fees for longbow not found»** — нет адаптера (= «нет данных», не 0) |
| DefiLlama | `/v2/chains`, `/v2/historicalChainTvl/{chain}`, `/overview/dexs/{chain}`, `/overview/fees/{chain}`, `stablecoins.llama.fi/stablecoincharts/{chain}` | Robinhood Chain: TVL $1.08 млрд, DEX-объём 30д $23.0 млрд, стейблкоины $1.04 млрд | Запись `Optimism` в `/v2/chains` показывает TVL 0, актуальное имя — `OP Mainnet` |
| Robinhood RPC (официальный) | `eth_chainId`, `eth_blockNumber`, `eth_call totalSupply()` | 4663, блок ~105.98 млн, totalSupply FABLES = 146 165 051.7 | Блок ≈ 0.1 с |
| Robinhood RPC | `eth_getLogs` Transfer from 0x0 | 1 млн блоков → 887 mint-событий за 0.2 с; **до 10 млн блоков** за запрос | частые запросы → JSON-RPC 429; **нет архивного состояния** (`historical state … is not available`) |
| publicnode / drpc (Robinhood) | `eth_getLogs` | publicnode: «Archive requests require a personal token»; drpc: «ranges over 10000 blocks are not supported on free plan» | использовать официальный RPC |
| Blockscout (Robinhood) | `/api/v2/tokens/{addr}` | **403 Cloudflare «Just a moment»** | Обход защиты не выполняется → верификация кода = UNKNOWN |
| Solana RPC (mainnet-beta) | `getTokenSupply`, `getAccountInfo(jsonParsed)` | 200: supply, `mintAuthority`, `freezeAuthority` | история mint требует индексатора |
| Официальный эндпоинт FABLES | `fables.fi/api/supply/total`, `/circulating` | 146 165 051.7 / 46 340 895.7 | Документирован на fables.fi/docs/token |

## Ключевые наблюдения, повлиявшие на архитектуру

1. **Новый пул ≠ новый токен.** BOW торгуется в 20+ пулах, созданных между 08.08 и 21.10; пул от 21.10 не делает токен
   21-дневным. Возраст считается по самому раннему известному пулу/первой сделке/TGE.
2. **MCAP ≠ FDV.** FABLES на одну дату: circulating (офиц.) 46.3 млн, totalSupply 146.2 млн, max 1 000 млн →
   MCAP ≈ $2.8 млн, FDV(total) ≈ $8.9 млн, FDV(max) ≈ $60 млн. CoinGecko/GT MCAP = $2.13–2.20 млн — **расхождение
   источников 30%+**, что фиксируется как конфликт.
3. **ATH и хвосты свечей.** FABLES: максимум первой 4-часовой свечи $0.145 при закрытии $0.065 — просадка 58% по хвосту
   против 20% по закрытиям. Показываются оба значения; аномальные хвосты (> 2× тела) не используются как ATH.
4. **Тикер не идентификатор.** По запросу «BOW» — 21 разный токен, в т.ч. другие «LONGBOW (BOW)» на той же
   Robinhood Chain (`0xad9e5f…`, `0x485743…`).
5. **Протокол старше токена.** TVL Fables в DefiLlama с 26.08, токен — с 20.10. Возраст протокола показывается отдельно.
6. **Эмиссия идёт непрерывно.** totalSupply FABLES вырос со 146.13 млн до 146.17 млн за несколько часов.

## Проверка объявления FABLES от 20.10.2026 (teapawt.net)

- Страница **не на домене fables.fi**, брендирована «teapawt»; год на странице не указан; автор — @fablesfi.
  Сама статья призывает пользоваться только fables.fi / claim.fables.fi. Вывод: **стороннее зеркало**.
- Параметры, совпадающие с официальной документацией fables.fi/docs/token: max 1 млрд, эмиссии 500 млн, старт 2%/потолок 5%
  в неделю, кумулятивные потолки 275/350/410/460 млн, 40 млн лок команды + 40 млн вестинг 6→24 мес., Seeder Safe 15 млн.
- Только на зеркале: недельные бюджеты 1–4 (10 / 9.8 / 9.604 / 9.41192 млн), детали points/PROLOGUE, и описание
  **предыдущей версии от 12.10** (1%/2%, потолки 210/330/400/440 млн, резерв 75 млн).
- В приложении сохранены **две версии**: v1 (12.10, источник — стороннее зеркало, не актуальна) и v2 (20.10, официальная
  документация, актуальна). Даты начала недель бюджета выведены из времени запуска и помечены как предположение.

## Что потребует платного API, ключа или ручной проверки

| Функция | Почему | Бесплатная альтернатива (реализована) |
|---|---|---|
| Полный поток новых пулов Solana | ~30 пулов/мин > бесплатного лимита GT | частичное покрытие с честной отметкой разрывов |
| Полный 90-дневный исторический поиск | new_pools покрывает только последние часы | топ-пулы по объёму + DefiLlama-листинги + собственная накопленная база |
| Верификация исходного кода | Blockscout закрыт Cloudflare; Etherscan V2 — бесплатный ключ | эвристика по байткоду (селекторы, EIP-1967) + статус UNKNOWN |
| Исторический totalSupply | нужен архивный RPC (платно) | восстановление из событий Transfer со сверкой с текущим totalSupply |
| Подтверждение No-VC | нет машиночитаемого источника | ручная запись с доказательством; DefiLlama raises → только FAIL |
| Дополнительная проверка circulating | CoinGecko Demo API — бесплатный ключ | GT market_cap (из CoinGecko) + официальные эндпоинты проектов |
| Dune | запросы требуют ключ, лимиты кредитов | не подключён (не нужен для v1) |
