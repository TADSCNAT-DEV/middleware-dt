# middleware-dt

Este projeto apresenta a primeira versão de um middleware para gêmeos digitais, que atua como uma camada de abstração entre o [ThingsBoard](https://thingsboard.io/) (ou outro gateway IoT) e uma API para plataformas visuais de gêmeos digitais.  
A solução visa integrar dispositivos físicos cadastrados em um gateway IoT com modelos DTDL, permitindo consultas complexas, representações hierárquicas e relações causais através de bancos de dados relacionais e orientados a grafos (Neo4j).

---

## Objetivo

O objetivo principal é criar uma camada intermediária em Python para conectar e gerenciar a comunicação entre dispositivos físicos (através do gateway IoT, como o ThingsBoard) e suas contrapartes de gêmeos digitais, facilitando a integração, expansão e manutenção da solução.

---

## Arquitetura dos Dados

- **PostgreSQL (ou outro BD relacional)**  
  Armazena dados estruturados, como usuários, dispositivos, modelos e informações tabulares.

- **Neo4j (banco de dados orientado a grafos)**  
  Usado para representar gêmeos digitais (nós) e suas propriedades, além de conexões (arestas) entre gêmeos. Permite consultas complexas e análise de relacionamentos hierárquicos e causais.

- **InfluxDB (banco de dados temporal)**
  Usado para armazenar séries temporais, leituras de sensores e métricas históricas do middleware e dos dispositivos integrados.
---

## Arquitetura do Middleware

O middleware é organizado em camadas para facilitar manutenção, escalabilidade e evolução:

- **Orchestrator:**  
  Coordena as operações entre os modelos de gêmeos digitais (DTDL) e o gateway IoT (OE Facade).

- **Facade:**  
  Fornece uma interface unificada para comunicação com o gateway IoT (ex: ThingsBoard).

- **Core:**  
  Gerencia a lógica principal do middleware, incluindo configurações, cadastro de gateways e demais recursos centrais.

- **Utils:**  
  Conjunto de utilitários e helpers para auxiliar diversas partes do middleware.

---

## Estrutura de Diretórios (Exemplo)

    middleware-dt/
    ├── core/
    │   ├── admin.py
    │   ├── api.py
    │   ├── models.py
    │   ├── schemas.py
    │   ├── urls.py
    │   └── views.py
    │
    ├── facade/
    │   ├── admin.py
    │   ├── api.py
    │   ├── models.py
    │   ├── schemas.py
    │   ├── urls.py
    │   └── views.py
    │
    ├── middleware-dt/
    │   ├── settings_base.py
    │   ├── settings.py
    │   ├── urls.py
    │   └── wsgi.py
    │
    ├── orchestrator/
    │   ├── admin.py
    │   ├── api.py
    │   ├── models.py
    │   ├── schemas.py
    │   ├── urls.py
    │   └── views.py
    │
    ├── manage.py
    └── requirements/
        ├── base.txt


Esta arquitetura modular permite fácil manutenção e expansão futura.

---

## Comece por aqui

Você pode usar o middleware de duas formas principais e complementar com os guias de suporte abaixo.

### Roteiro de laboratório para alunos

Para executar o teste completo do zero, siga o [Roteiro House 2.0](roteiro-teste-house.md). Ele inclui configuração das credenciais, acesso aos serviços, dispositivos do simulador, descoberta, criação dos gêmeos, associação automática e evidências da atividade.

### Fluxo rápido (primeira execução)

Se você quer apenas subir o ambiente completo local pela primeira vez, siga esta sequência:

1. `cp .env.example .env`
2. Revise as credenciais no `.env` conforme o [guia de acessos e senhas](docs/demo-make.md#2-preparar-o-ambiente). Para cadastrar o ThingsBoard nos dois serviços durante o seed, configure também o [gateway automático](docs/demo-make.md#cadastrar-o-gateway-automaticamente-no-seed).
3. `make build-with-deps`
4. `make up`
5. `make healthcheck`
6. `make seed-house`
7. [Crie o administrador do simulador e valide os logins](docs/demo-make.md#login-e-criação-do-administrador-do-simulador).

Se algum contexto de build estiver faltando (`iot_simulator` ou `middts-client`), o `make` vai avisar com instruções de correção.

### <a id="execucao-docker-make"></a>Execução com Docker e Make

Fluxo recomendado para subir a stack completa com Docker Compose e `make`.

[Abrir guia de execução com Docker e Make](docs/demo-make.md)

### <a id="instalacao-manual"></a>Instalação manual

Fluxo para subir cada serviço separadamente fora do compose principal.

[Abrir guia de instalação manual](docs/instalacao-manual.md)

### <a id="fluxo-thingsboard"></a>Fluxo com ThingsBoard

Cenário funcional para cadastro de gateway, descoberta de devices e vínculo com DTDL.

[Abrir guia de fluxo com ThingsBoard](docs/fluxo-thingsboard.md)

### <a id="configuracoes-importantes"></a>Configurações importantes

Resumo de parâmetros operacionais relevantes (como `DEFAULT_INACTIVITY_TIMEOUT`).

[Abrir configurações importantes](docs/configuracoes-importantes.md)

### <a id="relatorios-influxdb"></a>Relatórios InfluxDB

Consultas e relatórios para análise de latência e métricas temporais.

[Abrir documentação de relatórios InfluxDB](docs/README.md)

### <a id="changelog-tecnico"></a>Changelog técnico

Registro de mudanças de autenticação, parser e migrações recentes.

[Abrir changelog técnico](docs/changelog.md)

## Visão geral

O middleware conecta dispositivos físicos, gateway IoT, modelos DTDL e camadas de persistência relacional, grafo e séries temporais. A arquitetura segue quatro blocos principais:

- `Orchestrator`: coordena os modelos de gêmeos digitais e a integração com o gateway IoT.
- `Facade`: abstrai a comunicação com ThingsBoard ou outro gateway compatível.
- `Core`: concentra a lógica central, autenticação, configurações e cadastros.
- `Utils`: reúne utilitários compartilhados.

## Codespaces, CSRF e ALLOWED_HOSTS (atualizações)

Pequenas mudanças foram feitas para facilitar execução em ambientes GitHub Codespaces e similares:

- Cada serviço (`middleware`, `client`, `iot_simulator`) agora deriva entradas seguras para `CSRF_TRUSTED_ORIGINS` a partir das variáveis de ambiente `CODESPACE_NAME` e `GITHUB_CODESPACES_PORT_FORWARDING_DOMAIN` quando presentes. O formato gerado é:

  `https://<CODESPACE_NAME>-<PORT>.<GITHUB_CODESPACES_PORT_FORWARDING_DOMAIN>`

  - `MIDDLEWARE_PORT` (padrão `8000`) é usado para o `middleware`.
  - `CLIENT_PORT` (padrão `8002`) é usado para o `client`.
  - `SIMULATOR_PORT` (padrão `8001`) é usado para o `iot_simulator`.

- Cada serviço também inclui `localhost`, `127.0.0.1` e, quando definido, `HOST_IP` em `ALLOWED_HOSTS` e em `CSRF_TRUSTED_ORIGINS` (com e sem porta).

- Para ativar no Codespace, adicione ao arquivo `.env` (ou exporte no ambiente):

```env
CODESPACE_NAME=your-codespace-name
GITHUB_CODESPACES_PORT_FORWARDING_DOMAIN=app.github.dev
MIDDLEWARE_PORT=8000
CLIENT_PORT=8002
SIMULATOR_PORT=8001
```

- Reinicie os serviços afetados após alterar o `.env` (não é necessário rebuild):

```bash
docker compose up -d --no-deps --force-recreate middleware client simulator
```

Nota: o arquivo `.env.example` já contém as chaves `CODESPACE_NAME` e
`GITHUB_CODESPACES_PORT_FORWARDING_DOMAIN` (vazias por padrão). Você pode
copiá-lo para `.env` e preencher essas variáveis localmente ou adicionar as
mesmas nas configurações do Codespace. Após alterar `.env`, reinicie os
serviços como mostrado acima.

- Se houver erros de CSRF após a troca de host, limpe os cookies do navegador para esses domínios e tente novamente.

Essas mudanças são pontuais — podemos depois mover para um padrão por-service `.env` e documentação mais completa.


