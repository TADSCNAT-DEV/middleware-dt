# Instalação Manual do middleware-dt

Este guia reúne os passos para subir o middleware sem o fluxo automatizado do `Makefile`. Use este caminho quando você quiser gerenciar cada serviço separadamente.

[Voltar para a documentação principal](../README.md#instalacao-manual)

## O que precisa estar disponível

No modo manual, o middleware depende dos seguintes serviços externos:

- PostgreSQL para persistência relacional.
- Neo4j para a camada de grafo.
- InfluxDB para séries temporais.
- Redis para cache e sessões, quando habilitado.
- Parser DTDL, se você não for usar o container interno do compose.
- Gateway IoT, como ThingsBoard, para o fluxo de descoberta e integração.

## 1. Instalar as dependências Python

```bash
pip install -r requirements/base.txt
```

## 2. Provisionar os serviços externos

Você pode instalar os serviços localmente, em containers separados ou apontar o middleware para instâncias já existentes.

### PostgreSQL

Configure um banco relacional para o middleware e ajuste as credenciais no arquivo de settings usado pelo projeto.

Exemplo de configuração:

```python
DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.postgresql',
        'NAME': 'nomebanco',
        'USER': 'postgres',
        'PASSWORD': 'postgres',
        'HOST': 'localhost',
        'PORT': '5432',
    }
}
```

### Neo4j

Se o Neo4j estiver fora do Docker do `Makefile`, instale o banco e disponibilize o endpoint Bolt para o middleware.

Instalação rápida via Docker:

```bash
docker run -d \
    --name neo4j-container \
    -p 7474:7474 -p 7687:7687 \
    -e NEO4J_AUTH=neo4j/password \
    neo4j:latest
```

Instalação via Ubuntu/Debian segue o fluxo oficial do Neo4j e pode ser feita com o pacote do repositório da Neo4j.

Exemplo de configuração:

```python
from neomodel import config

config.DATABASE_URL = "bolt://neo4j:password@localhost:7687"
```

### InfluxDB

Suba o InfluxDB, crie o bucket e defina a organização e o token que serão usados pelo middleware e pelos relatórios de série temporal.

Instalação rápida via Docker:

```bash
docker run -d \
    --name influxdb-container \
    -p 8086:8086 \
    influxdb:latest
```

Pontos que normalmente precisam ser configurados:

- URL do InfluxDB.
- Organization.
- Bucket.
- Token de acesso.

### Parser DTDL

Se você for usar um parser externo, configure a URL do serviço na variável de ambiente `DTDL_PARSER_URL`.

Se preferir um container isolado, você pode subir a API auxiliar de parser com:

```bash
docker run -p 8082:8080 andregustavoo/parserwebapi:latest
```

Exemplo:

```bash
DTDL_PARSER_URL=http://parser:8080/api/DTDLModels/parse/
```

### Redis

Se o seu ambiente usar cache ou sessões com Redis, suba o serviço e ajuste a configuração correspondente no middleware.

### Gateway IoT

Cadastre e configure um gateway IoT compatível, como o ThingsBoard, para que o middleware consiga descobrir dispositivos e trocar comandos.

## 3. Configurar variáveis de ambiente

Ajuste o `.env` ou o arquivo de settings equivalente do seu ambiente com os dados dos serviços acima, incluindo:

- PostgreSQL.
- Neo4j.
- InfluxDB.
- Redis.
- Parser DTDL.
- Gateway IoT.

## 4. Aplicar migrações e subir o middleware

```bash
python manage.py migrate
python manage.py createsuperuser
python manage.py runserver
```

Se o ambiente também depender do Neo4j, InfluxDB ou parser externo, valide primeiro que esses serviços estão disponíveis e que as URLs/credenciais foram ajustadas no `.env` e nos settings.

O comando `createsuperuser` pede usuário, e-mail e senha; essa conta dá acesso a `/admin/` e à autenticação JWT do MiddTS. Na execução manual, definir `DJANGO_SUPERUSER_*` no `.env` não executa o bootstrap do container. Para recuperar a senha de uma conta existente:

```bash
python manage.py changepassword nome_do_usuario
```

Para executar fora do Docker, use endereços acessíveis pelo host: por exemplo, `POSTGRES_HOST=localhost` e a porta publicada do PostgreSQL. Nomes como `db`, `neo4j`, `influxdb` e `parser` do template são nomes da rede Compose. Configure também `NEO4J_USER`, `NEO4J_PASSWORD` e `NEO4J_URL`, além do token e endereço do InfluxDB.

O IoT Simulator tem banco e usuários próprios. Na pasta dele, com seu ambiente virtual e suas variáveis carregadas, execute:

```bash
export DJANGO_SETTINGS_MODULE=iot_simulator.settings_base
python manage.py migrate
python manage.py createsuperuser
python manage.py runserver 8001
```

Entre em `http://localhost:8001/admin/` com a conta criada nesse passo. Para trocar uma senha existente, execute `python manage.py changepassword nome_do_usuario` nessa mesma pasta. O login do ThingsBoard (`THINGSBOARD_USER` / `THINGSBOARD_PASSWORD`) é independente dessas contas. Evite reutilizar o ambiente PostgreSQL do middleware ao iniciar o simulador: sem `POSTGRES_HOST` e `POSTGRES_DB`, ele usa SQLite.

Para o comportamento de senhas no Docker e a integração com o client, consulte [acessos e senhas](demo-make.md#2-preparar-o-ambiente).

## 5. Observações

- O fluxo com `make` já sobe a pilha completa automaticamente.
- Se você usar o Docker/Make, não precisa instalar Neo4j, InfluxDB ou parser manualmente no host.
- Para o fluxo automatizado da demo, use [Execução com Docker e Make](demo-make.md).
