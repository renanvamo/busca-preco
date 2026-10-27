import json, sqlite3, unittest
from datetime import timedelta
import build_setups as bs
import price_watch as pw

PARTS = [  # nome, categoria, plataforma, loja, preço, disponível
 ("Ryzen 7 5800X3D Anniversary - Terabyte (sem cooler)", "processador", "AM4", "Terabyte", 2229.99, 1),
 ("Processador AMD Ryzen 7 5800X3D, 3.4GHz", "processador", "AM4", "KaBuM", 2049.99, None),
 ("Ryzen 7 5700X3D - Terabyte (sem cooler)", "processador", "AM4", "Terabyte", 1566.54, 0),
 ("Ryzen 5 7600 - KaBuM", "processador", "AM5", "KaBuM", 999.99, 1),
 ("Ryzen 5 7600 - Terabyte (com cooler Wraith Stealth)", "processador", "AM5", "Terabyte", 1299.99, 1),
 ("Processador AMD Ryzen 7 7800X3D, 4.2GHz", "processador", "AM5", "KaBuM", 1849.99, 1),
 ("Ryzen 7 9800X3D - KaBuM", "processador", "AM5", "KaBuM", 2389.99, 1),
 ("Placa de Vídeo Gigabyte RTX 5060 EAGLE MAX OC 8GB", "placa_de_video", "", "KaBuM", 2999.99, 1),
 ("RTX 5060 Ti 16GB - PCYes (Terabyte)", "placa_de_video", "", "Terabyte", 4739.99, 1),
 ("RX 9060 XT 16GB - ASUS Dual (Terabyte)", "placa_de_video", "", "Terabyte", 3599.99, 1),
 ("Placa de Video RTX 5070 Ti 16GB Qualquer", "placa_de_video", "", "KaBuM", 6900.00, 1),
 ("Placa de Video RX 9070 XT 16GB Qualquer", "placa_de_video", "", "KaBuM", 5200.00, 1),
 ("Placa de Video RTX 5070 12GB Qualquer", "placa_de_video", "", "KaBuM", 4300.00, 1),
 ("RAM DDR4 16GB 3200 Synex - Terabyte (x2 = 32GB) [preço ×2]", "memoria", "AM4", "Terabyte", 729.99, 1),
 ("Memória RAM XPG Lancer Blade, 16GB DDR5 6000 [preço ×2]", "memoria", "AM5", "KaBuM", 4159.98, 1),
]

def make_db():
    db = sqlite3.connect(":memory:")
    db.executescript(pw.SCHEMA)
    now = pw.utcnow()
    for i, (nome, cat, plat, loja, preco, av) in enumerate(PARTS):
        db.execute("INSERT INTO items(key,store,name,url,source,categoria,plataforma) VALUES(?,?,?,?,?,?,?)",
                   (f"k{i}", loja, nome, f"https://x/{i}", "t", cat, plat))
        db.execute("INSERT INTO prices(key,ts,price,available) VALUES(?,?,?,?)", (f"k{i}", pw.iso(now), preco, av))
    db.commit()
    return db

class SetupTests(unittest.TestCase):
    def setUp(self):
        self.db, self.bench = make_db(), bs.load_bench()

    def test_cheapest_in_stock_per_model(self):
        r = bs.compute(self.db, self.bench)
        w = r["plataformas"]["AM5"]["1440p"]["vencedor"]
        self.assertEqual(w["cpu"]["preco"] in (999.99, 1849.99, 2389.99), True)
        r_cpu = {c["modelo_nome"]: c["preco"] for c in r["ranking"]["cpus"]}
        self.assertEqual(r_cpu["Ryzen 5 7600"], 999.99)          # KaBuM, mais barato que a Terabyte
        self.assertEqual(r_cpu["Ryzen 7 5800X3D"], 2049.99)
        self.assertNotIn("Ryzen 7 5700X3D", r_cpu)               # esgotado: fora
        self.assertIn("Ryzen 7 5700X3D", r["faltando"])
        self.assertNotIn("Ryzen 5 5600", r_cpu)

    def test_regex_does_not_mix_models(self):
        r = bs.compute(self.db, self.bench)
        nomes = {g["modelo_nome"] for g in r["ranking"]["gpus"]}
        self.assertEqual(nomes, {"RTX 5060 8GB", "RTX 5060 Ti 16GB", "RX 9060 XT 16GB", "RTX 5070 12GB",
                                 "RX 9070 XT 16GB", "RTX 5070 Ti 16GB"})

    def test_target_fps_filters_and_totals(self):
        r = bs.compute(self.db, self.bench, 60)
        b = r["plataformas"]["AM5"]["4k"]
        for o in b["opcoes"]:
            self.assertGreaterEqual(o["fps"], 60)               # 4K a 60 FPS: só 5070 Ti / 9070 XT
        w = b["vencedor"]
        self.assertEqual(w["total"], round(w["cpu"]["preco"] + w["gpu"]["preco"] + w["ram"]["preco"]
                                           + w["custo_fixo"], 2))
        r2 = bs.compute(self.db, self.bench, 400)                # meta impossível: avisa e mostra o melhor geral
        self.assertIsNotNone(r2["plataformas"]["AM5"]["1080p"]["aviso"])
        self.assertIsNotNone(r2["plataformas"]["AM5"]["1080p"]["vencedor"])

    def test_cpu_bottleneck_model(self):
        # com GPU fraca no 4K, o processador quase não importa; com GPU forte no 1080p, importa
        fps_4k = {c: bs.softmin(21.8, 190 * i / 100, 4) for c, i in (("a", 59), ("b", 100))}
        self.assertLess(fps_4k["b"] / fps_4k["a"], 1.01)
        f = {c: bs.softmin(156.6, 190 * i / 100, 4) for c, i in (("a", 59), ("b", 100))}
        self.assertGreater(f["b"] / f["a"], 1.3)

    def test_options_have_marginal_cost_and_one_winner(self):
        r = bs.compute(self.db, self.bench, 60)
        opts = r["plataformas"]["AM5"]["1440p"]["opcoes"]
        self.assertEqual(sum(1 for o in opts if o["eh_vencedor"]), 1)
        totais = [o["total"] for o in opts]
        self.assertEqual(totais, sorted(totais))
        fps = [o["fps"] for o in opts]
        self.assertEqual(fps, sorted(fps))                       # fronteira: mais caro => mais FPS
        for o in opts[1:]:
            self.assertIn("vs_anterior", o)

    def test_flag_removes_part_until_price_changes(self):
        base = bs.compute(self.db, self.bench)
        self.assertIn("Ryzen 5 7600", {c["modelo_nome"] for c in base["ranking"]["cpus"]})
        key = [c["key"] for c in base["ranking"]["cpus"] if c["modelo_nome"] == "Ryzen 5 7600"][0]
        self.assertTrue(bs.flag_price(self.db, key, "esgotado"))
        r = bs.compute(self.db, self.bench)
        cpus = {c["modelo_nome"]: c for c in r["ranking"]["cpus"]}
        self.assertEqual(cpus["Ryzen 5 7600"]["preco"], 1299.99)  # caiu para a Terabyte
        self.assertEqual(len(r["invalidados"]), 1)
        # novo preço lido: o item volta
        self.db.execute("INSERT INTO prices(key,ts,price,available) VALUES(?,?,?,1)",
                        (key, pw.iso(pw.utcnow() + timedelta(seconds=5)), 1049.99))
        r = bs.compute(self.db, self.bench)
        self.assertEqual({c["modelo_nome"]: c for c in r["ranking"]["cpus"]}["Ryzen 5 7600"]["preco"], 1049.99)
        self.assertEqual(r["invalidados"], [])
        # restaurar manualmente
        bs.flag_price(self.db, key, "preço mudou")
        bs.unflag(self.db, key)
        self.assertEqual(bs.compute(self.db, self.bench)["invalidados"], [])

    def test_stale_readings_are_ignored(self):
        later = pw.utcnow() + timedelta(hours=100)
        r = bs.compute(self.db, self.bench, now=later)
        self.assertEqual(r["ranking"]["cpus"], [])

    def test_cooler_included_lowers_fixed_cost(self):
        r = bs.compute(self.db, self.bench)
        by = {(o["cpu"]["modelo"], o["gpu"]["modelo"]): o
              for p in r["plataformas"]["AM5"]["1440p"]["opcoes"] for o in [p]}
        for o in by.values():
            if o["cpu"]["modelo"] == "7600":
                self.assertEqual(o["fixos"]["cooler"], 0)
            else:
                self.assertEqual(o["fixos"]["cooler"], 150)


class PecasTests(unittest.TestCase):
    def setUp(self):
        self.db, self.bench = make_db(), bs.load_bench()

    def test_listar_pecas_por_loja(self):
        r = bs.listar_pecas(self.db, self.bench)
        nomes_cpu = [(c["modelo_nome"], c["loja"]) for c in r["cpus"]]
        self.assertIn(("Ryzen 5 7600", "KaBuM"), nomes_cpu)
        self.assertIn(("Ryzen 5 7600", "Terabyte"), nomes_cpu)           # cada loja aparece
        am5 = [c["indice"] for c in r["cpus"] if c["plataforma"] == "AM5"]
        self.assertEqual(am5, sorted(am5, reverse=True))                 # ranking por desempenho
        self.assertEqual({x["plataforma"] for x in r["rams"]}, {"AM4", "AM5"})
        self.assertEqual(r["gpus"][0]["modelo_nome"], "RTX 5070 Ti 16GB")

    def test_avaliar_soma_e_avisos(self):
        r = bs.listar_pecas(self.db, self.bench)
        cpu = next(c for c in r["cpus"] if c["modelo_nome"] == "Ryzen 7 7800X3D")
        gpu = next(g for g in r["gpus"] if g["modelo_nome"] == "RX 9060 XT 16GB")
        ram5 = next(x for x in r["rams"] if x["plataforma"] == "AM5")
        ram4 = next(x for x in r["rams"] if x["plataforma"] == "AM4")
        a = bs.avaliar(self.db, self.bench, cpu["key"], ram5["key"], gpu["key"])
        self.assertAlmostEqual(a["total"], 1849.99 + 4159.98 + 3599.99, places=2)
        self.assertEqual(a["avisos"], [])
        self.assertGreater(a["fps"]["1080p"], a["fps"]["1440p"])
        self.assertGreater(a["fps"]["1440p"], a["fps"]["4k"])
        b = bs.avaliar(self.db, self.bench, cpu["key"], ram4["key"], gpu["key"])
        self.assertTrue(any("não serve" in x for x in b["avisos"]))      # DDR4 em AM5
        self.assertEqual(a["plataforma"], "AM5")
        self.assertEqual(a["outras_total"], 1200 + 1000 + 400 + 200)    # placa-mãe + NVMe + fonte + gabinete
        self.assertAlmostEqual(a["completo"], a["total"] + 2800, places=2)
        self.assertTrue(a["sem_cooler"])                                  # 7800X3D não vem com cooler
        ram4_ = bs.avaliar(self.db, self.bench, None, ram4["key"], None)
        self.assertEqual(ram4_["outras_total"], 900 + 1000 + 400 + 200)  # AM4: placa-mãe 900
        self.assertIsNone(bs.avaliar(self.db, self.bench, None, None, gpu["key"])["completo"])
        c = bs.avaliar(self.db, self.bench, cpu["key"], None, None)
        self.assertEqual(c["total"], 1849.99)
        self.assertIsNone(c["fps"])
        d = bs.avaliar(self.db, self.bench, "inexistente", None, None)
        self.assertEqual(d["total"], 0)
        self.assertTrue(d["avisos"])



def ld(price, avail="InStock"):
    return ('<html><script type="application/ld+json">{"@type":"Product","name":"X","offers":{"@type":"Offer",'
            f'"price":"{price}","priceCurrency":"BRL","availability":"https://schema.org/{avail}"}}}}</script></html>')


class AtualizarTests(unittest.TestCase):
    def setUp(self):
        import webapp
        self.wa = webapp
        self.db = make_db()
        self.bench = bs.load_bench()

    def key_of(self, nome_parte):
        return next(k for k, n in self.db.execute("SELECT key, name FROM items") if nome_parte in n)

    def cpu_price(self):
        r = bs.listar_pecas(self.db, self.bench)
        return {(c["modelo_nome"], c["loja"]): c["preco"] for c in r["cpus"]}

    def test_price_change_updates_list(self):
        k = self.key_of("Ryzen 5 7600 - KaBuM")
        r = self.wa.atualizar_item(self.db, k, lambda url: ld("899.90"))
        self.assertEqual(r["resultado"], "mudou")
        self.assertEqual(self.cpu_price()[("Ryzen 5 7600", "KaBuM")], 899.90)

    def test_sold_out_removes_from_list(self):
        k = self.key_of("Ryzen 5 7600 - KaBuM")
        r = self.wa.atualizar_item(self.db, k, lambda url: ld("999.99", "OutOfStock"))
        self.assertEqual(r["resultado"], "esgotado")
        self.assertNotIn(("Ryzen 5 7600", "KaBuM"), self.cpu_price())

    def test_same_price_and_multiplier_kept(self):
        k = self.key_of("Ryzen 5 7600 - KaBuM")
        self.assertEqual(self.wa.atualizar_item(self.db, k, lambda url: ld("999.99"))["resultado"], "igual")
        kr = self.key_of("Synex")                      # nome tem [preço ×2]: a página mostra o preço de 1 pente
        r = self.wa.atualizar_item(self.db, kr, lambda url: ld("400.00"))
        self.assertEqual(r["novo"], 800.0)

    def test_errors_do_not_change_db(self):
        k = self.key_of("Ryzen 5 7600 - KaBuM")
        n = self.db.execute("SELECT COUNT(*) FROM prices").fetchone()[0]
        def blocked(url): raise pw.Blocked("HTTP 403")
        self.assertEqual(self.wa.atualizar_item(self.db, k, blocked)["resultado"], "erro")
        self.assertEqual(self.wa.atualizar_item(self.db, k, lambda u: "<html>nada</html>")["resultado"], "erro")
        self.assertEqual(self.wa.atualizar_item(self.db, "nao-existe", lambda u: ld("1"))["resultado"], "erro")
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM prices").fetchone()[0], n)


class HistoricoTests(unittest.TestCase):
    """Mín/máx e selo de queda a partir de uma série de leituras (uma por hora)."""
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.db.executescript(pw.SCHEMA)
        self.now = pw.utcnow()
        self.db.execute("INSERT INTO items(key,store,name,url,source,categoria,plataforma) VALUES('k','L','x','u','t','placa_de_video','')")

    def serie(self, precos, avail=None, horas_atras=None):
        n = len(precos)
        for i, p in enumerate(precos):
            ts = self.now - timedelta(hours=(horas_atras if horas_atras is not None else n) - i)
            a = avail[i] if avail else 1
            self.db.execute("INSERT INTO prices(key,ts,price,available) VALUES('k',?,?,?)", (pw.iso(ts), p, a))
        self.db.commit()

    def h(self, atual, dias=30):
        return bs.historico(self.db, {"k": atual}, dias, self.now).get("k")

    def test_sem_historico_suficiente(self):
        self.serie([100, 100, 100])
        h = self.h(100)
        self.assertIsNone(h["min"]); self.assertIsNone(h["selo"])

    def test_queda_recente_mostra_valor_anterior(self):
        self.serie([1000, 1000, 1000, 900, 900])
        h = self.h(900)
        self.assertEqual((h["min"], h["max"]), (900, 1000))
        self.assertEqual(h["selo"]["tipo"], "queda"); self.assertEqual(h["selo"]["ref"], 1000)
        self.assertEqual(h["selo"]["pct"], -10.0); self.assertEqual(h["var_pct"], -10.0)

    def test_alta_recente(self):
        self.serie([900, 900, 1000, 1000])
        self.assertEqual(self.h(1000)["selo"]["tipo"], "alta")

    def test_queda_antiga_compara_com_maximo_do_periodo(self):
        self.serie([1000, 1000, 900, 900], horas_atras=200)       # mudou há ~8 dias: não é recente
        h = self.h(900)
        self.assertEqual((h["selo"]["tipo"], h["selo"]["base"], h["selo"]["ref"]), ("queda", "máx", 1000))

    def test_leitura_isolada_e_ruido(self):
        self.serie([1000, 1000, 1000, 1500, 1000, 1000])           # 1 leitura de 1500 = erro da loja
        h = self.h(1000)
        self.assertIsNone(h["min"]); self.assertIsNone(h["selo"])  # nada mudou de verdade

    def test_esgotado_nao_entra_e_janela_corta(self):
        self.serie([2000, 2000, 1000, 1000], avail=[0, 0, 1, 1])  # 2000 era esgotado: ignora
        self.assertIsNone(self.h(1000)["min"])
        self.db.execute("DELETE FROM prices"); self.db.commit()
        self.serie([1200, 1200, 1000, 1000], horas_atras=24 * 40)  # 40 dias atrás
        self.assertIsNone((self.h(1000, dias=30) or {}).get("min"))  # fora da janela de 30 dias
        self.assertIsNotNone(self.h(1000, dias=None)["min"])       # "tudo" enxerga

    def test_faixa_de_cor_do_preco(self):
        f = bs.faixa_preco
        self.assertEqual([f(100, 100, 200), f(120, 100, 200), f(135, 100, 200), f(150, 100, 200),
                          f(165, 100, 200), f(180, 100, 200), f(200, 100, 200)], [-3, -2, -1, 0, 1, 2, 3])
        self.assertEqual(f(101, 100, 102), 0)                    # variação < 3%: sem cor
        self.serie([1000, 1000, 1000, 900, 900])
        self.assertEqual(self.h(900)["faixa"], -3)               # no mínimo do período
        self.db.execute("DELETE FROM prices"); self.db.commit()
        self.serie([900, 900, 900, 1000, 1000])
        self.assertEqual(self.h(1000)["faixa"], 3)               # no máximo do período

    def test_listar_pecas_inclui_hist(self):
        db = make_db()
        r = bs.listar_pecas(db, bs.load_bench())
        for p in r["cpus"] + r["rams"] + r["gpus"]:
            self.assertIn("hist", p)
        self.assertEqual(r["dias"], 30)


class SiteTests(unittest.TestCase):
    def test_build_site_gera_pagina_estatica_sem_segredos(self):
        import tempfile, os
        from pathlib import Path
        import build_site
        with tempfile.TemporaryDirectory() as tmp:
            dbp = Path(tmp) / "t.db"
            src = make_db()
            disk = sqlite3.connect(str(dbp)); src.backup(disk); disk.close()
            out = Path(tmp) / "site"
            data = build_site.build(dbp, out)
            self.assertEqual(set(data["pecas"]), {"7", "30", "90", "0"})
            self.assertEqual(set(data["config"]), {"cap_fps_base", "expoente", "fps_referencia", "custos_fixos"})
            self.assertIn("const MODO_ESTATICO = true;", (out / "index.html").read_text(encoding="utf-8"))
            self.assertTrue((out / ".nojekyll").exists())
            txt = (out / "data.json").read_text(encoding="utf-8")
            os.environ.pop("NTFY_TOPIC", None)
            self.assertNotIn("invalidados", txt)
            self.assertGreater(len(json.loads(txt)["pecas"]["30"]["cpus"]), 0)


if __name__ == "__main__":
    unittest.main()
