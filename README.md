# busca-preco

Monitor de preços de peças de PC (KaBuM e Terabyte) que roda sozinho no GitHub e publica uma página para comparar as peças.

- **Coleta:** um workflow do GitHub Actions consulta as lojas a cada 2 horas, guarda o histórico e te avisa no celular (ntfy) quando uma peça cai de preço ou bate a meta.
- **Página:** o GitHub Pages publica a lista de peças com preço atual, mínimo/máximo do período, selo de queda/alta, soma das peças escolhidas e FPS estimado. Dá para abrir de qualquer computador.
- **Nada no seu PC:** ele não precisa ficar ligado.

## Como funciona

```
Actions (a cada 2h)
  1. baixa o histórico (branch "data")
  2. roda price_watch.py: lê as lojas, grava no SQLite, manda aviso (ntfy)
  3. roda build_site.py: gera site/index.html + site/data.json
  4. guarda o histórico de volta na branch "data" (sempre 1 commit só)
  5. publica site/ no GitHub Pages
```

## Configuração (uma vez só)

1. **Segredo do ntfy.** No repositório: *Settings → Secrets and variables → Actions → New repository secret*. Nome `NTFY_TOPIC`, valor um nome **longo e aleatório** (ex.: gere com um gerenciador de senhas). Quem souber o nome do tópico lê seus avisos, por isso ele fica só no Secret e nunca no código. No app ntfy do celular, assine esse mesmo tópico.
2. **Pages.** *Settings → Pages → Build and deployment → Source: GitHub Actions*.
3. **Primeira execução.** Aba *Actions → Monitor de preços → Run workflow*. Depois disso ele roda sozinho a cada 2 horas. A página fica em `https://<seu-usuario>.github.io/busca-preco/`.

## O que é público e o que não é

Este repositório é público. Por isso:

| Fica público | Não fica no repositório |
|---|---|
| código, `watchlist.json` (o que você monitora), `benchmarks.json` | o tópico do ntfy (só no Secret `NTFY_TOPIC`) |
| `data.json` da página: preços, nomes e links das lojas | qualquer arquivo `.env` |
| branch `data`: o `prices.db` com o histórico de preços | páginas de loja salvas para diagnóstico (`*.html`, `debug/`) |

O `.gitignore` já bloqueia `.env`, `prices.db`, logs e arquivos `.html` (exceto `index.html`). Antes de qualquer commit, confira com `git status`.

## O que editar

- **`watchlist.json`:** produtos (link de página de produto + `target`, a meta de preço) e buscas (`query`, `include`/`exclude` em regex, `max_price`). Vale para KaBuM (busca e link) e Terabyte (só link de produto). Pichau e Mercado Livre bloqueiam o script e ficam em `disabled_stores`.
- **`benchmarks.json`:** notas de desempenho de CPUs e GPUs e os custos estimados das outras peças (`custos_fixos`).
- **`defaults.heartbeat`** (em `watchlist.json`): `true` manda um aviso discreto a cada execução ("Monitor rodou às HH:MM"), para você saber que está funcionando. Use `false` para desligar.
- **Frequência:** a linha `cron` em `.github/workflows/monitor.yml` (horário UTC).

## Limites que você precisa saber

- **As lojas podem bloquear o GitHub.** As execuções saem de IPs de datacenter, que lojas com proteção anti-bot costumam barrar com mais facilidade do que um computador de casa. O monitor **não tenta driblar** bloqueios: se uma loja devolver desafio, captcha ou login, ele pula a loja naquela execução e, depois de 3 execuções seguidas sem ler nada, manda um aviso. Se isso acontecer sempre, rode o monitor no seu PC (veja abaixo).
- **Agendamento do GitHub não é exato.** Execuções podem atrasar alguns minutos ou, raramente, ser puladas. Em repositório público, o GitHub desativa workflows agendados depois de 60 dias sem atividade no repositório; se parar, reative em *Actions*.
- **Preço da loja ≠ preço no carrinho.** O script lê o preço publicado na página (geralmente à vista/PIX). Confirme sempre na loja antes de comprar.

## Rodar no seu computador (opcional)

```bash
pip install -r requirements.txt
echo NTFY_TOPIC=seu-topico-secreto > .env      # opcional; o .env nunca sobe para o GitHub
python price_watch.py run                      # lê as lojas e grava em prices.db
python webapp.py                               # página local em http://127.0.0.1:8765
python price_watch.py test-url "https://www.kabum.com.br/produto/..."   # testa uma URL
python -m unittest test_price_watch test_setups                         # testes
```

A página local também tem os botões "Atualizar preços agora" e "↻ atualizar" por peça, que dependem de um servidor e por isso não existem na versão publicada.

## Arquivos

- `price_watch.py`: o monitor (lojas, histórico, avisos).
- `build_setups.py`: histórico (mín/máx/variação), cálculo de FPS e soma das peças.
- `build_site.py`: gera a página publicada.
- `webapp.py` e `index.html`: a página (local e publicada).
- `.github/workflows/monitor.yml`: o agendamento.
