#!/usr/bin/env python3
"""Roda no SEU PC só as lojas que bloqueiam o GitHub (Terabyte) e entrega o resultado ao GitHub.

    python rodar_terabyte.py                 # lê a Terabyte, envia o banco e pede para atualizar a página
    python rodar_terabyte.py --sem-publicar  # só lê e grava em prices_local.db (não envia nada)
    python rodar_terabyte.py --lojas terabyte,pichau

Passos:
  1. price_watch.py run --only-stores ...   -> grava em prices_local.db (fica só neste PC) e avisa no ntfy
  2. envia prices_local.db para a branch "data-local" do repositório (1 commit só, sobrescrito)
  3. pede ao GitHub para refazer a página (precisa do GitHub CLI "gh" logado; se não tiver, a próxima
     execução agendada do GitHub, a cada 2h, junta os dados sozinha)

Usa o git e o login do GitHub que você já tem neste PC (o mesmo do "git push").
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
DB_LOCAL = HERE / "prices_local.db"
BRANCH = "data-local"
WORKFLOW = "monitor.yml"


def sh(cmd: list[str], cwd: Path | None = None, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=cwd, check=check, text=True, encoding="utf-8", errors="replace",
                          capture_output=True)


def ler_lojas(lojas: str, db: Path, extra: list[str]) -> int:
    cmd = [sys.executable, str(HERE / "price_watch.py"), "--db", str(db), "run",
           "--only-stores", lojas, "--no-heartbeat", "-v", *extra]
    print("> " + " ".join(cmd))
    return subprocess.run(cmd, cwd=HERE).returncode


def enviar_banco(db: Path) -> None:
    """Sobe o banco para a branch data-local (sempre 1 commit, igual ao que o workflow faz com 'data')."""
    origem = sh(["git", "remote", "get-url", "origin"], cwd=HERE).stdout.strip()
    with tempfile.TemporaryDirectory() as tmp:
        t = Path(tmp)
        shutil.copy2(db, t / "prices_local.db")
        sh(["git", "init", "-q", "-b", BRANCH], cwd=t)
        sh(["git", "config", "user.name", "busca-preco (PC de casa)"], cwd=t)
        sh(["git", "config", "user.email", "busca-preco@users.noreply.github.com"], cwd=t)
        sh(["git", "add", "prices_local.db"], cwd=t)
        sh(["git", "commit", "-q", "-m", "Leituras do PC de casa"], cwd=t)
        sh(["git", "push", "-q", "-f", origem, BRANCH], cwd=t)
    print(f"Banco enviado para a branch '{BRANCH}'.")


def atualizar_pagina() -> bool:
    gh = shutil.which("gh")
    if not gh:
        print("GitHub CLI (gh) não encontrado: a página será atualizada na próxima execução agendada "
              "(ou use o botão da página).")
        return False
    r = sh([gh, "workflow", "run", WORKFLOW, "-f", "modo=so_site"], cwd=HERE, check=False)
    if r.returncode != 0:
        print("Não consegui pedir a atualização da página:", (r.stderr or r.stdout).strip(), file=sys.stderr)
        return False
    print("Pedi ao GitHub para refazer a página (leva ~1 minuto).")
    return True


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--lojas", default="terabyte", help="lojas para ler aqui, separadas por vírgula")
    ap.add_argument("--sem-publicar", action="store_true", help="não envia nada ao GitHub")
    ap.add_argument("--dry-run", action="store_true", help="repassa ao monitor: não avisa no ntfy")
    a = ap.parse_args(argv)

    codigo = ler_lojas(a.lojas, DB_LOCAL, ["--dry-run"] if a.dry_run else [])
    if codigo != 0:
        print(f"O monitor terminou com código {codigo}; não vou enviar nada ao GitHub.", file=sys.stderr)
        return codigo
    if a.sem_publicar:
        print("--sem-publicar: banco fica só neste PC.")
        return 0
    try:
        enviar_banco(DB_LOCAL)
    except (subprocess.CalledProcessError, OSError) as e:
        saida = getattr(e, "stderr", "") or str(e)
        print(f"Falha ao enviar para o GitHub: {saida.strip()}", file=sys.stderr)
        return 1
    atualizar_pagina()
    return 0


if __name__ == "__main__":
    sys.exit(main())
