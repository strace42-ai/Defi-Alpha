# FABLES: идентификация контрактов (этап 2, исследование)

Дата: 2026-11-05. Только чтение: официальная документация, публичный RPC Robinhood Chain и копия базы.
Рабочая база пользователя и реестр ролей **не изменялись**.

## Источники
1. **Официальная документация** `https://www.fables.fi/docs`, таблица «FABLES and voting contracts» («Live since TGE»).
   Сайт — одностраничное приложение: таблица и адреса взяты из его официального JS-бандла. Код бандла не выполнялся, читался как текст
   (модули `DocsRoute-*.js`, `GateCard-*.js`; объект конфигурации контрактов с полями `fables`, `minter`, `escrow`, `voter` …).
2. **Проверка ончейн**: `eth_getCode` для каждого адреса; `FABLES.minter()`; выборка транзакций mint (`eth_getTransactionByHash`).

## Контракты
| Название в документации | Адрес | Код (байт) | Mint FABLES на адрес (копия базы, до блока 106 573 185) |
|---|---|---|---|
| FABLES minter | `0x3c1672C615eF1F966A1CBcc37F924586a5a3d422` | 9 928; `FABLES.minter()` = этот адрес | 1 mint, 40 000 000 (блок 92 991 320, до TGE) |
| veFABLES escrow | `0xc1d5aDc963a57d24ADaf63aaE1bFbeCaB95f6EDD` | 14 062 | 0 |
| Voter and gauges | `0x71570b34d9200FFE5C3066f5A204f91229E7338E` | 15 760 | 0 |
| Voter incentives | `0x325532108660C1445e723d6205Ce89462ef61836` | 8 086 | 0 |
| Voter income | `0x498506256215702f0DA01c0f9Ab0C765488acd07` | 14 103 | 0 |
| Emission stream | `0x4aA54fA62bC8f6052fB4D3494A3fA79741Bda14c` | 8 290 | 476 mint, 150 740,10 |
| (stream poster) | `0x8Df9D3142400EEFFc40D70577AFb76132AA273BB` | 0 — это ключ (EOA), не контракт | 0 |
| Fee forwarder | `0x85F3AF4780FaBDC54B54EcD5Fe7045cD5627c52f` | 2 183 | 0 |
| Claim fee forwarder | `0x71EC190f76762f824407AEB2d244dC120C48262D` | 2 183 | 0 |
| Points claim, liquid | `0xf64aAC89B99f8910121b83003e2E118015297f0F` | 3 122 | 0 (минтит напрямую в кошельки) |
| Points claim, six months | `0x95d9A51fC8EE8C7b05d552842CD5bC0a17d53a59` | 3 122 | 677 mint, 19 035 010,81 |
| Points claim, Auto-max | `0x28097735d5D78499C08D7Bf89810b28f59299015` | 3 122 | 417 mint, 21 390 016,10 |
| PROLOGUE redeemer | `0x47876425BeD27BC8d396dAe951119F5d9CDb4B8D` | 5 974 | 0 |
| Team Safe (Lock #1 owner) | `0x215F159881964f1E924DBfE40E9e64181b0D4b73` | — | 0 |
| Seeder Safe | `0x385Bc909CA7EFd34788216Ea97346D3bA75C94de` | — | 3 mint, 15 088 495,23 |

## Что это меняет в модели Emission Pressure
1. **Два крупнейших «неизвестных» получателя** (21,39 млн и 19,04 млн) — контракты Points claim (Auto-max и six months).
   Это **airdrop/points, а не LP-эмиссия**. Документация: liquid 24,69 млн, six months 20,90 млн, Auto-max 21,48 млн.
   Выпущено на контракты блокировок на копии базы: 19,04 из 20,90 и 21,39 из 21,48 — claims ещё идут.
2. **LP-эмиссия минтится не на gauge, а в кошелёк LP при claim** через контракт Emission stream. В выборке из 30 случайных mint
   на «обычные» адреса:
   - 13 — транзакции к Emission stream;
   - 14 — к Points claim, liquid;
   - 1 — к PROLOGUE redeemer;
   - 2 — к контрактам вне таблицы документации.

   Поэтому классификация **по получателю** для FABLES принципиально неполна. Нужна классификация **по контракту, вызвавшему mint**:
   поле `to` транзакции или события этого контракта в той же транзакции. В версии 2.1.1 это даёт честный результат: 51,36 млн
   на 10 563 адреса — UNKNOWN, использование бюджета — «не проверено».
3. **40 000 000 на minter до TGE.** Документация: «Team: 40M Auto-max lock (Lock #1), minted at TGE», владелец Lock #1 — Team Safe.
   Совпадение суммы и времени — сильная подсказка, но для категории «Genesis: team» нужно ончейн-подтверждение: перевод
   minter → escrow в той же транзакции и Lock #1 у Team Safe.
4. **Eligible fees.** Документация: «budget … capped at 5 × (eligible staked fees + incentives)»; eligible fees — без wash trading и
   self-dealing; недельный файл публикуется на Analytics (`/api/budget`). Это прямой источник eligible fees и бюджетов по рынкам.
   Общие комиссии DefiLlama по-прежнему не считаются eligible.
5. В версии токеномики 2.0 записано `emission_recipient_kinds: ["gauge", "emissions"]`. Реальная архитектура другая (mint-on-claim
   через stream), поэтому нужна новая версия токеномики со ссылкой на документацию, а не правка старой.

## Предлагаемый план этапа 2 (ограниченный индексатор только для FABLES)
- **Роли с доказательством** (документация и `eth_getCode`; для minter также `FABLES.minter()`):
  minter, ve_escrow, voter, voter incentives, voter income, emission stream, points claim (3), redeemer.
- **Атрибуция mint по вызывающему контракту**: для каждой транзакции mint — поле `to` и события Emission stream, Points claim
  и redeemer в той же транзакции. Новая таблица событий `an_*` с идентичностью (tx, log_index), без изменения `supply_events`.
  Бюджет: N RPC-вызовов за задание. Только FABLES, история не переиндексируется при каждом запуске.
- **Метод `emission-1.2`**: категории «LP-эмиссия: claim из Emission stream», «Points/airdrop claim», «PROLOGUE redemption».
  FINAL использования бюджета — только если каждый mint недели атрибутирован.
- **Затем**: события escrow (новые локи, увеличения, выводы, Auto-max) и недельные файлы бюджетов и eligible fees.
