# Validação PIX

Sistema simples para validar chaves PIX em lote pela API do GPS Pay.
Substitui o job que lia a planilha do Google Drive.

## Como funciona

1. A pessoa entra com login e senha.
2. Importa um `.xlsx` ou `.csv` com as colunas `cpf`, `tipochave` e `chave` (tem um modelo para baixar na tela).
3. O sistema confere o arquivo **antes de gastar**: linhas sem CPF/tipo/chave ou com CPF inválido (dígito verificador errado) são descartadas e **não são cobradas**.
4. Confere o **saldo do mês** do usuário. Se o arquivo tiver mais linhas válidas do que o saldo, **nada é enviado**.
5. Envia as linhas para a API (5 chamadas simultâneas) e mostra o andamento. O resultado pode ser baixado em `.xlsx`.

## Controle de custo

- **Limite mensal por usuário**: cada linha enviada consome 1 do saldo (mês-calendário, horário de Brasília). O admin define o limite de cada pessoa na tela *Usuários*; limite 0 bloqueia.
- O saldo é reservado na mesma transação que grava as linhas — duas importações simultâneas não furam o limite.
- Teto por arquivo (`MAX_LINHAS_ARQUIVO`, padrão 5000).
- Se o servidor cair no meio de uma chamada, a linha vira `erro` com aviso e **não é reenviada automaticamente** (a cobrança pode ter ocorrido). Linhas ainda não enviadas continuam quando o app volta.
- Usuário comum só vê as próprias importações; admin vê todas.

## Rodando

```bash
py -m venv .venv
.venv\Scripts\pip install -r requirements.txt
copy .env.example .env      # e preencha
.venv\Scripts\python criar_admin.py seu.login "Seu Nome"
.venv\Scripts\python app.py # http://localhost:8080
```

## Produção (Render)

Deploy via Blueprint (`render.yaml`). Variáveis a preencher no Render:

- `DATABASE_URL` — Postgres do ETL (`postgresql://usuario:senha@host:5432/banco`)
- `GPS_PAY_URL`, `GPS_PAY_TOKEN` — API do GPS Pay
- `ADMIN_LOGIN`, `ADMIN_SENHA`, `ADMIN_NOME` — criam o primeiro admin **só se a tabela de usuários estiver vazia**. Depois de entrar e trocar a senha, pode apagar.
- `TEAMS_WEBHOOK` — opcional

As tabelas (`pix_usuario`, `pix_lote`, `pix_validacao`) são criadas automaticamente na primeira subida.
Rode **uma instância só** (o processamento em segundo plano roda dentro do próprio processo).
No plano free, enquanto houver lote processando o app chama a própria URL a cada 4 min para o Render não desligá-lo no meio.
