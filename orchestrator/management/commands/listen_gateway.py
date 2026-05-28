import asyncio
import json
import requests
import time
import logging
from collections import defaultdict
from asgiref.sync import sync_to_async
import websockets
from django.conf import settings
from django.core.management.base import BaseCommand
from facade.models import Property
from facade.utils import format_influx_line
from orchestrator.models import DigitalTwinInstance, DigitalTwinInstanceProperty
from core.api import get_gateway_auth_headers
from datetime import datetime
from urllib.parse import urlparse

# Configuração básica de logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

THINGSBOARD_WS_URL_TEMPLATE = "ws://{thingsboard_server}/api/ws/plugins/telemetry?token={your_jwt_token}"
INFLUXDB_URL = f"http://{settings.INFLUXDB_HOST}:{settings.INFLUXDB_PORT}/api/v2/write?org={settings.INFLUXDB_ORGANIZATION}&bucket={settings.INFLUXDB_BUCKET}&precision=ms"
INFLUXDB_TOKEN = settings.INFLUXDB_TOKEN
USE_INFLUX_TO_EVALUATE = settings.USE_INFLUX_TO_EVALUATE
INFLUX_WRITE_TIMEOUT = float(getattr(settings, 'INFLUX_WRITE_TIMEOUT', 0.2))

headers = {
    "Authorization": f"Token {INFLUXDB_TOKEN}",
    "Content-Type": "text/plain"
}

class Command(BaseCommand):
    help = 'Starts WebSocket client to listen for ThingsBoard updates for all devices'

    def __init__(self):
        super().__init__()
        self.active_tasks = {}  # Armazena tarefas ativas por device_id
        self.use_influxdb = bool(USE_INFLUX_TO_EVALUATE)
        # Failure counters and last-log timestamps to throttle noisy errors
        self.failure_counts = defaultdict(int)
        self.last_log_at = defaultdict(lambda: 0.0)
        # Token cache and per-gateway HTTP sessions to reduce auth/connection overhead
        self.token_cache = {}  # gateway_id -> {'token': str, 'expires_at': epoch_seconds}
        self.sessions = {}  # gateway_id -> requests.Session()
        # Concurrency semaphore will be set in handle() from CLI options
        self.sem = None

    async def get_jwt_token(self, device):
        # Unified auth: use get_gateway_auth_headers to support api_key or username/password
        gateway = device.gateway
        gw_id = getattr(gateway, 'id', None)
        # Return cached headers if still valid
        if gw_id is not None:
            cached = self.token_cache.get(gw_id)
            if cached and cached.get('headers') and cached.get('expires_at', 0) > time.time():
                return cached['headers']

        try:
            auth_response, status = await sync_to_async(get_gateway_auth_headers)(None, gw_id)
            if status != 200:
                # log and backoff
                raise Exception(f"Auth failure: {auth_response}")

            headers = auth_response.get('headers')
            token = auth_response.get('token')
            token_type = auth_response.get('token_type') or ( 'api_key' if headers and headers.get('X-Authorization','').lower().startswith('apikey') else None)

            # Cache headers/token for bearer tokens (approx 23 hours) and for api_key indefinite
            expires_at = time.time() + (23 * 3600) if token_type == 'bearer' else time.time() + (24 * 3600 * 365)
            if gw_id is not None:
                self.token_cache[gw_id] = {'headers': headers, 'token': token, 'token_type': token_type, 'expires_at': expires_at}

            # reset failure counter on success
            self.failure_counts[device.id] = 0
            return headers
        except Exception as e:
            # exponential backoff: cap at 60s
            self.failure_counts[device.id] += 1
            retries = self.failure_counts[device.id]
            delay = min(60, 2 ** min(retries, 6))
            now = time.time()
            if now - self.last_log_at[device.id] > 60:
                logger.warning(f"Failed to obtain auth headers for device {getattr(device, 'name', device.id)}: {str(e)}; will retry in {delay}s")
                self.last_log_at[device.id] = now
            await asyncio.sleep(delay)
            return None

    async def get_ws_connection_params(self, device):
        """Return (ws_url, extra_headers, auth_cmd_token).

        For ThingsBoard telemetry websocket we must authenticate using a JWT in
        the query string (?token=<jwt>). API key auth alone doesn't work for
        this endpoint, so when gateway auth is api_key we attempt a fallback
        login with username/password if available.
        """
        # Obtain auth headers (which may include token) first; if we couldn't get them, return (None, None)
        headers = await self.get_jwt_token(device)
        if not headers:
            return None, None, None

        gateway = device.gateway
        parsed = urlparse(gateway.url)
        netloc = parsed.netloc or parsed.path
        scheme = 'wss' if parsed.scheme == 'https' else 'ws'
        base_ws = f"{scheme}://{netloc}/api/ws/plugins/telemetry"

        # Prefer the actual auth mode returned by get_gateway_auth_headers.
        # This avoids relying only on gateway.auth_method when the DB value is stale.
        token_type = None
        gw_id = getattr(gateway, 'id', None)
        if gw_id is not None:
            cached = self.token_cache.get(gw_id) or {}
            token_type = cached.get('token_type')

        # API key auth isn't sufficient for /api/ws/plugins/telemetry in TB.
        # Try username/password fallback to obtain a JWT for websocket usage.
        if token_type == 'api_key' or getattr(gateway, 'auth_method', None) == getattr(gateway, 'AUTH_METHOD_API_KEY', 'api_key'):
            username = (getattr(gateway, 'username', None) or '').strip()
            password = (getattr(gateway, 'password', None) or '').strip()
            if username and password:
                try:
                    login_url = f"{gateway.url.rstrip('/')}/api/auth/login"
                    login_headers = {"Content-Type": "application/json"}
                    payload = {"username": username, "password": password}
                    response = await sync_to_async(requests.post)(
                        login_url,
                        headers=login_headers,
                        data=json.dumps(payload),
                        timeout=5,
                    )
                    if response.status_code == 200:
                        token = response.json().get('token')
                        if token:
                            bearer_headers = {
                                "Content-Type": "application/json",
                                "X-Authorization": f"Bearer {token}",
                            }
                            if gw_id is not None:
                                self.token_cache[gw_id] = {
                                    'headers': bearer_headers,
                                    'token': token,
                                    'token_type': 'bearer',
                                    'expires_at': time.time() + (23 * 3600),
                                }
                            return f"{base_ws}?token={token}", None, None
                    else:
                        logger.warning(
                            f"JWT fallback login failed for gateway {getattr(gateway, 'name', gateway)} "
                            f"({getattr(gateway, 'url', '')}): status={response.status_code}, body={response.text[:200]}"
                        )
                except Exception:
                    logger.exception(
                        f"JWT fallback login exception for gateway {getattr(gateway, 'name', gateway)} "
                        f"({getattr(gateway, 'url', '')})"
                    )

            now = time.time()
            if now - self.last_log_at[device.id] > 60:
                logger.warning(
                    f"Gateway {getattr(gateway, 'name', gateway)} is configured with API key, "
                    "but ThingsBoard telemetry websocket requires JWT token in query. "
                    "Configure gateway with username/password (or fill username/password for fallback)."
                )
                self.last_log_at[device.id] = now
            return None, None, None

        # Otherwise try to extract a bearer token to place in the query param
        token = None
        if gw_id is not None:
            cached = self.token_cache.get(gw_id) or {}
            if cached.get('token'):
                token = cached.get('token')
        if not token:
            xauth = headers.get('X-Authorization')
            if xauth and ' ' in xauth:
                token = xauth.split(' ', 1)[1]

        if not token:
            logger.warning(f"No token available for websocket for gateway {getattr(gateway, 'id', None)}")
            return None, None, None

        return f"{base_ws}?token={token}", None, None

    def add_arguments(self, parser):
        parser.add_argument(
            '--concurrency',
            type=int,
            help='Maximum concurrent HTTP requests when checking device status (defaults to unlimited)'
        )
        parser.add_argument(
            '--use-influxdb',
            action='store_true',
            default=None,
            help='Enable writing to InfluxDB (overrides default). If omitted, the setting USE_INFLUX_TO_EVALUATE is used.'
        )
        parser.add_argument(
            '--interval',
            type=int,
            default=5,
            help='Polling interval in seconds to refresh device list and tasks (default: 5)'
        )

    async def listen(self):
        while True:
            dtinstanceproperties = await sync_to_async(list)(DigitalTwinInstanceProperty.objects.filter(
                device_property__isnull=False
            ).select_related('device_property__device__gateway'))
            
            # Iniciar ou atualizar tasks para novos dispositivos
            for dtinstanceproperty in dtinstanceproperties:
                device = dtinstanceproperty.device_property.device
                device_id = device.id
                if device_id not in self.active_tasks:
                    # Create a per-device task that will obtain/refresh JWTs as needed
                    self.active_tasks[device_id] = asyncio.create_task(
                        self.listen_to_device(device)
                    )
            
            # Remover tasks de dispositivos que não existem mais no banco
            active_device_ids = {d.device_property.device.id for d in dtinstanceproperties}
            for device_id in list(self.active_tasks.keys()):
                if device_id not in active_device_ids:
                    self.active_tasks[device_id].cancel()
                    del self.active_tasks[device_id]
                    logger.info(f"Stopped listening for device {device_id}")

            # Sleep using configured interval (default 5s for higher responsiveness)
            sleep_interval = getattr(self, 'poll_interval', 5)
            await asyncio.sleep(sleep_interval)

    async def listen_to_device(self, device):
        while True:
            try:
                # Obtain a fresh WS URL and optional extra headers before each connection attempt
                ws_url, extra_headers, _auth_cmd_token = await self.get_ws_connection_params(device)
                if not ws_url:
                    # Failed to get auth (backoff already applied inside get_jwt_token)
                    await asyncio.sleep(5)
                    continue

                if extra_headers:
                    try:
                        header_keys = list(extra_headers.keys()) if isinstance(extra_headers, dict) else []
                    except Exception:
                        header_keys = []
                    logger.info(f"WS connect to {ws_url} for device {device.name} using extra_headers keys={header_keys}")
                else:
                    logger.info(f"WS connect to {ws_url} for device {device.name} using token in query")
                connect_kwargs = {'timeout': 10}
                if extra_headers:
                    connect_kwargs['extra_headers'] = extra_headers
                async with websockets.connect(ws_url, **connect_kwargs) as websocket:
                    logger.info(f"Connected to ThingsBoard WebSocket for device {device.name}")
                    
                    # Subscribe to updates for the device
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
                        "attrSubCmds": [
                            {
                                "entityType": "DEVICE",
                                "entityId": device.identifier,
                                "scope": "CLIENT_SCOPE",
                                "cmdId": 2
                            },
                            {
                                "entityType": "DEVICE",
                                "entityId": device.identifier,
                                "scope": "SHARED_SCOPE",
                                "cmdId": 3
                            },
                            {
                                "entityType": "DEVICE",
                                "entityId": device.identifier,
                                "scope": "SERVER_SCOPE",
                                "cmdId": 4
                            }
                        ]
                    }

                    await websocket.send(json.dumps(subscribe_message))

                    async for message in websocket:
                        data = json.loads(message)
                        await self.process_message(device, data)

            except (websockets.exceptions.ConnectionClosed, asyncio.TimeoutError, OSError) as e:
                # Throttle repeated connection error logs per device
                now = time.time()
                self.failure_counts[device.id] += 1
                retries = self.failure_counts[device.id]
                delay = min(60, 2 ** min(retries, 6))
                # If server returned a websocket close with code 1011 and message indicating
                # an invalid JWT, try to refresh token on the next loop iteration rather
                # than reconnecting immediately with the same (invalid) token.
                msg = str(e)
                if isinstance(e, websockets.exceptions.ConnectionClosed) and getattr(e, 'code', None) == 1011 and 'Invalid JWT' in msg:
                    # Invalidate bearer cache so next attempt forces a fresh login.
                    gw_id = getattr(getattr(device, 'gateway', None), 'id', None)
                    if gw_id is not None:
                        cached = self.token_cache.get(gw_id) or {}
                        if cached.get('token_type') == 'bearer':
                            self.token_cache.pop(gw_id, None)
                    logger.warning(f"Invalid JWT for device {device.name}; will refresh token and retry in {delay}s")
                else:
                    if now - self.last_log_at[device.id] > 60:
                        logger.warning(f"Connection error for device {device.name}: {str(e)}; reconnecting in {delay}s")
                        self.last_log_at[device.id] = now

                await asyncio.sleep(delay)
                continue

    async def check_device_status(self, device):
        """Verifica o status do dispositivo no ThingsBoard"""
        try:
            # Use unified auth headers from core.get_gateway_auth_headers
            gw_id = getattr(device.gateway, 'id', None)
            auth_response = None
            if gw_id is not None:
                cached = self.token_cache.get(gw_id)
                if cached and cached.get('headers') and cached.get('expires_at', 0) > time.time():
                    auth_response = cached.get('headers')
            if not auth_response:
                auth_response = await self.get_jwt_token(device)
            if not auth_response:
                return False
            url = f"{device.gateway.url.rstrip('/')}/api/plugins/telemetry/DEVICE/{device.identifier}/values/attributes"
            headers = auth_response
            # Use session per gateway to reuse connections
            gw_id = getattr(device.gateway, 'id', None)
            if gw_id not in self.sessions:
                self.sessions[gw_id] = requests.Session()
            session = self.sessions[gw_id]
            timeout_seconds = 3
            # If a semaphore was configured, acquire it to limit concurrency
            if self.sem is not None:
                async with self.sem:
                    response = await sync_to_async(session.get)(url, headers=headers, timeout=timeout_seconds)
            else:
                response = await sync_to_async(session.get)(url, headers=headers, timeout=timeout_seconds)
            if response.status_code == 200:
                attributes = response.json()
                # Verifica o atributo de status do dispositivo
                for attr in attributes:
                    if attr.get('key') == 'active':
                        return attr.get('value', False)
            return False
        except Exception as e:
            # Avoid noisy exception trace for transient errors; log once per minute
            now = time.time()
            if now - self.last_log_at[getattr(device, 'id', 'global')] > 60:
                logger.warning(f"Error checking device status for {getattr(device, 'name', device)}: {e}")
                self.last_log_at[getattr(device, 'id', 'global')] = now
            return False

    async def update_dt_instance_status(self, device, is_active):
        """Atualiza o status do DigitalTwinInstance e registra mudanças no InfluxDB"""
        dt_instances = await sync_to_async(list)(
            DigitalTwinInstance.objects.filter(
                digitaltwininstanceproperty__device_property__device=device
            ).distinct()
        )
        
        for dt_instance in dt_instances:
            current_state = await sync_to_async(lambda: dt_instance.active)()
            if current_state != is_active:
                current_timestamp = int(time.time() * 1000)  # Horário atual em milissegundos
                
                # Obtém o timestamp da última verificação
                last_check_timestamp = await sync_to_async(lambda: int(dt_instance.last_status_check.timestamp() * 1000))()
                
                # Calcula a duração da inatividade
                inactivity_duration = 0
                if not is_active:
                    # Se está ficando inativo agora, marca o início da inatividade
                    inactivity_duration = 0  # Não há inatividade ainda
                else:
                    # Se está voltando a ficar ativo, calcula o tempo que ficou inativo
                    inactivity_duration = current_timestamp - last_check_timestamp
                
                # Atualiza o estado no Django
                dt_instance.active = is_active
                dt_instance.last_status_check = datetime.fromtimestamp(current_timestamp / 1000)  # Salva em segundos
                await sync_to_async(dt_instance.save)()
                logger.info(f"Updated DT Instance {dt_instance.id} status to {'active' if is_active else 'inactive'}")
                
                # Envia os dados para o InfluxDB
                if self.use_influxdb and inactivity_duration > 0:
                    logger.info(f"Writing availability event to InfluxDB for device {device.identifier}")
                    try:
                        device_type_name = await sync_to_async(lambda: device.type.name if device.type else 'unknown')()
                        
                        tags = [
                            f"device={device.identifier}",
                            f"dt_instance={dt_instance.id}",
                            f"device_type={device_type_name}"
                        ]
                        
                        fields = [
                            f"active={1 if is_active else 0}i",
                            f"inactivity_duration={inactivity_duration}i"
                        ]
                        
                        measurement = f"device_availability,{','.join(tags)} {','.join(fields)} {current_timestamp}"
                        
                        response = await sync_to_async(requests.post)(
                            INFLUXDB_URL, 
                            headers=headers, 
                            data=measurement
                        )
                        
                        if response.status_code != 204:
                            logger.error(f"Failed to write availability event: {response.text}")
                            logger.error(f"Attempted measurement: {measurement}")
                        else:
                            logger.info(f"Device {device.identifier} {'activated' if is_active else 'deactivated'} " + 
                                        f"after {inactivity_duration / 1000:.2f} seconds of {'inactivity' if is_active else 'activity'}")
                        
                    except Exception as e:
                        logger.exception(f"Error writing availability to InfluxDB: {str(e)}")

    async def process_message(self, device, data):
        """Processa mensagens recebidas do ThingsBoard"""
        logger.info(f"Processing message for device {device.name}")
        
        # OPTIMIZATION: Skip device status check - it was blocking 91.5% of messages
        # Devices were being marked inactive because they lack 'active' attribute in TB
        # Simply process all received telemetry
        
        latest_values = data.get('data')
        if latest_values:
            for key, value in latest_values.items():
                try:
                    hora, valor = value[0]
                    # Atualiza a propriedade do dispositivo
                    await sync_to_async(Property.objects.filter(device=device, name=key).update)(value=valor)
                    
                    # Atualiza o DigitalTwinInstanceProperty apenas se o dispositivo estiver ativo
                    await sync_to_async(DigitalTwinInstanceProperty.objects.filter(
                        device_property__device=device,
                        property__name=key,
                        dtinstance__active=True
                    ).update)(value=valor)
                    
                    if self.use_influxdb and INFLUXDB_TOKEN:
                        timestamp = int(time.time() * 1000)
                        property = await sync_to_async(lambda: Property.objects.filter(device=device, name=key).first())()
                        # Do not append _i to the key. Force integer types for Boolean/Integer properties
                        if property:
                            try:
                                ptype = property.type
                            except Exception:
                                ptype = None
                            if ptype in ('Boolean', 'Integer'):
                                # ensure Python int so format_influx_line will render as integer (with i suffix)
                                try:
                                    property_value = int(property.get_value())
                                except Exception:
                                    # fallback: coerce from the raw telemetry value
                                    try:
                                        property_value = int(valor)
                                    except Exception:
                                        property_value = 0
                            elif ptype == 'Double':
                                try:
                                    property_value = float(property.get_value())
                                except Exception:
                                    property_value = property.get_value()
                            else:
                                property_value = property.get_value()
                        else:
                            # no local Property found — use the raw telemetry value
                            property_value = valor
                        
                        # Envia apenas o received_timestamp para o InfluxDB using safe formatter
                        # Prefer ThingsBoard internal id (thingsboard_id) when available to avoid
                        # conflicts with friendly names. Fall back to device.identifier.
                        sensor_id = device.identifier
                        tags = {"sensor": sensor_id, "source": "middts", "direction": "S2M"}
                        # Ensure numeric types for booleans/ints; send explicit integer suffix for status-like fields
                        if key.lower() in ('status', 'active'):
                            normalized = str(property_value).strip().lower() if property_value is not None else ''
                            if normalized in ('1', '1.0', 'true', 'yes', 'on'):
                                pv = 1.0
                            elif normalized in ('0', '0.0', 'false', 'no', 'off', ''):
                                pv = 0.0
                            else:
                                try:
                                    pv = 1.0 if float(property_value) != 0.0 else 0.0
                                except Exception:
                                    pv = 0.0
                            fields = {key: pv, "received_timestamp": timestamp}
                        else:
                            fields = {key: property_value, "received_timestamp": timestamp}
                        data = format_influx_line("device_data", tags, fields, timestamp=timestamp)
                        logger.debug(f"Posting to InfluxDB (middts listener): {data}")
                        response = await asyncio.to_thread(
                            requests.post,
                            INFLUXDB_URL,
                            headers=headers,
                            data=data,
                            timeout=INFLUX_WRITE_TIMEOUT,
                        )
                        logger.debug(f"Response Code: {response.status_code}, Response Text: {response.text} - Data Sent: {data}")
                        logger.info(f"Updated property for {device.name} - {key}: {valor} and sent to InfluxDB with received_timestamp")

                except Exception as e:
                    logger.exception(f"Error processing property {key} for device {device.name}: {e}")

    def handle(self, *args, **options):
        # CLI options: allow override of influx usage and concurrency
        concurrency = options.get('concurrency', None)
        # Determine whether to write to InfluxDB.
        # CLI flag (--use-influxdb) is explicit True when provided. We set its default to None so
        # we can differentiate "flag not provided" from "flag provided as false".
        cli_flag = options.get('use_influxdb', None)
        if cli_flag is None:
            # Respect the settings value. Settings may come from environment and could be a string.
            env_val = USE_INFLUX_TO_EVALUATE
            if isinstance(env_val, str):
                self.use_influxdb = env_val.strip().lower() in ('1', 'true', 'yes', 'y')
            else:
                self.use_influxdb = bool(env_val)
        else:
            # CLI flag explicitly provided => use it (True). Note: action='store_true' only sets True when passed.
            self.use_influxdb = bool(cli_flag)
        # polling interval in seconds for refreshing device list
        self.poll_interval = options.get('interval', 5)
        if concurrency:
            try:
                concurrency_val = int(concurrency)
                self.sem = asyncio.Semaphore(concurrency_val)
            except Exception:
                self.sem = None

        loop = asyncio.get_event_loop()
        try:
            logger.info("Starting WebSocket listener...")
            logger.info(f"Influx writes enabled: {self.use_influxdb}")
            loop.run_until_complete(self.listen())
        except KeyboardInterrupt:
            logger.info("Stopping WebSocket listener...")
            for task in self.active_tasks.values():
                task.cancel()
