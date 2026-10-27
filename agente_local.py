#!/usr/bin/env python3
"""Agente do seu PC: faz TODA a busca (KaBuM e Terabyte) e publica a página no GitHub Pages.

    python agente_local.py                 # fica rodando: busca a cada 2h e atende o botão da página
    python agente_local.py --uma-vez       # faz uma busca completa, publica e sai
    python agente_local.py --intervalo 60  # busca a cada 60 minutos
    python agente_local.py --sem-publicar  # só busca e grava no prices.db (não envia nada ao GitHub)

O que ele faz:
  1. A cada `--intervalo` minutos: roda `price_watch.py run` (todas as lojas, com o IP da sua casa),
     monta a página (build_site.py) e publica na branch "gh-pages" (é de lá que o GitHub Pages serve).
  2. A cada `--poll` segundos olha se a página pediu uma atualização (botão "Atualizar" ou "↻" de uma
     peça). O pedido chega por uma branch "pedido" que o workflow pedido.yml cria. Se chegou: atende,
     publica e a página mostra o preço novo.
  3. Guarda uma cópia do prices.db na branch "data" (backup do histórico).

Só precisa de: Python, git com push funcionando neste PC (o mesmo login do seu "git push"), e o .env
com NTFY_TOPIC se quiser os avisos de queda no celular. Não executa nada que venha do pedido: o pedido
só pode ser "todos" ou a chave (16 letras/números) de uma peça.
"""
from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
BR_PAGINA, BR_DADOS, BR_PEDIDO = "gh-pages", "data", "pedido"
CHAVE_RE = re.compile(r"^[0-9a-f]{16}$")
PORTA_TRAVA = 47653            # só uma cópia do agente por vez (a porta some quando o programa fecha)
LOG_MAX = 2_000_000            # bytes; acima disso o log velho é apagado ao iniciar


class GitErro(RuntimeError):
    pass


@contextlib.contextmanager
def pasta_temp():
    """Pasta temporária que também some no Windows (os arquivos do git são somente-leitura)."""
    d = Path(tempfile.mkdtemp(prefix="busca-preco-"))
    try:
        yield d
    finally:
        def _livre(func, path, _exc):
            os.chmod(path, stat.S_IWRITE)
            func(path)
        shutil.rmtree(d, onerror=_livre)


def git(args: list[str], cwd: Path, texto: bool = True) -> subprocess.CompletedProcess:
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}          # nunca ficar esperando senha no terminal
    r = subprocess.run(["git", *args], cwd=cwd, env=env, capture_output=True,
                       **({"text": True, "encoding": "utf-8", "errors": "replace"} if texto else {}))
    if r.returncode != 0:
        err = r.stderr if texto else r.stderr.decode("utf-8", "replace")
        raise GitErro(f"git {' '.join(args[:2])}: {err.strip()[:400]}")
    return r


def agora_txt() -> str:
    return datetime.now().strftime("%d/%m %H:%M:%S")


class Agente:
    def __init__(self, pasta: Path, origin: str, intervalo_min: float = 120, publicar: bool = True,
                 buscar=None, atualizar=None, log=None) -> None:
        self.pasta = Path(pasta)
        self.origin = origin
        self.intervalo = intervalo_min * 60
        self.publicar_ligado = publicar
        self.db = self.pasta / "prices.db"
        self.site = self.pasta / "site"
        self.estado_arq = self.pasta / ".agente_estado.json"
        self._buscar = buscar or self._buscar_real
        self._atualizar = atualizar or self._atualizar_real
        self.log = log or self._log_real
        self.estado = self._ler_estado()

    # ---------------------------------------------------------------- log e estado
    def _log_real(self, msg: str) -> None:
        linha = f"[{agora_txt()}] {msg}"
        print(linha, flush=True)
        try:
            with open(self.pasta / "agente.log", "a", encoding="utf-8") as f:
                f.write(linha + "\n")
        except OSError:
            pass

    def _ler_estado(self) -> dict:
        try:
            return json.loads(self.estado_arq.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _salvar_estado(self) -> None:
        try:
            self.estado_arq.write_text(json.dumps(self.estado), encoding="utf-8")
        except OSError as e:
            self.log(f"Não consegui gravar o estado: {e}")

    # ---------------------------------------------------------------- GitHub (só git)
    def sha_pedido(self) -> str | None:
        """Código da última versão da branch 'pedido' (muda a cada clique no botão da página)."""
        out = git(["ls-remote", self.origin, f"refs/heads/{BR_PEDIDO}"], self.pasta).stdout.split()
        return out[0] if out else None

    def baixar_da_branch(self, branch: str, arquivo: str) -> bytes | None:
        with pasta_temp() as t:
            git(["init", "-q"], t)
            try:
                git(["fetch", "-q", "--depth=1", self.origin, branch], t)
                return git(["show", f"FETCH_HEAD:{arquivo}"], t, texto=False).stdout
            except GitErro:
                return None

    def ler_pedido(self) -> dict | None:
        bruto = self.baixar_da_branch(BR_PEDIDO, "pedido.json")
        if not bruto:
            return None
        try:
            p = json.loads(bruto.decode("utf-8"))
        except ValueError:
            return None
        alvo = str(p.get("alvo", ""))
        if alvo != "todos" and not CHAVE_RE.match(alvo):
            self.log(f"Pedido ignorado (alvo inválido): {alvo[:40]!r}")
            return None
        return {"id": str(p.get("id", "")), "alvo": alvo}

    def _enviar_branch(self, branch: str, copiar, mensagem: str) -> None:
        """Cria um repositório novo com 1 commit só e sobrescreve a branch remota."""
        with pasta_temp() as t:
            copiar(t)
            git(["init", "-q", "-b", branch], t)
            git(["config", "user.name", "busca-preco (PC de casa)"], t)
            git(["config", "user.email", "busca-preco@users.noreply.github.com"], t)
            git(["add", "-A"], t)
            git(["commit", "-q", "-m", mensagem], t)
            git(["push", "-q", "-f", self.origin, branch], t)

    def publicar(self) -> None:
        if not self.publicar_ligado:
            self.log("--sem-publicar: nada enviado ao GitHub.")
            return
        carimbo = datetime.now().strftime("%Y-%m-%d %H:%M")

        def copiar_site(t: Path) -> None:
            for f in self.site.iterdir():
                if f.is_file():
                    shutil.copy2(f, t / f.name)

        self._enviar_branch(BR_PAGINA, copiar_site, f"Página {carimbo}")
        self.log(f"Página publicada na branch '{BR_PAGINA}' (o GitHub Pages atualiza em ~1 minuto).")
        try:
            self._enviar_branch(BR_DADOS, lambda t: shutil.copy2(self.db, t / "prices.db"), f"Histórico {carimbo}")
        except GitErro as e:                       # o backup não pode derrubar a publicação
            self.log(f"Aviso: não consegui guardar o backup do histórico: {e}")

    # ---------------------------------------------------------------- busca
    def _buscar_real(self, manual: bool) -> int:
        cmd = [sys.executable, str(self.pasta / "price_watch.py"), "run", "-v"]
        if manual:
            cmd.append("--no-heartbeat")           # o aviso "Monitor rodou" é só das buscas agendadas
        env = {**os.environ, "PYTHONUTF8": "1"}
        self.log("Rodando price_watch.py (todas as lojas)…")
        p = subprocess.Popen(cmd, cwd=self.pasta, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             text=True, encoding="utf-8", errors="replace")
        resumo = []
        for linha in p.stdout:                     # type: ignore[union-attr]
            linha = linha.rstrip()
            if linha.startswith(("[", "Resumo", "  ", "AVISO", "Sem ofertas", "Resultado")) and "fora do filtro" not in linha:
                resumo.append(linha)
        p.wait()
        for linha in resumo[-25:]:
            self.log("  " + linha)
        return p.returncode

    def _atualizar_real(self, chave: str) -> dict:
        import price_watch as pw
        import webapp
        db = pw.open_db(self.db)
        try:
            return webapp.atualizar_item(db, chave)
        finally:
            db.close()

    def montar_site(self) -> None:
        import build_site
        build_site.build(self.db, self.site)

    def _bootstrap(self) -> None:
        if self.db.exists():
            return
        self.log("Não há prices.db neste PC: procurando o histórico salvo no GitHub (branch 'data')…")
        try:
            bruto = self.baixar_da_branch(BR_DADOS, "prices.db")
        except GitErro as e:
            self.log(f"Não consegui falar com o GitHub: {e}")
            bruto = None
        if bruto:
            self.db.write_bytes(bruto)
            self.log("Histórico recuperado.")
        else:
            self.log("Sem histórico salvo: começando do zero.")

    # ---------------------------------------------------------------- ciclos
    def busca_completa(self, manual: bool) -> None:
        self.estado["ultima_busca"] = time.time()
        self._salvar_estado()
        codigo = self._buscar(manual)
        if codigo != 0:
            self.log(f"price_watch.py terminou com código {codigo} (a página sai com o que já estava salvo).")
        self.montar_site()
        self.publicar()

    def atender(self, pedido: dict) -> str:
        if pedido["alvo"] == "todos":
            self.log("Pedido da página: atualizar tudo.")
            self.busca_completa(manual=True)
            return "pedido:todos"
        self.log(f"Pedido da página: atualizar a peça {pedido['alvo']}.")
        r = self._atualizar(pedido["alvo"])
        self.log(f"  {r.get('mensagem', r)}")
        self.montar_site()
        self.publicar()
        return f"pedido:{pedido['alvo']}"

    def tick(self, agora: float | None = None) -> str | None:
        """Um passo do laço: atende um pedido novo OU faz a busca agendada, se já passou o intervalo."""
        agora = time.time() if agora is None else agora
        try:
            sha = self.sha_pedido()
        except GitErro as e:
            self.log(f"Sem conexão com o GitHub agora ({e}); tento de novo já já.")
            sha = None
        if sha and sha != self.estado.get("pedido_sha"):
            self.estado["pedido_sha"] = sha          # grava antes: se algo quebrar, não repete em loop
            self._salvar_estado()
            pedido = self.ler_pedido()
            if pedido:
                return self.atender(pedido)
        if agora - self.estado.get("ultima_busca", 0) >= self.intervalo:
            self.log("Hora da busca agendada.")
            self.busca_completa(manual=False)
            return "agendada"
        return None

    def iniciar(self) -> None:
        self._bootstrap()
        if "pedido_sha" not in self.estado:           # na primeira vez, ignora pedidos que já estavam lá
            try:
                self.estado["pedido_sha"] = self.sha_pedido() or ""
            except GitErro:
                self.estado["pedido_sha"] = ""
            self._salvar_estado()


def origem_do_repo(pasta: Path) -> str:
    return git(["remote", "get-url", "origin"], pasta).stdout.strip()


def trava_unica() -> socket.socket | None:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", PORTA_TRAVA))
    except OSError:
        return None
    return s


def aparar_log(pasta: Path) -> None:
    arq = pasta / "agente.log"
    try:
        if arq.stat().st_size > LOG_MAX:
            arq.unlink()
    except OSError:
        pass


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--intervalo", type=float, default=120, help="minutos entre as buscas agendadas (padrão 120)")
    ap.add_argument("--poll", type=float, default=30, help="segundos entre as checagens de pedido (padrão 30)")
    ap.add_argument("--uma-vez", action="store_true", help="uma busca completa, publica e sai")
    ap.add_argument("--sem-publicar", action="store_true", help="não envia nada ao GitHub")
    a = ap.parse_args(argv)

    sys.path.insert(0, str(HERE))
    try:
        from price_watch import load_dotenv
        load_dotenv(HERE / ".env")                    # NTFY_TOPIC para os avisos
    except ImportError:
        pass
    aparar_log(HERE)
    trava = trava_unica()
    if trava is None:
        print("O agente já está rodando neste PC (feche a outra janela antes).", file=sys.stderr)
        return 1
    try:
        origin = origem_do_repo(HERE)
    except GitErro as e:
        print(f"Esta pasta não é um repositório git com 'origin': {e}", file=sys.stderr)
        return 1

    ag = Agente(HERE, origin, a.intervalo, publicar=not a.sem_publicar)
    ag.iniciar()
    if a.uma_vez:
        try:
            ag.busca_completa(manual=False)
        except GitErro as e:
            ag.log(f"Falha ao publicar: {e}")
            return 1
        return 0

    ag.log(f"Agente no ar. Busca a cada {a.intervalo:g} min; olhando pedidos da página a cada {a.poll:g} s. "
           f"(Ctrl+C para parar)")
    while True:
        try:
            ag.tick()
        except GitErro as e:
            ag.log(f"Falha com o GitHub: {e}")
        except Exception as e:                        # noqa: BLE001 - o agente nunca pode morrer por um erro isolado
            ag.log(f"Erro inesperado: {type(e).__name__}: {e}")
        time.sleep(a.poll)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nAgente parado.")
        sys.exit(0)
