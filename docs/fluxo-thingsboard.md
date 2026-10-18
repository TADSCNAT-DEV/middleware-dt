# Fluxo de Uso com ThingsBoard

Este guia descreve o cenário funcional do middleware com um gateway IoT, como o ThingsBoard.

[Voltar para a documentação principal](../README.md#fluxo-thingsboard)

Antes de começar, valide o [acesso ao MiddTS e ao simulador](demo-make.md#login-e-criação-do-administrador-do-simulador). A conta do ThingsBoard é independente dos administradores Django. Configure as credenciais da instância no `GatewayIOT` do middleware e, para o simulador, nas variáveis `THINGSBOARD_*` descritas no [guia de configuração](demo-make.md#credenciais-do-thingsboard).

## 1. Cadastrar dispositivos no gateway IoT

- No ThingsBoard local, em nuvem ou em `demo.thingsboard.io`, cadastre os dispositivos físicos.
- Garanta que o gateway esteja conectado ao dispositivo para permitir RPC e monitoramento.

## 2. Conectar o gateway ao middleware

- Acesse `http://{endereco_middleware}/admin` e cadastre o gateway IoT.
- Pela API do middleware, importe os dispositivos do gateway:

  - Endpoint: `/gatewaysiot/{gateway_id}/discover-devices`

O middleware cadastra localmente os dispositivos encontrados no gateway.

## 3. Associar o dispositivo ao modelo DTDL

- Importe o modelo DTDL para o middleware:

  - Endpoint: `/import-dtdl/`

- Envie um JSON do modelo DTDL para criar a representação interna.
- Crie uma instância desse modelo e relacione-a ao dispositivo físico já cadastrado no middleware.

## 4. Comandos úteis

- Executar um parser DTDL auxiliar em container:

```bash
docker run -p 8082:8080 andregustavoo/parserwebapi:latest
```

- Ouvir eventos do gateway:

```bash
python manage.py listen_gateway
```

## 5. Leituras relacionadas

- [Execução com Docker e Make](demo-make.md)
- [Instalação manual](instalacao-manual.md)
