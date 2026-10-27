#!/usr/bin/env python3
"""Junta as leituras de um banco de preços em outro (modo híbrido: GitHub + PC de casa).

    python merge_db.py prices_local.db prices.db     # copia do 1º para o 2º

O GitHub lê KaBuM e grava em prices.db. O PC de casa lê a Terabyte (que bloqueia o GitHub) e grava
em prices_local.db. Antes de gerar a página, o workflow junta os dois com este script.
Pode rodar várias vezes: leitura que já existe (mesmo item e mesmo horário) não é copiada de novo.
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import price_watch as pw

ITEM_COLS = "key, store, name, url, source, categoria, plataforma"


def merge(src_path: Path, dst_path: Path) -> dict:
    """Copia itens e leituras de `src_path` para `dst_path`. Devolve quantas linhas entraram."""
    if not Path(src_path).exists():
        raise FileNotFoundError(f"{src_path} não existe")
    dst = pw.open_db(Path(dst_path))
    try:
        dst.execute("ATTACH DATABASE ? AS origem", (str(src_path),))
        n_items = dst.execute(
            f"INSERT OR REPLACE INTO items({ITEM_COLS}) SELECT {ITEM_COLS} FROM origem.items"
        ).rowcount
        n_prices = dst.execute(
            "INSERT INTO prices(key, ts, price, available) "
            "SELECT s.key, s.ts, s.price, s.available FROM origem.prices s "
            "WHERE NOT EXISTS (SELECT 1 FROM prices p WHERE p.key = s.key AND p.ts = s.ts)"
        ).rowcount
        dst.commit()
        dst.execute("DETACH DATABASE origem")
    finally:
        dst.close()
    return {"itens": n_items, "leituras_novas": n_prices}


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 2:
        print(__doc__, file=sys.stderr)
        return 2
    src, dst = Path(args[0]), Path(args[1])
    if not src.exists():
        print(f"{src} não existe: nada para juntar (a Terabyte ainda não rodou no PC de casa).")
        return 0
    try:
        r = merge(src, dst)
    except sqlite3.Error as e:
        print(f"Erro ao juntar os bancos: {e}", file=sys.stderr)
        return 1
    print(f"Juntado {src.name} em {dst.name}: {r['itens']} itens, {r['leituras_novas']} leituras novas.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
