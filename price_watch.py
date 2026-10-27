#!/usr/bin/env python3
"""price_watch.py - monitor de preços e promoções (Pichau, KaBuM, Terabyte, ...).

Uso rápido:
    python price_watch.py test-url <URL>     # confere se a extração funciona numa página
    python price_watch.py test-notify        # manda uma notificação de teste
    python price_watch.py run --dry-run      # roda tudo sem notificar
    python price_watch.py run                # roda de verdade (use no agendador)
    python price_watch.py history            # resumo do histórico gravado

Só depende de `requests` (pip install requests).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import re
import sqlite3
import sys
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import median
from typing import Any, Iterator
from urllib.parse import parse_qsl, quote_plus, urlencode, urljoin, urlparse, urlunparse

import requests

HERE = Path(__file__).resolve().parent

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.6",
    # sem "br": o requests não decodifica brotli sem pacote extra
    "Accept-Encoding": "gzip, deflate",
}

STORE_NAMES = {
    "kabum.com.br": "KaBuM",
    "pichau.com.br": "Pichau",
    "terabyteshop.com.br": "Terabyte",
    "mercadolivre.com.br": "MercadoLivre",
}

# URLs de busca padrão. São palpites razoáveis: confirme no navegador e, se a
# loja usar outro formato, sobrescreva em "search_urls" no watchlist.json.
# Placeholders: {q} = "rx+9060+xt"   {q_dash} = "rx-9060-xt"
STORE_SEARCH = {
    "kabum": "https://www.kabum.com.br/busca/{q_dash}",
    "pichau": "https://www.pichau.com.br/search?q={q}",
    "terabyte": "https://www.terabyteshop.com.br/busca?str={q}",
    "mercadolivre": "https://lista.mercadolivre.com.br/{q_dash}",
}

PRICE_KEYS = (
    "priceWithDiscount", "price_with_discount", "pixPrice", "cashPrice",
    "finalPrice", "salePrice", "specialPrice", "price",
)
NAME_KEYS = ("name", "title", "nome")
URL_KEYS = ("url", "link", "href", "friendlyUrl", "slug", "path")
# Sinais de estoque em JSON de loja (cada loja usa um nome diferente).
# Ordem de confiança: booleano direto > booleano invertido > texto > quantidade.
STOCK_BOOL_KEYS = ("available", "isAvailable", "inStock", "in_stock", "hasStock",
                   "hasStockAvailable", "purchasable", "isBuyable", "canBuy", "isSellable")
STOCK_INV_KEYS = ("outOfStock", "isOutOfStock", "soldOut", "isSoldOut", "unavailable",
                  "isUnavailable", "esgotado")
STOCK_TEXT_KEYS = ("availability", "stockStatus", "stock_status")
STOCK_QTY_KEYS = ("stockQuantity", "availableQuantity", "quantityAvailable", "stock", "estoque",
                  "quantity", "qty")

OUT_OF_STOCK_WORDS = ("outofstock", "out_of_stock", "soldout", "sold_out", "discontinued",
                      "unavailable", "indispon", "esgotad", "sem estoque")

# Texto visível da página de produto. Só é usado quando NÃO há sinal estruturado.
TEXT_OUT_RE = re.compile(
    r"produto\s+indispon[ií]vel|temporariamente\s+indispon[ií]vel|\besgotado\b|"
    r"sem\s+estoque|fora\s+de\s+estoque|avise[-\s]?me\s+quando", re.I)


# --------------------------------------------------------------------------
# utilidades
# --------------------------------------------------------------------------
def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def brl(x: float) -> str:
    return "R$ " + f"{x:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def load_dotenv(path: Path) -> None:
    """Carrega KEY=VALUE de um .env simples (sem depender de pacote)."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip("'\""))


def store_of(url: str) -> str:
    host = (urlparse(url).hostname or "").lower().removeprefix("www.")
    for dom, name in STORE_NAMES.items():
        if host.endswith(dom):
            return name
    return host or "?"


def parse_price(v: Any) -> float | None:
    """Aceita 549.95, "549.95", "R$ 1.234,56", "1.234", 1234 ..."""
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        p = float(v)
    else:
        s = str(v).replace("\xa0", " ")
        m = re.search(r"\d[\d.,]*", s)
        if not m:
            return None
        t = m.group(0).rstrip(".,")
        if "," in t:                       # formato BR: 1.234,56
            t = t.replace(".", "").replace(",", ".")
        elif t.count(".") > 1:             # 1.234.567
            t = t.replace(".", "")
        elif re.fullmatch(r"\d{1,3}\.\d{3}", t):   # "1.234" -> milhar
            t = t.replace(".", "")
        try:
            p = float(t)
        except ValueError:
            return None
    return p if 1 <= p <= 1_000_000 else None


def stock_tag(av: bool | None) -> str:
    return "em estoque" if av is True else "ESGOTADO" if av is False else "estoque ?"


def stock_from_text(s: Any) -> bool | None:
    """True/False conforme o texto de disponibilidade; None se não há informação."""
    if s is None or str(s).strip() == "":
        return None
    low = str(s).lower()
    return not any(w in low for w in OUT_OF_STOCK_WORDS)


def _as_bool(v: Any) -> bool | None:
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)) and v in (0, 1):
        return bool(v)
    if isinstance(v, str) and v.strip().lower() in ("true", "false"):
        return v.strip().lower() == "true"
    return None


def stock_from_dict(d: dict, prefix: str) -> tuple[bool | None, str]:
    """Procura sinais de estoque num dict de produto: (estado, de onde veio)."""
    for k in STOCK_BOOL_KEYS:
        b = _as_bool(d.get(k))
        if b is not None:
            return b, f"{prefix}:{k}={d[k]}"
    for k in STOCK_INV_KEYS:
        b = _as_bool(d.get(k))
        if b is not None:
            return (not b), f"{prefix}:{k}={d[k]}"
    for k in STOCK_TEXT_KEYS:
        if isinstance(d.get(k), str):
            st = stock_from_text(d[k])
            if st is not None:
                return st, f"{prefix}:{k}={d[k]}"
    for k in STOCK_QTY_KEYS:
        v = d.get(k)
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            return v > 0, f"{prefix}:{k}={v}"
    return None, f"{prefix}:sem-sinal-de-estoque"


def text_stock(html: str) -> tuple[bool | None, str]:
    """Último recurso: procura 'esgotado/indisponível' no texto visível da página."""
    visible = re.sub(r"<(script|style)\b.*?</\1>", " ", html, flags=re.S | re.I)
    visible = re.sub(r"<[^>]+>", " ", visible)
    m = TEXT_OUT_RE.search(visible)
    if m:
        return False, f"texto:'{re.sub(chr(32) + '+', chr(32), m.group(0).lower())}'"
    return None, "sem-sinal-de-estoque"


IMG_URL_RE = re.compile(r"\.(jpe?g|png|webp|gif|svg)(\?|$)|/(fotos|images?|img|media|static)/", re.I)

# Quando o JSON da loja não traz o link pronto, monta a partir de código + nome.
# {slug} = nome do produto em minúsculas, sem acento, separado por hífen.
STORE_PRODUCT_URL = {
    "kabum.com.br": "/produto/{code}/{slug}",
}


def slugify(text: str) -> str:
    import unicodedata
    t = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii").lower()
    return re.sub(r"-{2,}", "-", re.sub(r"[^a-z0-9]+", "-", t)).strip("-")


def find_product_url(d: dict) -> str | None:
    """Link do produto dentro de um dict de JSON de loja (ou None)."""
    for k in URL_KEYS:                     # campos usuais; slug puro (sem "/") não é link
        v = d.get(k)
        if isinstance(v, str) and "/" in v and not IMG_URL_RE.search(v):
            return v.strip()
    for v in d.values():                   # qualquer texto que pareça link de produto
        if isinstance(v, str) and not IMG_URL_RE.search(v) and (
                re.match(r"^https?://\S+$", v) or re.match(r"^/(produto|product|p)/\S+", v)):
            return v.strip()
    return None


def walk(obj: Any) -> Iterator[dict]:
    """Percorre JSON aninhado e devolve todos os dicts."""
    stack = [obj]
    while stack:
        cur = stack.pop()
        if isinstance(cur, dict):
            yield cur
            stack.extend(reversed(list(cur.values())))
        elif isinstance(cur, list):
            stack.extend(reversed(cur))


# --------------------------------------------------------------------------
# extração de preços do HTML
# --------------------------------------------------------------------------
LD_RE = re.compile(
    r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>', re.S | re.I
)
NEXT_RE = re.compile(r'<script[^>]+id=["\']__NEXT_DATA__["\'][^>]*>(.*?)</script>', re.S | re.I)
META_RE = re.compile(r"<meta\b[^>]*>", re.I)


def _types(d: dict) -> list[str]:
    t = d.get("@type")
    return [t] if isinstance(t, str) else [x for x in (t or []) if isinstance(x, str)]


def _offer_info(offers: Any) -> tuple[float | None, bool | None, str]:
    """(menor preço em estoque, estoque True/False/None, sinal usado)"""
    if isinstance(offers, dict):
        offers = [offers]
    rows = []
    for o in offers or []:
        if not isinstance(o, dict):
            continue
        p = parse_price(o.get("price") if o.get("price") is not None else o.get("lowPrice"))
        if p is None:
            continue
        av = o.get("availability")
        rows.append((p, stock_from_text(av), str(av).rsplit("/", 1)[-1] if av else ""))
    if not rows:
        return None, None, "json-ld:sem-oferta"
    in_stock = [r for r in rows if r[1] is True]
    if in_stock:
        return min(r[0] for r in in_stock), True, f"json-ld:availability={in_stock[0][2]}"
    sold_out = [r for r in rows if r[1] is False]
    if sold_out:   # tudo esgotado: guarda o preço para registro, mas marca como esgotado
        return min(r[0] for r in sold_out), False, f"json-ld:availability={sold_out[0][2]}"
    return min(r[0] for r in rows), None, "json-ld:sem-availability"


def _ld_url(d: dict) -> str | None:
    """Link do produto num bloco JSON-LD: url, @id ou url da oferta."""
    offers = d.get("offers")
    offer_list = offers if isinstance(offers, list) else [offers]
    cands = [d.get("url"), d.get("@id")] + [o.get("url") for o in offer_list if isinstance(o, dict)]
    for c in cands:
        if isinstance(c, str) and "/" in c and not IMG_URL_RE.search(c):
            return c.strip()
    return None


def ld_products(html: str) -> list[dict]:
    out = []
    for m in LD_RE.finditer(html):
        try:
            data = json.loads(m.group(1).strip())
        except (ValueError, TypeError):
            continue
        for d in walk(data):
            if "Product" in _types(d) and d.get("offers") is not None:
                price, ok, sig = _offer_info(d.get("offers"))
                if price is not None:
                    out.append({
                        "name": str(d.get("name") or "").strip(),
                        "price": price,
                        "available": ok,
                        "stock_signal": sig,
                        "url": _ld_url(d),
                    })
    return out


def next_candidates(html: str) -> list[dict]:
    """Procura, no JSON do Next.js, dicts que parecem um produto (nome + preço)."""
    m = NEXT_RE.search(html)
    if not m:
        return []
    try:
        data = json.loads(m.group(1))
    except ValueError:
        return []
    out, seen = [], set()
    for d in walk(data):
        name = next((d[k] for k in NAME_KEYS if isinstance(d.get(k), str) and len(d[k]) > 5), None)
        if not name:
            continue
        price = None
        for k in PRICE_KEYS:
            price = parse_price(d.get(k))
            if price is not None:
                break
        if price is None:
            continue
        url = find_product_url(d)
        avail, stock_sig = stock_from_dict(d, "next")
        sig = (name, price)
        if sig in seen:
            continue
        seen.add(sig)
        slug = next((d[k] for k in ("friendlyName", "friendly_name", "slug")
                     if isinstance(d.get(k), str) and d[k] and "/" not in d[k]), None)
        out.append({"name": name.strip(), "price": price, "available": avail,
                    "stock_signal": stock_sig, "url": url,
                    "code": d.get("code") if isinstance(d.get("code"), (int, str)) else None,
                    "slug": slug, "_keys": sorted(d.keys())})
    return out


def meta_price(html: str) -> dict | None:
    price, name = None, None
    for tag in META_RE.findall(html):
        low = tag.lower()
        content = re.search(r'content=["\']([^"\']*)["\']', tag, re.I)
        if not content:
            continue
        if any(k in low for k in ('product:price:amount', 'og:price:amount', 'itemprop="price"', "itemprop='price'")):
            price = price or parse_price(content.group(1))
        if 'property="og:title"' in low or "property='og:title'" in low:
            name = content.group(1)
    return ({"name": name or "", "price": price, "available": None,
             "stock_signal": "meta:sem-sinal-de-estoque", "url": None} if price else None)


def extract_product(html: str, custom_regex: str | None = None) -> tuple[dict | None, str]:
    """Página de UM produto. Devolve (dados, método usado)."""
    data, method = None, "nenhum"
    if custom_regex:
        m = re.search(custom_regex, html, re.S | re.I)
        if m:
            p = parse_price(m.group(1) if m.groups() else m.group(0))
            if p:
                title = re.search(r"<title[^>]*>(.*?)</title>", html, re.S | re.I)
                data = {"name": (title.group(1).strip() if title else ""), "price": p,
                        "available": None, "stock_signal": "regex:sem-sinal-de-estoque",
                        "url": None}
                method = "regex"
    if data is None:
        for finder, label in ((ld_products, "json-ld"), (next_candidates, "next-data")):
            found = finder(html)
            if found:
                data, method = found[0], label
                break
    if data is None:
        mp = meta_price(html)
        if mp:
            data, method = mp, "meta"
    if data is not None and data.get("available") is None:
        # sem sinal estruturado: tenta o texto da página ("Produto indisponível" etc.)
        av, sig = text_stock(html)
        if av is not None:
            data["available"], data["stock_signal"] = av, sig
    return data, method


ANCHOR_RE = re.compile(r"""href=["']((?:https?://[^"'\s]+)?/(?:produto|product|p)/[^"'\s#?]+)""", re.I)


def _slug_tokens(s: str) -> set[str]:
    return {t for t in slugify(s).split("-") if t}


def link_from_anchors(html: str, base_url: str, items: list[dict]) -> int:
    """Preenche o link dos itens sem url usando os <a href> da própria página.

    Casa pelo nome: slug idêntico, ou (senão) o link cujo slug mais se parece com o
    nome, desde que a semelhança seja alta e sem empate. Devolve quantos preencheu.
    """
    links = {}
    for m in ANCHOR_RE.finditer(html):
        href = m.group(1)
        links.setdefault(href.rstrip("/"), href)
    if not links:
        return 0
    by_slug = {}
    for href in links:
        by_slug.setdefault(href.rsplit("/", 1)[-1].lower(), href)
    used, filled = set(), 0
    for it in items:
        if it.get("url") or not it.get("name"):
            continue
        slug = slugify(it["name"])
        href = by_slug.get(slug)
        if not href:
            toks = _slug_tokens(it["name"])
            scored = []
            for s, h in by_slug.items():
                st = set(s.split("-"))
                if toks and st:
                    scored.append((len(toks & st) / len(toks | st), h))
            scored.sort(reverse=True)
            if scored and scored[0][0] >= 0.7 and (len(scored) == 1 or scored[0][0] - scored[1][0] >= 0.1):
                href = scored[0][1]
        if href and href not in used:
            it["url"] = urljoin(base_url, href)
            used.add(href)
            filled += 1
    return filled


def extract_listing(html: str, base_url: str) -> tuple[list[dict], str]:
    """Página de busca/categoria: lista de produtos."""
    items, method = [], "nenhum"
    lds = ld_products(html)
    nxt = next_candidates(html)
    if len(lds) >= 2:
        items, method = lds, "json-ld"
    elif nxt:
        items, method = nxt, "next-data"
    elif lds:
        items, method = lds, "json-ld"
    host = (urlparse(base_url).hostname or "").lower().removeprefix("www.")
    template = next((t for dom, t in STORE_PRODUCT_URL.items() if host.endswith(dom)), None)
    for it in items:
        if not it.get("url") and template and it.get("code"):
            it["url"] = template.format(code=it["code"], slug=it.get("slug") or slugify(it["name"]))
        if it.get("url"):
            it["url"] = clean_url(urljoin(base_url, it["url"]))
    link_from_anchors(html, base_url, items)
    for it in items:
        if it.get("url"):
            it["url"] = clean_url(it["url"])
    return items, method


# --------------------------------------------------------------------------
# rede
# --------------------------------------------------------------------------
class Blocked(Exception):
    """Loja recusou (403/429/503/desafio). Não insistimos nem contornamos."""


class FetchError(Exception):
    pass


CHALLENGE_HINTS = ("just a moment", "cf-challenge", "captcha", "access denied", "px-captcha")


def fetch(session: requests.Session, url: str, timeout: int = 25, retries: int = 2) -> str:
    last: Exception | None = None
    for i in range(retries + 1):
        try:
            r = session.get(url, headers=HEADERS, timeout=timeout)
        except requests.RequestException as e:
            last = e
            time.sleep(2 * (i + 1))
            continue
        if r.status_code in (403, 429, 503):
            raise Blocked(f"HTTP {r.status_code}")
        if r.status_code == 404:
            raise FetchError("HTTP 404 (URL mudou ou produto saiu do ar)")
        if r.status_code >= 500:
            last = FetchError(f"HTTP {r.status_code}")
            time.sleep(2 * (i + 1))
            continue
        r.raise_for_status()
        return r.text
    raise FetchError(str(last))


def looks_like_challenge(html: str) -> bool:
    """Desafio anti-bot: página curta com as palavras típicas. Páginas grandes de loja
    citam 'captcha' em scripts normais (reCAPTCHA), então não contam."""
    if len(html) > 80_000:
        return False
    head = html[:6000].lower()
    if any(h in head for h in CHALLENGE_HINTS):
        return True
    low = html.lower()   # Mercado Livre: muro de login ("acesse sua conta") para tráfego automatizado
    return "negative_traffic" in low or ("para continuar, acesse sua conta" in low and "<h1" not in low)


TRACKING_PARAMS = {"srsltid", "gclid", "gbraid", "wbraid", "gad_source", "gad_campaignid",
                   "fbclid", "msclkid", "ref", "ref_", "tag", "aff", "afiliado", "partner"}


def clean_url(url: str) -> str:
    """Tira parâmetros de rastreamento (srsltid, utm_*, gclid...) para o mesmo produto
    ter sempre a mesma chave no histórico."""
    p = urlparse(url)
    if not p.query:
        return url
    keep = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True)
            if k.lower() not in TRACKING_PARAMS and not k.lower().startswith("utm_")]
    return urlunparse(p._replace(query=urlencode(keep)))


# --------------------------------------------------------------------------
# banco de dados
# --------------------------------------------------------------------------
CATEGORIAS = ("processador", "placa_mae", "memoria", "placa_de_video", "ssd")

TABLES = """
CREATE TABLE IF NOT EXISTS items(key TEXT PRIMARY KEY, store TEXT, name TEXT, url TEXT, source TEXT,
                                 categoria TEXT, plataforma TEXT);
CREATE TABLE IF NOT EXISTS prices(key TEXT, ts TEXT, price REAL, available INTEGER);
CREATE INDEX IF NOT EXISTS idx_prices_key_ts ON prices(key, ts);
CREATE TABLE IF NOT EXISTS alerts(key TEXT, ts TEXT, price REAL, reason TEXT);
CREATE INDEX IF NOT EXISTS idx_alerts_key_ts ON alerts(key, ts);
CREATE TABLE IF NOT EXISTS health(store TEXT PRIMARY KEY, fails INTEGER, notified INTEGER, last_ok TEXT);
"""

_ESTOQUE_SQL = ("CASE WHEN p.available = 1 THEN 'em estoque' "
                "WHEN p.available = 0 THEN 'ESGOTADO' ELSE 'estoque ?' END")


def _views_sql() -> str:
    """Visões para abrir no cliente de banco (uma "tabela" por tipo de peça)."""
    cols = (f"i.categoria AS categoria, i.plataforma AS plataforma, i.store AS loja, i.name AS nome, "
            f"p.price AS preco, {_ESTOQUE_SQL} AS estoque, i.url AS link, p.ts AS lido_em")
    out = ["DROP VIEW IF EXISTS precos;", "DROP VIEW IF EXISTS atual;"]
    out += [f"DROP VIEW IF EXISTS atual_{c};" for c in CATEGORIAS]
    # precos = histórico completo (uma linha por leitura)
    out.append(f"CREATE VIEW precos AS SELECT {cols} FROM prices p JOIN items i ON i.key = p.key;")
    # atual = última leitura de cada item; esgotados por último dentro de cada grupo
    out.append(
        f"CREATE VIEW atual AS SELECT {cols} FROM items i JOIN prices p ON p.key = i.key "
        "WHERE p.ts = (SELECT MAX(ts) FROM prices WHERE key = i.key) "
        "ORDER BY i.categoria, i.plataforma, COALESCE(p.available, 1) = 0, p.price;")
    for c in CATEGORIAS:
        out.append(f"CREATE VIEW atual_{c} AS SELECT * FROM atual WHERE categoria = '{c}' "
                   "ORDER BY plataforma, estoque = 'ESGOTADO', preco;")
    return "\n".join(out)


VIEWS = _views_sql()
SCHEMA = TABLES + VIEWS


SCHEMA_VERSION = 1


def open_db(path: Path, timeout: float = 30) -> sqlite3.Connection:
    """Abre o banco. Espera até `timeout` s se outro programa (ex.: seu cliente de banco) estiver
    com o arquivo travado. A estrutura (colunas/visões) só é mexida quando a versão muda; nas
    rodadas normais o banco não é reescrito."""
    db = sqlite3.connect(path, timeout=timeout)
    if db.execute("PRAGMA user_version").fetchone()[0] < SCHEMA_VERSION:
        db.executescript(TABLES)
        have = {r[1] for r in db.execute("PRAGMA table_info(items)")}
        for col in ("categoria", "plataforma"):          # bancos criados antes desta versão
            if col not in have:
                db.execute(f"ALTER TABLE items ADD COLUMN {col} TEXT")
        db.executescript(VIEWS)
        db.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        db.commit()
    return db


LOCKED_MSG = ("O prices.db está travado por outro programa (provavelmente seu cliente de banco de dados "
              "com o arquivo aberto). Feche ou desconecte o prices.db lá e rode de novo.")


def item_key(store: str, ident: str) -> str:
    return hashlib.sha1(f"{store}|{ident}".encode("utf-8")).hexdigest()[:16]


@dataclass
class Obs:
    key: str
    store: str
    name: str
    url: str
    price: float | None
    available: bool | None     # True = em estoque, False = ESGOTADO, None = a página não diz
    source: str
    target: float | None
    drop_pct: float
    stock_signal: str = ""
    direct_link: bool = True   # False = só temos o link da página de busca, não o do produto
    categoria: str = ""        # processador, placa_mae, memoria, placa_de_video, ssd
    plataforma: str = ""       # AM4, AM5...
    mult: float = 1.0          # preço × mult (ex.: 2 pentes de 16GB vendidos separados = 32GB)


MIN_HISTORY_DAYS = 3


def evaluate(db: sqlite3.Connection, o: Obs, now: datetime, dedup_hours: float,
             strict_stock: bool = False) -> dict | None:
    """Decide se vale alertar. Devolve dict com motivos ou None."""
    if o.price is None or o.available is False:
        return None            # esgotado: preço não vale (costuma ser valor defasado)
    if o.available is None and strict_stock:
        return None            # modo estrito: sem confirmação de estoque, não avisa
    # histórico só com leituras em estoque (ou sem informação); esgotados não entram
    hist = db.execute(
        "SELECT ts, price FROM prices WHERE key=? AND COALESCE(available,1)=1 "
        "AND price IS NOT NULL ORDER BY ts",
        (o.key,),
    ).fetchall()
    prev = hist[-1][1] if hist else None
    # mediana e "menor preço" só valem com histórico de verdade (várias leituras no mesmo
    # dia, ex.: testes com --dry-run, não contam como histórico)
    span_ok = bool(hist) and hist[0][0] <= iso(now - timedelta(days=MIN_HISTORY_DAYS))
    cut90 = iso(now - timedelta(days=90))
    cut30 = iso(now - timedelta(days=30))
    last90 = [p for ts, p in hist if ts >= cut90]
    last30 = [p for ts, p in hist if ts >= cut30]

    reasons = []
    if o.target is not None and o.price <= o.target:
        reasons.append(f"abaixo da meta ({brl(o.target)})")
    if prev and o.price <= prev * (1 - o.drop_pct / 100):
        reasons.append(f"queda de {100 * (1 - o.price / prev):.0f}% vs. última checagem ({brl(prev)})")
    if span_ok and len(last90) >= 3 and o.price < min(last90):
        reasons.append("menor preço já registrado")
    if not reasons:
        return None

    last = db.execute(
        "SELECT ts, price FROM alerts WHERE key=? ORDER BY ts DESC LIMIT 1", (o.key,)
    ).fetchone()
    if last and last[1] <= o.price and last[0] >= iso(now - timedelta(hours=dedup_hours)):
        return None  # já avisamos esse preço (ou menor) recentemente

    med30 = median(last30) if span_ok and len(last30) >= 3 else None
    vs_med = (1 - o.price / med30) * 100 if med30 else None
    return {"reasons": reasons, "median30": med30, "vs_median": vs_med,
            "stock_confirmed": o.available is True}


def record(db: sqlite3.Connection, o: Obs, now: datetime) -> None:
    db.execute(
        "INSERT INTO items(key,store,name,url,source,categoria,plataforma) VALUES(?,?,?,?,?,?,?) "
        "ON CONFLICT(key) DO UPDATE SET name=excluded.name, url=excluded.url, "
        "categoria=excluded.categoria, plataforma=excluded.plataforma",
        (o.key, o.store, o.name, o.url, o.source, o.categoria or None, o.plataforma or None),
    )
    db.execute(
        "INSERT INTO prices(key,ts,price,available) VALUES(?,?,?,?)",
        (o.key, iso(now), o.price, None if o.available is None else int(o.available)),
    )


# --------------------------------------------------------------------------
# notificações
# --------------------------------------------------------------------------
def notify(title: str, message: str, click: str | None = None, priority: int = 4) -> list[str]:
    """Envia por ntfy e/ou Telegram, conforme as variáveis de ambiente."""
    sent = []
    topic = os.environ.get("NTFY_TOPIC")
    if topic:
        server = os.environ.get("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
        body = {"topic": topic, "title": title, "message": message[:3800], "priority": priority}
        if click:
            body["click"] = click
        r = requests.post(server, json=body, timeout=20)
        r.raise_for_status()
        sent.append("ntfy")
    tok, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if tok and chat:
        r = requests.post(
            f"https://api.telegram.org/bot{tok}/sendMessage",
            json={"chat_id": chat, "text": f"{title}\n\n{message}"[:4000],
                  "disable_web_page_preview": True},
            timeout=20,
        )
        r.raise_for_status()
        sent.append("telegram")
    return sent


# --------------------------------------------------------------------------
# configuração e execução
# --------------------------------------------------------------------------
@dataclass
class Job:
    kind: str            # "product" | "search"
    store: str
    url: str
    cfg: dict
    label: str


def build_jobs(cfg: dict) -> list[Job]:
    jobs: list[Job] = []
    for p in cfg.get("products", []):
        if not p.get("url"):
            continue
        url = clean_url(p["url"].strip())
        jobs.append(Job("product", store_of(url), url, p, p.get("name") or url))
    templates = {**STORE_SEARCH, **cfg.get("search_urls", {})}
    for s in cfg.get("searches", []):
        q = s.get("query")
        if not q:
            continue
        stores = s.get("stores") or cfg.get("stores") or list(templates)
        for st in stores:
            tpl = templates.get(st)
            if not tpl:
                continue
            url = tpl.format(q=quote_plus(q), q_dash="-".join(q.split()))
            jobs.append(Job("search", store_of(url), url, s, f"{s.get('name', q)} @ {st}"))
    off = {str(s).lower() for s in cfg.get("disabled_stores", [])}
    jobs = [j for j in jobs if j.store.lower() not in off]
    # intercala lojas para não bater duas vezes seguidas no mesmo site
    queues: dict[str, deque] = defaultdict(deque)
    for j in jobs:
        queues[j.store].append(j)
    ordered = []
    while any(queues.values()):
        for st in list(queues):
            if queues[st]:
                ordered.append(queues[st].popleft())
    return ordered


def parse_stores(text: str | None) -> set[str]:
    """'terabyte, KaBuM' -> {'terabyte', 'kabum'} (minúsculas, sem vazios)."""
    return {x.strip().lower() for x in (text or "").split(",") if x.strip()}


def filter_jobs(jobs: list[Job], only: set[str] | None = None, skip: set[str] | None = None) -> list[Job]:
    """Divide o trabalho entre máquinas: `only` mantém só essas lojas, `skip` tira essas lojas.
    Ex.: o GitHub roda com skip={'terabyte'} e o PC de casa roda com only={'terabyte'}."""
    if only:
        jobs = [j for j in jobs if j.store.lower() in only]
    if skip:
        jobs = [j for j in jobs if j.store.lower() not in skip]
    return jobs


def match_filters(name: str, job_cfg: dict) -> bool:
    inc, exc = job_cfg.get("include"), job_cfg.get("exclude")
    flags = re.I | re.S
    if inc and not re.search(inc, name, flags):
        return False
    if exc and re.search(exc, name, flags):
        return False
    return True


DEBUG_DIR = Path(__file__).resolve().parent / "debug"


def save_debug(store: str, html: str) -> Path | None:
    """Guarda o HTML recebido (só quando uma lista veio vazia) para eu poder analisar."""
    try:
        DEBUG_DIR.mkdir(exist_ok=True)
        p = DEBUG_DIR / f"{re.sub(r'[^A-Za-z0-9_.-]', '_', store)}.html"
        p.write_text(html, encoding="utf-8")
        return p
    except OSError:
        return None


def run(cfg: dict, db: sqlite3.Connection, dry: bool, delay: tuple[float, float], verbose: bool,
        out_path: Path | None = None, only_stores: set[str] | None = None,
        skip_stores: set[str] | None = None, heartbeat: bool | None = None) -> int:
    defaults = cfg.get("defaults", {})
    strict_stock = bool(defaults.get("strict_stock", False))
    drop_default = float(defaults.get("drop_pct", 8))
    dedup_hours = float(defaults.get("dedup_hours", 24))
    max_alerts = int(defaults.get("max_alerts_per_run", 10))
    now = utcnow()
    session = requests.Session()

    attempted: dict[str, int] = defaultdict(int)
    succeeded: dict[str, int] = defaultdict(int)
    blocked: set[str] = set()
    debug_saved: set[str] = set()
    observations: list[Obs] = []
    log =(lambda *a: print(*a, file=sys.stderr)) if verbose else (lambda *a: None)

    jobs = filter_jobs(build_jobs(cfg), only_stores, skip_stores)
    if (only_stores or skip_stores) and not jobs:
        print("Nenhum item para as lojas escolhidas (confira --only-stores / --skip-stores).", file=sys.stderr)
    for idx, job in enumerate(jobs):
        if job.store in blocked:
            log(f"[{job.store}] pulado (loja bloqueou nesta execução): {job.label}")
            continue
        if idx and delay[1] > 0:
            time.sleep(random.uniform(*delay))
        attempted[job.store] += 1
        try:
            html = fetch(session, job.url)
        except Blocked as e:
            blocked.add(job.store)
            print(f"[{job.store}] bloqueio ({e}) em {job.label}", file=sys.stderr)
            continue
        except FetchError as e:
            print(f"[{job.store}] falha em {job.label}: {e}", file=sys.stderr)
            continue

        if job.kind == "product":
            data, method = extract_product(html, job.cfg.get("regex"))
            if not data:
                why = "página de desafio/captcha" if looks_like_challenge(html) else "preço não encontrado"
                print(f"[{job.store}] {why}: {job.label}", file=sys.stderr)
                continue
            succeeded[job.store] += 1
            mult = float(job.cfg.get("multiplicador", 1) or 1)
            name = job.cfg.get("name") or data["name"] or job.url
            if mult != 1:
                name += f" [preço ×{mult:g}]"
            observations.append(Obs(
                item_key(job.store, job.url), job.store, name, job.url, round(data["price"] * mult, 2),
                data["available"], f"product:{job.label}", job.cfg.get("target"),
                float(job.cfg.get("drop_pct", drop_default)), data.get("stock_signal", ""),
                categoria=job.cfg.get("categoria", ""), plataforma=job.cfg.get("plataforma", ""),
                mult=mult,
            ))
            log(f"[{job.store}] [{stock_tag(data['available'])}] {name[:60]}: "
                f"{brl(data['price'])} (preço via {method}; estoque via {data.get('stock_signal', '')})")
        else:
            items, method = extract_listing(html, job.url)
            if not items:
                why = "página de desafio/captcha" if looks_like_challenge(html) else \
                    "nenhum produto no HTML (a loja carrega a lista só via JavaScript, ou bloqueou)"
                m = re.search(r"<title[^>]*>(.*?)</title>", html, re.I | re.S)
                title = re.sub(r"\s+", " ", m.group(1)).strip()[:90] if m else "(sem <title>)"
                print(f"[{job.store}] {why}: {job.label} | {len(html):,} bytes | título: {title}",
                      file=sys.stderr)
                if looks_like_challenge(html):      # a loja barrou o script: não insiste nesta rodada
                    blocked.add(job.store)
                if job.store not in debug_saved:
                    debug_saved.add(job.store)
                    saved = save_debug(job.store, html)
                    if saved:
                        print(f"[{job.store}] salvei o HTML recebido em {saved} (me mande esse arquivo)",
                              file=sys.stderr)
                continue
            succeeded[job.store] += 1
            kept = 0
            rejected = []
            for it in items:
                if not match_filters(it["name"], job.cfg):
                    rejected.append(it["name"])
                    continue
                kept += 1
                ident = it.get("url") or it["name"]
                smult = float(job.cfg.get("multiplicador", 1) or 1)
                sname = it["name"] + (f" [preço ×{smult:g}]" if smult != 1 else "")
                observations.append(Obs(
                    item_key(job.store, ident), job.store, sname,
                    it.get("url") or job.url, round(it["price"] * smult, 2), it["available"],
                    f"search:{job.label}", job.cfg.get("max_price"),
                    float(job.cfg.get("drop_pct", drop_default)), it.get("stock_signal", ""),
                    direct_link=bool(it.get("url")),
                    categoria=job.cfg.get("categoria", ""), plataforma=job.cfg.get("plataforma", ""),
                    mult=smult,
                ))
                if kept <= 12:
                    log(f"    [{stock_tag(it['available'])}] {brl(it['price'])}  {it['name'][:70]}")
            log(f"[{job.store}] {job.label}: {len(items)} itens, {kept} passaram nos filtros ({method})")
            if rejected and kept < 3:   # ajuda a acertar include/exclude
                for nm in rejected[:10]:
                    log(f"    [fora do filtro] {nm[:100]}")
            no_link = sum(1 for o in observations[-kept:] if not o.direct_link) if kept else 0
            if no_link:
                print(f"[{job.store}] AVISO: {no_link} de {kept} itens de '{job.label}' sem link do "
                      f"produto (guardei o link da busca). Rode: python price_watch.py test-url "
                      f"\"{job.url}\" --keys   e me mande o resultado.", file=sys.stderr)

    # Mesmo produto vindo de link direto e de busca: vale o link direto (preço da página do produto).
    best: dict[str, Obs] = {}
    for o in observations:
        cur = best.get(o.key)
        if cur is None or (o.source.startswith("product:") and not cur.source.startswith("product:")):
            best[o.key] = o
    observations = list(best.values())

    # Algumas listas (KaBuM) mostram o preço "de" riscado, não o preço real. Para as lojas em
    # detail_price_stores, o preço de itens vindos de BUSCA é lido na página do produto.
    detail_stores = {s.lower() for s in defaults.get("detail_price_stores", ["KaBuM"])}
    detail_max = int(defaults.get("detail_price_max", 80))
    per_search = int(defaults.get("detail_per_search", 5))
    if detail_stores and detail_max > 0:
        cand = [o for o in observations if o.source.startswith("search:") and o.direct_link
                and o.store.lower() in detail_stores and o.store not in blocked]
        # por busca, só os N mais baratos da lista (o preço da lista é inflado, mas a ordem se mantém)
        by_src: dict[str, list[Obs]] = defaultdict(list)
        for o in cand:
            by_src[o.source].append(o)
        todo, drop = [], set()
        for lst in by_src.values():
            lst.sort(key=lambda o: o.price if o.price is not None else float("inf"))
            todo += lst[:per_search]
            drop |= {o.key for o in lst[per_search:]}
        if len(todo) > detail_max:
            print(f"AVISO: {len(todo)} itens precisam de preço pela página do produto; "
                  f"só os {detail_max} primeiros serão lidos (defaults.detail_price_max).", file=sys.stderr)
        drop |= {o.key for o in todo[detail_max:]}
        for o in todo[:detail_max]:
            if o.store in blocked:
                drop.add(o.key)
                continue
            if delay[1] > 0:
                time.sleep(random.uniform(*delay))
            try:
                data, method = extract_product(fetch(session, o.url, retries=1))
            except Blocked:
                blocked.add(o.store)
                drop.add(o.key)
                print(f"[{o.store}] bloqueio ao ler o preço de {o.name[:50]}", file=sys.stderr)
                continue
            except FetchError as e:
                drop.add(o.key)
                log(f"[{o.store}] não li a página do produto ({e}); descartei: {o.name[:60]}")
                continue
            if not data:
                drop.add(o.key)
                log(f"[{o.store}] página do produto sem preço; descartei: {o.name[:60]}")
                continue
            if abs(data["price"] - o.price) > 0.005:
                log(f"[{o.store}] preço corrigido pela página: {brl(o.price)} -> {brl(data['price'] * o.mult)}  {o.name[:55]}")
            o.price = round(data["price"] * o.mult, 2)
            if data.get("available") is not None:
                o.available, o.stock_signal = data["available"], "pagina-do-produto:" + data["stock_signal"]
            o.source = o.source.replace("search:", "search+pagina:", 1)
        observations = [o for o in observations if o.key not in drop]

    # Listas de busca costumam não informar estoque. Para os itens que DISPARARIAM aviso,
    # abre a página do produto e confirma (poucas requisições; esgotado é descartado).
    if defaults.get("confirm_stock", True):
        limit = int(defaults.get("confirm_stock_max", 10))
        cands = [o for o in observations
                 if o.available is None and o.direct_link and o.source.startswith("search:")
                 and o.store not in blocked
                 and evaluate(db, o, now, dedup_hours, False)]
        for o in cands[:limit]:
            if delay[1] > 0:
                time.sleep(random.uniform(*delay))
            try:
                data, _ = extract_product(fetch(session, o.url, retries=1))
            except Blocked:
                blocked.add(o.store)
                print(f"[{o.store}] bloqueio ao confirmar estoque de {o.name[:50]}", file=sys.stderr)
                continue
            except FetchError as e:
                log(f"[{o.store}] não consegui confirmar estoque ({e}): {o.name[:60]}")
                continue
            if data and data.get("available") is not None:
                o.available, o.stock_signal = data["available"], "pagina-do-produto:" + data["stock_signal"]
                log(f"[{o.store}] estoque confirmado na página: [{stock_tag(o.available)}] {o.name[:60]}")
            else:
                log(f"[{o.store}] página do produto sem sinal de estoque: {o.name[:60]}")

    # decide alertas ANTES de gravar os preços desta rodada
    hits: list[tuple[Obs, dict]] = []
    for o in observations:
        res = evaluate(db, o, now, dedup_hours, strict_stock)
        if res:
            hits.append((o, res))
    for o in observations:
        record(db, o, now)

    # relatório de estoque: mostra que o filtro está funcionando
    summary: dict[str, dict[str, int]] = defaultdict(
        lambda: {"lidos": 0, "em_estoque": 0, "esgotados": 0, "estoque_desconhecido": 0})
    for o in observations:
        s = summary[o.store]
        s["lidos"] += 1
        if o.available is True:
            s["em_estoque"] += 1
        elif o.available is False:
            s["esgotados"] += 1
        else:
            s["estoque_desconhecido"] += 1
    if summary:
        print("\nResumo por loja:")
        for store, s in summary.items():
            print(f"  {store}: {s['lidos']} itens | {s['em_estoque']} em estoque | "
                  f"{s['esgotados']} ESGOTADOS | {s['estoque_desconhecido']} sem informação de estoque")
    ignored = [o for o in observations if o.available is False and o.price is not None
               and o.target is not None and o.price <= o.target]
    if ignored:
        print(f"\n{len(ignored)} item(ns) com preço abaixo da meta IGNORADO(S) por estar(em) ESGOTADO(S):")
        for o in sorted(ignored, key=lambda x: x.price)[:10]:
            print(f"  [ESGOTADO] {o.store} - {o.name[:70]}  {brl(o.price)}  ({o.stock_signal})")
    hit_by_key = {o.key: r for o, r in hits}

    # saúde por loja
    health_msgs = []
    for store, n in attempted.items():
        row = db.execute("SELECT fails, notified FROM health WHERE store=?", (store,)).fetchone()
        fails, notified = (row if row else (0, 0))
        if succeeded[store] > 0:
            db.execute(
                "INSERT INTO health(store,fails,notified,last_ok) VALUES(?,?,?,?) "
                "ON CONFLICT(store) DO UPDATE SET fails=0, notified=0, last_ok=excluded.last_ok",
                (store, 0, 0, iso(now)),
            )
        else:
            fails += 1
            if fails >= 3 and not notified:
                health_msgs.append(
                    f"O monitor não conseguiu ler nada da {store} em {fails} execuções seguidas "
                    f"(bloqueio ou o site mudou). Rode: python price_watch.py test-url <URL>"
                )
                notified = 1
            db.execute(
                "INSERT INTO health(store,fails,notified,last_ok) VALUES(?,?,?,NULL) "
                "ON CONFLICT(store) DO UPDATE SET fails=excluded.fails, notified=excluded.notified",
                (store, fails, notified),
            )

    # monta o resumo (um aviso só por execução)
    def score(h: tuple[Obs, dict]) -> float:
        o, r = h
        return (r["vs_median"] or 0) + (10 if o.target and o.price <= o.target else 0)

    hits.sort(key=score, reverse=True)
    shown = hits[:max_alerts]
    if shown:
        lines = []
        for o, r in shown:
            extra = "em estoque" if r["stock_confirmed"] else "⚠ estoque NÃO confirmado"
            extra += "; " + "; ".join(r["reasons"])
            if r["median30"]:
                extra += f"; mediana 30d {brl(r['median30'])} ({r['vs_median']:+.0f}% hoje)"
            tag = " ".join(x for x in (o.categoria.replace("_", " "), o.plataforma) if x)
            lines.append(f"• {('[' + tag + '] ') if tag else ''}{o.store} - {o.name[:90]}\n"
                         f"  {brl(o.price)}  [{extra}]\n  {o.url}")
        more = f"\n(+{len(hits) - len(shown)} outras ofertas no histórico)" if len(hits) > len(shown) else ""
        title = f"{len(hits)} oferta(s) monitorada(s)"
        body = "\n".join(lines) + more
        print(f"\n{title}\n{body}")
        if not dry:
            try:
                sent = notify(title, body, click=shown[0][0].url)
                if not sent:
                    print("Aviso: nenhum canal configurado (defina NTFY_TOPIC ou TELEGRAM_*).", file=sys.stderr)
            except requests.RequestException as e:
                print(f"Falha ao notificar: {e}", file=sys.stderr)
                db.rollback()   # não marca como avisado se não conseguiu enviar
                return 2
            for o, r in shown:
                db.execute("INSERT INTO alerts(key,ts,price,reason) VALUES(?,?,?,?)",
                           (o.key, iso(now), o.price, "; ".join(r["reasons"])))
    else:
        print("Sem ofertas novas nesta rodada.")

    for msg in health_msgs:
        print("AVISO:", msg, file=sys.stderr)
        if not dry:
            try:
                notify("Monitor de preços: precisa de atenção", msg, priority=3)
            except requests.RequestException as e:
                print(f"Falha ao notificar: {e}", file=sys.stderr)

    # sinal de vida: um aviso discreto por execução, para você saber que o agendador está rodando
    use_heartbeat = defaults.get("heartbeat", False) if heartbeat is None else heartbeat
    if use_heartbeat and not dry and not shown:
        lojas = " | ".join(f"{s}: {v['lidos']}" for s, v in summary.items()) or "nenhum item lido"
        total = sum(v["lidos"] for v in summary.values())
        hora = datetime.now().astimezone().strftime("%H:%M")
        try:
            notify(f"Monitor rodou às {hora}",
                   f"{total} itens lidos ({lojas}). Nenhuma oferta nova.", priority=2)
        except requests.RequestException as e:
            print(f"Falha ao enviar o sinal de vida: {e}", file=sys.stderr)
    db.commit()

    if out_path:   # resultado completo, com marcador de estoque por item
        payload = {
            "gerado_em": datetime.now().astimezone().isoformat(timespec="seconds"),  # hora do seu PC, com fuso
            "resumo_por_loja": summary,
            "itens": [{
                "categoria": o.categoria or None, "plataforma": o.plataforma or None,
                "loja": o.store, "nome": o.name, "preco": o.price,
                "estoque": stock_tag(o.available), "esgotado": o.available is False,
                "sinal_estoque": o.stock_signal, "meta": o.target,
                "alerta": hit_by_key[o.key]["reasons"] if o.key in hit_by_key else None,
                "url": o.url, "link_direto": o.direct_link, "origem": o.source,
            } for o in observations],
        }
        try:
            out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"\nResultado completo (com estoque de cada item) em: {out_path}")
        except OSError as e:
            print(f"Não consegui gravar {out_path}: {e}", file=sys.stderr)
    return 0


# --------------------------------------------------------------------------
# comandos auxiliares
# --------------------------------------------------------------------------
def cmd_test_url(url: str, regex: str | None, show_keys: bool = False, save: str | None = None) -> int:
    session = requests.Session()
    try:
        html = fetch(session, url, retries=1)
    except (Blocked, FetchError) as e:
        print(f"Não consegui baixar: {e}")
        return 1
    print(f"Baixado: {len(html):,} bytes de {store_of(url)}")
    if save:
        Path(save).write_text(html, encoding="utf-8")
        print(f"HTML salvo em {save}")
    lds = ld_products(html)
    nxt = next_candidates(html)
    if looks_like_challenge(html) and not (lds or nxt or meta_price(html)):
        print("ATENÇÃO: a página parece um desafio anti-bot/captcha. Não vou contornar isso.")
    print(f"JSON-LD: {len(lds)} produto(s) com preço | Next.js: {len(nxt)} candidato(s) | "
          f"meta: {'sim' if meta_price(html) else 'não'}")
    data, method = extract_product(html, regex)
    if data:
        print(f"\nComo PÁGINA DE PRODUTO -> via {method}:")
        print(f"  nome:    {data['name'][:100]}")
        print(f"  preço:   {brl(data['price'])}   (confira com o que aparece no site: PIX ou parcelado?)")
        print(f"  estoque: {stock_tag(data['available'])}   (sinal: {data.get('stock_signal', '?')})")
        if data["available"] is None:
            print("           a página não informa estoque de forma clara; o monitor avisa, mas marca "
                  "\"estoque NÃO confirmado\" (use defaults.strict_stock para ignorar esses casos)")
    else:
        print("\nComo página de produto: nada encontrado. Use \"regex\" no watchlist (veja o README).")
    if len(lds) >= 2 or len(nxt) >= 2:
        print("\nFONTES DE LISTA (compare com o que você vê no navegador):")
        for label, src in (("JSON-LD", lds), ("Next.js", nxt)):
            print(f"  {label}: {len(src)} itens")
            for it in src[:6]:
                print(f"     {brl(it['price']):>12}  {it['name'][:75]}")
    items, lmethod = extract_listing(html, url)
    if len(items) >= 2:
        print(f"\nComo LISTA/BUSCA -> via {lmethod}: {len(items)} itens. Primeiros:")
        for it in items[:8]:
            print(f"  {brl(it['price']):>12}  [{stock_tag(it['available']):<10}] {it['name'][:70]}")
            print(f"{'':>16}{it.get('url') or '(SEM LINK DO PRODUTO)'}")
        sold = sum(1 for it in items if it["available"] is False)
        unk = sum(1 for it in items if it["available"] is None)
        nolink = sum(1 for it in items if not it.get("url"))
        print(f"  -> {sold} esgotado(s), {unk} sem informação de estoque, {nolink} sem link do produto, "
              f"de {len(items)} itens")
        if show_keys:
            print("\nCampos que a loja envia para os primeiros itens (me mande isto se faltar link/estoque):")
            for it in items[:2]:
                print("  ", it.get("_keys"))
    return 0


def cmd_export(db: sqlite3.Connection, path: Path, por_categoria: bool = False) -> int:
    """CSV (abre no Excel) com a última leitura de cada item, incluindo o link.
    Com por_categoria, gera também um arquivo por tipo de peça (precos_processador.csv...)."""
    import csv
    cur = db.execute("SELECT categoria, plataforma, loja, nome, preco, estoque, link, lido_em FROM atual")
    rows = cur.fetchall()
    header = [d[0] for d in cur.description]

    def write(dest: Path, subset: list) -> None:
        with open(dest, "w", newline="", encoding="utf-8-sig") as f:     # utf-8-sig: Excel lê acentos
            w = csv.writer(f, delimiter=";")
            w.writerow(header)
            for r in subset:
                r = list(r)
                r[4] = f"{r[4]:.2f}".replace(".", ",") if r[4] is not None else ""
                w.writerow(r)

    write(path, rows)
    print(f"{len(rows)} itens exportados para {path}")
    if por_categoria:
        for cat in sorted({r[0] for r in rows if r[0]}):
            dest = path.with_name(f"{path.stem}_{cat}{path.suffix}")
            sub = [r for r in rows if r[0] == cat]
            write(dest, sub)
            print(f"  {len(sub):>3} itens em {dest.name}")
    return 0


def cmd_history(db: sqlite3.Connection, limit: int, urls: bool = False) -> int:
    rows = db.execute(
        """
        SELECT i.categoria, i.plataforma, i.store, i.name, i.url,
               (SELECT price FROM prices p WHERE p.key=i.key ORDER BY ts DESC LIMIT 1),
               (SELECT available FROM prices p WHERE p.key=i.key ORDER BY ts DESC LIMIT 1),
               (SELECT MIN(price) FROM prices p WHERE p.key=i.key AND COALESCE(p.available,1)=1),
               (SELECT COUNT(*) FROM prices p WHERE p.key=i.key)
        FROM items i
        ORDER BY COALESCE(i.categoria, 'zzz'), i.plataforma, i.store, i.name LIMIT ?
        """,
        (limit,),
    ).fetchall()
    if not rows:
        print("Histórico vazio. Rode: python price_watch.py run")
        return 0
    group = object()
    for cat, plat, store, name, url, last, avail, mn, n in rows:
        if (cat, plat) != group:
            group = (cat, plat)
            title = (cat or "sem categoria").replace("_", " ").upper() + (f"  ({plat})" if plat else "")
            print(f"\n== {title} ==")
        tag = "ESGOTADO" if avail == 0 else ("?" if avail is None else "ok")
        print(f"{store:<12} {brl(last) if last else '-':>12} [{tag:<8}] mín em estoque "
              f"{brl(mn) if mn else '-':>12}  ({n:>3} leituras)  {name[:60]}")
        if urls:
            print(f"{'':<12} {url}")
    return 0


def main(argv: list[str] | None = None) -> int:
    load_dotenv(HERE / ".env")
    ap = argparse.ArgumentParser(description="Monitor de preços para Black Friday")
    ap.add_argument("--config", default=str(HERE / "watchlist.json"))
    ap.add_argument("--db", default=str(HERE / "prices.db"))
    sub = ap.add_subparsers(dest="cmd")

    r = sub.add_parser("run", help="consulta as lojas e avisa das ofertas")
    r.add_argument("--dry-run", action="store_true", help="não envia notificação nem grava alertas")
    r.add_argument("--delay", default="2,5", help="pausa aleatória entre requisições, ex.: 2,5")
    r.add_argument("-v", "--verbose", action="store_true")
    r.add_argument("--out", default=str(HERE / "last_run.json"),
                   help="arquivo JSON com todos os itens lidos e o estoque de cada um")
    r.add_argument("--only-stores", metavar="LOJAS",
                   help="só estas lojas, separadas por vírgula (ex.: terabyte)")
    r.add_argument("--skip-stores", metavar="LOJAS",
                   help="pula estas lojas, separadas por vírgula (ex.: terabyte)")
    r.add_argument("--no-heartbeat", action="store_true",
                   help="não manda o aviso 'Monitor rodou às HH:MM' desta execução")

    t = sub.add_parser("test-url", help="mostra o que o script extrai de uma URL")
    t.add_argument("url")
    t.add_argument("--regex", help="regex com 1 grupo de captura para o preço")
    t.add_argument("--keys", action="store_true", help="mostra os campos que a loja envia por item")
    t.add_argument("--save", metavar="ARQUIVO", help="salva o HTML baixado (para análise)")

    sub.add_parser("test-notify", help="envia uma notificação de teste")
    h = sub.add_parser("history", help="resumo do histórico")
    h.add_argument("--limit", type=int, default=200)
    h.add_argument("--urls", action="store_true", help="mostra também o link de cada item")
    e = sub.add_parser("export", help="exporta a última leitura de cada item para CSV (Excel)")
    e.add_argument("--csv", default=str(HERE / "precos.csv"))
    e.add_argument("--por-categoria", action="store_true",
                   help="gera também um CSV por tipo de peça (precos_processador.csv...)")

    args = ap.parse_args(argv)
    cmd = args.cmd or "run"

    if cmd == "test-url":
        return cmd_test_url(args.url, args.regex, args.keys, args.save)
    if cmd == "test-notify":
        sent = notify("Monitor de preços", "Teste: se você leu isso, as notificações funcionam.", priority=3)
        print("Enviado por:", ", ".join(sent) if sent else "nenhum canal configurado (veja o README)")
        return 0 if sent else 1

    try:
        db = open_db(Path(args.db))
    except sqlite3.OperationalError as e:
        print(f"{LOCKED_MSG if 'locked' in str(e) else 'Erro no banco: ' + str(e)}", file=sys.stderr)
        return 1
    if cmd == "history":
        return cmd_history(db, args.limit, args.urls)
    if cmd == "export":
        return cmd_export(db, Path(args.csv), args.por_categoria)

    try:
        cfg = json.loads(Path(args.config).read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        print(f"Erro lendo {args.config}: {e}", file=sys.stderr)
        return 1
    lo, _, hi = getattr(args, "delay", "2,5").partition(",")
    delay = (float(lo), float(hi or lo))
    out = getattr(args, "out", None)
    try:
        return run(cfg, db, getattr(args, "dry_run", False), delay, getattr(args, "verbose", False),
                   Path(out) if out else None,
                   only_stores=parse_stores(getattr(args, "only_stores", None)),
                   skip_stores=parse_stores(getattr(args, "skip_stores", None)),
                   heartbeat=False if getattr(args, "no_heartbeat", False) else None)
    except sqlite3.OperationalError as e:
        db.rollback()
        print(f"{LOCKED_MSG if 'locked' in str(e) else 'Erro no banco: ' + str(e)}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
