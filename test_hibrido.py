"""Testes do filtro de lojas do monitor (--only-stores / --skip-stores).

Rodar:  python -m unittest -v test_hibrido.py
"""
import tempfile
import unittest
from pathlib import Path

import price_watch as pw


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


if __name__ == "__main__":
    unittest.main()
