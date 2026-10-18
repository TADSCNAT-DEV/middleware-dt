# Execução com Docker e Make

Este guia descreve o fluxo recomendado para subir a demo do middleware com o `Makefile`. Esse caminho sobe a pilha completa e já inclui middleware, nginx, simulator, client, PostgreSQL, Neo4j, InfluxDB, Redis e parser DTDL.

[Voltar para a documentação principal](../README.md#execucao-docker-make)

Para uma atividade completa guiada, use o [Roteiro de laboratório House 2.0](../roteiro-teste-house.md).

## Fluxo rápido (primeira execução)

```bash
cp .env.example .env
# Revise as credenciais conforme a seção 2 antes de continuar.
make build-with-deps
make up
make healthcheck
make seed-house
```

Esse caminho cobre bootstrap de dependências (quando houver submodules), build e subida da stack completa.

Se `iot_simulator` e `middts-client` nao existirem na raiz do projeto, `make build-with-deps` tenta criar as pastas automaticamente com `git clone`.

## 1. Pré-requisitos

- Docker
- Docker Compose v2 com o comando `docker compose`
- `make`

Se estiver em WSL e seu usuário ainda não tiver acesso ao socket do Docker, rode os comandos com `sudo`.

Bootstrap de dependencias:

```bash
make deps
```

Com submodules, esse comando executa `git submodule update --init --recursive`.

Sem submodules, ele tenta clonar automaticamente:

- `https://github.com/Digital-Twins-RSBR/iot_simulator.git`
- `https://github.com/Digital-Twins-RSBR/middts-client.git`

O bootstrap tenta fazer clone anonimo. Se o seu ambiente injetar configuracoes/credenciais Git e ainda assim o clone falhar, faca o clone manualmente ou aponte `SIMULATOR_CONTEXT` e `CLIENT_CONTEXT` para pastas locais existentes.

Ou faca tudo em um comando:

```bash
make build-with-deps
```

Se o simulador estiver em uma pasta irma (por exemplo `../iot_simulator`) em vez de `./iot_simulator`, o `Makefile` tenta detectar isso automaticamente. Se necessario, voce pode sobrescrever manualmente:

```bash
make SIMULATOR_CONTEXT=../iot_simulator build
```

Da mesma forma para o client:

```bash
make CLIENT_CONTEXT=../middts-client build
```

Se voce nao usa submodule, o fluxo recomendado e deixar `make deps` ou `make build-with-deps` criar essas pastas automaticamente. Se preferir, voce ainda pode usar os overrides de contexto.

## 2. Preparar o ambiente

Crie o arquivo `.env` a partir do template:

```bash
cp .env.example .env
```

Antes do primeiro `make up`, revise as senhas. Os valores do template são exemplos para a demo local. Se já existe um `.env`, edite-o sem copiar o template por cima.

| Acesso | Usuário / senha no template | Onde definir |
| --- | --- | --- |
| MiddTS Admin e JWT (`:8000`) | `middts` / `middts123` | `DJANGO_SUPERUSER_USERNAME`, `DJANGO_SUPERUSER_PASSWORD`, `DJANGO_SUPERUSER_EMAIL` |
| Client Admin (`:8002/admin/`) | `middts` / `middts123` | As mesmas variáveis `DJANGO_SUPERUSER_*`, em banco separado |
| Client → API do MiddTS | `middts` / `middts123` por padrão no Compose | `MIDDTS_API_USERNAME`, `MIDDTS_API_PASSWORD` |
| Simulator Admin (`:8001/admin/`) | Não garantido pelo Compose atual | Criar ou redefinir conforme seção 6 |
| PostgreSQL | `middts` / `middts` | `POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_DB` |
| Neo4j Browser (`:7474`) | `neo4j` / `middts123` | `NEO4J_AUTH=neo4j/senha` e `NEO4J_PASSWORD` com a mesma senha |
| InfluxDB (`:8086`) | `middts` / `middts123` | `INFLUXDB_INIT_USERNAME`, `INFLUXDB_INIT_PASSWORD` |
| Token InfluxDB | `middts_token` | `INFLUXDB_TOKEN` (usado pelo Compose no setup e nas aplicações) |

O middleware cria ou atualiza seu superusuário em **cada inicialização**, aplicando a senha de `DJANGO_SUPERUSER_PASSWORD`. O client também aplica essas variáveis ao seu administrador local. Ao alterar as credenciais do middleware, adicione ao `.env` as variáveis abaixo com os mesmos valores escolhidos, para manter a integração do client:

```dotenv
MIDDTS_API_USERNAME=middts
MIDDTS_API_PASSWORD=sua_senha_do_middleware
```

O Compose define valores próprios para `MIDDTS_API_*`; eles não acompanham automaticamente uma alteração de `DJANGO_SUPERUSER_*`.

As variáveis `SIMULATOR_SUPERUSER_*` do template não são repassadas pelo serviço `simulator` no Compose atual. Defini-las somente no `.env` da raiz não cria o usuário. Siga o procedimento explícito da seção 6.

### Credenciais do ThingsBoard

`THINGSBOARD_USER` e `THINGSBOARD_PASSWORD` são o login da conta do ThingsBoard, não o login do Django Admin. Configure uma conta válida na instância escolhida; os exemplos `tenant@thingsboard.org` / `tenant` não garantem acesso ao serviço público.

O middleware usa seu `GatewayIOT` para as operações. Na versão atual do simulador, as operações usam o `GatewayIOT` ativo no banco próprio; as variáveis legadas `THINGSBOARD_*` também aparecem no entrypoint. Cadastrar um gateway apenas no admin do middleware não configura o simulador. Use o seed automático abaixo para preencher ambos com a mesma instância e credenciais. No Compose principal, `SIMULATOR_THINGSBOARD_HOST` configura o host legado do entrypoint; mantenha-o alinhado com `THINGSBOARD_GATEWAY_URL`.

### Cadastrar o gateway automaticamente no seed

Para preencher o gateway no MiddTS e no simulador sem cadastro manual, ajuste o `.env` **antes de `make up`**:

```dotenv
SEED_THINGSBOARD_GATEWAY=1
THINGSBOARD_GATEWAY_NAME=Meu ThingsBoard
THINGSBOARD_GATEWAY_URL=https://sua-instancia-thingsboard
THINGSBOARD_AUTH_METHOD=api_key
THINGSBOARD_API_KEY=sua_api_key_da_conta
```

Use uma API Key da conta ThingsBoard com permissão para gerenciar os dispositivos do tenant, não o access token de um dispositivo nem um JWT de login que expira. Como alternativa, use `THINGSBOARD_AUTH_METHOD=user_password` e preencha `THINGSBOARD_USER` / `THINGSBOARD_PASSWORD`.

Execute `make seed-house`: ele cria o gateway ausente na organização `Default` do MiddTS e no simulador, além de carregar o sistema e os oito modelos DTDL. Repetir o comando preserva o cadastro identificado pelo mesmo nome, incluindo credenciais e configurações. Mudar o nome cria outro cadastro; no simulador, um gateway novo só é ativado quando não existe outro ativo. Para alterar credenciais existentes, use o admin de cada serviço. Credenciais não são exibidas pelo bootstrap. Ele não testa acesso remoto, cria dispositivos nem vincula instâncias aos modelos.

O simulador precisa de uma versão que tenha o modelo `devices.GatewayIOT`; versões antigas falham com orientação para atualizar e refazer o build. O script é enviado pelo Make aos containers a partir deste repositório.

Se a stack já estiver rodando, após editar o `.env` recrie os serviços e crie somente os cadastros ausentes:

```bash
docker compose --profile simulator up -d --no-deps --force-recreate middleware simulator
make seed-gateway
```

Com `SEED_THINGSBOARD_GATEWAY=0` (padrão), o seed mantém o cadastro manual. `make seed-house ARGS="--dry-run"` não grava o gateway. O administrador do simulador continua sendo criado pelo procedimento da seção 6.

## 3. Subir a demo

Build do ambiente:

```bash
make build
```

Subida dos serviços:

```bash
make up
```

O `make up` sobe os profiles `simulator` e `client` por padrão.

Observação: o container do middleware executa migrações, coleta de estáticos e bootstrap do superusuário durante a inicialização.

## 4. Verificar saúde do ambiente

```bash
make healthcheck
```

O healthcheck valida:

- middleware
- simulator
- client
- parser DTDL
- InfluxDB
- Neo4j
- PostgreSQL
- Redis

## 5. Carregar o cenário demo

O seed padrão é aditivo: preserva sistemas, modelos e gateways existentes. Não exclui dispositivos, instâncias, usuários nem volumes. Também não troca credenciais nem o gateway ativo existente do simulador. Para recriar os exemplos, use `make reset-house`, conforme abaixo. `--force` é um alias obsoleto do reset protegido.


```bash
make seed-house
```

Esse comando cria o contexto de sistema e os modelos DTDL usados na demo. Com `SEED_THINGSBOARD_GATEWAY=1`, também configura o gateway nos dois serviços conforme a seção 2. Não cria todos os dispositivos e vínculos automaticamente.

Se você já tiver um gateway IoT configurado no banco e quiser carregar o cenário e disparar a descoberta de devices em seguida:

```bash
make seed-house-devices
```

Se quiser apenas redescobrir dispositivos dos gateways já cadastrados:

```bash
make discover-devices
```

Observação: `discover-devices` depende de pelo menos um `GatewayIOT` cadastrado. Use o cadastro automático do seed ou o Django admin em `/admin`.

Antes de usar `discover-devices`, confirme que existe um `GatewayIOT` válido cadastrado no admin e com credenciais corretas para o ThingsBoard alvo.

### Recriar somente os modelos de exemplo

```bash
# Conferir o escopo sem gravar:
make reset-house ARGS="--dry-run"
# Remover e recarregar apenas os modelos originais da demo:
make reset-house
```

O reset preserva o sistema, os modelos personalizados, os gateways, suas chaves, os usuários e os dispositivos dos dois serviços. Ele recusa sistemas que não tenham a descrição original da demo e o usuário criador esperado, modelos originais editados e modelos com instâncias ou outros vínculos que seriam removidos em cascata. A exclusão e a carga dos modelos ficam na mesma transação: uma falha na carga desfaz a exclusão. O reset não executa o cadastro de gateways.

A imagem do middleware precisa conter a versão atual deste comando; após atualizar o código, refaça seu build e recrie o container. `make fullclean` remove volumes e imagens e não é necessário para repetir a demo. Se estiver recomeçando do zero, mantenha a chave no `.env`, habilite `SEED_THINGSBOARD_GATEWAY=1`, configure `THINGSBOARD_AUTH_METHOD=api_key` e a URL, e siga `make build-with-deps`, `make up`, `make healthcheck`, `make seed-house`.

## 6. Acessar a aplicação

- Middleware: `http://localhost:8000`
- Swagger / OpenAPI: `http://localhost:8000/api/docs`
- Schema OpenAPI JSON: `http://localhost:8000/api/openapi.json`
- Django admin: `http://localhost:8000/admin`
- Simulator: `http://localhost:8001`
- Client: `http://localhost:8002`
- Parser Swagger: `http://localhost:8082/swagger/index.html`
- Neo4j Browser: `http://localhost:7474`
- InfluxDB: `http://localhost:8086`

### Login e criação do administrador do simulador

No middleware, entre em `http://localhost:8000/admin/` com `DJANGO_SUPERUSER_USERNAME` e `DJANGO_SUPERUSER_PASSWORD` do `.env` (template: `middts` / `middts123`). No client, o administrador local usa essas mesmas variáveis, mas o banco de usuários é independente.

No simulador, crie uma conta explicitamente após a subida:

```bash
docker compose --profile simulator exec simulator python manage.py createsuperuser
```

Informe usuário, e-mail e senha nos prompts e entre em `http://localhost:8001/admin/`. Não há uma senha universal para usuários vindos de um banco restaurado. Se o usuário já existe, redefina a senha (substitua `middts` pelo nome existente):

```bash
docker compose --profile simulator exec simulator python manage.py changepassword middts
```

O `healthcheck` verifica disponibilidade dos serviços, não valida o login. Confirme o acesso aos dois admins antes de cadastrar gateways e dispositivos.

### Alterar ou recuperar credenciais

Para middleware e client, edite `DJANGO_SUPERUSER_*` no `.env`, atualize também `MIDDTS_API_*` e recrie os containers:

```bash
docker compose --profile client up -d --no-deps --force-recreate middleware client
```

Um simples `restart` não carrega alterações de ambiente do Compose. Alterar a senha do superusuário do middleware apenas no admin ou com `changepassword` será desfeito no próximo boot se o `.env` continuar com a senha antiga.

Para o simulador do Compose atual, use `changepassword` conforme acima. Se o seu Compose personalizado repassar `SIMULATOR_SUPERUSER_*` e o entrypoint suportar esse bootstrap, ajuste também essas variáveis: nesse caso, o boot reaplica a senha configurada.

PostgreSQL, Neo4j e InfluxDB mantêm credenciais nos volumes: alterar o `.env` não troca automaticamente uma senha já inicializada. Faça a alteração no serviço e atualize as aplicações consumidoras. Não apague volumes para recuperar acesso.

## 7. Obter um token JWT

Exemplo com `curl`:

```bash
curl -X POST "http://localhost:8000/api/core/token/?username=middts&password=middts123"
```

O retorno contém `access` e `refresh`. Use o `access` como Bearer token nos endpoints protegidos.

## 8. Fluxo mínimo da demo

1. Entrar em `http://localhost:8000/api/docs`.
2. Gerar um JWT em `POST /api/core/token/`.
3. Consultar `GET /api/orchestrator/systems/`.
4. Criar ou listar modelos DTDL em `/api/orchestrator/systems/{system_id}/dtdlmodels/`.
5. Criar instâncias em `/api/orchestrator/systems/{system_id}/instances/`.
6. Se houver gateway configurado, rodar `make discover-devices` e usar os endpoints do módulo `facade`.

## 9. Comandos operacionais úteis

- `make logs`
- `make restart`
- `make shell`
- `make migrate`
- `make collectstatic`
- `make down`
- `make clean`
- `make fullclean`

## 10. Troubleshooting rápido

- Se `make healthcheck` falhar no Postgres por porta ocupada, ajuste `DB_HOST_PORT` no `.env`.
- Se mudanças no `.env` não refletirem, recrie os containers afetados com `--force-recreate`. Para credenciais persistidas, siga a seção 6. `make clean` e `make fullclean` podem remover dados; não são procedimentos de troca de senha.
- Se `discover-devices` não encontrar nada, confirme primeiro no `/admin` se existe um `GatewayIOT` válido.
- O arquivo `middts.sql` é um dump histórico e não faz parte do fluxo atual da demo baseada em migrações e `make`.
- Se `make build` falhar por contexto ausente do simulador/client, rode `make deps` ou `make build-with-deps`. Se os repositorios estiverem em outro local, informe os caminhos com `SIMULATOR_CONTEXT` e `CLIENT_CONTEXT`.

### Login do MiddTS recusado no admin

Uma conta pode ter senha válida para a API e ainda não acessar `/admin/` se não tiver `is_active=True` e `is_staff=True`. O bootstrap do middleware aplica as permissões de superusuário à conta configurada em `DJANGO_SUPERUSER_USERNAME`; o Compose fornece os padrões do template mesmo quando um `.env` antigo não contém essas variáveis. Mudanças no Compose/entrypoint precisam ser aplicadas aos containers para surtir efeito.

### ThingsBoard retorna 401 ao validar o gateway

Se o erro ocorre em `/api/auth/login`, o ThingsBoard recusou o usuário e a senha enviados pelo gateway. No simulador atual, a validação usa o cadastro `GatewayIOT` ativo no banco, não lê as credenciais do `.env` a cada teste. Confira a URL, o e-mail e a senha no admin do simulador e tente entrar na mesma instância ThingsBoard pelo navegador com esses dados. Uma conta de outra instância não garante acesso a `demo.thingsboard.io`.

Alterar o `.env` e repetir o seed não atualiza um gateway existente: isso é intencional para preservar cadastros. Edite o gateway no admin do simulador e no admin do MiddTS. Os valores `tenant@thingsboard.org` / `tenant` do template são exemplos; não use o login do MiddTS nem uma API Key no campo de senha. Para API Key, selecione o método `api_key` e preencha o campo próprio. Não é necessário resetar a demo nem apagar volumes para corrigir esse erro.

### Discovery usa login mesmo com API Key preenchida

O campo `auth_method` determina a autenticação: preencher a chave não troca o método automaticamente. No gateway do MiddTS, selecione **API Key** (`api_key`), preencha a chave e salve antes de executar `discover-devices`. Com **Usuario e senha** (`user_password`), a descoberta chama `/api/auth/login` mesmo que o campo API Key esteja preenchido. Os campos de login podem ficar vazios no modo API Key. O JWT usado para autenticar você na API do MiddTS é independente da API Key usada pelo gateway no ThingsBoard.

### API Key retorna “Invalid username or password” na descoberta

Essa mensagem também pode ser retornada pelo próprio ThingsBoard em um `GET /api/tenant/devices` autenticado com `X-Authorization: ApiKey ...`, sem tentativa de login por usuário e senha. A descoberta repassa o erro remoto; no modo API Key, um 401 agora inclui `gateway_auth_method=api_key` e uma explicação em `detail`.

A presença de usuário e senha no cadastro não muda a autenticação REST quando o método é `api_key`. Confira se a chave foi criada para um usuário na mesma instância, está ativa e não expirou. Não use um access token de dispositivo, JWT de login ou token do InfluxDB nesse campo. O suporte a API Key exige ThingsBoard 4.3 ou superior, conforme a [documentação oficial](https://thingsboard.io/docs/reference/rest-api/). O listener WebSocket tem um fluxo separado que pode tentar login de fallback; isso não é a chamada de descoberta.

### Criação de DTs falha com Neo4j Unauthorized

O middleware e o Neo4j devem usar a mesma senha: `NEO4J_PASSWORD` deve corresponder à senha de `NEO4J_AUTH=neo4j/senha`. O Compose repassa essas variáveis ao middleware. Em uma base Neo4j já inicializada, mudar `NEO4J_AUTH` não redefine a senha persistida.

Depois de corrigir a configuração, recrie somente o middleware; não apague volumes. A geração de DTs verifica a conexão Neo4j antes das escritas e usa uma transação para os registros relacionais. Isso não constitui uma transação distribuída com o grafo. Se uma execução antiga deixou instâncias parciais, inspecione as instâncias existentes antes de repetir a carga; não use reset ou fullclean para corrigir autenticação.
