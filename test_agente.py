"""Testes do agente do PC com um "GitHub" de mentira (repositório git local).

Rodar:  python -m unittest -v test_agente.py
"""
import json
import shutil
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path

import agente_local as ag
import price_watch as pw
import test_setups as ts

HERE = Path(__file__).resolve().parent
CODIGO = ("build_site.py", "build_setups.py", "benchmarks.json", "index.html", "price_watch.py", "webapp.py")


def git(args, cwd):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        t = Path(self._tmp.name)
        self.origin = t / "origin.git"
        git(["init", "-q", "--bare", str(self.origin)], t)
        self.pasta = t / "pc"
        self.pasta.mkdir()
        for f in CODIGO:
            shutil.copy2(HERE / f, self.pasta / f)
        self.logs = []
        self.buscas = []
        self.atualizadas = []

    def tearDown(self):
        self._tmp.cleanup()

    def agente(self, **kw):
        a = ag.Agente(self.pasta, str(self.origin), buscar=self.buscar_falsa,
                      atualizar=self.atualizar_falsa, log=self.logs.append, **kw)
        return a

    def buscar_falsa(self, manual):
        """Faz de conta que o price_watch rodou: grava peças de exemplo no prices.db."""
        self.buscas.append(manual)
        src = ts.make_db()
        dst = sqlite3.connect(self.pasta / "prices.db")
        src.backup(dst)
        dst.close()
        return 0

    def atualizar_falsa(self, chave):
        self.atualizadas.append(chave)
        return {"resultado": "igual", "mensagem": "Preço confirmado, sem mudança."}

    def fazer_pedido(self, alvo, id_="123"):
        """O que o workflow pedido.yml faz: uma branch 'pedido' com um commit só."""
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as w:
            w = Path(w)
            git(["init", "-q", "-b", "pedido"], w)
            git(["config", "user.name", "t"], w); git(["config", "user.email", "t@t"], w)
            (w / "pedido.json").write_text(json.dumps({"id": id_, "alvo": alvo}))
            git(["add", "-A"], w); git(["commit", "-q", "-m", "p"], w)
            git(["push", "-q", "-f", str(self.origin), "pedido"], w)

    def arquivos(self, branch):
        return set(git(["ls-tree", "-r", "--name-only", branch], self.origin).split())


class AgendadaTests(Base):
    def test_primeira_busca_publica_pagina_e_backup(self):
        a = self.agente(intervalo_min=120)
        a.iniciar()
        self.assertEqual(a.tick(1e10), "agendada")
        self.assertEqual(self.buscas, [False])                       # busca agendada: com o aviso "rodou"
        self.assertEqual(self.arquivos("gh-pages"), {"index.html", "data.json", ".nojekyll"})
        self.assertEqual(self.arquivos("data"), {"prices.db"})
        html = git(["show", "gh-pages:index.html"], self.origin)
        self.assertIn("const MODO_ESTATICO = true;", html)           # a página publicada é a versão estática
        dados = json.loads(git(["show", "gh-pages:data.json"], self.origin))
        self.assertTrue(dados["pecas"]["30"]["cpus"])                # com as peças da busca

    def test_respeita_o_intervalo(self):
        a = self.agente(intervalo_min=120)
        a.iniciar()
        self.assertEqual(a.tick(10_000.0), "agendada")
        self.assertIsNone(a.tick(10_000.0 + 60))                     # 1 min depois: nada
        self.assertEqual(self.buscas, [False])
        a.estado["ultima_busca"] -= 121 * 60                         # passaram 2h
        self.assertEqual(a.tick(), "agendada")
        self.assertEqual(self.buscas, [False, False])

    def test_sem_publicar_nao_envia_nada(self):
        a = self.agente(intervalo_min=1, publicar=False)
        a.iniciar()
        a.tick(1e10)
        self.assertEqual(git(["branch", "-a"], self.origin).strip(), "")
        self.assertTrue((self.pasta / "site" / "data.json").exists())

    def test_recupera_historico_do_github_na_primeira_vez(self):
        # um banco já salvo na branch data (ex.: veio do tempo em que o GitHub fazia a busca)
        banco = ts.make_db()
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as w:
            w = Path(w)
            dst = sqlite3.connect(w / "prices.db"); banco.backup(dst); dst.close()
            git(["init", "-q", "-b", "data"], w)
            git(["config", "user.name", "t"], w); git(["config", "user.email", "t@t"], w)
            git(["add", "-A"], w); git(["commit", "-q", "-m", "x"], w)
            git(["push", "-q", "-f", str(self.origin), "data"], w)
        a = self.agente()
        a.iniciar()
        self.assertTrue((self.pasta / "prices.db").exists())
        n = sqlite3.connect(self.pasta / "prices.db").execute("SELECT COUNT(*) FROM items").fetchone()[0]
        self.assertEqual(n, len(ts.PARTS))


class PedidoTests(Base):
    def test_pedido_antigo_e_ignorado_na_primeira_vez(self):
        self.fazer_pedido("todos")
        a = self.agente(intervalo_min=10_000)
        a.iniciar()
        a.estado["ultima_busca"] = 1e12                              # sem busca agendada
        self.assertIsNone(a.tick(1.0))
        self.assertEqual(self.buscas, [])

    def test_pedido_todos_roda_busca_completa_sem_aviso_de_heartbeat(self):
        a = self.agente(intervalo_min=10_000)
        a.iniciar()
        a.estado["ultima_busca"] = 1e12
        self.fazer_pedido("todos")
        self.assertEqual(a.tick(1.0), "pedido:todos")
        self.assertEqual(self.buscas, [True])                        # manual=True -> --no-heartbeat
        self.assertIn("index.html", self.arquivos("gh-pages"))
        self.assertIsNone(a.tick(2.0))                               # mesmo pedido não repete

    def test_pedido_de_uma_peca_so_atualiza_ela(self):
        a = self.agente(intervalo_min=10_000)
        a.iniciar()
        a.estado["ultima_busca"] = 1e12
        self.buscar_falsa(False)                                     # há um banco para montar a página
        self.buscas.clear()
        chave = "0123456789abcdef"
        self.fazer_pedido(chave)
        self.assertEqual(a.tick(1.0), f"pedido:{chave}")
        self.assertEqual(self.atualizadas, [chave])
        self.assertEqual(self.buscas, [])                            # não rodou a busca completa
        self.assertIn("data.json", self.arquivos("gh-pages"))

    def test_novo_clique_gera_novo_pedido(self):
        a = self.agente(intervalo_min=10_000)
        a.iniciar()
        a.estado["ultima_busca"] = 1e12
        self.fazer_pedido("todos", "1")
        self.assertEqual(a.tick(1.0), "pedido:todos")
        self.fazer_pedido("todos", "2")
        self.assertEqual(a.tick(2.0), "pedido:todos")
        self.assertEqual(self.buscas, [True, True])

    def test_alvo_invalido_e_ignorado(self):
        a = self.agente(intervalo_min=10_000)
        a.iniciar()
        a.estado["ultima_busca"] = 1e12
        for ruim in ("'; DROP TABLE items;--", "../../etc/passwd", "ABCDEF0123456789", "123"):
            self.fazer_pedido(ruim)
            self.assertIsNone(a.tick(1.0), ruim)
        self.assertEqual(self.buscas, [])
        self.assertEqual(self.atualizadas, [])
        self.assertTrue(any("inválido" in l for l in self.logs))

    def test_sem_conexao_nao_derruba(self):
        a = self.agente(intervalo_min=10_000)
        a.iniciar()
        a.estado["ultima_busca"] = 1e12
        a.origin = str(self.origin) + "_nao_existe"
        self.assertIsNone(a.tick(1.0))
        self.assertTrue(any("Sem conexão" in l for l in self.logs))


if __name__ == "__main__":
    unittest.main()
