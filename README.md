# busca-preco

Monitor de preços de peças de PC (KaBuM e Terabyte). **Quem busca é o seu PC**; o GitHub só hospeda a página, que você abre de qualquer computador.

- **Coleta:** o agente (`agente_local.py`) roda no seu PC, lê KaBuM e Terabyte a cada 2 horas com o seu IP de casa (a Terabyte bloqueia os servidores do GitHub), guarda o histórico e te avisa no celular (ntfy) quando uma peça cai de preço ou bate a meta.
- **Página:** o agente monta a página e publica no GitHub Pages: lista de peças com preço atual, mínimo/máximo do período, selo de queda/alta, soma das peças escolhidas e FPS estimado.
- **Botões da página:** "Atualizar tudo (PC)" e "↻ atualizar" em cada peça deixam um pedido no GitHub; o agente do seu PC atende e publica o preço novo.
- **PC desligado:** a página continua no ar com os últimos preços; só não atualiza até o PC voltar.

## Como funciona

```
Seu PC (agente_local.py, sempre rodando)
  a cada 2h, ou quando a página pede:
  1. price_watch.py run: lê as lojas, grava em prices.db, manda aviso (ntfy)
  2. build_site.py: gera site/index.html + site/data.json
  3. git push -f para a branch "gh-pages"  -> GitHub Pages atualiza a página (~1 min)
  4. git push -f para a branch "data"      -> backup do prices.db
  a cada 30s: olha a branch "pedido" (git ls-remote)

Página (GitHub Pages), ao clicar em atualizar
  -> chama o workflow pedido.yml, que só grava um bilhete na branch "pedido"
  -> o agente vê o bilhete, busca, publica; a página percebe que data.json mudou e recarrega
```

O GitHub **não faz mais buscas** (o workflow agendado `monitor.yml` foi aposentado). O `pedido.yml` não lê preço nenhum: só deixa o bilhete.

## Configuração (uma vez só)

**No GitHub (no navegador)**
1. *Settings → Pages → Build and deployment → Source: Deploy from a branch → Branch `gh-pages`, pasta `/ (root)`.* (A branch aparece depois da primeira publicação do agente.)
2. Apague o workflow antigo: `git rm .github/workflows/monitor.yml`. Mantenha o `pedido.yml`.
3. *Settings → Secrets* não é mais necessário: o `NTFY_TOPIC` fica só no PC.
4. Token para o botão da página (uma vez por navegador): *Settings → Developer settings → Personal access tokens → Fine-grained tokens → Generate new token*, *Repository access:* só o `busca-preco`, *Repository permissions → Actions: Read and write* (nenhuma outra). A página pede o token no primeiro clique e guarda só no navegador (`localStorage`); o botão 🔑 troca ou esquece.

**No PC (Windows)**
1. `pip install -r requirements.txt`
2. Arquivo `.env` na pasta do projeto com `NTFY_TOPIC=seu-topico-secreto` (nunca sobe para o GitHub).
3. O `git push` precisa funcionar sem pedir senha (login do Git Credential Manager já resolve).
4. Teste uma busca sem publicar: `python agente_local.py --uma-vez --sem-publicar`
5. Primeira publicação: `python agente_local.py --uma-vez` (cria a branch `gh-pages`; depois faça o passo 1 de cima).
6. Deixe rodando sozinho ao ligar o PC (abre uma janela de terminal minimizada; não feche):

```
schtasks /Create /SC ONLOGON /TN "busca-preco agente" /F /TR "cmd /c cd /d C:\Users\Suporte\Desktop\Particular\projetos\busca-preco && python agente_local.py >> agente.out 2>&1"
```

Se o `schtasks` responder "Acesso negado", abra o terminal como administrador, ou use o jeito sem admin: `Win+R` → `shell:startup` → crie um atalho para `python agente_local.py` com a pasta do projeto como "Iniciar em".

Para ver se está vivo: abra `agente.log` na pasta do projeto (cada busca e cada pedido ficam registrados), ou veja o horário em "Preços de…" no topo da página. Para parar: Gerenciador de Tarefas → finalizar o `python.exe`, ou `schtasks /Delete /TN "busca-preco agente" /F`. Dá para rodar também de uma janela: `python agente_local.py`.

## O que é público e o que não é

Este repositório é público. Por isso:

| Fica público | Não fica no repositório |
|---|---|
| código, `watchlist.json` (o que você monitora), `benchmarks.json` | o tópico do ntfy (só no `.env` do PC) |
| `data.json` da página: preços, nomes e links das lojas | qualquer arquivo `.env`, o token do botão |
| branch `data`: o `prices.db` com o histórico de preços | páginas de loja salvas para diagnóstico (`*.html`, `debug/`) |

O `.gitignore` bloqueia `.env`, `prices.db`, logs e arquivos `.html` (exceto `index.html`). Antes de qualquer commit, confira com `git status`. O agente só envia o que está em `site/` (a página) e o `prices.db`.

## O que editar

- **`watchlist.json`:** produtos (link de página de produto + `target`, a meta de preço) e buscas (`query`, `include`/`exclude` em regex, `max_price`). Vale para KaBuM (busca e link) e Terabyte (só link de produto). Pichau e Mercado Livre bloqueiam o script e ficam em `disabled_stores`.
- **`benchmarks.json`:** notas de desempenho de CPUs e GPUs e os custos estimados das outras peças (`custos_fixos`).
- **`defaults.heartbeat`** (em `watchlist.json`): `true` manda um aviso discreto a cada busca agendada ("Monitor rodou às HH:MM"), para você saber que está funcionando. Os pedidos feitos pela página não mandam esse aviso.
- **Frequência:** `python agente_local.py --intervalo 60` (minutos; padrão 120). Para mudar, ajuste o comando do `schtasks`.

## Limites que você precisa saber

- **O PC precisa estar ligado e com a sessão aberta** para buscar e atender os botões. Se ele dormir, o agente retoma sozinho ao acordar (faz uma busca na hora se já passou o intervalo).
- **Pedido da página não é instantâneo:** o GitHub leva uns segundos para registrar, o agente olha a cada 30 s, a busca completa leva alguns minutos e o GitHub Pages leva cerca de 1 minuto para publicar. Uma peça sozinha costuma levar 1 a 3 minutos no total.
- **Sem execução simultânea:** o agente faz uma coisa por vez; pedidos feitos durante uma busca esperam a vez. Só um agente pode rodar por PC.
- **As lojas podem bloquear.** O monitor **não tenta driblar** bloqueios: se uma loja devolver desafio, captcha ou login, ele pula a loja naquela busca e, depois de 3 buscas seguidas sem ler nada, manda um aviso.
- **Preço da loja ≠ preço no carrinho.** O script lê o preço publicado na página (geralmente à vista/PIX). Confirme sempre na loja antes de comprar.
- **Token do botão:** quem tiver o token consegue disparar pedidos neste repositório (nada além disso). Use o token só neste navegador e revogue em *Settings → Developer settings* se perder o controle dele.

## Uso manual no PC

```bash
python agente_local.py --uma-vez --sem-publicar   # busca e grava em prices.db, sem enviar nada
python price_watch.py run                         # só a busca (sem montar nem publicar a página)
python webapp.py                                  # página local em http://127.0.0.1:8765, com botões próprios
python price_watch.py test-url "https://www.terabyteshop.com.br/produto/..."   # testa uma URL
python price_watch.py run --only-stores terabyte  # só uma loja (ou --skip-stores kabum)
python -m unittest test_price_watch test_setups test_hibrido test_agente       # testes
```

## Arquivos

- `agente_local.py`: o agente do PC (busca agendada, pedidos da página, publicação).
- `price_watch.py`: o monitor (lojas, histórico, avisos).
- `build_setups.py`: histórico (mín/máx/variação), cálculo de FPS e soma das peças.
- `build_site.py`: gera a página publicada.
- `webapp.py` e `index.html`: a página (local e publicada).
- `.github/workflows/pedido.yml`: o bilhete que liga o botão da página ao seu PC.
