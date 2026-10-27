#!/usr/bin/env python3
"""Gera a versão publicada da página (GitHub Pages): site/index.html + site/data.json.

    python build_site.py                 # lê prices.db, escreve em site/
    python build_site.py --db outro.db --out pasta

A página publicada não tem servidor: ela lê o data.json e faz a soma/FPS no navegador.
Só entram preços e links de lojas (nada de segredos): o data.json é público.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

import build_setups as bs

HERE = Path(__file__).resolve().parent
PERIODOS = (7, 30, 90, 0)      # dias; 0 = todo o histórico (mesmas opções do seletor da página)


def build(db_path: Path, out: Path) -> dict:
    bench = bs.load_bench(HERE / "benchmarks.json")
    cfg = bench["config"]
    db = sqlite3.connect(str(db_path))
    try:
        pecas = {}
        for d in PERIODOS:
            r = bs.listar_pecas(db, bench, dias=d or None)
            r.pop("invalidados", None)          # lista interna, sem uso na página publicada
            pecas[str(d)] = r
    finally:
        db.close()
    data = {
        "gerado_em": datetime.now().astimezone().isoformat(timespec="seconds"),
        "config": {k: cfg[k] for k in ("cap_fps_base", "expoente", "fps_referencia", "custos_fixos")},
        "pecas": pecas,
    }
    out.mkdir(parents=True, exist_ok=True)
    html = (HERE / "index.html").read_text(encoding="utf-8")
    marca = "const MODO_ESTATICO = false;"
    if marca not in html:
        raise SystemExit("index.html mudou: não achei a linha MODO_ESTATICO")
    (out / "index.html").write_text(html.replace(marca, "const MODO_ESTATICO = true;", 1), encoding="utf-8")
    (out / "data.json").write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    (out / ".nojekyll").write_text("", encoding="utf-8")
    return data


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default=str(HERE / "prices.db"))
    ap.add_argument("--out", default=str(HERE / "site"))
    a = ap.parse_args(argv)
    if not Path(a.db).exists():
        print(f"{a.db} não existe: rode o monitor antes (python price_watch.py run).", file=sys.stderr)
        return 1
    d = build(Path(a.db), Path(a.out))
    n = d["pecas"]["30"]
    print(f"Página gerada em {a.out}: {len(n['cpus'])} processadores, {len(n['rams'])} memórias, "
          f"{len(n['gpus'])} placas de vídeo.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
