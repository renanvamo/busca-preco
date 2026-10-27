# busca-preco

Monitor de preços de peças de PC (KaBuM e Terabyte) que roda sozinho no GitHub e publica uma página para comparar as peças.

- **Coleta (modo híbrido):** um workflow do GitHub Actions consulta a KaBuM a cada 2 horas, guarda o histórico e te avisa no celular (ntfy) quando uma peça cai de preço ou bate a meta. A **Terabyte bloqueia o GitHub**, então ela é lida pelo seu PC (`rodar_terabyte.py`) e os dois lados são juntados na página.
- **Página:** o GitHub Pages publica a lista de peças com preço atual, mínimo/máximo do período, selo de queda/alta, soma das peças escolhidas e FPS estimado. Dá para abrir de qualquer computador.
- **Botão "Atualizar agora":** na página, pede ao GitHub para rodar a busca na hora (sem esperar o horário). Veja "Botão da página" abaixo.
- **PC ligado só para a Terabyte:** o resto roda sozinho no GitHub.

## Como funciona

```
Actions (a cada 2h, ou pelo botão da página)
  1. baixa o histórico (branch "data") e as leituras do PC de casa (branch "data-local")
  2. roda price_watch.py --skip-stores terabyte: lê as lojas, grava no SQLite, manda aviso (ntfy)
  3. merge_db.py: junta as leituras do PC de casa no banco
  4. roda build_site.py: gera site/index.html + site/data.json
  5. guarda o histórico de volta na branch "data" (sempre 1 commit só)
  6. publica site/ no GitHub Pages

PC de casa (Agendador de Tarefas do Windows, a cada 2h)
  rodar_terabyte.py
  1. price_watch.py --only-stores terabyte: lê a Terabyte, grava em prices_local.db, avisa no ntfy
  2. envia prices_local.db para a branch "data-local"
  3. pede ao GitHub para refazer a página (precisa do GitHub CLI "gh" logado; senão a próxima execução do GitHub junta)
```

## Configuração (uma vez só)

1. **Segredo do ntfy.** No repositório: *Settings → Secrets and variables → Actions → New repository secret*. Nome `NTFY_TOPIC`, valor um nome **longo e aleatório** (ex.: gere com um gerenciador de senhas). Quem souber o nome do tópico lê seus avisos, por isso ele fica só no Secret e nunca no código. No app ntfy do celular, assine esse mesmo tópico.
2. **Pages.** *Settings → Pages → Build and deployment → Source: GitHub Actions*.
3. **Primeira execução.** Aba *Actions → Monitor de preços → Run workflow*. Depois disso ele roda sozinho a cada 2 horas. A página fica em `https://<seu-usuario>.github.io/busca-preco/`.

## Modo híbrido: Terabyte no seu PC

O GitHub lê a KaBuM, mas a Terabyte devolve 403 para IPs de datacenter. O mesmo código lê a Terabyte normalmente de um IP residencial, então ela roda no seu PC.

1. No PC, na pasta do projeto: `pip install -r requirements.txt` e um arquivo `.env` com `NTFY_TOPIC=seu-topico-secreto` (os avisos de queda da Terabyte saem daqui; o `.env` nunca sobe para o GitHub).
2. Teste: `python rodar_terabyte.py --sem-publicar` (lê a Terabyte e grava em `prices_local.db`, sem enviar nada).
3. Para o PC enviar o resultado e pedir a atualização da página, o `git push` já precisa funcionar nele. Opcional: instalar o [GitHub CLI](https://cli.github.com) e rodar `gh auth login` (sem ele, a página só atualiza na próxima execução do GitHub, a cada 2h).
4. Agendar a cada 2h (no minuto 47, para não bater com o GitHub, que roda no 17):

```
schtasks /Create /SC HOURLY /MO 2 /ST 00:47 /TN "busca-preco Terabyte" /F /TR "cmd /c cd /d C:\Users\Suporte\Desktop\Particular\projetos\busca-preco && python rodar_terabyte.py >> terabyte.log 2>&1"
```

O PC precisa estar ligado e com a sessão aberta na hora; se ficar desligado, a Terabyte só fica sem leitura nova (o resto continua). Para ler a Terabyte na hora, rode `python rodar_terabyte.py`.

Os dois lados usam bancos separados (`prices.db` no GitHub, `prices_local.db` no PC) e `merge_db.py` junta antes de gerar a página, sem duplicar leituras. Se a Terabyte parar de ser lida por 3 execuções seguidas, o aviso de "precisa de atenção" vem do PC.

## Botão da página ("Atualizar agora")

A página publicada não tem servidor, então o botão chama a API do GitHub direto do seu navegador para rodar o workflow. Na primeira vez ele pede um token:

1. GitHub → *Settings → Developer settings → Personal access tokens → Fine-grained tokens → Generate new token*.
2. *Repository access:* só o `busca-preco`. *Repository permissions → Actions:* **Read and write**. Mais nada.
3. Cole na caixa que aparece ao clicar no botão. O token fica **só no localStorage deste navegador** (não vai para o repositório nem para a página publicada). O botão 🔑 token troca ou esquece.

O botão roda o workflow no modo `completo` (KaBuM e demais lojas + refaz a página) e acompanha até terminar, sem mexer na Terabyte, que continua com o PC. Também dá para rodar pela aba *Actions → Run workflow* (escolha `completo` ou `so_site`). Na página local (`python webapp.py`) o botão continua lendo todas as lojas aqui mesmo.

## O que é público e o que não é

Este repositório é público. Por isso:

| Fica público | Não fica no repositório |
|---|---|
| código, `watchlist.json` (o que você monitora), `benchmarks.json` | o tópico do ntfy (só no Secret `NTFY_TOPIC`) |
| `data.json` da página: preços, nomes e links das lojas | qualquer arquivo `.env` |
| branches `data` e `data-local`: `prices.db` e `prices_local.db` com o histórico de preços | páginas de loja salvas para diagnóstico (`*.html`, `debug/`) |

O `.gitignore` já bloqueia `.env`, `prices.db`, logs e arquivos `.html` (exceto `index.html`). Antes de qualquer commit, confira com `git status`.

## O que editar

- **`watchlist.json`:** produtos (link de página de produto + `target`, a meta de preço) e buscas (`query`, `include`/`exclude` em regex, `max_price`). Vale para KaBuM (busca e link) e Terabyte (só link de produto). Pichau e Mercado Livre bloqueiam o script e ficam em `disabled_stores`.
- **`benchmarks.json`:** notas de desempenho de CPUs e GPUs e os custos estimados das outras peças (`custos_fixos`).
- **`defaults.heartbeat`** (em `watchlist.json`): `true` manda um aviso discreto a cada execução ("Monitor rodou às HH:MM"), para você saber que está funcionando. Use `false` para desligar.
- **Frequência:** a linha `cron` em `.github/workflows/monitor.yml` (horário UTC).

## Limites que você precisa saber

- **As lojas podem bloquear o GitHub.** As execuções saem de IPs de datacenter, que lojas com proteção anti-bot costumam barrar com mais facilidade do que um computador de casa (é o caso da Terabyte, por isso o modo híbrido acima). O monitor **não tenta driblar** bloqueios: se uma loja devolver desafio, captcha ou login, ele pula a loja naquela execução e, depois de 3 execuções seguidas sem ler nada, manda um aviso.
- **Agendamento do GitHub não é exato.** Execuções podem atrasar alguns minutos ou, raramente, ser puladas. Em repositório público, o GitHub desativa workflows agendados depois de 60 dias sem atividade no repositório; se parar, reative em *Actions*.
- **Preço da loja ≠ preço no carrinho.** O script lê o preço publicado na página (geralmente à vista/PIX). Confirme sempre na loja antes de comprar.

## Rodar no seu computador (opcional)

```bash
pip install -r requirements.txt
echo NTFY_TOPIC=seu-topico-secreto > .env      # opcional; o .env nunca sobe para o GitHub
python price_watch.py run                      # lê as lojas e grava em prices.db
python webapp.py                               # página local em http://127.0.0.1:8765
python price_watch.py test-url "https://www.kabum.com.br/produto/..."   # testa uma URL
python -m unittest test_price_watch test_setups test_hibrido           # testes
```

A página local também tem os botões "Atualizar preços agora" e "↻ atualizar" por peça, que dependem de um servidor e por isso não existem na versão publicada.

## Arquivos

- `price_watch.py`: o monitor (lojas, histórico, avisos). `--only-stores` / `--skip-stores` dividem as lojas entre o GitHub e o PC.
- `rodar_terabyte.py`: roda no PC só as lojas que bloqueiam o GitHub e entrega o resultado.
- `merge_db.py`: junta o banco do PC no banco do GitHub antes de gerar a página.
- `build_setups.py`: histórico (mín/máx/variação), cálculo de FPS e soma das peças.
- `build_site.py`: gera a página publicada.
- `webapp.py` e `index.html`: a página (local e publicada).
- `.github/workflows/monitor.yml`: o agendamento.
