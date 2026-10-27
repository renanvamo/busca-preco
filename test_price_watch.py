"""Testes offline: páginas simuladas + servidores locais (lojas e ntfy falsos).

Rodar:  python -m unittest -v test_price_watch.py
"""
import json
import os
import sqlite3
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

os.environ["NO_PROXY"] = os.environ["no_proxy"] = "127.0.0.1,localhost"

import price_watch as pw  # noqa: E402


# ---------------------------------------------------------------- fixtures
def ld_page(name, price, availability="InStock"):
    data = {"@context": "https://schema.org", "@type": "Product", "name": name,
            "offers": {"@type": "Offer", "price": price, "priceCurrency": "BRL",
                       "availability": f"https://schema.org/{availability}"}}
    return f'<html><head><script type="application/ld+json">{json.dumps(data)}</script></head></html>'


def next_page(blob):
    return ('<html><script id="__NEXT_DATA__" type="application/json">'
            + json.dumps(blob) + "</script></html>")


def listing(items):
    prods = [{"name": n, "priceWithDiscount": p, "price": round(p * 1.15, 2),
              "available": a, "url": f"/produto/{i}/slug"} for i, (n, p, a) in enumerate(items)]
    return next_page({"props": {"pageProps": {"data": {"products": prods}}}})


# ---------------------------------------------------------------- servidores
class Shop:
    pages: dict = {}


class ShopHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        host = self.headers.get("Host", "").split(":")[0]
        status, body = Shop.pages.get((host, self.path.split("?")[0]), (404, "nf"))
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(body.encode())

    def log_message(self, *a):
        pass


class Ntfy:
    received: list = []
    status = 200


class NtfyHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(n))
        if Ntfy.status == 200:
            Ntfy.received.append(body)
        self.send_response(Ntfy.status)
        self.end_headers()

    def log_message(self, *a):
        pass


def serve(handler):
    srv = HTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


# ---------------------------------------------------------------- testes
class ParsingTests(unittest.TestCase):
    def test_parse_price(self):
        cases = {"R$ 1.234,56": 1234.56, "549.95": 549.95, "1.234": 1234.0, 3899.99: 3899.99,
                 "R$ 4.359,99 no PIX": 4359.99, "12,90": 12.9, "abc": None, "R$ 0": None, None: None}
        for raw, want in cases.items():
            self.assertEqual(pw.parse_price(raw), want, raw)

    def test_json_ld(self):
        d, m = pw.extract_product(ld_page("RAM 32GB", "549.95"))
        self.assertEqual((m, d["price"], d["available"]), ("json-ld", 549.95, True))

    def test_json_ld_out_of_stock(self):
        d, _ = pw.extract_product(ld_page("RAM 32GB", 549.95, "OutOfStock"))
        self.assertFalse(d["available"])

    def test_next_data_prefers_pix_price_and_first_product(self):
        blob = {"props": {"pageProps": {
            "product": {"name": "Memória Kingston Fury 32GB", "price": 5299, "priceWithDiscount": 4769.1,
                        "available": True},
            "related": [{"name": "Outro produto qualquer", "price": 99}]}}}
        d, m = pw.extract_product(next_page(blob))
        self.assertEqual((m, d["price"]), ("next-data", 4769.1))

    def test_meta_and_regex_fallbacks(self):
        html = '<meta property="og:title" content="X"><meta property="product:price:amount" content="1234.50">'
        self.assertEqual(pw.extract_product(html)[1], "meta")
        html2 = '<title>T</title><p id="valVista">R$ 2.345,67</p>'
        d, m = pw.extract_product(html2, r'id="valVista">\s*R\$\s*([\d.,]+)')
        self.assertEqual((m, d["price"]), ("regex", 2345.67))

    def test_nothing_found(self):
        self.assertEqual(pw.extract_product("<html>nada</html>")[1], "nenhum")

    # ---- estoque ----
    def test_stock_signals_in_store_json(self):
        sold_out = [{"available": False}, {"isAvailable": 0}, {"outOfStock": True},
                    {"stockQuantity": 0}, {"availability": "OutOfStock"},
                    {"availability": "Indisponível"}, {"inStock": "false"}, {"esgotado": True}]
        in_stock = [{"available": True}, {"isAvailable": 1}, {"outOfStock": False},
                    {"stockQuantity": 3}, {"availability": "InStock"}, {"inStock": "true"}]
        for d in sold_out:
            self.assertIs(pw.stock_from_dict(d, "t")[0], False, d)
        for d in in_stock:
            self.assertIs(pw.stock_from_dict(d, "t")[0], True, d)
        self.assertIsNone(pw.stock_from_dict({"name": "x", "price": 10}, "t")[0])

    def test_next_data_sold_out_item_is_flagged(self):
        html = listing([("Memória DDR4 32GB barata demais", 199, False),
                        ("Memória DDR4 32GB normal", 520, True)])
        items, _ = pw.extract_listing(html, "http://x.test/")
        self.assertEqual([i["available"] for i in items], [False, True])
        self.assertIn("available=False", items[0]["stock_signal"])

    def test_json_ld_without_availability_is_unknown(self):
        data = {"@type": "Product", "name": "RAM", "offers": {"price": "500"}}
        html = f'<script type="application/ld+json">{json.dumps(data)}</script>'
        d, _ = pw.extract_product(html)
        self.assertIsNone(d["available"])

    def test_json_ld_prefers_in_stock_offer_price(self):
        data = {"@type": "Product", "name": "RAM", "offers": [
            {"price": "100", "availability": "https://schema.org/OutOfStock"},
            {"price": "550", "availability": "https://schema.org/InStock"}]}
        html = f'<script type="application/ld+json">{json.dumps(data)}</script>'
        d, _ = pw.extract_product(html)
        self.assertEqual((d["price"], d["available"]), (550.0, True))

    def test_visible_text_fallback_only_when_no_structured_signal(self):
        meta = '<meta property="product:price:amount" content="300">'
        d, _ = pw.extract_product(meta + "<body><h1>RAM</h1><p>Produto indisponível</p></body>")
        self.assertIs(d["available"], False)
        self.assertTrue(d["stock_signal"].startswith("texto:"))
        # a palavra dentro de <script> não conta
        d2, _ = pw.extract_product(meta + "<script>var msg='esgotado'</script><body>ok</body>")
        self.assertIsNone(d2["available"])
        # sinal estruturado em estoque não é sobrescrito pelo texto da página
        d3, _ = pw.extract_product(ld_page("RAM", "300") + "<p>Esgotado</p>")
        self.assertIs(d3["available"], True)

    # ---- links ----
    def test_find_product_url(self):
        self.assertEqual(pw.find_product_url({"url": "/produto/1/x"}), "/produto/1/x")
        self.assertEqual(pw.find_product_url({
            "image": "https://images.kabum.com.br/produtos/fotos/1.jpg",
            "link": "https://www.kabum.com.br/produto/2/y"}), "https://www.kabum.com.br/produto/2/y")
        self.assertEqual(pw.find_product_url({"outro": "/produto/9/z"}), "/produto/9/z")
        self.assertIsNone(pw.find_product_url({"image": "https://img.x.com/fotos/1.jpg"}))
        self.assertIsNone(pw.find_product_url({"slug": "so-o-slug-sem-barra"}))

    def test_clean_url_removes_tracking_but_keeps_real_params(self):
        u = ("https://www.terabyteshop.com.br/produto/38270/gpu?srsltid=AU7gw4Uc&utm_source=x"
             "&cor=preta&gclid=1")
        self.assertEqual(pw.clean_url(u), "https://www.terabyteshop.com.br/produto/38270/gpu?cor=preta")
        self.assertEqual(pw.clean_url("https://x.com/p/1?srsltid=abc"), "https://x.com/p/1")
        self.assertEqual(pw.clean_url("https://x.com/p/1"), "https://x.com/p/1")
        jobs = pw.build_jobs({"products": [{"name": "a", "url": "https://x.com/p/1?srsltid=zzz"}]})
        self.assertEqual(jobs[0].url, "https://x.com/p/1")

    def test_recaptcha_word_in_big_page_is_not_a_challenge(self):
        big = "<html><head><script src='https://www.google.com/recaptcha/api.js'></script>" \
              + "<!-- captcha -->" + "x" * 100_000
        self.assertFalse(pw.looks_like_challenge(big))
        self.assertTrue(pw.looks_like_challenge("<html><title>Just a moment...</title></html>"))
        data, _ = pw.extract_product(big + ld_page("GPU", "3599.99"))
        self.assertEqual(data["price"], 3599.99)

    def test_slugify_matches_real_kabum_url(self):
        self.assertEqual(
            pw.slugify("Memória RAM XPG Lancer, RGB, 32GB (2x16GB), 6000MHz, DDR5, CL30, Preto - "
                       "AX5U6000C3016G-DCLARBK"),
            "memoria-ram-xpg-lancer-rgb-32gb-2x16gb-6000mhz-ddr5-cl30-preto-ax5u6000c3016g-dclarbk")

    def test_kabum_link_built_from_code_when_json_has_no_url(self):
        blob = {"props": {"pageProps": {"products": [
            {"name": "Memória RAM XPG Lancer, RGB, 32GB (2x16GB)", "priceWithDiscount": 3500,
             "code": 526516, "available": True},
            {"name": "Memória Corsair Vengeance 32GB", "priceWithDiscount": 3600,
             "code": 111, "friendlyName": "memoria-corsair-vengeance", "available": True},
            {"name": "Item sem código nenhum aqui", "priceWithDiscount": 3700, "available": True}]}}}
        items, _ = pw.extract_listing(next_page(blob), "https://www.kabum.com.br/busca/x")
        self.assertEqual(items[0]["url"],
                         "https://www.kabum.com.br/produto/526516/memoria-ram-xpg-lancer-rgb-32gb-2x16gb")
        self.assertEqual(items[1]["url"],
                         "https://www.kabum.com.br/produto/111/memoria-corsair-vengeance")
        self.assertIsNone(items[2]["url"])
        # em outra loja não inventa link
        items2, _ = pw.extract_listing(next_page(blob), "https://www.outraloja.com.br/busca/x")
        self.assertIsNone(items2[0]["url"])

    def test_link_from_anchors_when_json_ld_has_no_url(self):
        ld = {"@context": "https://schema.org", "@graph": [
            {"@type": "Product", "name": "Placa-Mãe Gigabyte B650M H, AMD, Micro ATX, Preto - B650M H",
             "offers": {"price": 966.56, "availability": "https://schema.org/InStock"}},
            {"@type": "Product", "name": "Placa Mãe Msi B550m-a Pro Am4 2xDDR4",
             "offers": {"price": 569.98}},
            {"@type": "Product", "name": "Produto sem link na pagina",
             "offers": {"price": 100}}]}
        html = ('<html><script type="application/ld+json">' + json.dumps(ld) + "</script><body>"
                '<a href="/produto/111/placa-mae-gigabyte-b650m-h-amd-micro-atx-preto-b650m-h">x</a>'
                '<a href="/produto/222/placa-mae-msi-b550m-a-pro-am4-2xddr4-chipset-amd">y</a>'
                '<a href="/produto/333/outra-coisa-totalmente-diferente">z</a></body></html>')
        items, m = pw.extract_listing(html, "https://www.kabum.com.br/busca/placa-mae")
        self.assertEqual(m, "json-ld")
        self.assertEqual(items[0]["url"], "https://www.kabum.com.br/produto/111/"
                         "placa-mae-gigabyte-b650m-h-amd-micro-atx-preto-b650m-h")
        self.assertEqual(items[1]["url"], "https://www.kabum.com.br/produto/222/"
                         "placa-mae-msi-b550m-a-pro-am4-2xddr4-chipset-amd")
        self.assertIsNone(items[2]["url"])

    def test_listing_and_filters(self):
        html = listing([("Memória Kingston 32GB (2x16GB) DDR4 3200", 520, True),
                        ("Memória 8GB DDR4 Notebook SODIMM", 150, True),
                        ("Memória XPG 32GB DDR4 3600", 580, False)])
        items, m = pw.extract_listing(html, "http://x.test/busca")
        self.assertEqual((m, len(items)), ("next-data", 3))
        self.assertEqual(items[0]["url"], "http://x.test/produto/0/slug")
        cfg = {"include": r"(?=.*DDR4)(?=.*32\s*GB)", "exclude": "sodimm"}
        kept = [i["name"] for i in items if pw.match_filters(i["name"], cfg)]
        self.assertEqual(len(kept), 2)


class RunTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.shop, cls.ntfy = serve(ShopHandler), serve(NtfyHandler)
        cls.shop_port = cls.shop.server_address[1]
        os.environ["NTFY_TOPIC"] = "teste"
        os.environ["NTFY_SERVER"] = f"http://127.0.0.1:{cls.ntfy.server_address[1]}"
        os.environ.pop("TELEGRAM_BOT_TOKEN", None)

    @classmethod
    def tearDownClass(cls):
        cls.shop.shutdown()
        cls.ntfy.shutdown()

    def setUp(self):
        Shop.pages = {}
        Ntfy.received, Ntfy.status = [], 200
        self.db = pw.open_db(":memory:") if False else sqlite3.connect(":memory:")
        self.db.executescript(pw.SCHEMA)

    def go(self, cfg, out_path=None):
        return pw.run(cfg, self.db, dry=False, delay=(0, 0), verbose=False, out_path=out_path)

    def age_history(self, days):
        """Recua as leituras já gravadas, simulando histórico de vários dias."""
        old = pw.iso(pw.utcnow() - pw.timedelta(days=days))
        self.db.execute("UPDATE prices SET ts=?", (old,))
        self.db.execute("DELETE FROM alerts")

    def base(self, host="127.0.0.1"):
        return f"http://{host}:{self.shop_port}"

    def test_alert_flow_target_dedup_and_lower_price(self):
        url = self.base() + "/p"
        cfg = {"products": [{"name": "RAM A", "url": url, "target": 600}]}
        Shop.pages[("127.0.0.1", "/p")] = (200, ld_page("RAM A", "700.00"))
        self.go(cfg)
        self.assertEqual(len(Ntfy.received), 0)               # acima da meta, sem histórico

        Shop.pages[("127.0.0.1", "/p")] = (200, ld_page("RAM A", "540.00"))
        self.go(cfg)
        self.assertEqual(len(Ntfy.received), 1)               # meta + queda de 23%
        msg = Ntfy.received[0]["message"]
        self.assertIn("abaixo da meta", msg)
        self.assertIn("queda de 23%", msg)
        self.assertIn("R$ 540,00", msg)

        self.go(cfg)                                          # mesmo preço: dedup
        self.assertEqual(len(Ntfy.received), 1)

        Shop.pages[("127.0.0.1", "/p")] = (200, ld_page("RAM A", "500.00"))
        self.go(cfg)                                          # caiu mais: avisa de novo
        self.assertEqual(len(Ntfy.received), 2)

    def test_out_of_stock_is_recorded_but_not_alerted(self):
        url = self.base() + "/p"
        cfg = {"products": [{"name": "RAM B", "url": url, "target": 600}]}
        Shop.pages[("127.0.0.1", "/p")] = (200, ld_page("RAM B", "300", "OutOfStock"))
        self.go(cfg)
        self.assertEqual(len(Ntfy.received), 0)
        self.assertEqual(self.db.execute("SELECT available FROM prices").fetchone()[0], 0)

    def test_new_low_without_target(self):
        url = self.base() + "/p"
        cfg = {"products": [{"name": "RAM C", "url": url}]}
        for _ in range(3):
            Shop.pages[("127.0.0.1", "/p")] = (200, ld_page("RAM C", "1000"))
            self.go(cfg)
        Shop.pages[("127.0.0.1", "/p")] = (200, ld_page("RAM C", "950"))   # só -5%, mas é mínima
        self.go(cfg)
        self.assertEqual(len(Ntfy.received), 0)       # histórico de minutos não vale como "mínimo"
        self.age_history(5)
        Shop.pages[("127.0.0.1", "/p")] = (200, ld_page("RAM C", "940"))
        self.go(cfg)
        self.assertEqual(len(Ntfy.received), 1)
        self.assertIn("menor preço já registrado", Ntfy.received[0]["message"])

    def test_search_filters_and_digest(self):
        Shop.pages[("127.0.0.1", "/busca")] = (200, listing([
            ("Memória Kingston 32GB (2x16GB) DDR4 3200", 520, True),
            ("Memória 8GB DDR4 Notebook SODIMM", 150, True),
            ("Memória XPG 32GB DDR4 3600 RGB", 900, True)]))
        cfg = {"search_urls": {"loja": self.base() + "/busca?q={q}"},
               "searches": [{"name": "DDR4 32GB", "query": "ddr4 32gb", "stores": ["loja"],
                             "include": r"(?=.*DDR4)(?=.*32\s*GB)", "exclude": "sodimm", "max_price": 600}]}
        self.go(cfg)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM items").fetchone()[0], 2)  # sodimm fora
        self.assertEqual(len(Ntfy.received), 1)               # só o de R$ 520 passa da meta
        self.assertIn("Kingston", Ntfy.received[0]["message"])
        self.assertNotIn("XPG", Ntfy.received[0]["message"])

    def test_search_hits_get_stock_confirmed_on_product_page(self):
        Shop.pages[("127.0.0.1", "/busca")] = (200, listing([
            ("Memória Kingston 32GB DDR4 3200 A", 500, None),
            ("Memória Corsair 32GB DDR4 3200 B", 510, None),
            ("Memória Rise 32GB DDR4 3200 C", 520, None),
            ("Memória Fury 32GB DDR4 3200 D", 900, None)]))      # acima da meta: nem abre a página
        Shop.pages[("127.0.0.1", "/produto/0/slug")] = (200, ld_page("A", "500", "InStock"))
        Shop.pages[("127.0.0.1", "/produto/1/slug")] = (200, ld_page("B", "510", "OutOfStock"))
        Shop.pages[("127.0.0.1", "/produto/2/slug")] = (200, "<html>sem dados</html>")
        cfg = {"search_urls": {"loja": self.base() + "/busca?q={q}"},
               "searches": [{"name": "DDR4", "query": "ddr4", "stores": ["loja"], "max_price": 600}]}
        out = Path(tempfile.mkdtemp()) / "last.json"
        self.go(cfg, out_path=out)
        msg = Ntfy.received[0]["message"]
        self.assertIn("Kingston", msg)
        self.assertIn("em estoque", msg)                       # A confirmado
        self.assertNotIn("Corsair", msg)                       # B esgotado: descartado
        self.assertIn("Rise", msg)                             # C sem sinal: avisa, marcado como não confirmado
        self.assertIn("NÃO confirmado", msg)
        itens = {i["nome"]: i for i in json.loads(out.read_text(encoding="utf-8"))["itens"]}
        self.assertTrue(itens["Memória Corsair 32GB DDR4 3200 B"]["esgotado"])
        self.assertTrue(itens["Memória Corsair 32GB DDR4 3200 B"]["sinal_estoque"].startswith("pagina-do-produto"))
        self.assertEqual(itens["Memória Fury 32GB DDR4 3200 D"]["estoque"], "estoque ?")
        # com confirm_stock desligado, volta ao comportamento antigo
        self.setUp()
        Shop.pages[("127.0.0.1", "/busca")] = (200, listing([("Memória Corsair 32GB DDR4 3200 B", 510, None)]))
        cfg["defaults"] = {"confirm_stock": False}
        self.go(cfg)
        self.assertIn("Corsair", Ntfy.received[0]["message"])

    def test_detail_price_replaces_listing_price(self):
        Shop.pages[("127.0.0.1", "/busca")] = (200, listing([
            ("Placa TUF B650M-E", 1622.22, True),          # preço "de" riscado da lista
            ("Placa Quebrada X", 900, True)]))             # página do produto falha: descartada
        Shop.pages[("127.0.0.1", "/produto/0/slug")] = (200, ld_page("TUF", "1149.99", "InStock"))
        Shop.pages[("127.0.0.1", "/produto/1/slug")] = (200, "<html>sem dados</html>")
        cfg = {"defaults": {"detail_price_stores": ["127.0.0.1"], "confirm_stock": False},
               "search_urls": {"loja": self.base() + "/busca?q={q}"},
               "searches": [{"name": "placa", "query": "placa", "stores": ["loja"]}]}
        self.go(cfg)
        rows = self.db.execute("SELECT i.name, p.price FROM prices p JOIN items i ON i.key=p.key").fetchall()
        self.assertEqual(rows, [("Placa TUF B650M-E", 1149.99)])

    def test_detail_price_only_for_cheapest_per_search(self):
        Shop.pages[("127.0.0.1", "/busca")] = (200, listing([
            ("Placa A", 1000, True), ("Placa B", 2000, True), ("Placa C", 3000, True)]))
        for i, pr in enumerate(("900", "1900", "2900")):
            Shop.pages[("127.0.0.1", f"/produto/{i}/slug")] = (200, ld_page("P", pr, "InStock"))
        cfg = {"defaults": {"detail_price_stores": ["127.0.0.1"], "detail_per_search": 2, "confirm_stock": False},
               "search_urls": {"loja": self.base() + "/busca?q={q}"},
               "searches": [{"name": "placa", "query": "placa", "stores": ["loja"]}]}
        self.go(cfg)
        rows = sorted(self.db.execute("SELECT p.price FROM prices p").fetchall())
        self.assertEqual(rows, [(900.0,), (1900.0,)])     # a mais cara da lista não foi gravada

    def test_product_link_wins_over_search_duplicate(self):
        Shop.pages[("127.0.0.1", "/busca")] = (200, listing([("Placa TUF", 1622.22, True)]))
        Shop.pages[("127.0.0.1", "/produto/0/slug")] = (200, ld_page("Placa TUF", "1149.99", "InStock"))
        cfg = {"defaults": {"detail_price_stores": []},
               "products": [{"name": "Placa TUF", "url": self.base() + "/produto/0/slug"}],
               "search_urls": {"loja": self.base() + "/busca?q={q}"},
               "searches": [{"name": "placa", "query": "placa", "stores": ["loja"]}]}
        self.go(cfg)
        rows = self.db.execute("SELECT price FROM prices").fetchall()
        self.assertEqual(rows, [(1149.99,)])

    def test_empty_listing_saves_debug_html(self):
        Shop.pages[("127.0.0.1", "/busca")] = (200, "<html><title>Algo   estranho</title></html>")
        cfg = {"search_urls": {"loja": self.base() + "/busca?q={q}"},
               "searches": [{"name": "x", "query": "x", "stores": ["loja"]}]}
        old = pw.DEBUG_DIR
        pw.DEBUG_DIR = Path(tempfile.mkdtemp()) / "debug"
        try:
            self.go(cfg)
            self.assertTrue((pw.DEBUG_DIR / "127.0.0.1.html").exists())
        finally:
            pw.DEBUG_DIR = old

    def test_login_wall_is_detected_as_challenge(self):
        html = "<html><title>Mercado Libre</title><body>Olá! Para continuar, acesse sua conta</body></html>"
        self.assertTrue(pw.looks_like_challenge(html))

    def test_login_wall_blocks_store_for_the_run(self):
        wall = "<html><title>Mercado Libre</title><body>Olá! Para continuar, acesse sua conta</body></html>"
        self.assertTrue(pw.looks_like_challenge(wall))
        Shop.pages[("127.0.0.1", "/busca")] = (200, wall)
        cfg = {"search_urls": {"loja": self.base() + "/busca?q={q}"},
               "searches": [{"name": "a", "query": "a", "stores": ["loja"]},
                            {"name": "b", "query": "b", "stores": ["loja"]}]}
        old = pw.DEBUG_DIR
        pw.DEBUG_DIR = Path(tempfile.mkdtemp()) / "debug"
        hits = []
        orig = pw.fetch
        pw.fetch = lambda *a, **k: (hits.append(1), orig(*a, **k))[1]
        try:
            self.go(cfg)
        finally:
            pw.fetch, pw.DEBUG_DIR = orig, old
        self.assertEqual(len(hits), 1)               # a 2ª busca foi pulada

    def test_multiplicador_scales_price_for_products_and_searches(self):
        Shop.pages[("127.0.0.1", "/p16")] = (200, ld_page("RAM 16", "400.00"))
        Shop.pages[("127.0.0.1", "/busca")] = (200, listing([("Memoria 16GB DDR4 3200", 300, True)]))
        cfg = {"defaults": {"confirm_stock": False},
               "products": [{"name": "RAM 16", "url": self.base() + "/p16", "multiplicador": 2}],
               "search_urls": {"loja": self.base() + "/busca?q={q}"},
               "searches": [{"name": "r", "query": "r", "stores": ["loja"], "multiplicador": 2}]}
        self.go(cfg)
        rows = dict(self.db.execute("SELECT i.name, p.price FROM prices p JOIN items i ON i.key=p.key").fetchall())
        self.assertEqual(rows["RAM 16 [preço ×2]"], 800.0)
        self.assertEqual(rows["Memoria 16GB DDR4 3200 [preço ×2]"], 600.0)

    def test_disabled_stores_are_not_requested(self):
        cfg = {"products": [{"name": "a", "url": self.base("127.0.0.1") + "/a"},
                            {"name": "b", "url": self.base("localhost") + "/b"}],
               "disabled_stores": ["LOCALHOST"]}
        jobs = pw.build_jobs(cfg)
        self.assertEqual([j.label for j in jobs], ["a"])

    def test_failed_notification_does_not_mark_alert(self):
        url = self.base() + "/p"
        cfg = {"products": [{"name": "RAM D", "url": url, "target": 600}]}
        Shop.pages[("127.0.0.1", "/p")] = (200, ld_page("RAM D", "500"))
        Ntfy.status = 500
        self.assertEqual(self.go(cfg), 2)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM alerts").fetchone()[0], 0)
        Ntfy.status = 200
        self.go(cfg)                                          # tenta de novo e agora avisa
        self.assertEqual(len(Ntfy.received), 1)

    def test_blocked_store_is_skipped_and_health_alert_after_3_runs(self):
        cfg = {"products": [
            {"name": "ok", "url": self.base("127.0.0.1") + "/ok", "target": 1},
            {"name": "bloq", "url": self.base("localhost") + "/b1"},
            {"name": "bloq2", "url": self.base("localhost") + "/b2"}]}
        Shop.pages[("127.0.0.1", "/ok")] = (200, ld_page("ok", "100"))
        Shop.pages[("localhost", "/b1")] = (403, "blocked")
        Shop.pages[("localhost", "/b2")] = (200, ld_page("bloq2", "100"))   # não deve ser nem pedido
        for _ in range(3):
            self.go(cfg)
        titles = [m["title"] for m in Ntfy.received]
        self.assertEqual(titles.count("Monitor de preços: precisa de atenção"), 1)
        self.assertIn("localhost", Ntfy.received[-1]["message"])
        self.go(cfg)                                          # 4ª falha: não repete o aviso
        titles = [m["title"] for m in Ntfy.received]
        self.assertEqual(titles.count("Monitor de preços: precisa de atenção"), 1)
        self.assertEqual(self.db.execute(
            "SELECT COUNT(*) FROM items WHERE name='bloq2'").fetchone()[0], 0)

    def test_heartbeat_only_when_enabled_and_no_offer(self):
        Shop.pages[("127.0.0.1", "/ok")] = (200, ld_page("ok", "100"))
        cfg = {"products": [{"name": "ok", "url": self.base("127.0.0.1") + "/ok"}]}
        self.go(cfg)                                           # desligado por padrão
        self.assertEqual(Ntfy.received, [])
        cfg["defaults"] = {"heartbeat": True}
        self.go(cfg)
        self.assertEqual(len(Ntfy.received), 1)
        self.assertTrue(Ntfy.received[0]["title"].startswith("Monitor rodou às "))
        self.assertIn("1 itens lidos", Ntfy.received[0]["message"])
        cfg["products"][0]["target"] = 500                     # com oferta: só o aviso da oferta
        n = len(Ntfy.received)
        self.go(cfg)
        self.assertEqual(len(Ntfy.received), n + 1)
        self.assertNotIn("Monitor rodou", Ntfy.received[-1]["title"])

    # ---- estoque ----
    def test_sold_out_cheap_item_ignored_and_marked_in_json(self):
        Shop.pages[("127.0.0.1", "/busca")] = (200, listing([
            ("Memória Kingston 32GB DDR4 3200 (preço defasado)", 199, False),
            ("Memória Corsair 32GB DDR4 3200", 520, True),
            ("Memória XPG 32GB DDR4 3200 sem informação", 580, None)]))
        cfg = {"search_urls": {"loja": self.base() + "/busca?q={q}"},
               "searches": [{"name": "DDR4", "query": "ddr4", "stores": ["loja"], "max_price": 600}]}
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "last_run.json"
            self.go(cfg, out)
            data = json.loads(out.read_text(encoding="utf-8"))
        by_name = {i["nome"]: i for i in data["itens"]}
        sold = by_name["Memória Kingston 32GB DDR4 3200 (preço defasado)"]
        self.assertEqual((sold["estoque"], sold["esgotado"], sold["alerta"]), ("ESGOTADO", True, None))
        self.assertEqual(by_name["Memória Corsair 32GB DDR4 3200"]["estoque"], "em estoque")
        self.assertEqual(by_name["Memória XPG 32GB DDR4 3200 sem informação"]["estoque"], "estoque ?")
        self.assertEqual(data["resumo_por_loja"]["127.0.0.1"],
                         {"lidos": 3, "em_estoque": 1, "esgotados": 1, "estoque_desconhecido": 1})
        msg = Ntfy.received[0]["message"]
        self.assertNotIn("defasado", msg)            # o esgotado de R$ 199 não gera aviso
        self.assertIn("Corsair", msg)
        self.assertIn("estoque NÃO confirmado", msg)  # o sem informação avisa, mas com ressalva

    def test_strict_stock_skips_unconfirmed(self):
        url = self.base() + "/p"
        data = {"@type": "Product", "name": "RAM", "offers": {"price": "500"}}
        Shop.pages[("127.0.0.1", "/p")] = (
            200, f'<script type="application/ld+json">{json.dumps(data)}</script>')
        cfg = {"products": [{"name": "RAM", "url": url, "target": 600}]}
        self.go({**cfg, "defaults": {"strict_stock": True}})
        self.assertEqual(len(Ntfy.received), 0)
        self.go(cfg)                                  # sem modo estrito: avisa com ressalva
        self.assertEqual(len(Ntfy.received), 1)

    def test_sold_out_reading_does_not_pollute_history(self):
        url = self.base() + "/p"
        cfg = {"products": [{"name": "RAM", "url": url}]}
        seq = [("600", "InStock")] * 3 + [("100", "OutOfStock")]
        for price, av in seq:
            Shop.pages[("127.0.0.1", "/p")] = (200, ld_page("RAM", price, av))
            self.go(cfg)
        self.age_history(5)
        Shop.pages[("127.0.0.1", "/p")] = (200, ld_page("RAM", "550", "InStock"))
        self.go(cfg)
        # sem o filtro, o "100 esgotado" viraria referência e esconderia esta queda real
        self.assertEqual(len(Ntfy.received), 1)
        self.assertIn("menor preço já registrado", Ntfy.received[0]["message"])
        self.assertIn("queda de 8%", Ntfy.received[0]["message"])

    def test_text_fallback_marks_product_page_sold_out(self):
        url = self.base() + "/p"
        Shop.pages[("127.0.0.1", "/p")] = (200, (
            '<meta property="product:price:amount" content="300"><body>Produto indisponível</body>'))
        self.go({"products": [{"name": "RAM", "url": url, "target": 600}]})
        self.assertEqual(len(Ntfy.received), 0)
        self.assertEqual(self.db.execute("SELECT available FROM prices").fetchone()[0], 0)

    def test_items_without_link_are_flagged_and_view_and_export_show_link(self):
        blob = {"props": {"pageProps": {"products": [
            {"name": "Memória DDR4 32GB sem link", "priceWithDiscount": 520, "available": True}]}}}
        Shop.pages[("127.0.0.1", "/busca")] = (200, next_page(blob))
        search_url = self.base() + "/busca?q=ddr4"
        cfg = {"search_urls": {"loja": self.base() + "/busca?q={q}"},
               "searches": [{"name": "DDR4", "query": "ddr4", "stores": ["loja"], "max_price": 600}]}
        with tempfile.TemporaryDirectory() as tmp:
            out, csv_path = Path(tmp) / "r.json", Path(tmp) / "p.csv"
            self.go(cfg, out)
            item = json.loads(out.read_text(encoding="utf-8"))["itens"][0]
            self.assertFalse(item["link_direto"])
            self.assertEqual(item["url"], search_url)
            row = self.db.execute("SELECT loja, nome, preco, estoque, link FROM precos").fetchone()
            self.assertEqual(row[1:], ("Memória DDR4 32GB sem link", 520.0, "em estoque", search_url))
            pw.cmd_export(self.db, csv_path)
            raw = csv_path.read_bytes()
            self.assertTrue(raw.startswith(b"\xef\xbb\xbf"))          # BOM para o Excel
            text = raw.decode("utf-8-sig")
            self.assertIn("loja;nome;preco;estoque;link;lido_em", text)
            self.assertIn("520,00", text)
            self.assertIn(search_url, text)

    def test_categories_views_export_history_and_alert_label(self):
        Shop.pages[("127.0.0.1", "/cpu")] = (200, ld_page("Ryzen 5 5600", "700"))
        Shop.pages[("127.0.0.1", "/ssd")] = (200, ld_page("SSD NVMe 1TB", "600"))
        Shop.pages[("127.0.0.1", "/ssd2")] = (200, ld_page("SSD sem categoria", "650"))
        cfg = {"products": [
            {"name": "CPU Ryzen 5 5600", "url": self.base() + "/cpu", "categoria": "processador",
             "plataforma": "AM4", "target": 800},
            {"name": "SSD NVMe 1TB", "url": self.base() + "/ssd", "categoria": "ssd"},
            {"name": "outro", "url": self.base() + "/ssd2"}]}
        self.go(cfg)
        self.assertIn("[processador AM4]", Ntfy.received[0]["message"])
        rows = self.db.execute("SELECT categoria, plataforma, nome FROM atual_processador").fetchall()
        self.assertEqual(rows, [("processador", "AM4", "CPU Ryzen 5 5600")])
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM atual_ssd").fetchone()[0], 1)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM atual_memoria").fetchone()[0], 0)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM atual").fetchone()[0], 3)
        with tempfile.TemporaryDirectory() as tmp:
            csv_path = Path(tmp) / "precos.csv"
            pw.cmd_export(self.db, csv_path, por_categoria=True)
            self.assertTrue((Path(tmp) / "precos_processador.csv").exists())
            self.assertTrue((Path(tmp) / "precos_ssd.csv").exists())
            self.assertFalse((Path(tmp) / "precos_memoria.csv").exists())
            cpu_csv = (Path(tmp) / "precos_processador.csv").read_text(encoding="utf-8-sig")
            self.assertIn("Ryzen 5 5600", cpu_csv)
            self.assertNotIn("SSD NVMe", cpu_csv)

    def test_old_database_is_migrated_and_keeps_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "old.db"
            old = sqlite3.connect(path)
            old.executescript("""
                CREATE TABLE items(key TEXT PRIMARY KEY, store TEXT, name TEXT, url TEXT, source TEXT);
                CREATE TABLE prices(key TEXT, ts TEXT, price REAL, available INTEGER);
                CREATE TABLE alerts(key TEXT, ts TEXT, price REAL, reason TEXT);
                CREATE TABLE health(store TEXT PRIMARY KEY, fails INTEGER, notified INTEGER, last_ok TEXT);
                CREATE VIEW precos AS SELECT i.store AS loja FROM prices p JOIN items i ON i.key=p.key;
                INSERT INTO items VALUES('k1','KaBuM','Memória X','https://x/p/1','search:x');
                INSERT INTO prices VALUES('k1','2026-10-23T10:00:00+00:00',500.0,1);""")
            old.commit(); old.close()
            db = pw.open_db(path)
            self.assertEqual(db.execute("SELECT nome, preco, categoria FROM atual").fetchone(),
                             ("Memória X", 500.0, None))
            self.assertEqual(db.execute("SELECT COUNT(*) FROM precos").fetchone()[0], 1)
            pw.cmd_history(db, 10)      # sem categoria: não pode quebrar
            db.close()

    def test_open_db_waits_for_lock_and_does_not_rewrite_schema_each_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "p.db"
            pw.open_db(path).close()                    # primeira vez: cria e marca a versão
            holder = sqlite3.connect(path, isolation_level=None)
            holder.execute("BEGIN IMMEDIATE")           # outro programa segurando escrita
            try:
                db = pw.open_db(path, timeout=0.2)      # versão em dia: não escreve, não trava
                self.assertEqual(db.execute("SELECT COUNT(*) FROM atual").fetchone()[0], 0)
                db.close()
                with self.assertRaises(sqlite3.OperationalError):   # escrever de verdade trava
                    c = sqlite3.connect(path, timeout=0.2)
                    c.execute("INSERT INTO health(store,fails,notified) VALUES('x',0,0)")
            finally:
                holder.execute("ROLLBACK")
                holder.close()

    def test_items_with_link_are_marked_direct(self):
        Shop.pages[("127.0.0.1", "/busca")] = (200, listing([("Memória DDR4 32GB", 520, True)]))
        cfg = {"search_urls": {"loja": self.base() + "/busca?q={q}"},
               "searches": [{"name": "DDR4", "query": "ddr4", "stores": ["loja"], "max_price": 600}]}
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "r.json"
            self.go(cfg, out)
            item = json.loads(out.read_text(encoding="utf-8"))["itens"][0]
        self.assertTrue(item["link_direto"])
        self.assertTrue(item["url"].endswith("/produto/0/slug"))

    def test_missing_price_counts_as_failure_not_crash(self):
        cfg = {"products": [{"name": "x", "url": self.base() + "/vazio"}]}
        Shop.pages[("127.0.0.1", "/vazio")] = (200, "<html>sem preço</html>")
        self.assertEqual(self.go(cfg), 0)
        self.assertEqual(self.db.execute("SELECT fails FROM health").fetchone()[0], 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
