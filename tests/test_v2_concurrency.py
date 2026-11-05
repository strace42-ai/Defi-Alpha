"""v2: параллельные очереди не должны затирать изменения друг друга в сводке фактов токена (синтетические данные)."""
from app import db, discovery, enrich
from app.chains import CHAINS


def test_parallel_fact_updates_do_not_overwrite():
    uid, _ = discovery.upsert_token(CHAINS["robinhood"], {"address": "0x" + "2" * 40, "symbol": "T"}, source="t", reason="r")
    a = enrich.get_facts(uid)   # очередь «котировки» прочитала факты
    b = enrich.get_facts(uid)   # очередь «CoinGecko» прочитала те же факты
    a["price"] = {"value": 1.0}
    enrich.save_facts(uid, a)
    b["coingecko"] = {"circulating_supply": 100}
    enrich.save_facts(uid, b, "updated_at")  # раньше эта запись стирала price
    f = enrich.get_facts(uid)
    assert f["price"]["value"] == 1.0 and f["coingecko"]["circulating_supply"] == 100
    # удаление ключа одной очередью применяется, не трогая остальные
    c = enrich.get_facts(uid)
    c.pop("price")
    enrich.save_facts(uid, c, "updated_at")
    f = enrich.get_facts(uid)
    assert "price" not in f and f["coingecko"]["circulating_supply"] == 100
