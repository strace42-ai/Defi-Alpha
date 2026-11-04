"""Реестр сетей. Идентификаторы проверены реальными запросами 2026-11-04
(см. docs/STAGE0_API_FEASIBILITY.md).

gt     — id сети в GeckoTerminal (/api/v2/networks)
ds     — chainId в DEX Screener
llama  — имя сети в DefiLlama (/v2/chains)
"""
from __future__ import annotations

from dataclasses import dataclass, field

ZERO = "0x0000000000000000000000000000000000000000"
NATIVE_EEEE = "0xeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"


@dataclass
class Chain:
    key: str
    name: str
    kind: str  # "evm" | "solana"
    gt: str | None
    ds: str | None
    llama: str | None
    chain_id: int | None = None
    rpc: list[str] = field(default_factory=list)
    explorer: str | None = None
    explorer_token_path: str = "/token/{address}"
    explorer_address_path: str = "/address/{address}"
    explorer_tx_path: str = "/tx/{tx}"
    max_log_range: int = 10_000  # максимальный диапазон блоков eth_getLogs (подтверждён для RPC)
    logs_ok: bool = True  # бесплатный RPC отдаёт eth_getLogs (проверено 2026-11-04)
    logs_note: str = ""
    support: str = "experimental"  # "supported" | "experimental" | "planned"
    default_enabled: bool = False
    quote_assets: dict[str, str] = field(default_factory=dict)  # address(normalized) -> symbol
    notes: str = ""

    def explorer_token(self, address: str) -> str | None:
        return (self.explorer + self.explorer_token_path.format(address=address)) if self.explorer else None

    def explorer_address(self, address: str) -> str | None:
        return (self.explorer + self.explorer_address_path.format(address=address)) if self.explorer else None

    def explorer_tx(self, tx: str) -> str | None:
        return (self.explorer + self.explorer_tx_path.format(tx=tx)) if self.explorer else None


def _q(d: dict[str, str]) -> dict[str, str]:
    return {k.lower(): v for k, v in d.items()}


CHAINS: dict[str, Chain] = {
    "robinhood": Chain(
        key="robinhood", name="Robinhood Chain", kind="evm", gt="robinhood", ds="robinhood",
        llama="Robinhood Chain", chain_id=4663,
        rpc=["https://rpc.mainnet.chain.robinhood.com"],
        explorer="https://robinhoodchain.blockscout.com",
        max_log_range=2_000_000,  # официальный RPC принимает до 10 млн блоков; берём запас
        support="supported", default_enabled=True,
        quote_assets=_q({ZERO: "ETH", NATIVE_EEEE: "ETH",
                         "0x0bd7d308f8e1639fab988df18a8011f41eacad73": "WETH",
                         "0x5fc5360d0400a0fd4f2af552add042d716f1d168": "USDG"}),
        notes="Блок ~0.1 c. Публичный RPC без архивного состояния: исторический totalSupply недоступен, "
              "история предложения восстанавливается из событий Transfer. Blockscout API закрыт Cloudflare-проверкой.",
    ),
    "base": Chain(
        key="base", name="Base", kind="evm", gt="base", ds="base", llama="Base", chain_id=8453,
        rpc=["https://mainnet.base.org"], explorer="https://basescan.org",
        max_log_range=2_000, support="supported", default_enabled=True, logs_ok=False,
        logs_note="бесплатные RPC Base не отдают eth_getLogs (mainnet.base.org: 429 «request limit reached», drpc: лимит free-плана)",
        quote_assets=_q({ZERO: "ETH", NATIVE_EEEE: "ETH",
                         "0x4200000000000000000000000000000000000006": "WETH",
                         "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913": "USDC",
                         "0xd9aaec86b65d86f6a7b5b1b0c42ffa531710b6ca": "USDbC",
                         "0x50c5725949a6f0c72e6c4a641f24049a917db0cb": "DAI",
                         "0xcbb7c0000ab88b473b1f5afd9ef808440eed33bf": "cbBTC",
                         "0xfde4c96c8593536e31f229ea8f37b2ada2699bb2": "USDT"}),
    ),
    "eth": Chain(
        key="eth", name="Ethereum", kind="evm", gt="eth", ds="ethereum", llama="Ethereum", chain_id=1,
        rpc=["https://ethereum-rpc.publicnode.com"], explorer="https://etherscan.io",
        max_log_range=10_000, logs_note="publicnode: только недавние блоки (старые — «Archive requests require a personal token»)", support="supported", default_enabled=True,
        quote_assets=_q({ZERO: "ETH", NATIVE_EEEE: "ETH",
                         "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2": "WETH",
                         "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48": "USDC",
                         "0xdac17f958d2ee523a2206206994597c13d831ec7": "USDT",
                         "0x6b175474e89094c44da98b954eedeac495271d0f": "DAI",
                         "0x2260fac5e5542a773aa44fbcfedf7c193bc2c599": "WBTC"}),
    ),
    "arbitrum": Chain(
        key="arbitrum", name="Arbitrum", kind="evm", gt="arbitrum", ds="arbitrum", llama="Arbitrum",
        chain_id=42161, rpc=["https://arb1.arbitrum.io/rpc"], explorer="https://arbiscan.io",
        max_log_range=50_000, support="supported", default_enabled=True,
        quote_assets=_q({ZERO: "ETH", NATIVE_EEEE: "ETH",
                         "0x82af49447d8a07e3bd95bd0d56f35241523fbab1": "WETH",
                         "0xaf88d065e77c8cc2239327c5edb3a432268e5831": "USDC",
                         "0xff970a61a04b1ca14834a43f5de4533ebddb5cc8": "USDC.e",
                         "0xfd086bc7cd5c481dcc9c85ebe478a1c0b69fcbb9": "USDT",
                         "0x912ce59144191c1204e64559fe8253a0e49e6548": "ARB"}),
    ),
    "bsc": Chain(
        key="bsc", name="BNB Chain", kind="evm", gt="bsc", ds="bsc", llama="BSC", chain_id=56,
        rpc=["https://bsc-rpc.publicnode.com"], explorer="https://bscscan.com",
        max_log_range=20_000, logs_note="официальный dataseed отвечает «limit exceeded» на getLogs; используется publicnode", support="supported", default_enabled=True,
        quote_assets=_q({ZERO: "BNB", NATIVE_EEEE: "BNB",
                         "0xbb4cdb9cbd36b01bd1cbaebf2de08d9173bc095c": "WBNB",
                         "0x55d398326f99059ff775485246999027b3197955": "USDT",
                         "0x8ac76a51cc950d9822d68b83fe1ad97b32cd580d": "USDC",
                         "0xe9e7cea3dedca5984780bafc599bd69add087d56": "BUSD",
                         "0x2170ed0880ac9a755fd29b2688956bd959f933f8": "ETH",
                         "0x7130d2a12b9bcbfae4f2634d864a1ee1ce3ead9c": "BTCB"}),
    ),
    "optimism": Chain(
        key="optimism", name="Optimism", kind="evm", gt="optimism", ds="optimism", llama="OP Mainnet",
        chain_id=10, rpc=["https://mainnet.optimism.io"], explorer="https://optimistic.etherscan.io",
        max_log_range=2_000, support="experimental", logs_ok=False, logs_note="getLogs на бесплатном RPC не проверен",
        quote_assets=_q({ZERO: "ETH", NATIVE_EEEE: "ETH",
                         "0x4200000000000000000000000000000000000006": "WETH",
                         "0x0b2c639c533813f4aa9d7837caf62653d097ff85": "USDC",
                         "0x7f5c764cbc14f9669b88837ca1490cca17c31607": "USDC.e",
                         "0x94b008aa00579c1307b0ef2c499ad98a8ce58e58": "USDT",
                         "0x4200000000000000000000000000000000000042": "OP"}),
        notes="В DefiLlama /v2/chains запись 'Optimism' показывает TVL=0, актуальная — 'OP Mainnet'.",
    ),
    "solana": Chain(
        key="solana", name="Solana", kind="solana", gt="solana", ds="solana", llama="Solana",
        rpc=["https://api.mainnet-beta.solana.com"], explorer="https://solscan.io",
        explorer_address_path="/account/{address}",
        support="experimental",
        quote_assets={"So11111111111111111111111111111111111111112": "SOL",
                      "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v": "USDC",
                      "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB": "USDT"},
        notes="Отдельный адаптер. Mint-адреса регистрозависимы. ~20 новых пулов за 40 секунд: бесплатный "
              "GeckoTerminal покрывает лишь минуты — покрытие заведомо частичное.",
    ),
    "hyperevm": Chain(
        key="hyperevm", name="HyperEVM", kind="evm", gt="hyperevm", ds="hyperevm", llama="Hyperliquid L1",
        chain_id=999, rpc=["https://rpc.hyperliquid.xyz/evm"], explorer="https://hyperevmscan.io",
        max_log_range=1_000, support="experimental",
        quote_assets=_q({ZERO: "HYPE", NATIVE_EEEE: "HYPE",
                         "0x5555555555555555555555555555555555555555": "WHYPE",
                         "0xb8ce59fc3717ada4c02eadf9682a9e934f625ebb": "USDT0"}),
        notes="DefiLlama-сеть 'Hyperliquid L1' включает не только HyperEVM.",
    ),
    "megaeth": Chain(
        key="megaeth", name="MegaETH", kind="evm", gt="megaeth", ds="megaeth", llama="MegaETH",
        chain_id=4326, rpc=["https://mainnet.megaeth.com/rpc"], explorer="https://mega.etherscan.io",
        max_log_range=1_000, support="experimental",
        quote_assets=_q({ZERO: "ETH", NATIVE_EEEE: "ETH",
                         "0x4200000000000000000000000000000000000006": "WETH"}),
        notes="На 2026-11-04 GeckoTerminal new_pools вернул 0 пулов.",
    ),
}

# Символы, которые сами по себе НЕ доказывают, что токен — стандартный quote-актив
# (их часто подделывают). Используются только для пометки «имитация крупного актива».
MAJOR_SYMBOLS = {"ETH", "WETH", "USDC", "USDT", "DAI", "WBTC", "BTC", "BNB", "WBNB", "SOL", "WSOL",
                 "USDG", "USDE", "USDC.E", "CBBTC", "HYPE", "WHYPE", "USDT0", "OP", "ARB"}


def get_chain(key: str) -> Chain:
    return CHAINS[key]


def by_gt(gt_id: str) -> Chain | None:
    for c in CHAINS.values():
        if c.gt == gt_id:
            return c
    return None


def by_ds(ds_id: str) -> Chain | None:
    for c in CHAINS.values():
        if c.ds == ds_id:
            return c
    return None


def by_llama(name: str) -> Chain | None:
    for c in CHAINS.values():
        if c.llama and c.llama.lower() == (name or "").lower():
            return c
    # DefiLlama в поле address использует короткие префиксы ("robinhood:0x..", "base:0x..")
    aliases = {"ethereum": "eth", "bsc": "bsc", "arbitrum": "arbitrum", "base": "base",
               "optimism": "optimism", "solana": "solana", "robinhood": "robinhood",
               "hyperliquid": "hyperevm", "megaeth": "megaeth", "robinhood chain": "robinhood"}
    k = aliases.get((name or "").lower())
    return CHAINS.get(k) if k else None


def is_quote_asset(chain: Chain, address: str) -> bool:
    return address in chain.quote_assets
