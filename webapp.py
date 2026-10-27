#!/usr/bin/env python3
"""Página local para ver os setups ganhadores e atualizar os preços.

    python webapp.py              # abre em http://127.0.0.1:8765
    python webapp.py --port 9000

Só escuta em 127.0.0.1 (apenas este computador). Não precisa instalar nada além do que o monitor já usa.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import subprocess
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import build_setups as bs
import price_watch as pw
import requests

HERE = Path(__file__).resolve().parent


MULT_RE = re.compile(r"\[preço ×([\d.]+)\]")


def atualizar_item(db: sqlite3.Connection, key: str, fetch_fn=None) -> dict:
    """Lê a página do produto agora e grava uma nova leitura no banco.
    Esgotado: a peça sai da lista. Preço diferente: a lista passa a mostrar o novo."""
    fetch_fn = fetch_fn or (lambda url: pw.fetch(requests.Session(), url, retries=1))
    row = db.execute("SELECT name, url, store FROM items WHERE key=?", (key,)).fetchone()
    if not row:
        return {"resultado": "erro", "mensagem": "item não encontrado no banco"}
    name, url, store = row
    prev = db.execute("SELECT price, available FROM prices WHERE key=? ORDER BY ts DESC, rowid DESC LIMIT 1", (key,)).fetchone()
    m = MULT_RE.search(name or "")
    mult = float(m.group(1)) if m else 1.0
    try:
        data, _ = pw.extract_product(fetch_fn(url))
    except pw.Blocked as e:
        return {"resultado": "erro", "mensagem": f"{store} bloqueou o acesso ({e}). Tente de novo mais tarde."}
    except pw.FetchError as e:
        return {"resultado": "erro", "mensagem": f"não consegui abrir a página: {e}"}
    if not data:
        return {"resultado": "erro", "mensagem": "não encontrei o preço na página (ela mudou ou foi bloqueada)"}
    price = round(data["price"] * mult, 2)
    avail = data.get("available")
    db.execute("INSERT INTO prices(key,ts,price,available) VALUES(?,?,?,?)",
               (key, pw.iso(pw.utcnow()), price, None if avail is None else int(avail)))
    if _has_flags(db):
        db.execute("UPDATE price_flags SET ativo=0 WHERE key=?", (key,))
    db.commit()
    antigo = prev[0] if prev else None
    out = {"antigo": antigo, "novo": price}
    if avail is False:
        out.update(resultado="esgotado", mensagem="Esgotado na loja: a peça saiu da lista.")
    elif antigo is not None and abs(antigo - price) > 0.005:
        out.update(resultado="mudou", mensagem=f"Preço atualizado: {bs_brl(antigo)} → {bs_brl(price)}.")
    else:
        out.update(resultado="igual", mensagem="Preço confirmado, sem mudança.")
    return out


def _has_flags(db: sqlite3.Connection) -> bool:
    return db.execute("SELECT 1 FROM sqlite_master WHERE name='price_flags'").fetchone() is not None


def bs_brl(v: float) -> str:
    return bs.brl(v)


class RunState:
    """Uma execução do monitor por vez; o log fica na memória para a página ler."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.proc: subprocess.Popen | None = None
        self.lines: list[str] = []
        self.started = ""
        self.exit_code: int | None = None

    def running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def start(self, script: Path, cwd: Path) -> bool:
        with self.lock:
            if self.running():
                return False
            self.lines, self.exit_code = [], None
            self.started = bs.utcnow().isoformat(timespec="seconds")
            env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
            self.proc = subprocess.Popen(
                [sys.executable, "-u", str(script), "run", "--dry-run", "-v"], cwd=str(cwd), env=env,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                errors="replace")
            threading.Thread(target=self._pump, args=(self.proc,), daemon=True).start()
            return True

    def _pump(self, proc: subprocess.Popen) -> None:
        assert proc.stdout
        for line in proc.stdout:
            self.lines.append(line.rstrip("\n"))
        self.exit_code = proc.wait()

    def status(self) -> dict:
        return {"rodando": self.running(), "inicio": self.started, "codigo": self.exit_code,
                "log": "\n".join(self.lines[-400:])}


def make_handler(db_path: Path, bench_path: Path, run: RunState, port: int):
    allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):          # silêncio no terminal
            pass

        # -------- util
        def send_json(self, obj, code=200):
            data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def host_ok(self) -> bool:
            return self.headers.get("Host", "") in allowed_hosts

        def origin_ok(self) -> bool:
            origin = self.headers.get("Origin")
            return origin is None or origin in {f"http://{h}" for h in allowed_hosts}

        def db(self) -> sqlite3.Connection:
            return sqlite3.connect(str(db_path), timeout=30)

        # -------- GET
        def do_GET(self):
            if not self.host_ok():
                return self.send_json({"erro": "host não permitido"}, 403)
            u = urlparse(self.path)
            if u.path in ("/", "/index.html"):
                data = (HERE / "index.html").read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return
            if u.path == "/api/setups":
                if not db_path.exists():
                    return self.send_json({"erro": "prices.db não existe. Clique em 'Atualizar preços agora'."}, 404)
                q = parse_qs(u.query)
                try:
                    fps = float(q["fps"][0]) if "fps" in q else None
                except ValueError:
                    fps = None
                try:
                    db = self.db()
                    try:
                        res = bs.compute(db, bs.load_bench(bench_path), fps)
                    finally:
                        db.close()
                except sqlite3.OperationalError as e:
                    return self.send_json({"erro": f"banco ocupado ou inválido: {e}. Feche o cliente de banco e tente de novo."}, 503)
                return self.send_json(res)
            if u.path in ("/api/pecas", "/api/soma"):
                if not db_path.exists():
                    return self.send_json({"erro": "prices.db não existe. Clique em 'Atualizar preços agora'."}, 404)
                q = parse_qs(u.query)
                try:
                    db = self.db()
                    try:
                        bench = bs.load_bench(bench_path)
                        if u.path == "/api/pecas":
                            try:
                                dias = int(q.get("dias", ["30"])[0])
                            except ValueError:
                                dias = 30
                            dias = max(0, min(dias, 3650)) or None      # 0 = todo o histórico
                            res = bs.listar_pecas(db, bench, dias=dias)
                        else:
                            g = lambda k: (q.get(k) or [None])[0] or None
                            res = bs.avaliar(db, bench, g("cpu"), g("ram"), g("gpu"))
                    finally:
                        db.close()
                except sqlite3.OperationalError as e:
                    return self.send_json({"erro": f"banco ocupado ou inválido: {e}. Feche o cliente de banco e tente de novo."}, 503)
                return self.send_json(res)
            if u.path == "/api/run/status":
                return self.send_json(run.status())
            self.send_json({"erro": "não encontrado"}, 404)

        # -------- POST
        def do_POST(self):
            if not self.host_ok() or not self.origin_ok():
                return self.send_json({"erro": "origem não permitida"}, 403)
            if "application/json" not in self.headers.get("Content-Type", ""):
                return self.send_json({"erro": "use application/json"}, 415)
            n = int(self.headers.get("Content-Length", 0) or 0)
            try:
                body = json.loads(self.rfile.read(n) or b"{}")
            except json.JSONDecodeError:
                return self.send_json({"erro": "JSON inválido"}, 400)
            path = urlparse(self.path).path
            if path == "/api/run":
                ok = run.start(HERE / "price_watch.py", HERE)
                return self.send_json({"iniciado": ok, "ja_rodando": not ok})
            if path == "/api/atualizar":
                key = str(body.get("key", ""))
                if not key:
                    return self.send_json({"erro": "key obrigatório"}, 400)
                try:
                    db = self.db()
                    try:
                        res = atualizar_item(db, key)
                    finally:
                        db.close()
                except sqlite3.OperationalError as e:
                    return self.send_json({"erro": f"banco ocupado: {e}"}, 503)
                return self.send_json(res)
            if path in ("/api/flag", "/api/unflag"):
                key = str(body.get("key", ""))
                if not key:
                    return self.send_json({"erro": "key obrigatório"}, 400)
                try:
                    db = self.db()
                    try:
                        if path == "/api/flag":
                            motivo = "esgotado" if body.get("motivo") == "esgotado" else "preço mudou"
                            ok = bs.flag_price(db, key, motivo)
                        else:
                            bs.unflag(db, key)
                            ok = True
                    finally:
                        db.close()
                except sqlite3.OperationalError as e:
                    return self.send_json({"erro": f"banco ocupado: {e}"}, 503)
                return self.send_json({"ok": ok})
            self.send_json({"erro": "não encontrado"}, 404)

    return Handler


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--db", default=str(HERE / "prices.db"))
    ap.add_argument("--no-browser", action="store_true")
    a = ap.parse_args()
    handler = make_handler(Path(a.db), HERE / "benchmarks.json", RunState(), a.port)
    srv = ThreadingHTTPServer(("127.0.0.1", a.port), handler)
    url = f"http://127.0.0.1:{a.port}"
    print(f"Página no ar em {url}  (Ctrl+C para encerrar)")
    if not a.no_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nEncerrado.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
