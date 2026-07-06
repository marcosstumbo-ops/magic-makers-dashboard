# Magic Makers Arts — Painel de Vendas

Dashboard de vendas conectado à API da Etsy, com coleta automática de
dados e publicação via GitHub Pages.

## Estrutura

- `index.html` — o dashboard (visual). Lê `data.json` quando existe;
  usa dados de exemplo enquanto isso.
- `collector.py` — script que busca vendas, taxas, favoritos e
  cadência de publicação na API da Etsy e gera `data.json`.
- `.github/workflows/collect.yml` — roda o `collector.py` automaticamente
  das 08h à meia-noite (horário de Brasília), a cada 5 minutos.
- `requirements.txt` — dependências do `collector.py`.

## Configuração (uma vez só)

### 1. Criar o app na Etsy
Em https://developers.etsy.com, criar um novo app. Anotar:
- **API Key (keystring)**
- **Shared secret**

Callback URL a usar no cadastro do app (ajustar depois de saber a URL
final do GitHub Pages):
```
https://SEU-USUARIO.github.io/NOME-DO-REPO/oauth/callback
```

### 2. Gerar o refresh token (autorização OAuth)
Seguir o fluxo de autorização OAuth 2.0 da Etsy (PKCE) — o dono da loja
faz login e autoriza uma única vez. O resultado desse processo é um
**refresh token**, que é o que o robô automático usa para continuar
renovando o acesso sozinho depois.

### 3. Configurar os "Secrets" do repositório
Em **Settings > Secrets and variables > Actions** deste repositório,
criar:
- `ETSY_API_KEY`
- `ETSY_SHARED_SECRET`
- `ETSY_REFRESH_TOKEN`
- `ETSY_SHOP_ID`

### 4. Ativar o GitHub Pages
Em **Settings > Pages**, publicar a partir da branch principal, pasta raiz.
O dashboard fica disponível em `https://SEU-USUARIO.github.io/NOME-DO-REPO/`.

### 5. Testar o coletor manualmente
Em **Actions > Coletar dados da Etsy > Run workflow**, disparar uma
execução manual para validar que tudo está funcionando antes de
esperar pelo agendamento automático.

## Notas importantes

- A Etsy **não expõe** via API: visitas por período, origem de tráfego,
  termos de busca ou métricas de Star Seller. Por isso o dashboard não
  inclui esses dados — só o que é 100% automatizável.
- "Visitas" no dashboard são uma **aproximação**, calculada pela
  diferença entre o contador acumulado de cada anúncio a cada leitura
  (arquivo `state.json`, mantido pelo próprio coletor).
- Nomes exatos de alguns campos de resposta da API (principalmente no
  endpoint de "ledger") podem precisar de pequenos ajustes na primeira
  execução real — validar e ajustar `collector.py` conforme necessário.
