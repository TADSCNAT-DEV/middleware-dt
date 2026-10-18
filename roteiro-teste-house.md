# Roteiro de laboratório — House 2.0 com MiddTS

Este roteiro parte de uma instalação nova e percorre: simulador → ThingsBoard → descoberta no MiddTS → criação dos gêmeos digitais → associação de propriedades → visualização no client, Neo4j e InfluxDB.

Use uma instalação local e uma conta ThingsBoard de laboratório por aluno ou grupo. Todos os comandos de terminal deste roteiro partem da pasta `middleware-dt`, salvo indicação contrária. Não é necessário instalar Python ou os bancos no computador: as aplicações executam em containers.

[Documentação principal](README.md) · [Referência de Docker/Make e credenciais](docs/demo-make.md)

## 1. Preparação

Tenha Git, Docker em execução, Docker Compose v2 (`docker compose`) e Make, espaço para as imagens e acesso à internet para baixar dependências e acessar o ThingsBoard. No Windows, execute os comandos no terminal da distribuição WSL integrada ao Docker.

```bash
git --version
docker --version
docker compose version
make --version
```

Obtenha a versão do repositório indicada pelo professor:

```bash
git clone <URL_DO_REPOSITORIO_FORNECIDA_PELO_PROFESSOR>
cd middleware-dt
cp .env.example .env
```

Copie o template apenas na primeira configuração. Se já existe `.env`, edite-o para preservar as credenciais. Ele é local e não deve ser incluído na entrega ou no commit.

Os repositórios `iot_simulator` e `middts-client` são projetos separados: o build abaixo tenta cloná-los quando as pastas não existem. O professor deve disponibilizar versões compatíveis dos três projetos. O simulador precisa conter o cadastro `GatewayIOT` e a dashboard; uma versão antiga pode não suportar este roteiro.

## 2. Configurar o `.env`

Edite as entradas existentes e acrescente as que faltarem. Não deixe várias definições da mesma variável.

### 2.1 Endereços e credenciais locais

Para executar no próprio computador, ajuste:

```dotenv
HOST_IP=127.0.0.1
MIDDLEWARE_PORT=8000
SIMULATOR_PORT=8001
CLIENT_PORT=8002
CLIENT_SITE_URL=http://localhost:8002

DJANGO_SUPERUSER_USERNAME=middts
DJANGO_SUPERUSER_PASSWORD=middts123
DJANGO_SUPERUSER_EMAIL=middts@example.com
MIDDTS_API_USERNAME=middts
MIDDTS_API_PASSWORD=middts123

NEO4J_USER=neo4j
NEO4J_PASSWORD=middts123
NEO4J_AUTH=neo4j/middts123

INFLUXDB_INIT_USERNAME=middts
INFLUXDB_INIT_PASSWORD=middts123
INFLUXDB_ORGANIZATION=middts
INFLUXDB_BUCKET=iot_data
INFLUXDB_TOKEN=middts_token
```

Essas senhas são exemplos locais. Se escolher outras, mantenha `MIDDTS_API_*` alinhado à conta do MiddTS e a senha de `NEO4J_AUTH` igual a `NEO4J_PASSWORD`. Preserve as demais configurações do template, incluindo PostgreSQL e parser. Os nomes `db`, `neo4j`, `influxdb` e `parser` são endereços internos dos containers; o navegador usa `localhost`.

### 2.2 Conta ThingsBoard

Primeiro entre pelo navegador na instância escolhida e confirme seu e-mail e sua senha. O roteiro usa `https://demo.thingsboard.io` como exemplo; se o professor fornecer outra instância, substitua todos os endereços correspondentes.

```dotenv
SEED_THINGSBOARD_GATEWAY=1
THINGSBOARD_GATEWAY_NAME=ThingsBoard Laboratorio
THINGSBOARD_GATEWAY_URL=https://demo.thingsboard.io
THINGSBOARD_AUTH_METHOD=user_password
THINGSBOARD_USER=seu_email_cadastrado_no_thingsboard
THINGSBOARD_PASSWORD=sua_senha_do_thingsboard

SIMULATOR_THINGSBOARD_HOST=https://demo.thingsboard.io
MIDDLEWARE_THINGSBOARD_HOST=demo.thingsboard.io
MIDDLEWARE_TB_HOST=demo.thingsboard.io
MIDDLEWARE_TB_SCHEME=https
MIDDLEWARE_TB_PORT=443
THINGSBOARD_MQTT_PORT=1883
THINGSBOARD_MQTT_KEEP_ALIVE=60
```

A conta ThingsBoard é independente da conta `middts`. O exemplo `tenant@thingsboard.org` / `tenant` não garante acesso ao serviço público. Use uma conta do laboratório com permissão para gerenciar dispositivos do tenant. Para uma instância diferente, ajuste também a porta MQTT conforme sua configuração.

### 2.3 Começar com dispositivos novos e iniciar a telemetria manualmente

Para uma instalação nova, acrescente:

```dotenv
SIMULATOR_AUTO_START=0
SIMULATOR_SEED_DB_ON_FIRST_BOOT=0
SIMULATOR_RESTORE_DB_ON_BOOT=0
RESET_SIM_DB=0
```

Assim o simulador aguarda o início pela dashboard e não restaura um banco de exemplo com dados anteriores. Essas opções não esvaziam um volume que já exista. Não use `fullclean` para avançar neste roteiro.

## 3. Construir e subir os serviços

```bash
make build-with-deps
make up
make healthcheck
docker compose --profile simulator --profile client ps
```

Na primeira execução, o download das imagens e das dependências pode demorar. Aguarde os serviços ficarem disponíveis. O healthcheck verifica disponibilidade, não garante que os logins e integrações estejam corretos.

**Resultado esperado:** middleware, simulador, client, parser, PostgreSQL, Redis, Neo4j e InfluxDB disponíveis. A stack não instala o ThingsBoard: ela se conecta à instância externa configurada.

## 4. Acessos aos serviços

Endereços e credenciais abaixo correspondem à configuração local deste roteiro.

| Serviço | Endereço no navegador | Acesso |
| --- | --- | --- |
| MiddTS | http://localhost:8000/ | Página inicial |
| Admin MiddTS | http://localhost:8000/admin/ | `middts` / `middts123`, ou `DJANGO_SUPERUSER_*` |
| Swagger MiddTS | http://localhost:8000/api/docs | Sessão do admin ou JWT MiddTS nas chamadas protegidas |
| Schema OpenAPI | http://localhost:8000/api/openapi.json | Descrição das rotas |
| Simulador | http://localhost:8001/dashboard/ | Administrador próprio, criado na seção 5 |
| Admin simulador | http://localhost:8001/admin/ | Mesma conta própria do simulador |
| Client | http://localhost:8002/ | `middts` / `middts123`, ou administrador local configurado |
| Admin client | http://localhost:8002/admin/ | `DJANGO_SUPERUSER_*`; banco separado do MiddTS |
| Neo4j Browser | http://localhost:7474/ | Conectar a `bolt://localhost:7687`, usuário `neo4j`, senha `NEO4J_PASSWORD` |
| InfluxDB | http://localhost:8086/ | `INFLUXDB_INIT_USERNAME` / `INFLUXDB_INIT_PASSWORD` |
| Parser DTDL Swagger | http://localhost:8082/swagger/index.html | Interface do parser, sem conta definida por esta stack |
| ThingsBoard | https://demo.thingsboard.io/ | Sua conta ThingsBoard na instância escolhida |

PostgreSQL e Redis não têm página web nesta stack. Para verificá-los no terminal:

```bash
docker compose exec db sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "SELECT 1;"'
docker compose exec redis redis-cli ping
```

**Resultado esperado:** PostgreSQL retorna `1` e Redis retorna `PONG`. Para um cliente SQL externo, use `localhost`, a porta `DB_HOST_PORT` e as credenciais `POSTGRES_*` do `.env`.

## 5. Carregar os modelos e configurar o acesso ao simulador

```bash
make seed-house
docker compose --profile simulator exec simulator python manage.py createsuperuser
```

O primeiro comando cria o sistema **House 2.0 Condominium**, carrega os oito modelos DTDL e, com `SEED_THINGSBOARD_GATEWAY=1`, cria os gateways ausentes no MiddTS e no simulador. Não cria os dispositivos físicos nem todos os gêmeos e vínculos.

No segundo comando, escolha usuário, e-mail e senha para o admin do simulador. Guarde-os. Definir `SIMULATOR_SUPERUSER_*` somente no `.env` principal não garante a criação dessa conta pelo Compose atual. Se a conta já existe, recupere a senha com:

```bash
docker compose --profile simulator exec simulator python manage.py changepassword nome_do_usuario
```

Entre nos dois admins e confira:

1. No MiddTS, o gateway tem a URL e o método de autenticação escolhidos. Use a ação **Verificar acesso ao gateway**.
2. No simulador, abra **Gateways IoT**, valide o acesso e confira se o gateway está **ativo**. Se necessário, selecione-o e use **Ativar gateway selecionado**.
3. Na dashboard do simulador, a conexão deve ser válida. Ainda não inicie a telemetria.

Repetir o seed preserva gateways existentes, inclusive suas credenciais. Se corrigir o `.env` depois do primeiro cadastro, edite também os gateways nos respectivos admins. Recriar containers não substitui automaticamente esses registros.

## 6. Criar dispositivos de exemplo no simulador e no ThingsBoard

Em uma base nova, importe uma casa do template incluído no simulador:

```bash
docker compose --profile simulator exec simulator python manage.py import_devices_from_json /iot_simulator/condominium.json --system "Laboratorio House" --replicas 1
```

O template atual tem **sete dispositivos por casa**: bomba, irrigação, sensor de umidade do solo, dois aparelhos de ar condicionado e duas lâmpadas. O cadastro tenta criar ou localizar os dispositivos no ThingsBoard e recuperar os tokens individuais.

**Resultado esperado:** sete dispositivos no admin do simulador e os correspondentes na mesma conta ThingsBoard. No simulador, confira que possuem `thingsboard_id` e `token`. Uma mensagem de criação local não basta para garantir que o cadastro remoto funcionou.

Execute essa importação uma vez na base nova: o comando atual usa criação direta e repetir o mesmo template pode causar conflito de identificadores. Se já existem dispositivos, inspecione-os antes de importar. Para mais casas em uma primeira importação, altere `--replicas`.

## 7. Iniciar e conferir a telemetria

Acesse http://localhost:8001/dashboard/, valide o gateway, habilite a opção de escrita no InfluxDB e inicie a simulação. Se a dashboard oferecer filtro de sistema, escolha **Laboratorio House**.

Confira:

- A dashboard informa a simulação em execução e mostra os logs.
- Os dispositivos possuem tokens e conectam ao MQTT.
- No ThingsBoard, abra um dispositivo e observe **Latest telemetry**. Aguarde alguns ciclos para os valores ou timestamps avançarem.

O acesso REST ao ThingsBoard e o acesso MQTT são conexões distintas: validar o gateway não garante que a porta MQTT esteja acessível pela sua rede.

## 8. Autenticar e descobrir os dispositivos no MiddTS

Abra http://localhost:8000/api/docs. Você pode fazer login no admin MiddTS no mesmo navegador ou obter um JWT em:

```http
POST /api/core/token/?username=middts&password=middts123
```

Use as credenciais que escolheu. Copie `access`, clique em **Authorize** no Swagger e informe o token no esquema Bearer. Nas requisições externas, envie `Authorization: Bearer <access>`. Esse JWT autentica você no MiddTS; não é a credencial do gateway ThingsBoard. Não inclua senhas ou tokens nas evidências da atividade.

Anote os IDs reais consultando:

```http
GET /api/core/gatewaysiot/
GET /api/orchestrator/systems/
```

Nas próximas chamadas, substitua `{gateway_id}` e `{system_id}` pelos IDs encontrados. Não presuma que sejam sempre `1`.

Execute a descoberta:

```http
GET /api/facade/gatewaysiot/{gateway_id}/discover-devices/?pageSize=200&page=0
```

**Resultado esperado:** resposta com `created` e `updated`. Confira os dispositivos e suas propriedades em:

```http
GET /api/facade/devices/
```

O gateway pode enxergar outros dispositivos do mesmo tenant; em uma conta nova deste laboratório, espere os sete do template. Se o tenant tiver mais de 200 dispositivos, solicite as páginas seguintes (`page=1`, `page=2`, etc.) até cobrir o conjunto. O endpoint não percorre todas as páginas automaticamente.

Alternativa no terminal, para o gateway de ID 1:

```bash
make discover-devices ARGS="--gateway-ids=1 --page-size=200 --page=0"
```

## 9. Criar os gêmeos digitais a partir dos dispositivos

Confirme os modelos carregados:

```http
GET /api/orchestrator/systems/{system_id}/dtdlmodels/
```

No Swagger, execute primeiro a simulação:

```http
POST /api/orchestrator/systems/{system_id}/instances/from-devices/
```

```json
{"dry_run": true}
```

Leia o campo `output`: confira os dispositivos, os modelos e a hierarquia sugerida. Depois repita no mesmo endpoint para gravar:

```json
{"dry_run": false}
```

**Resultado esperado:** `status: "success"` e relatório em `output`. Consulte:

```http
GET /api/orchestrator/systems/{system_id}/instances/
```

A criação usa heurísticas sobre tipos, nomes e metadados; confira se casas, cômodos e equipamentos ficaram associados aos modelos esperados. A quantidade de gêmeos não precisa ser igual à quantidade de dispositivos, pois existem nós de hierarquia.

Esse fluxo já tenta criar relacionamentos e associar propriedades por correspondência. Ele pode deixar poucas ou nenhuma propriedade pendente para a associação semântica da próxima seção.

## 10. Testar a associação automática de propriedades

### 10.1 Visualizar sugestões sem gravar

```http
POST /api/orchestrator/systems/{system_id}/instances/autobinding/preview/
```

```json
{
  "threshold": 0.6,
  "only_unbound": true,
  "causal_only": true,
  "allow_device_property_reuse": false,
  "gateway_ids": [1],
  "limit": 200
}
```

Substitua `[1]` pelo ID real do gateway. Observe `candidates`, os nomes do DT e do dispositivo, as propriedades e o `score`. O score é uma medida de similaridade, não uma garantia de associação correta. Na primeira execução, o modelo semântico pode precisar carregar ou baixar seus arquivos.

**Resultado esperado:** sugestões coerentes para propriedades causais ainda sem vínculo. Uma lista vazia não implica falha: a criação anterior pode já ter associado todas elas. Confira `device_property` nas propriedades das instâncias retornadas na seção 9.

Para inspecionar outras possibilidades sem gravar, repita somente o **preview** com `only_unbound: false`, `causal_only: false` e `allow_device_property_reuse: true`. Essas opções ampliam o conjunto avaliado; mantenha as opções conservadoras abaixo ao aplicar o teste principal.

### 10.2 Aplicar sugestões preservando vínculos existentes

```http
POST /api/orchestrator/systems/{system_id}/instances/autobinding/apply/
```

```json
{
  "threshold": 0.6,
  "only_unbound": true,
  "causal_only": true,
  "allow_device_property_reuse": false,
  "overwrite_existing": false,
  "gateway_ids": [1],
  "limit": 200
}
```

O endpoint recalcula as sugestões; ele não recebe uma lista congelada do preview. Anote `evaluated`, `applied` e `skipped`. Consulte novamente as instâncias e confira os IDs de `device_property`. Com as opções acima, vínculos existentes são preservados e a mesma propriedade física não é reutilizada para outro vínculo.

`applied: 0` é esperado quando não há candidatos pendentes. Não remova vínculos apenas para obter uma contagem positiva.

### 10.3 Relacionamentos entre gêmeos, se houver pendências

Relacionamentos entre instâncias são diferentes dos vínculos entre propriedades. Para avaliar os que faltam:

```http
POST /api/orchestrator/systems/{system_id}/instances/relationships/preview/
```

```json
{"threshold": 0.5, "only_missing": true, "limit": 200}
```

Se houver sugestões coerentes, envie o mesmo JSON para:

```http
POST /api/orchestrator/systems/{system_id}/instances/relationships/apply/
```

Anote `created` e `skipped`. Zero relacionamentos novos pode significar que a criação já montou a hierarquia.

## 11. Visualizar no client

Entre em http://localhost:8002/ com a conta local do client. No painel, use a opção de importação/sincronização; o caminho http://localhost:8002/import-all/ importa sistemas, modelos, instâncias, relacionamentos e vínculos do MiddTS para o banco local do client.

Depois visite:

- http://localhost:8002/systems/ — sistemas importados.
- http://localhost:8002/instances/ — instâncias.
- http://localhost:8002/dtexplorer/ — exploração da estrutura.
- http://localhost:8002/manage-bindings/ — vínculos.

**Resultado esperado:** o sistema House e os objetos consultados pela API aparecem no client. Se os dados estiverem antigos, sincronize novamente; o client tem banco próprio. Se a autenticação com a API falhar, confira `MIDDTS_API_USERNAME`, `MIDDTS_API_PASSWORD` e a existência da conta no MiddTS.

## 12. Inspecionar o grafo no Neo4j

Abra http://localhost:7474/ e conecte com `bolt://localhost:7687`, usuário `neo4j` e senha `NEO4J_PASSWORD` do `.env`.

Execute consultas de leitura no Browser:

```cypher
MATCH (n)
RETURN labels(n) AS labels, count(*) AS quantidade;
```

```cypher
MATCH (n)-[r]->(m)
RETURN n, r, m
LIMIT 100;
```

**Resultado esperado:** nós e relações criados pela sincronização dos gêmeos. Um modelo DTDL apenas carregado pelo seed não garante um grafo populado; faça a criação de instâncias e propriedades antes desta verificação.

## 13. Consultar os dados no InfluxDB

Abra http://localhost:8086/ com `INFLUXDB_INIT_USERNAME` e `INFLUXDB_INIT_PASSWORD`. Selecione a organização `middts` e o bucket `iot_data`, ou os valores que escolheu.

No **Data Explorer**, use o editor de script para consultar os últimos 15 minutos:

```flux
from(bucket: "iot_data")
  |> range(start: -15m)
  |> filter(fn: (r) => r._measurement == "device_data")
  |> limit(n: 50)
```

**Resultado esperado:** dados recentes durante a simulação com escrita InfluxDB habilitada. Se não houver pontos, confira a simulação, a opção de escrita, bucket, organização, token e intervalo de tempo. Login na interface InfluxDB e token de escrita das aplicações são credenciais distintas.

Este roteiro verifica presença de telemetria. Relatórios de latência exigem medições e correlações específicas; não conclua que há latência zero quando uma consulta não retorna pontos. Para essa etapa adicional, consulte [Relatórios InfluxDB](docs/README.md).

## 14. Teste opcional de comando RPC

Com o simulador em execução, obtenha o ID de uma lâmpada em `GET /api/facade/devices/` e confira os métodos em:

```http
GET /api/facade/devices/{device_id}/rpc-methods/
```

Para uma lâmpada que exponha `switchLed`, envie:

```http
POST /api/facade/devices/{device_id}/rpc/
```

```json
{"method": "switchLed", "params": {"status": true}}
```

Neste teste, o objeto de parâmetros não vazio liga a lâmpada no handler atual (`bool(params)`). Não conclua que `{"status": false}` desligará o equipamento: esse objeto também é avaliado como verdadeiro nessa implementação. Confira o handler/método listado e os logs do simulador para outros comandos. O endpoint envia RPC de uma via: uma resposta de aceitação não prova, sozinha, que o dispositivo executou o comando. Confirme a mudança no estado/telemetria do equipamento simulado.

## 15. Teste alternativo com API Key

Faça primeiro o fluxo principal funcionar. Depois, em cada gateway dos dois admins:

1. Selecione o método **API Key**.
2. Cole no campo próprio somente o valor de uma chave de usuário criada na mesma instância ThingsBoard.
3. Salve e valide a conexão separadamente em cada serviço.
4. Repita a descoberta no MiddTS e compare o resultado.

Usuário e senha preenchidos não alteram a autenticação REST no modo API Key. O listener WebSocket do MiddTS tem um fluxo separado com fallback para login, portanto o sucesso da descoberta com chave não valida automaticamente esse listener.

A autenticação por chave de usuário exige ThingsBoard 4.3 ou superior: consulte a [referência oficial](https://thingsboard.io/docs/reference/rest-api/). Não use o token individual de um dispositivo nem o token do InfluxDB. Se a mesma chave funcionar em um serviço e falhar no outro, confira as URLs e os valores efetivamente salvos nos dois bancos. O seed preserva gateways existentes.

### 15.1 Usar ThingsBoard Cloud como segundo gateway

Para comparar as instâncias, mantenha o gateway da demo e crie um segundo cadastro em cada admin com a URL da sua região ThingsBoard Cloud (por exemplo, `https://thingsboard.cloud`). Use uma conta criada nessa instância; o cadastro da demo não implica acesso ao Cloud.

No simulador, preencha o host e a porta MQTT fornecidos pela sua instância Cloud; não presuma que a porta MQTT da demo seja a mesma. Primeiro valide o acesso REST. Validar um gateway inativo não altera o gateway usado pela simulação. Ative o gateway Cloud somente quando quiser direcionar a simulação para ele e confira os dispositivos e tokens da instância de destino antes de iniciar.

No MiddTS, selecione o ID real do segundo gateway nas chamadas. Se ele recebeu ID 2:

```http
GET /api/facade/gatewaysiot/2/discover-devices/?pageSize=200&page=0
```

Na associação automática, use `"gateway_ids": [2]`. O endpoint de criação `instances/from-devices/` não oferece filtro por gateway no payload; ele percorre os dispositivos importados. Para um teste isolado de criação no Cloud, use uma base de laboratório separada ou verifique o conjunto de dispositivos antes da execução.

A versão atual normaliza a barra final da URL nas chamadas REST. Em imagens antigas, salvar `https://thingsboard.cloud/` pode gerar `//api/auth/login` e falhar; use o endereço sem a barra final ou atualize a imagem. O teste de login e listagem REST não comprova telemetria MQTT, RPC ou WebSocket: valide cada etapa no Cloud como nas seções anteriores.

## 16. Repetir o teste e encerrar

Para parar mantendo os dados:

```bash
make down
```

Para continuar depois:

```bash
make up
```

Para carregar modelos ausentes, use `make seed-house`. O seed preserva modelos existentes e credenciais dos gateways. A descoberta pode atualizar dispositivos importados, e a importação JSON do simulador não deve ser repetida indiscriminadamente.

Para avaliar uma recriação apenas dos modelos originais de exemplo:

```bash
make reset-house ARGS="--dry-run"
```

O reset recusa modelos editados ou com instâncias/vínculos que seriam apagados. Após criar os DTs deste laboratório, essa recusa pode ser esperada. Ele não é um reset completo de uma atividade já populada. Para repetir a atividade inteira desde uma base vazia, use um ambiente separado definido pelo professor; não remova dados do ambiente compartilhado. `make clean` e `make fullclean` removem volumes e não fazem parte da repetição normal.

## 17. Diagnóstico dos problemas encontrados no laboratório

| Sintoma | Verificação e correção |
| --- | --- |
| Login no admin MiddTS recusado | Confira senha e permissões `is_active`/`is_staff`. O administrador é configurado por `DJANGO_SUPERUSER_*`. O Compose atual fornece os padrões mesmo com `.env` antigo. |
| Simulador sem login | Crie o administrador próprio da seção 5; ele não é a conta do ThingsBoard. |
| Dashboard sem gateway ativo | Valide e ative o gateway no admin do simulador. Validar acesso e ativar são ações diferentes. |
| ThingsBoard responde 401 | Confira a instância e as credenciais do gateway salvo, não só o `.env`. |
| API Key recebe “Invalid username or password” | Essa mensagem também pode vir de uma requisição autenticada por API Key; não prova tentativa de login. Confira chave, instância e método salvo. |
| `Neo.ClientError.Security.Unauthorized` ao criar DT | Alinhe `NEO4J_PASSWORD` e `NEO4J_AUTH`. Use o Compose atualizado, que não fixa a senha do middleware. Não apague volumes. |
| Discovery sem dispositivos | Confirme que os dispositivos existem no mesmo tenant, que o cadastro remoto funcionou e que está consultando a página correta. |
| Associação sem candidatos | Confira propriedades importadas, vínculos já existentes, `causal_only`, gateway e threshold. Use preview ampliado para diagnóstico. |
| Client sem objetos | Sincronize os dados e confira as credenciais `MIDDTS_API_*`. |
| InfluxDB sem pontos | Confira simulação ativa, escrita habilitada, token, bucket, organização e janela temporal. |
| Configuração nova do `.env` não aparece | Recrie os containers afetados com `--force-recreate`. Credenciais de bancos já inicializados e gateways existentes exigem alteração no cadastro correspondente. |
| Código novo não aparece no container | Faça build e recrie o serviço; reiniciar apenas não atualiza a imagem. |

Para logs dos serviços:

```bash
docker compose --profile simulator --profile client logs --tail=100 middleware simulator client
```

Para o listener interno do middleware:

```bash
docker compose exec middleware tail -n 100 /middleware-dt/logs/listen_gateway.log
```

Revise os logs antes de compartilhar: podem conter identificadores e credenciais de dispositivos.

## 18. Evidências para entregar

Registre a versão/commit dos três projetos usados e entregue um relatório sem senhas ou tokens contendo:

- Estado dos serviços e resultado do healthcheck.
- IDs do sistema e do gateway usados nas chamadas.
- Dispositivos criados no simulador e encontrados no ThingsBoard.
- Resultado da descoberta (`created` / `updated`).
- Saída do dry-run e da criação dos DTs, com uma conferência da hierarquia.
- Sugestões do autobinding e resultado da aplicação (`evaluated` / `applied` / `skipped`), explicando contagens zero.
- Visualização das instâncias no client, consulta do grafo e amostra temporal no InfluxDB.
- Se executados, resultado do RPC e comparação entre autenticação por senha e por API Key.

A atividade está concluída quando os dispositivos enviam telemetria, os DTs e seus vínculos foram conferidos, e os dados correspondentes podem ser inspecionados nos serviços. O relatório deve descrever também sugestões rejeitadas ou limitações observadas, sem assumir que toda associação automática está correta.
