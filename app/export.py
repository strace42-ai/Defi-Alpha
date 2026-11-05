"""Экспорт таблицы сканера в CSV/XLSX. Секреты в экспорт не попадают (они не хранятся в БД)."""
from __future__ import annotations

import csv
import io

COLUMNS = [
    ("symbol", "Тикер"), ("name", "Проект"), ("chain", "Сеть"), ("address", "Контракт"),
    ("first_trade", "Начало торгов"), ("age_days", "Возраст, дн."), ("age_basis", "Основа возраста"),
    ("price", "Цена, $"), ("mcap", "MCAP (circ.), $"), ("mcap_type", "Тип MCAP"), ("mcap_low", "MCAP от, $"), ("mcap_high", "MCAP до, $"), ("mcap_basis", "Основа MCAP"),
    ("circ_supply", "Circulating supply"), ("total_supply", "Total supply"), ("fdv_total", "FDV (totalSupply), $"),
    ("fdv_max", "FDV (max), $"), ("liquidity", "Ликвидность, $"), ("volume", "Объём 24ч, $"), ("drawdown", "Просадка от ATH, %"),
    ("f_age", "TGE / возраст"), ("f_mcap", "MCAP"), ("f_drawdown", "Просадка"), ("f_utility", "DeFi-утилити"),
    ("f_ecosystem", "Экосистема"), ("level", "Уровень"), ("status", "Итоговый статус"), ("status_reason", "Причина"),
    ("score_preliminary", "Оценка (предв.)"), ("score_confirmed", "Подтверждённые баллы"), ("score_max_verifiable", "Проверяемый максимум"),
    ("confidence", "Достоверность"), ("protocol", "Протокол DefiLlama"), ("first_seen_at", "Обнаружен (UTC)"),
    ("explorer", "Ссылка на контракт"),
]


def to_csv(rows: list[dict]) -> bytes:
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";")
    w.writerow([c[1] for c in COLUMNS])
    for r in rows:
        w.writerow(["" if r.get(k) is None else r.get(k) for k, _ in COLUMNS])
    return ("﻿" + buf.getvalue()).encode("utf-8")  # BOM — чтобы Excel открыл кириллицу


def to_xlsx(rows: list[dict]) -> bytes:
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill
    wb = Workbook()
    ws = wb.active
    ws.title = "Сканер"
    ws.append([c[1] for c in COLUMNS])
    for cell in ws[1]:
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor="1F2937")
    for r in rows:
        ws.append([r.get(k) for k, _ in COLUMNS])
    ws.freeze_panes = "A2"
    for i, (k, t) in enumerate(COLUMNS, 1):
        ws.column_dimensions[ws.cell(1, i).column_letter].width = max(10, min(45, len(t) + 4))
    ws2 = wb.create_sheet("Примечания")
    ws2.append(["Данные — из публичных API (GeckoTerminal, DEX Screener, DefiLlama, RPC). Пустая ячейка = нет данных (не ноль)."])
    ws2.append(["MCAP не приравнивается к FDV. Статусы PASS/FAIL/UNKNOWN — см. карточку проекта для доказательств."])
    ws2.append(["Оценка — предварительная, не рекомендация к покупке."])
    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()
