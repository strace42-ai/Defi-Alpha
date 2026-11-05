"""FABLES (Robinhood Chain): официальные контракты, события и правила атрибуции выпуска (этап 2, релиз A).

Источники (проверено 2026-11-05, см. docs/research/FABLES_RELEASE_A.md):
- официальная документация fables.fi/docs, таблица «FABLES and voting contracts» (адреса — из официального JS-бандла сайта,
  читался как текст); ABI функций и событий — из того же бандла;
- ончейн: код по адресам, FABLES.minter(), Minter.isMinter(allocation, caller), Minter.ceilingOf/mintedOf/totalMinted,
  Stream.startAt, Escrow.epochStart (чистая функция контракта), события Minted / Claimed / WeeklyBudgetSet / WeeksClosed.
Обозреватель Blockscout из облачной сети закрыт Cloudflare: verified source/ABI не получены (статус «не проверено»).
"""
from __future__ import annotations

from typing import Any

from .keccak import topic

CHAIN = "robinhood"
CHAIN_ID = 4663
TOKEN = "0xb63d07cef74be747231d601bb2faa6b252c68106"
TOKEN_UID = f"{CHAIN}:{TOKEN}"
DEPLOY_BLOCK = 92_925_258          # из официальной конфигурации сайта (deployBlock)
WEEK = 7 * 86400
WEEK_OFFSET = 5 * 86400            # неделя w начинается в w·7 дн. + 5 дн. от 1970-01-01 → вторник 00:00 UTC
FIRST_WEEK = 2963                  # Stream.startAt() = 2026-10-20 00:00 UTC = начало недели 2963
METHOD_VERSION = "fables-A1"

# Официальный реестр. role_docs — как названо в документации; role_expected — роль в модели; kind — ожидаемый тип адреса.
CONTRACTS: list[dict[str, Any]] = [
    {"key": "token", "address": TOKEN, "docs": "FABLES token", "role_expected": "token", "kind": "contract"},
    {"key": "minter", "address": "0x3c1672c615ef1f966a1cbcc37f924586a5a3d422", "docs": "FABLES minter", "role_expected": "minter", "kind": "contract"},
    {"key": "escrow", "address": "0xc1d5adc963a57d24adaf63aae1bfbecab95f6edd", "docs": "veFABLES escrow", "role_expected": "ve_escrow", "kind": "contract"},
    {"key": "voter", "address": "0x71570b34d9200ffe5c3066f5a204f91229e7338e", "docs": "Voter and gauges", "role_expected": "voter", "kind": "contract"},
    {"key": "bribes", "address": "0x325532108660c1445e723d6205ce89462ef61836", "docs": "Voter incentives", "role_expected": "bribes", "kind": "contract"},
    {"key": "distributor", "address": "0x498506256215702f0da01c0f9ab0c765488acd07", "docs": "Voter income", "role_expected": "voter_income_distributor", "kind": "contract"},
    {"key": "stream", "address": "0x4aa54fa62bc8f6052fb4d3494a3fa79741bda14c", "docs": "Emission stream", "role_expected": "emission_stream", "kind": "contract"},
    {"key": "stream_poster", "address": "0x8df9d3142400eeffc40d70577afb76132aa273bb", "docs": "stream poster (конфигурация сайта)", "role_expected": "root_poster", "kind": "eoa"},
    {"key": "fee_forwarder", "address": "0x85f3af4780fabdc54b54ecd5fe7045cd5627c52f", "docs": "Fee forwarder", "role_expected": "fee_forwarder", "kind": "contract"},
    {"key": "claim_fee_forwarder", "address": "0x71ec190f76762f824407aeb2d244dc120c48262d", "docs": "Claim fee forwarder", "role_expected": "fee_forwarder", "kind": "contract"},
    {"key": "points_liquid", "address": "0xf64aac89b99f8910121b83003e2e118015297f0f", "docs": "Points claim, liquid", "role_expected": "points_claim_liquid", "kind": "contract"},
    {"key": "points_six_months", "address": "0x95d9a51fc8ee8c7b05d552842cd5bc0a17d53a59", "docs": "Points claim, six months", "role_expected": "points_claim_locked", "kind": "contract"},
    {"key": "points_auto_max", "address": "0x28097735d5d78499c08d7bf89810b28f59299015", "docs": "Points claim, Auto-max", "role_expected": "points_claim_locked", "kind": "contract"},
    {"key": "prologue_redeemer", "address": "0x47876425bed27bc8d396dae951119f5d9cdb4b8d", "docs": "PROLOGUE redeemer", "role_expected": "prologue_redeemer", "kind": "contract"},
    {"key": "prologue_token", "address": "0xb9972ca7188e511174947e3936a5315ac7073277", "docs": "PROLOGUE", "role_expected": "prologue_token", "kind": "contract"},
    {"key": "team_safe", "address": "0x215f159881964f1e924dbfe40e9e64181b0d4b73", "docs": "Team Safe (владелец Lock #1)", "role_expected": "team_safe", "kind": "safe"},
    {"key": "seeder_safe", "address": "0x385bc909ca7efd34788216ea97346d3ba75c94de", "docs": "Seeder Safe", "role_expected": "seeder_safe", "kind": "safe"},
]
BY_ADDR = {c["address"]: c for c in CONTRACTS}
ADDR = {c["key"]: c["address"] for c in CONTRACTS}

# Распределения Minter: номер → (название, потолок, основание сопоставления). Номер — ончейн; название — по совпадению
# потолка ceilingOf(i) с документацией (9 распределений). 4 и 8 имеют одинаковый потолок 40M — название не различимо.
ALLOCATIONS: dict[int, dict[str, Any]] = {
    0: {"name": "Emissions", "ceiling_m": 500, "naming": "единственный потолок 500M = «Emissions» в документации"},
    1: {"name": "Community (Points)", "ceiling_m": 120, "naming": "единственный потолок 120M = «Community»; минтят только контракты Points claim"},
    2: {"name": "PROLOGUE", "ceiling_m": 25, "naming": "единственный потолок 25M = «PROLOGUE»; минтит только PROLOGUE redeemer"},
    3: {"name": "Team lock", "ceiling_m": 40, "naming": "потолок 40M; единственный mint — при TGE в veFABLES Lock #1 Team Safe (документация: «the team's lock»)"},
    4: {"name": "40M (Team vesting или Operations — не различимо по потолку)", "ceiling_m": 40, "naming": "потолок 40M; выпусков нет"},
    5: {"name": "Ecosystem", "ceiling_m": 80, "naming": "потолок 80M; выпусков нет"},
    6: {"name": "Strategic reserve", "ceiling_m": 75, "naming": "единственный потолок 75M"},
    7: {"name": "Grants", "ceiling_m": 50, "naming": "единственный потолок 50M"},
    8: {"name": "40M (Team vesting или Operations — не различимо по потолку)", "ceiling_m": 40, "naming": "потолок 40M; выпусков нет"},
    9: {"name": "Liquidity", "ceiling_m": 30, "naming": "единственный потолок 30M; mint 15M при TGE на Seeder Safe («launch liquidity»)"},
}

CATEGORIES = {
    "GENESIS_TEAM": "Genesis: команда (Lock #1)", "GENESIS_SEEDER": "Genesis: ликвидность запуска (Seeder Safe)",
    "GENESIS_TREASURY": "Genesis: казначейство", "GENESIS_OTHER": "Genesis: прочее",
    "LP_EMISSION": "LP-эмиссия (claim из Emission stream)", "POINTS_LIQUID": "Points: ликвидный claim",
    "POINTS_LOCKED": "Points: claim с блокировкой (six months / Auto-max)", "REBASE": "Rebase (в ve-локи)",
    "OPERATING_EMISSION": "Операционная эмиссия", "PROLOGUE": "PROLOGUE redemption", "BURN": "Сжигание", "UNKNOWN": "Не установлено",
}

# События (topic0 считается из сигнатуры; поля — порядок как в ABI; indexed → topics)
EVENTS: dict[str, dict[str, Any]] = {
    "Minted": {"sig": "Minted(uint8,address,uint256,address)", "contract": "minter",
               "fields": [("allocation", "uint8", True), ("to", "address", True), ("amount", "uint256", False), ("by", "address", True)]},
    "StreamClaimed": {"sig": "Claimed(bytes32,uint8,uint256,uint256,address,uint64)", "contract": "stream",
                      "fields": [("key", "bytes32", True), ("kind", "uint8", False), ("id", "uint256", True), ("amount", "uint256", False),
                                 ("to", "address", True), ("asOf", "uint64", False)]},
    "RootPosted": {"sig": "RootPosted(uint256,bytes32,uint256,uint64)", "contract": "stream",
                   "fields": [("seq", "uint256", True), ("root", "bytes32", False), ("cumulativeTotal", "uint256", False), ("asOf", "uint64", False)]},
    "WeeksClosed": {"sig": "WeeksClosed(uint256,uint256)", "contract": "stream",
                    "fields": [("throughWeek", "uint256", True), ("closedTotal", "uint256", False)]},
    "WeeklyBudgetSet": {"sig": "WeeklyBudgetSet(uint256,uint256)", "contract": "stream",
                        "fields": [("week", "uint256", True), ("amount", "uint256", False)]},
    "Transfer": {"sig": "Transfer(address,address,uint256)", "contract": "token",
                 "fields": [("from", "address", True), ("to", "address", True), ("value", "uint256", False)]},
}
for _n, _e in EVENTS.items():
    _e["topic0"] = topic(_e["sig"])
TOPIC_TO_EVENT = {(ADDR[e["contract"]], e["topic0"]): n for n, e in EVENTS.items()}
DECODE_VERSION = "abi-fables-site-2026-11-05"

# Claimed.kind потока: по коду официального сайта kind 0 — получатель-аккаунт (LP), kind 1 — номер ve-лока;
# для kind 1 в той же транзакции поток переводит FABLES в escrow и escrow записывает пополнение этого лока.
STREAM_KIND = {0: "LP_EMISSION", 1: "REBASE"}


def _word(hexdata: str, i: int) -> str:
    return hexdata[2 + 64 * i: 2 + 64 * (i + 1)]


def _val(word: str, typ: str) -> Any:
    if typ == "address":
        return "0x" + word[-40:].lower()
    if typ == "bytes32":
        return "0x" + word.lower()
    if typ == "bool":
        return int(word, 16) != 0
    return str(int(word, 16))  # uint* — строкой (точно, без float)


def decode(name: str, log: dict) -> dict:
    spec = EVENTS[name]
    topics = log.get("topics") or []
    data = log.get("data") or "0x"
    out: dict[str, Any] = {}
    ti, di = 1, 0
    for fname, typ, indexed in spec["fields"]:
        if indexed:
            out[fname] = _val(topics[ti][2:].rjust(64, "0"), typ)
            ti += 1
        else:
            out[fname] = _val(_word(data, di), typ)
            di += 1
    return out


def week_of(ts: int) -> int:
    return (int(ts) - WEEK_OFFSET) // WEEK


def week_start(w: int) -> int:
    return int(w) * WEEK + WEEK_OFFSET


def classify_mint(minted: dict, claim: dict | None) -> tuple[str, str, str]:
    """Категория первичного выпуска по записи Minter (allocation, by, to) и событию Claimed потока в той же транзакции.
    → (категория, доказательность, основание). Неизвестное сочетание — UNKNOWN (не угадывается)."""
    al = int(minted["allocation"])
    by, to = minted["by"], minted["to"]
    cb = BY_ADDR.get(by, {}).get("key")
    if al == 0:
        if cb != "stream":
            return "UNKNOWN", "unknown", f"распределение Emissions, вызывающий {by} — не Emission stream"
        if claim is None:
            return "UNKNOWN", "unknown", "mint из Emissions без события Claimed потока в той же транзакции"
        cat = STREAM_KIND.get(int(claim["kind"]))
        if cat is None:
            return "UNKNOWN", "unknown", f"Claimed.kind={claim['kind']} — значение не установлено"
        if cat == "REBASE" and to != ADDR["stream"]:
            return "UNKNOWN", "unknown", "rebase-claim, но mint не на контракт потока (ожидается stream → escrow)"
        return cat, "confirmed", f"Minted(allocation=0, by=Emission stream) + Claimed(kind={claim['kind']}) в той же транзакции"
    if al == 1:
        if cb == "points_liquid":
            return "POINTS_LIQUID", "confirmed", "Minted(allocation=1, by=Points claim liquid)"
        if cb in ("points_six_months", "points_auto_max"):
            return "POINTS_LOCKED", "confirmed", f"Minted(allocation=1, by={BY_ADDR[by]['docs']})"
        return "UNKNOWN", "unknown", f"распределение Community, вызывающий {by} не из реестра"
    if al == 2:
        return ("PROLOGUE", "confirmed", "Minted(allocation=2, by=PROLOGUE redeemer)") if cb == "prologue_redeemer" else \
            ("UNKNOWN", "unknown", f"распределение PROLOGUE, вызывающий {by} не из реестра")
    if al == 3:
        return "GENESIS_TEAM", "confirmed", "Minted(allocation=3) при TGE; в той же транзакции — Lock #1 Team Safe (см. расследование)"
    if al == 9:
        return ("GENESIS_SEEDER", "confirmed", "Minted(allocation=9, to=Seeder Safe) при TGE") if to == ADDR["seeder_safe"] else \
            ("UNKNOWN", "unknown", "распределение Liquidity на адрес не Seeder Safe")
    return "UNKNOWN", "unknown", f"распределение {al} ({ALLOCATIONS.get(al, {}).get('name', '?')}) — правило не задано"
