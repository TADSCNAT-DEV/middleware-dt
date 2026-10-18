import asyncio
import json
import requests
import time
import logging
from asgiref.sync import sync_to_async
import websockets
from django.conf import settings
from django.core.management.base import BaseCommand
from facade.models import Device, Property, write_inactivity_event, InactivityType
from orchestrator.models import DigitalTwinInstance, DigitalTwinInstanceProperty
from facade.api import get_jwt_token_gateway
from datetime import datetime, timedelta

# Configuração básica de logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

THINGSBOARD_WS_URL_TEMPLATE = "ws://{thingsboard_server}/api/ws/plugins/telemetry?token={your_jwt_token}"
INFLUXDB_URL = f"http://{settings.INFLUXDB_HOST}:{settings.INFLUXDB_PORT}/api/v2/write?org={settings.INFLUXDB_ORGANIZATION}&bucket={settings.INFLUXDB_BUCKET}&precision=ms"
INFLUXDB_TOKEN = settings.INFLUXDB_TOKEN
USE_INFLUX_TO_EVALUATE = settings.USE_INFLUX_TO_EVALUATE

headers = {
    "Authorization": f"Token {INFLUXDB_TOKEN}",
    "Content-Type": "text/plain"
}

class JWTTokenManager:
    def __init__(self):
        self.tokens = {}  # {gateway_id: {'token': token, 'expires_at': datetime}}
        self.token_lifetime = timedelta(hours=23)  # Define um tempo de vida menor que 24h para ter margem de segurança

    def get_token(self, gateway_id):
        if gateway_id in self.tokens:
            token_data = self.tokens[gateway_id]
            if datetime.now() < token_data['expires_at']:
                return token_data['token']
        return None

    def set_token(self, gateway_id, token):
        self.tokens[gateway_id] = {
            'token': token,
            'expires_at': datetime.now() + self.token_lifetime
        }

class Command(BaseCommand):
    help = 'Unified command to check device status and listen for ThingsBoard updates'

    def __init__(self):
        super().__init__()
        self.token_manager = JWTTokenManager()
        self.active_tasks = {}  # Armazena tarefas ativas por device_id

    def add_arguments(self, parser):
        parser.add_argument(
            '--interval',
            type=int,
            default=10,
            help='Interval in seconds between status checks (default: 10)'
        )

    def handle(self, *args, **options):
        interval = options['interval']
        self.use_influxdb = USE_INFLUX_TO_EVALUATE  # Inicializa a variável com o valor da configuração
        loop = asyncio.get_event_loop()
        try:
            logger.info("Starting unified device monitor...")
            loop.run_until_complete(self.run_monitor(interval))
        except KeyboardInterrupt:
            logger.info("Stopping unified device monitor...")
            for task in self.active_tasks.values():
                task.cancel()

    async def get_jwt_token(self, device):
        """Obtém o token JWT para o gateway do dispositivo"""
        gateway_id = device.gateway_id
        cached_token = self.token_manager.get_token(gateway_id)
        if cached_token:
            # logger.info(f"Using cached JWT token for gateway {gateway_id}")
            return cached_token

        # logger.info(f"Requesting new JWT token for gateway {gateway_id}")
        response, status_code = await sync_to_async(get_jwt_token_gateway)(None, gateway_id)
        if status_code == 200:
            token = response['token']
            self.token_manager.set_token(gateway_id, token)
            return token
        logger.error(f"Failed to get JWT token for gateway {gateway_id}, status code: {status_code}")
        return None

    async def get_ws_url(self, device):
        """Constrói a URL do WebSocket para o dispositivo"""
        jwt_token = await self.get_jwt_token(device)
        if not jwt_token:
            raise ValueError(f"Failed to get JWT token for device {device.name}")
        return THINGSBOARD_WS_URL_TEMPLATE.format(
            thingsboard_server=device.gateway.url,
            your_jwt_token=jwt_token
        )

    async def process_message(self, device, data):
        """Processa mensagens recebidas do ThingsBoard"""
        logger.info(f"Processing message for device {device.name}")
        # Aqui você pode implementar o processamento da mensagem recebida
        pass

    async def run_monitor(self, interval):
        """Executa o monitoramento unificado"""
        await asyncio.gather(
            self.check_devices_status(interval),
            self.listen_for_updates()
        )

    async def check_devices_status(self, interval):
        """Verifica periodicamente o status dos dispositivos no ThingsBoard"""
        logger.info(f"Starting periodic device status checks every {interval} seconds")
        while True:
            try:
                devices = await sync_to_async(list)(Device.objects.all().select_related('gateway'))
                logger.info(f"Found {len(devices)} devices to check")
                tasks = [self.check_device_status(device) for device in devices]
                await asyncio.gather(*tasks)
                await asyncio.sleep(interval)
            except Exception as e:
                logger.exception(f"Error in check_devices_status: {e}")
                await asyncio.sleep(interval)

    async def check_device_status(self, device):
        """Verifica o status de um dispositivo no ThingsBoard"""
        logger.info(f"Checking status for device {device.name} (ID: {device.id})")
        try:
            jwt_token = await self.get_jwt_token(device)
            if not jwt_token:
                logger.warning(f"Failed to get JWT token for device {device.name}")
                await sync_to_async(write_inactivity_event)(
                    device, 
                    InactivityType.CONNECTION_ERROR, 
                    "Failed to get JWT token"
                )
                return False

            url = f"{device.gateway.url.rstrip('/')}/api/plugins/telemetry/DEVICE/{device.identifier}/values/attributes"
            headers = {
                "Content-Type": "application/json",
                "X-Authorization": f"Bearer {jwt_token}"
            }
            
            response = await sync_to_async(requests.get)(url, headers=headers)
            if response.status_code == 200:
                attributes = response.json()
                is_active = any(attr.get('key') == 'active' and attr.get('value', False) for attr in attributes)
                logger.info(f"Device {device.name} is {'active' if is_active else 'inactive'}")
                await self.update_dt_instance_status(device, is_active)
                return is_active
            else:
                logger.error(f"Failed to check status for device {device.name}, HTTP {response.status_code}: {response.text}")
                return False
        except Exception as e:
            logger.exception(f"Error while checking status for device {device.name}: {e}")
            return False

    async def listen_for_updates(self):
        """Escuta atualizações de telemetria do ThingsBoard"""
        while True:
            dtinstanceproperties = await sync_to_async(list)(DigitalTwinInstanceProperty.objects.filter(
                device_property__isnull=False
            ).select_related('device_property__device__gateway'))
            
            for dtinstanceproperty in dtinstanceproperties:
                device_id = dtinstanceproperty.device_property.device.id
                if device_id not in self.active_tasks:
                    ws_url = await self.get_ws_url(dtinstanceproperty.device_property.device)
                    self.active_tasks[device_id] = asyncio.create_task(
                        self.listen_to_device(ws_url, dtinstanceproperty.device_property.device)
                    )
            
            active_device_ids = {d.device_property.device.id for d in dtinstanceproperties}
            for device_id in list(self.active_tasks.keys()):
                if device_id not in active_device_ids:
                    self.active_tasks[device_id].cancel()
                    del self.active_tasks[device_id]
                    logger.info(f"Stopped listening for device {device_id}")

            await asyncio.sleep(30)

    async def listen_to_device(self, ws_url, device):
        """Escuta atualizações de um dispositivo específico"""
        while True:
            try:
                async with websockets.connect(ws_url, timeout=600) as websocket:
                    logger.info(f"Connected to ThingsBoard WebSocket for device {device.name}")
                    subscribe_message = {
                        "tsSubCmds": [
                            {
                                "entityType": "DEVICE",
                                "entityId": device.identifier,
                                "scope": "LATEST_TELEMETRY",
                                "cmdId": 1
                            }
                        ],
                        "historyCmds": [],
                        "attrSubCmds": []
                    }
                    await websocket.send(json.dumps(subscribe_message))
                    async for message in websocket:
                        data = json.loads(message)
                        await self.process_message(device, data)
            except (websockets.exceptions.ConnectionClosed, asyncio.TimeoutError) as e:
                logger.error(f"Connection error for device {device.name}: {e.message}")
                await asyncio.sleep(10)

    async def update_dt_instance_status(self, device, is_active):
        """Atualiza o status do DigitalTwinInstance e registra mudanças no InfluxDB"""
        dt_instances = await sync_to_async(list)(
            DigitalTwinInstance.objects.filter(
                digitaltwininstanceproperty__device_property__device=device
            ).distinct()
        )
        for dt_instance in dt_instances:
            current_state = await sync_to_async(lambda: dt_instance.active)()
            current_timestamp = int(time.time() * 1000)  # Horário atual em milissegundos

            if current_state != is_active:
                # Obtém o timestamp da última verificação
                # last_check_timestamp = await sync_to_async(lambda: int(dt_instance.last_status_check.timestamp() * 1000))()

                # # Calcula a duração com base no estado anterior
                # if is_active:
                #     # Se o dispositivo está sendo reativado, calcula o tempo de inatividade
                #     inactivity_duration = current_timestamp - last_check_timestamp
                #     logger.info(f"Device {device.identifier} reactivated after {inactivity_duration / 1000:.2f} seconds of inactivity")
                #     if self.use_influxdb:
                #         await self.write_influx_event(
                #             device=device,
                #             dt_instance=dt_instance,
                #             duration=inactivity_duration,
                #             event_type="inactivity_duration"
                #         )
                # else:
                #     # Se o dispositivo está sendo desativado, calcula o tempo de atividade
                #     activity_duration = current_timestamp - last_check_timestamp
                #     logger.info(f"Device {device.identifier} deactivated after {activity_duration / 1000:.2f} seconds of activity")
                #     if self.use_influxdb:
                #         await self.write_influx_event(
                #             device=device,
                #             dt_instance=dt_instance,
                #             duration=activity_duration,
                #             event_type="activity_duration"
                #         )
                if self.use_influxdb:
                    await self.write_influx_event_status(
                        device=device,
                        dt_instance=dt_instance,
                        status=is_active
                    )
                # Atualiza o estado no Django
                dt_instance.active = is_active
                dt_instance.last_status_check = datetime.fromtimestamp(current_timestamp / 1000)  # Salva em segundos
                await sync_to_async(dt_instance.save)()
                logger.info(f"Updated DT Instance {dt_instance.id} status to {'active' if is_active else 'inactive'}")

    async def write_influx_event(self, device, dt_instance, duration, event_type):
        """Escreve um evento no InfluxDB"""
        try:
            # Obter o nome do tipo de dispositivo de forma assíncrona
            device_type_name = await sync_to_async(lambda: device.type.name if device.type else 'unknown')()

            tags = [
                f"device={device.identifier}",
                f"dt_instance={dt_instance.id}",
                f"device_type={device_type_name}"
            ]
            fields = [
                f"{event_type}={duration}i"
            ]
            current_timestamp = int(time.time() * 1000)
            measurement = f"device_availability,{','.join(tags)} {','.join(fields)} {current_timestamp}"
            response = await sync_to_async(requests.post)(INFLUXDB_URL, headers=headers, data=measurement)
            if response.status_code != 204:
                logger.error(f"Failed to write {event_type} event: {response.text}")
        except Exception as e:
            logger.exception(f"Error writing {event_type} to InfluxDB: {str(e)}")
    
    async def write_influx_event_status(self, device, dt_instance, status):
        """Escreve um evento no InfluxDB"""
        try:
            # Obter o nome do tipo de dispositivo de forma assíncrona

            tags = [
                f"device={device.identifier}",
                f"dt_instance={dt_instance.id}",
            ]
            fields = [
                f"active={1 if status else 0}i",
            ]
            current_timestamp = int(time.time() * 1000)
            measurement = f"device_status,{','.join(tags)} {','.join(fields)} {current_timestamp}"
            response = await sync_to_async(requests.post)(INFLUXDB_URL, headers=headers, data=measurement)
            if response.status_code != 204:
                logger.error(f"Failed to write status of {device} event: {response.text}")
        except Exception as e:
            logger.exception(f"Error writing status of {device} to InfluxDB: {str(e)}")