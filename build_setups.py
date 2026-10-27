#!/usr/bin/env python3
"""Monta setups (CPU + GPU + RAM) com os melhores preços do prices.db e calcula o custo-benefício.

Uso:
    python build_setups.py                 # resumo no terminal
    python build_setups.py --fps 90        # outra meta de FPS
    python build_setups.py --json          # saída completa em JSON

Como calcula (resumo):
  1. Para cada modelo (7600, 9800X3D, RTX 5070 Ti...) pega o item MAIS BARATO que está em estoque,
     lido recentemente e não marcado como inválido (tabela price_flags).
  2. FPS do setup = o MENOR entre o FPS da placa de vídeo (na resolução) e o limite do processador,
     suavizado (média "power mean" com expoente 4), porque CPU e GPU se limitam mutuamente.
  3. Custo total = CPU + GPU + RAM + custos fixos estimados (placa-mãe, SSD, fonte, gabinete, cooler).
  4. Só concorrem os setups que atingem a meta de FPS. O ganhador é o de maior FPS por real.
  5. Além do ganhador, mostra os degraus da fronteira (mais barato que atende, ganhador, melhor
     desempenho...) com quanto custa cada FPS a mais.
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
RESOLUCOES = ["1080p", "1440p", "4k"]
PLATAFORMAS = ["AM4", "AM5"]


# --------------------------------------------------------------------------
# banco
# --------------------------------------------------------------------------
def ensure_tables(db: sqlite3.Connection) -> None:
    db.execute("CREATE TABLE IF NOT EXISTS price_flags(key TEXT, price REAL, motivo TEXT, ts TEXT, "
               "ativo INTEGER DEFAULT 1)")
    db.execute("CREATE INDEX IF NOT EXISTS idx_flags_key ON price_flags(key, ativo)")
    db.commit()


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def parse_ts(ts: str) -> datetime:
    d = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def flag_price(db: sqlite3.Connection, key: str, motivo: str) -> bool:
    """Marca o preço ATUAL do item como inválido (mudou ou esgotou). Sai do cálculo até o preço mudar."""
    ensure_tables(db)
    row = db.execute("SELECT price FROM prices WHERE key=? ORDER BY ts DESC, rowid DESC LIMIT 1", (key,)).fetchone()
    if not row:
        return False
    db.execute("UPDATE price_flags SET ativo=0 WHERE key=?", (key,))
    db.execute("INSERT INTO price_flags(key,price,motivo,ts,ativo) VALUES(?,?,?,?,1)",
               (key, row[0], motivo, utcnow().isoformat(timespec="seconds")))
    db.commit()
    return True


def unflag(db: sqlite3.Connection, key: str) -> None:
    ensure_tables(db)
    db.execute("UPDATE price_flags SET ativo=0 WHERE key=?", (key,))
    db.commit()


def load_offers(db: sqlite3.Connection, max_age_h: float, now: datetime | None = None):
    """Última leitura de cada item. Devolve (ofertas válidas, itens marcados como inválidos)."""
    ensure_tables(db)
    now = now or utcnow()
    flags = {k: p for k, p in db.execute("SELECT key, price FROM price_flags WHERE ativo=1")}
    rows = db.execute(
        "SELECT i.key, i.store, i.name, i.url, i.categoria, i.plataforma, p.price, p.available, p.ts "
        "FROM items i JOIN prices p ON p.key = i.key "
        "WHERE p.rowid = (SELECT MAX(rowid) FROM prices WHERE key = i.key)").fetchall()
    offers, flagged = [], []
    for key, store, name, url, cat, plat, price, avail, ts in rows:
        o = {"key": key, "loja": store, "nome": name, "link": url, "categoria": cat or "",
             "plataforma": plat or "", "preco": price, "disponivel": avail, "lido_em": ts}
        if key in flags and price is not None and abs(flags[key] - price) < 0.01:
            flagged.append(o)
            continue
        if price is None or avail == 0:
            continue
        try:
            if now - parse_ts(ts) > timedelta(hours=max_age_h):
                continue
        except ValueError:
            continue
        o["estoque_confirmado"] = avail == 1
        offers.append(o)
    return offers, flagged


# --------------------------------------------------------------------------
# cálculo
# --------------------------------------------------------------------------
def softmin(a: float, b: float, p: float) -> float:
    return (a ** -p + b ** -p) ** (-1.0 / p)


def cheapest(offers: list[dict], pattern: str, categoria: str, plataforma: str | None = None) -> list[dict]:
    rx = re.compile(pattern, re.I | re.S)
    out = [o for o in offers if o["categoria"] == categoria and rx.search(o["nome"])
           and (plataforma is None or o["plataforma"] in ("", plataforma))]
    return sorted(out, key=lambda o: o["preco"])


def part(model_id: str, model_name: str, o: dict, outras: list[dict]) -> dict:
    return {"modelo": model_id, "modelo_nome": model_name, "key": o["key"], "loja": o["loja"],
            "nome": o["nome"], "preco": round(o["preco"], 2), "link": o["link"],
            "estoque_confirmado": o["estoque_confirmado"], "lido_em": o["lido_em"],
            "outras_ofertas": [{"key": x["key"], "loja": x["loja"], "preco": round(x["preco"], 2),
                                "link": x["link"], "nome": x["nome"]} for x in outras[:3]]}


def compute(db: sqlite3.Connection, bench: dict, fps_alvo: float | None = None,
            now: datetime | None = None) -> dict:
    cfg = bench["config"]
    alvo = float(fps_alvo if fps_alvo else cfg.get("fps_alvo", 60))
    base, pexp = float(cfg["cap_fps_base"]), float(cfg.get("expoente", 4))
    offers, flagged = load_offers(db, float(cfg.get("idade_maxima_horas", 72)), now)

    cpus, gpus, faltando = {}, {}, []
    for cid, c in bench["cpus"].items():
        found = cheapest(offers, c["regex"], "processador", c["plataforma"])
        if found:
            cpus[cid] = part(cid, c["nome"], found[0], found[1:])
        else:
            faltando.append(c["nome"])
    for gid, g in bench["gpus"].items():
        found = cheapest(offers, g["regex"], "placa_de_video")
        if found:
            gpus[gid] = part(gid, g["nome"], found[0], found[1:])
        else:
            faltando.append(g["nome"])
    rams = {}
    for plat in PLATAFORMAS:
        found = sorted([o for o in offers if o["categoria"] == "memoria" and o["plataforma"] == plat],
                       key=lambda o: o["preco"])
        if found:
            rams[plat] = part("RAM32_" + plat, f"32GB {'DDR4' if plat == 'AM4' else 'DDR5'}",
                              found[0], found[1:])

    resultado = {"fps_alvo": alvo, "plataformas": {}, "faltando": faltando,
                 "invalidados": flagged, "referencia": cfg.get("referencia_nome", ""),
                 "gerado_em": datetime.now().astimezone().isoformat(timespec="seconds")}

    for plat in PLATAFORMAS:
        resultado["plataformas"][plat] = {}
        ram = rams.get(plat)
        plat_cpus = {cid: c for cid, c in cpus.items() if bench["cpus"][cid]["plataforma"] == plat}
        for res in RESOLUCOES:
            bloco = {"vencedor": None, "opcoes": [], "total_setups": 0, "atingem_meta": 0, "aviso": None}
            resultado["plataformas"][plat][res] = bloco
            if not ram:
                bloco["aviso"] = "sem memória RAM em estoque no banco para esta plataforma"
                continue
            if not plat_cpus or not gpus:
                bloco["aviso"] = "faltam processadores ou placas de vídeo com preço em estoque"
                continue
            setups = []
            for cid, c in plat_cpus.items():
                bc = bench["cpus"][cid]
                cap = base * bc["indice"] / 100.0
                for gid, g in gpus.items():
                    bg = bench["gpus"][gid]
                    gfps = bg["fps"][res]
                    fps = softmin(gfps, cap, pexp)
                    fixos = dict(cfg["custos_fixos"][plat])
                    if bc.get("cooler_incluso"):
                        fixos["cooler"] = 0
                    if bg.get("fonte_extra"):
                        fixos["fonte"] = fixos.get("fonte", 0) + bg["fonte_extra"]
                    custo_fixo = sum(fixos.values())
                    total = c["preco"] + g["preco"] + ram["preco"] + custo_fixo
                    limite = "CPU" if cap < gfps * 0.92 else ("GPU" if gfps < cap * 0.92 else "equilibrado")
                    ref = cfg["fps_referencia"][res]
                    setups.append({
                        "plataforma": plat, "resolucao": res, "cpu": c, "gpu": g, "ram": ram,
                        "fixos": fixos, "custo_fixo": round(custo_fixo, 2), "total": round(total, 2),
                        "fps": round(fps, 1), "nota_pct": round(100 * fps / ref, 1),
                        "fps_por_mil_reais": round(1000 * fps / total, 2), "limitado_por": limite,
                        "vram": bg["vram"]})
            bloco["total_setups"] = len(setups)
            qual = [s for s in setups if s["fps"] >= alvo]
            bloco["atingem_meta"] = len(qual)
            pool = qual
            if not qual:
                bloco["aviso"] = (f"nenhum setup chega a {alvo:g} FPS em {res}; "
                                  "mostrando o melhor custo-benefício geral")
                pool = setups
            winner = max(pool, key=lambda s: (s["fps_por_mil_reais"], -s["total"]))
            bloco["vencedor"] = winner
            bloco["opcoes"] = pick_options(pool, winner)
    resultado["comparacao"] = comparar(resultado)
    resultado["ranking"] = ranking(bench, cpus, gpus, cfg)
    return resultado


def pick_options(pool: list[dict], winner: dict) -> list[dict]:
    """Degraus da fronteira (mais barato -> mais forte): nenhum setup é ao mesmo tempo mais caro e mais fraco."""
    ordered = sorted(pool, key=lambda s: (s["total"], -s["fps"]))
    frontier, best = [], -1.0
    for s in ordered:
        if s["fps"] > best + 1e-9:
            frontier.append(s)
            best = s["fps"]
    chosen = {id(frontier[0]): frontier[0], id(frontier[-1]): frontier[-1]}
    wf = next((s for s in frontier if s["cpu"]["modelo"] == winner["cpu"]["modelo"]
               and s["gpu"]["modelo"] == winner["gpu"]["modelo"]), winner)
    chosen[id(wf)] = wf
    if len(frontier) > len(chosen):                  # um degrau acima do ganhador (se houver)
        i = frontier.index(wf)
        if i + 1 < len(frontier):
            chosen[id(frontier[i + 1])] = frontier[i + 1]
    if len(frontier) > len(chosen) and len(chosen) < 4:   # e um abaixo
        i = frontier.index(wf)
        if i - 1 >= 0:
            chosen[id(frontier[i - 1])] = frontier[i - 1]
    opcoes = sorted(chosen.values(), key=lambda s: s["total"])
    out, prev = [], None
    for s in opcoes:
        item = dict(s)
        item["eh_vencedor"] = s is wf or s is winner
        if prev:
            dc, df = s["total"] - prev["total"], s["fps"] - prev["fps"]
            item["vs_anterior"] = {"custo": round(dc, 2), "fps": round(df, 1),
                                   "fps_pct": round(100 * df / prev["fps"], 1) if prev["fps"] else None,
                                   "reais_por_fps": round(dc / df, 1) if df > 0.05 else None}
        out.append(item)
        prev = s
    return out


def comparar(res: dict) -> dict:
    out = {}
    for r in RESOLUCOES:
        a, b = (res["plataformas"][p][r]["vencedor"] for p in PLATAFORMAS)
        if a and b:
            out[r] = {"AM4": brief(a), "AM5": brief(b),
                      "diferenca_custo": round(b["total"] - a["total"], 2),
                      "diferenca_fps_pct": round(100 * (b["fps"] - a["fps"]) / a["fps"], 1)}
    return out


def brief(s: dict) -> dict:
    return {"cpu": s["cpu"]["modelo_nome"], "gpu": s["gpu"]["modelo_nome"], "total": s["total"],
            "fps": s["fps"], "nota_pct": s["nota_pct"], "fps_por_mil_reais": s["fps_por_mil_reais"]}


def ranking(bench: dict, cpus: dict, gpus: dict, cfg: dict) -> dict:
    g_rows = []
    for gid, g in gpus.items():
        fps = bench["gpus"][gid]["fps"]
        g_rows.append({**{k: g[k] for k in ("modelo_nome", "loja", "nome", "preco", "link", "key")},
                       "vram": bench["gpus"][gid]["vram"], "fps": fps,
                       "fps_por_mil_reais": {r: round(1000 * fps[r] / g["preco"], 2) for r in RESOLUCOES}})
    g_rows.sort(key=lambda x: -x["fps"]["1440p"])
    c_rows = []
    for cid, c in cpus.items():
        c_rows.append({**{k: c[k] for k in ("modelo_nome", "loja", "nome", "preco", "link", "key")},
                       "plataforma": bench["cpus"][cid]["plataforma"], "indice": bench["cpus"][cid]["indice"],
                       "indice_por_mil_reais": round(1000 * bench["cpus"][cid]["indice"] / c["preco"], 2)})
    c_rows.sort(key=lambda x: -x["indice"])
    return {"gpus": g_rows, "cpus": c_rows}


# --------------------------------------------------------------------------
# lista de peças por loja e soma da seleção (usado pela página)
# --------------------------------------------------------------------------
def classify(offers: list[dict], bench: dict) -> dict[str, dict]:
    """Associa cada oferta a um modelo conhecido. Devolve {key: oferta + tipo/modelo/notas}."""
    out = {}
    for o in offers:
        base = {k: o[k] for k in ("key", "loja", "nome", "link", "preco", "estoque_confirmado", "lido_em")}
        if o["categoria"] == "processador":
            for cid, c in bench["cpus"].items():
                if c["plataforma"] in ("", o["plataforma"] or c["plataforma"]) and \
                        re.search(c["regex"], o["nome"], re.I | re.S):
                    out[o["key"]] = {**base, "tipo": "cpu", "modelo": cid, "modelo_nome": c["nome"],
                                     "plataforma": c["plataforma"], "indice": c["indice"],
                                     "cooler_incluso": bool(c.get("cooler_incluso"))}
                    break
        elif o["categoria"] == "placa_de_video":
            for gid, g in bench["gpus"].items():
                if re.search(g["regex"], o["nome"], re.I | re.S):
                    out[o["key"]] = {**base, "tipo": "gpu", "modelo": gid, "modelo_nome": g["nome"],
                                     "vram": g["vram"], "fps": g["fps"]}
                    break
        elif o["categoria"] == "memoria" and o["plataforma"] in PLATAFORMAS:
            out[o["key"]] = {**base, "tipo": "ram", "modelo": "RAM_" + o["plataforma"],
                             "modelo_nome": "32GB " + ("DDR4" if o["plataforma"] == "AM4" else "DDR5"),
                             "plataforma": o["plataforma"]}
    return out


def faixa_preco(atual: float, mn: float, mx: float) -> int:
    """Onde o preço atual está entre o mínimo e o máximo do período: de -3 (bem barato, verde forte)
    a +3 (bem caro, vermelho forte); 0 = no meio ou variação pequena demais (menos de 3%)."""
    if mn <= 0 or mx / mn - 1 < 0.03:
        return 0
    pos = (atual - mn) / (mx - mn)
    for limite, f in ((0.10, -3), (0.25, -2), (0.40, -1)):
        if pos <= limite:
            return f
    for limite, f in ((0.90, 3), (0.75, 2), (0.60, 1)):
        if pos >= limite:
            return f
    return 0


def historico(db: sqlite3.Connection, precos_atuais: dict[str, float], dias: int | None = 30,
              now: datetime | None = None) -> dict[str, dict]:
    """Mín/máx e selo de queda por item, a partir das leituras guardadas.

    - Só entram leituras com o item em estoque (ou estoque desconhecido) dentro da janela.
    - Um preço só conta se durou 2 leituras seguidas ou se é o preço atual: uma leitura isolada
      que some na rodada seguinte costuma ser erro da loja/listagem, não promoção.
    - Sem pelo menos 2 preços diferentes ainda não há o que comparar (min/max = None).
    Selo: queda/alta recente (preço mudou nas últimas 48h) ou, senão, posição frente ao
    máximo/mínimo do período."""
    now = now or utcnow()
    corte = now - timedelta(days=dias) if dias else None
    ks = set(precos_atuais)
    leituras: dict[str, list[tuple[datetime, float]]] = {}
    for key, ts, price, avail in db.execute(
            "SELECT key, ts, price, available FROM prices WHERE price IS NOT NULL ORDER BY key, rowid"):
        if key not in ks or avail == 0:
            continue
        try:
            t = parse_ts(ts)
        except ValueError:
            continue
        if corte and t < corte:
            continue
        leituras.setdefault(key, []).append((t, price))
    out: dict[str, dict] = {}
    for key, pts in leituras.items():
        atual = precos_atuais[key]
        # corridas de leituras com o mesmo preço: [preço, quantidade, primeira leitura]
        runs: list[list] = []
        for t, pr in pts:
            if runs and abs(runs[-1][0] - pr) < 0.005:
                runs[-1][1] += 1
            else:
                runs.append([pr, 1, t])
        ultimo = len(runs) - 1
        runs = [r for i, r in enumerate(runs) if r[1] >= 2 or i == ultimo]
        fundidas: list[list] = []                        # junta vizinhas iguais após tirar ruído
        for r in runs:
            if fundidas and abs(fundidas[-1][0] - r[0]) < 0.005:
                fundidas[-1][1] += r[1]
            else:
                fundidas.append(list(r))
        precos = [r[0] for r in fundidas] + [atual]
        if len(fundidas) < 2:
            out[key] = {"min": None, "max": None, "leituras": len(pts), "selo": None, "var_pct": None, "faixa": 0}
            continue
        mn, mx = min(precos), max(precos)
        antes, mudou_em = fundidas[-2][0], fundidas[-1][2]
        recente = (now - mudou_em) <= timedelta(hours=48)
        selo = None
        if recente and antes > atual * 1.02:
            selo = {"tipo": "queda", "pct": round((atual - antes) / antes * 100, 1), "ref": antes, "base": "antes"}
        elif recente and antes < atual * 0.98:
            selo = {"tipo": "alta", "pct": round((atual - antes) / antes * 100, 1), "ref": antes, "base": "antes"}
        elif atual <= mx * 0.98:
            selo = {"tipo": "queda", "pct": round((atual - mx) / mx * 100, 1), "ref": mx, "base": "máx"}
        elif atual >= mn * 1.10:
            selo = {"tipo": "alta", "pct": round((atual - mn) / mn * 100, 1), "ref": mn, "base": "mín"}
        out[key] = {"min": round(mn, 2), "max": round(mx, 2), "leituras": len(pts), "selo": selo,
                    "var_pct": round((atual - mx) / mx * 100, 1), "faixa": faixa_preco(atual, mn, mx)}
    return out


def listar_pecas(db: sqlite3.Connection, bench: dict, now: datetime | None = None,
                 dias: int | None = 30) -> dict:
    cfg = bench["config"]
    offers, flagged = load_offers(db, float(cfg.get("idade_maxima_horas", 72)), now)
    cls = classify(offers, bench)
    hist = historico(db, {k: v["preco"] for k, v in cls.items()}, dias, now)
    for k, v in cls.items():
        v["hist"] = hist.get(k, {"min": None, "max": None, "leituras": 0, "selo": None, "var_pct": None, "faixa": 0})
    cpus = sorted((v for v in cls.values() if v["tipo"] == "cpu"),
                  key=lambda v: (v["plataforma"], -v["indice"], v["preco"]))
    rams = sorted((v for v in cls.values() if v["tipo"] == "ram"), key=lambda v: (v["plataforma"], v["preco"]))
    gpus = sorted((v for v in cls.values() if v["tipo"] == "gpu"),
                  key=lambda v: (-v["fps"]["1440p"], v["preco"]))
    com_oferta = {v["modelo"] for v in cls.values()}
    faltando = [c["nome"] for k, c in {**bench["cpus"], **bench["gpus"]}.items() if k not in com_oferta]
    return {"cpus": cpus, "rams": rams, "gpus": gpus, "faltando": faltando, "invalidados": flagged, "dias": dias,
            "referencia": cfg.get("referencia_nome", ""), "gerado_em": datetime.now().astimezone().isoformat(timespec="seconds")}


def avaliar(db: sqlite3.Connection, bench: dict, cpu_key: str | None, ram_key: str | None,
            gpu_key: str | None, now: datetime | None = None) -> dict:
    """Soma dos preços das peças escolhidas + FPS estimado por resolução (sem custos fixos)."""
    cfg = bench["config"]
    offers, _ = load_offers(db, float(cfg.get("idade_maxima_horas", 72)), now)
    cls = classify(offers, bench)
    cpu, ram, gpu = (cls.get(k) if k else None for k in (cpu_key, ram_key, gpu_key))
    itens = [x for x in (cpu, ram, gpu) if x]
    avisos = []
    for nome, k, x in (("processador", cpu_key, cpu), ("memória", ram_key, ram), ("placa de vídeo", gpu_key, gpu)):
        if k and not x:
            avisos.append(f"o item escolhido ({nome}) não está mais disponível no banco")
    if cpu and ram and cpu["plataforma"] != ram["plataforma"]:
        avisos.append(f"memória {ram['modelo_nome']} não serve no processador {cpu['plataforma']}")
    res = {"total": round(sum(x["preco"] for x in itens), 2), "itens": itens, "avisos": avisos,
           "fps": None, "nota_pct": None, "plataforma": None, "outras": None, "outras_total": 0,
           "completo": None, "sem_cooler": False}
    plat = (cpu or ram or {}).get("plataforma")
    if plat in cfg["custos_fixos"] and (cpu or ram):
        outras = {k: v for k, v in cfg["custos_fixos"][plat].items() if k != "cooler"}
        res.update(plataforma=plat, outras=outras, outras_total=round(sum(outras.values()), 2),
                   completo=round(res["total"] + sum(outras.values()), 2),
                   sem_cooler=bool(cpu and not cpu.get("cooler_incluso")))
    if cpu and gpu:
        base, pexp = float(cfg["cap_fps_base"]), float(cfg.get("expoente", 4))
        cap = base * cpu["indice"] / 100.0
        fps = {r: round(softmin(gpu["fps"][r], cap, pexp), 1) for r in RESOLUCOES}
        res["fps"] = fps
        res["nota_pct"] = {r: round(100 * fps[r] / cfg["fps_referencia"][r], 1) for r in RESOLUCOES}
    return res


# --------------------------------------------------------------------------
# entrada/saída
# --------------------------------------------------------------------------
def load_bench(path: Path | None = None) -> dict:
    return json.loads((path or HERE / "benchmarks.json").read_text(encoding="utf-8"))


def brl(v: float) -> str:
    return "R$ " + f"{v:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def print_report(res: dict) -> None:
    print(f"Meta: {res['fps_alvo']:g} FPS (média em jogos, ultra). Nota = % de {res['referencia']}.")
    for plat in PLATAFORMAS:
        for r in RESOLUCOES:
            b = res["plataformas"][plat][r]
            print(f"\n== {plat} @ {r} ==")
            if b["aviso"]:
                print("  aviso:", b["aviso"])
            w = b["vencedor"]
            if not w:
                continue
            print(f"  GANHADOR: {w['cpu']['modelo_nome']} + {w['gpu']['modelo_nome']} + RAM 32GB"
                  f" = {brl(w['total'])} | {w['fps']} FPS (nota {w['nota_pct']}%) | "
                  f"{w['fps_por_mil_reais']} FPS por R$ 1.000 | limitado por {w['limitado_por']}")
            for o in b["opcoes"]:
                tag = "*" if o["eh_vencedor"] else " "
                extra = ""
                if o.get("vs_anterior"):
                    v = o["vs_anterior"]
                    extra = f"  (+{brl(v['custo'])} -> {v['fps']:+} FPS"
                    extra += f", {brl(v['reais_por_fps'])} por FPS)" if v["reais_por_fps"] else ")"
                print(f"  {tag} {o['cpu']['modelo_nome']:<16} + {o['gpu']['modelo_nome']:<18} "
                      f"{brl(o['total']):>14}  {o['fps']:>6} FPS{extra}")
    if res["comparacao"]:
        print("\n== AM4 x AM5 (ganhadores) ==")
        for r, c in res["comparacao"].items():
            print(f"  {r}: AM5 custa {brl(c['diferenca_custo'])} a mais e rende {c['diferenca_fps_pct']:+}% FPS")
    if res["faltando"]:
        print("\nSem preço em estoque no banco:", ", ".join(res["faltando"]))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default=str(HERE / "prices.db"))
    ap.add_argument("--fps", type=float, help="meta de FPS (padrão: benchmarks.json)")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    if not Path(a.db).exists():
        print(f"Banco não encontrado: {a.db}. Rode primeiro: python price_watch.py run", file=sys.stderr)
        return 1
    db = sqlite3.connect(a.db, timeout=30)
    res = compute(db, load_bench(), a.fps)
    if a.json:
        print(json.dumps(res, ensure_ascii=False, indent=2))
    else:
        print_report(res)
    return 0


if __name__ == "__main__":
    sys.exit(main())
