"""Загрузка исследовательских записей (research/control_tokens.json) — один раз, идемпотентно."""
from __future__ import annotations

import json

from . import config, db
from .chains import CHAINS
from .discovery import add_token_manually
from .util import dumps, normalize_address, now_iso, token_uid

SEED = config.ROOT / "research" / "control_tokens.json"


def load_seed(force: bool = False) -> list[str]:
    if not SEED.exists():
        return []
    data = json.loads(SEED.read_text(encoding="utf-8"))
    ver = str(data.get("seed_version", 1))
    if not force and db.q1("SELECT value FROM meta WHERE key='research_seed'") and db.q1("SELECT value FROM meta WHERE key='research_seed'")["value"] == ver:
        return []
    uids = []
    for t in data.get("tokens", []):
        ch = CHAINS[t["chain"]]
        uid, _ = add_token_manually(ch.key, t["address"], t.get("note", ""))
        if not uid:
            continue
        uids.append(uid)
        db.ex("UPDATE tokens SET address_display=? WHERE uid=?", (t["address"], uid))
        for f in t.get("facts", []):
            if not db.q1("SELECT 1 FROM manual_facts WHERE token_uid=? AND fact_key=? AND value=? AND entered_by='research_seed'",
                         (uid, f["fact_key"], f.get("value"))):
                db.ex("INSERT INTO manual_facts(token_uid,fact_key,value,category,evidence_url,source_type,note,entered_by,entered_at) "
                      "VALUES(?,?,?,?,?,?,?,?,?)", (uid, f["fact_key"], f.get("value"), f.get("category"), f.get("evidence_url"),
                                                     f.get("source_type"), f.get("note"), "research_seed", now_iso()))
        for lb in t.get("labels", []):
            db.ex("INSERT OR REPLACE INTO address_labels(chain,address,label,kind,source_url,source_type,token_uid) VALUES(?,?,?,?,?,?,?)",
                  (ch.key, normalize_address(ch.kind, lb["address"]), lb["label"], lb["kind"], lb.get("source_url"), lb.get("source_type"), uid))
        for tv in t.get("tokenomics", []):
            if not db.q1("SELECT 1 FROM tokenomics_versions WHERE token_uid=? AND version_label=?", (uid, tv["version_label"])):
                db.ex("INSERT INTO tokenomics_versions(token_uid,version_label,published_at,source_url,source_type,is_current,params,note,entered_at) "
                      "VALUES(?,?,?,?,?,?,?,?,?)", (uid, tv["version_label"], tv.get("published_at"), tv.get("source_url"),
                                                     tv.get("source_type"), 1 if tv.get("is_current") else 0, dumps(tv.get("params", {})),
                                                     tv.get("note"), now_iso()))
    db.ex("INSERT OR REPLACE INTO meta(key,value) VALUES('research_seed',?)", (ver,))
    return uids
