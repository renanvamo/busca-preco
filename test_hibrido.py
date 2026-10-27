"""Testes do modo híbrido: divisão de lojas entre GitHub e PC de casa, e junção dos bancos.

Rodar:  python -m unittest -v test_hibrido.py
"""
import sqlite3
import tempfile
import unittest
from pathlib import Path

import merge_db
import price_watch as pw


def make_db(path: Path, rows):
    """rows: (key, loja, nome, ts, preço, disponível)"""
    db = pw.open_db(path)
    for key, loja, nome, ts, preco, av in rows:
        db.execute("INSERT OR IGNORE INTO items(key,store,name,url,source,categoria,plataforma) VALUES(?,?,?,?,?,?,?)",
                   (key, loja, nome, f"https://x/{key}", "t", "processador", "AM5"))
        db.execute("INSERT INTO prices(key,ts,price,available) VALUES(?,?,?,?)", (key, ts, preco, av))
    db.commit()
    db.close()


class FilterJobsTests(unittest.TestCase):
    CFG = {
        "products": [
            {"name": "a", "url": "https://www.kabum.com.br/produto/1/a"},
            {"name": "b", "url": "https://www.terabyteshop.com.br/produto/2/b"},
            {"name": "c", "url": "https://www.terabyteshop.com.br/produto/3/c"},
        ],
    }

    def lojas(self, **kw):
        return sorted(j.store for j in pw.filter_jobs(pw.build_jobs(self.CFG), **kw))

    def test_sem_filtro_mantem_tudo(self):
        self.assertEqual(self.lojas(), ["KaBuM", "Terabyte", "Terabyte"])

    def test_github_pula_terabyte(self):
        self.assertEqual(self.lojas(skip={"terabyte"}), ["KaBuM"])

    def test_pc_de_casa_so_terabyte(self):
        self.assertEqual(self.lojas(only={"terabyte"}), ["Terabyte", "Terabyte"])

    def test_nome_da_loja_ignora_maiusculas(self):
        self.assertEqual(pw.parse_stores(" Terabyte , KaBuM,, "), {"terabyte", "kabum"})
        self.assertEqual(pw.parse_stores(None), set())


class RunStoreFilterTests(unittest.TestCase):
    def test_run_com_skip_nao_toca_na_loja_pulada(self):
        """Com --skip-stores terabyte nenhuma requisição é feita à Terabyte (nem conta como falha)."""
        cfg = {"products": [{"name": "b", "url": "https://www.terabyteshop.com.br/produto/2/b"}],
               "defaults": {"heartbeat": False}}
        with tempfile.TemporaryDirectory() as tmp:
            db = pw.open_db(Path(tmp) / "p.db")
            chamadas = []
            orig = pw.fetch
            pw.fetch = lambda *a, **k: chamadas.append(a) or ""
            try:
                code = pw.run(cfg, db, dry=True, delay=(0, 0), verbose=False, skip_stores={"terabyte"})
            finally:
                pw.fetch = orig
            self.assertEqual(code, 0)
            self.assertEqual(chamadas, [])
            self.assertEqual(db.execute("SELECT COUNT(*) FROM health").fetchone()[0], 0)
            db.close()


class MergeTests(unittest.TestCase):
    def test_junta_sem_duplicar(self):
        with tempfile.TemporaryDirectory() as tmp:
            t = Path(tmp)
            make_db(t / "main.db", [("k1", "KaBuM", "Ryzen 5 7600", "2026-10-27T10:00:00Z", 999.99, 1)])
            make_db(t / "local.db", [("t1", "Terabyte", "Ryzen 7 9800X3D", "2026-10-27T11:00:00Z", 2500.0, 1),
                                     ("t1", "Terabyte", "Ryzen 7 9800X3D", "2026-10-27T13:00:00Z", 2450.0, 1)])
            r1 = merge_db.merge(t / "local.db", t / "main.db")
            self.assertEqual(r1["leituras_novas"], 2)
            r2 = merge_db.merge(t / "local.db", t / "main.db")      # rodar de novo não duplica
            self.assertEqual(r2["leituras_novas"], 0)
            db = sqlite3.connect(t / "main.db")
            self.assertEqual(db.execute("SELECT COUNT(*) FROM items").fetchone()[0], 2)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM prices").fetchone()[0], 3)
            self.assertEqual(db.execute("SELECT price FROM prices WHERE key='k1'").fetchone()[0], 999.99)
            db.close()

    def test_leitura_nova_da_mesma_peca_entra(self):
        with tempfile.TemporaryDirectory() as tmp:
            t = Path(tmp)
            make_db(t / "main.db", [("t1", "Terabyte", "Ryzen 7 9800X3D", "2026-10-27T11:00:00Z", 2500.0, 1)])
            make_db(t / "local.db", [("t1", "Terabyte", "Ryzen 7 9800X3D", "2026-10-27T11:00:00Z", 2500.0, 1),
                                     ("t1", "Terabyte", "Ryzen 7 9800X3D", "2026-10-27T15:00:00Z", 2399.0, 1)])
            self.assertEqual(merge_db.merge(t / "local.db", t / "main.db")["leituras_novas"], 1)

    def test_sem_banco_local_nao_quebra(self):
        with tempfile.TemporaryDirectory() as tmp:
            t = Path(tmp)
            make_db(t / "main.db", [("k1", "KaBuM", "x", "2026-10-27T10:00:00Z", 1.0, 1)])
            self.assertEqual(merge_db.main([str(t / "nao_existe.db"), str(t / "main.db")]), 0)


if __name__ == "__main__":
    unittest.main()
