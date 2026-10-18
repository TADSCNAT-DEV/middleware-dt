# Create your models here.

# gateway/models.py
import time
import os
from decimal import Decimal
import requests
from django.conf import settings
from django.db import models
from django.contrib.auth.models import User
from enum import Enum
from facade.utils import format_influx_line, get_session_for_gateway
import traceback

from core.models import GatewayIOT, Organization
# INFLUX configuration
INFLUXDB_HOST = settings.INFLUXDB_HOST
INFLUXDB_PORT = settings.INFLUXDB_PORT
INFLUXDB_BUCKET = settings.INFLUXDB_BUCKET
INFLUXDB_ORGANIZATION = settings.INFLUXDB_ORGANIZATION
INFLUXDB_URL = f"http://{INFLUXDB_HOST}:{INFLUXDB_PORT}/api/v2/write?org={INFLUXDB_ORGANIZATION}&bucket={INFLUXDB_BUCKET}&precision=ms"
INFLUXDB_TOKEN = settings.INFLUXDB_TOKEN

USE_INFLUX_TO_EVALUATE = settings.USE_INFLUX_TO_EVALUATE

# Session helper moved to facade.utils.get_session_for_gateway


class DeviceType(models.Model):
    name = models.CharField(max_length=100, unique=True)
    organization = models.ForeignKey(Organization, null=True, blank=True, on_delete=models.SET_NULL)
    created_by = models.ForeignKey(User, null=True, blank=True, related_name='created_device_types', on_delete=models.SET_NULL)
    inactivityTimeout = models.IntegerField(
        null=True, 
        blank=True, 
        help_text="Timeout in seconds for device inactivity",
        default=getattr(settings, 'DEFAULT_INACTIVITY_TIMEOUT', 60)
    )

    def save(self, *args, **kwargs):
        if self.inactivityTimeout is None:
            self.inactivityTimeout = getattr(settings, 'DEFAULT_INACTIVITY_TIMEOUT', 60)
        super().save(*args, **kwargs)

    def __str__(self):
        return self.name

class Device(models.Model):
    name = models.CharField(max_length=255)
    identifier = models.CharField(max_length=255)
    status = models.CharField(max_length=255)
    type = models.ForeignKey(DeviceType, on_delete=models.CASCADE, null=True)
    gateway = models.ForeignKey(GatewayIOT, related_name='devices', on_delete=models.CASCADE)
    organization = models.ForeignKey(Organization, null=True, blank=True, on_delete=models.SET_NULL)
    created_by = models.ForeignKey(User, null=True, blank=True, related_name='created_devices', on_delete=models.SET_NULL)
    user = models.ForeignKey(User, related_name='devices', on_delete=models.CASCADE)
    inactivityTimeout = models.IntegerField(null=True, blank=True, help_text="Timeout in seconds for device inactivity. Overrides type's timeout")
    metadata = models.TextField(blank=True, default='')  # Novo campo para armazenar metadados dos labels do ThingsBoard

    def __str__(self):
        return self.name

    def save(self, *args, **kwargs):
        if self.gateway_id and not self.organization_id:
            self.organization = self.gateway.organization
        if self.user_id and not self.created_by_id:
            self.created_by = self.user
        super().save(*args, **kwargs)
    
    class Meta:
        unique_together = ('identifier', 'gateway')

    def get_inactivity_timeout(self):
        """Returns the effective inactivity timeout for this device"""
        if self.inactivityTimeout is not None:
            return self.inactivityTimeout
        if self.type and self.type.inactivityTimeout is not None:
            return self.type.inactivityTimeout
        return None

    def sync_inactivity_timeout(self):
        """Syncs the inactivity timeout with ThingsBoard"""
        timeout = self.get_inactivity_timeout()
        if timeout is None:
            return  # Let ThingsBoard handle default timeout
        
        from core.api import get_gateway_auth_headers
        response, status_code = get_gateway_auth_headers(None, self.gateway.id)

        if status_code == 200:
            url = f"{self.gateway.url.rstrip('/')}/api/plugins/telemetry/DEVICE/{self.identifier}/SERVER_SCOPE"
            headers = response['headers']
            
            # Format payload as a dictionary
            payload = {
                "inactivityTimeout": str(timeout)  # Convert to string as ThingsBoard expects string values
            }
            
            try:
                response = requests.post(url, json=payload, headers=headers)
                if response.status_code != 200:
                    print(f"Failed to sync inactivity timeout for device {self.name}: {response.text}")
                return response.status_code == 200
            except Exception as e:
                print(f"Error syncing inactivity timeout for device {self.name}: {str(e)}")
                return False
        return False

    def sync_properties_from_thingsboard(self):
        """
        Busca atributos compartilhados do ThingsBoard e cria/atualiza Properties locais.
        Os shared attributes sao a fonte preferencial para rpc_read_method/rpc_write_method;
        o fallback por mapeamento de tipo deve ser usado apenas quando esses metadados nao existirem.
        """
        from core.api import get_gateway_auth_headers
        response, status_code = get_gateway_auth_headers(None, self.gateway.id)
        if status_code != 200:
            print(f"Erro ao obter token JWT para gateway {self.gateway}: {response}")
            return

        headers = response['headers']
        url_get = f"{self.gateway.url.rstrip('/')}/api/plugins/telemetry/DEVICE/{self.identifier}/values/attributes/SHARED_SCOPE"

        def normalize_property_entry(prop_name, prop_data):
            if not prop_name or not isinstance(prop_data, dict):
                return None
            return {
                "name": prop_name,
                "type": prop_data.get("type", "Boolean"),
                "rpc_read_method": prop_data.get("rpc_read_method", "") or "",
                "rpc_write_method": prop_data.get("rpc_write_method", "") or "",
            }

        def extract_property_metadata(shared_attrs):
            metadata = {}
            if not isinstance(shared_attrs, list):
                return metadata

            for attr in shared_attrs:
                if not isinstance(attr, dict):
                    continue
                key = attr.get("key")
                value = attr.get("value")

                if key == "properties" and isinstance(value, dict):
                    for prop_name, prop_data in value.items():
                        entry = normalize_property_entry(prop_name, prop_data)
                        if entry:
                            metadata[prop_name] = entry
                    continue

                if isinstance(value, dict) and (
                    "rpc_read_method" in value or "rpc_write_method" in value or "type" in value
                ):
                    entry = normalize_property_entry(key, value)
                    if entry:
                        metadata[key] = entry

            return metadata

        try:
            resp = requests.get(url_get, headers=headers)
            if resp.status_code == 200:
                shared_attrs = resp.json()
                property_metadata = extract_property_metadata(shared_attrs)
                for prop_name, prop_defaults in property_metadata.items():
                    Property.objects.update_or_create(
                        device=self,
                        name=prop_name,
                        defaults={
                            "type": prop_defaults["type"],
                            "rpc_read_method": prop_defaults["rpc_read_method"],
                            "rpc_write_method": prop_defaults["rpc_write_method"],
                        }
                    )
        except Exception as e:
            print(f"Erro ao sincronizar propriedades do ThingsBoard: {str(e)}")

    def sync_metadata_from_thingsboard(self):
        """
        Busca os labels do ThingsBoard e popula o campo metadata.
        """
        from core.api import get_gateway_auth_headers
        response, status_code = get_gateway_auth_headers(None, self.gateway.id)
        if status_code != 200:
            print(f"Erro ao obter token JWT para gateway {self.gateway}: {response}")
            return

        url = f"{self.gateway.url.rstrip('/')}/api/device/{self.identifier}"
        headers = response['headers']
        try:
            resp = requests.get(url, headers=headers)
            if resp.status_code == 200:
                device_data = resp.json()
                # O ThingsBoard armazena labels em 'label' ou 'labels' (verifique conforme sua configuração)
                labels = device_data.get('label') or device_data.get('labels')
                if isinstance(labels, dict):
                    # Serializa dict para string
                    self.metadata = " ".join(f"{k}:{v}" for k, v in labels.items())
                elif isinstance(labels, list):
                    self.metadata = " ".join(str(l) for l in labels)
                elif isinstance(labels, str):
                    self.metadata = labels
                else:
                    self.metadata = ""
                super().save(update_fields=['metadata'])
            else:
                print(f"Erro ao buscar labels do ThingsBoard: {resp.text}")
        except Exception as e:
            print(f"Erro ao sincronizar labels do ThingsBoard: {str(e)}")

    def save(self, *args, **kwargs):
        if self.gateway and not self.organization_id:
            self.organization = self.gateway.organization
        is_new = self.pk is None
        super().save(*args, **kwargs)
        # Se não houver nenhuma propriedade associada, busca do ThingsBoard e cria
        if not Property.objects.filter(device=self).exists():
            self.sync_properties_from_thingsboard()
        # Sempre busca e atualiza metadados dos labels
        self.sync_metadata_from_thingsboard()
        if is_new or self.inactivityTimeout is not None:
            self.sync_inactivity_timeout()

#ENUM para definir os tipos de chamada. Pode trocar por uma estratégia melhor, se for o caso
class RPCCallTypes(Enum):
    READ=1
    WRITE=2

class InactivityType(Enum):
    DEVICE_INACTIVE = "device_inactive"  # Dispositivo marcado como inativo no ThingsBoard
    CONNECTION_ERROR = "connection_error"  # Erro de conexão com ThingsBoard
    TIMEOUT = "timeout"  # Tempo limite excedido sem resposta

class Property(models.Model):
    TYPE_CHOICES = (("Boolean", "Boolean"), ("Integer", "Integer", ),("Double", "Double",))
    device = models.ForeignKey(Device, null=False, on_delete=models.CASCADE)
    name = models.CharField(max_length=255)
    type = models.CharField(choices=TYPE_CHOICES)
    value = models.CharField(max_length=255, blank=True, default='')
    rpc_read_method=models.CharField(max_length=255,blank=True, default='') #Nome do RPC de leitura. Deve retornar um valor compatível com self.value
    rpc_write_method=models.CharField(max_length=255,blank=True, default='') #Nome 
    # rpc_methods = models.JSONField() {"read", "checkStatus", "write": "setStatus"}
    # Eu pensei em colocar uma regra que seria chamado em cima do value da propriedade
    # policies = models.CharField()
    
    class Meta:
        unique_together = ('device', 'name')

    def __str__(self):
        return f'{self.name} - {self.device}'
    
    def write_influx(self, request_id=None, measurement="device_data", extra_fields=None):
        timestamp = int(time.time() * 1000)
        headers = {
            "Authorization": f"Token {INFLUXDB_TOKEN}",
            "Content-Type": "text/plain"
        }
        key = self.name
        valor = self.get_value()
        if self.type == 'Boolean' or self.type == 'Integer':
            try:
                ival = int(valor)
            except Exception:
                if self.type == 'Boolean':
                    ival = 1 if str(valor).lower() in ('true', '1', 'yes') else 0
                else:
                    try:
                        ival = int(float(valor))
                    except Exception:
                        ival = 0
            valor = float(ival)
        sensor_id = self.device.identifier
        # Propagação robusta do request_id
        if request_id is None:
            # Tenta obter do contexto salvo
            if hasattr(self, 'last_payload') and isinstance(self.last_payload, dict):
                request_id = self.last_payload.get('request_id')
            if request_id is None and hasattr(self, 'request_id'):
                request_id = getattr(self, 'request_id')
        tags = {"sensor": sensor_id, "source": "middts", "direction": "S2M" if measurement=="device_data" else "M2S"}

        correlation_id = None
        sent_timestamp = None
        normalized_request_id = request_id
        if isinstance(request_id, (list, tuple)) and len(request_id) >= 2:
            try:
                sent_timestamp = int(request_id[0])
            except Exception:
                sent_timestamp = None
            correlation_id = str(request_id[1])
            normalized_request_id = correlation_id
        elif isinstance(request_id, str) and request_id.startswith('[') and ',' in request_id:
            # Best-effort parsing for stringified legacy format: [timestamp, 'uuid']
            try:
                parts = request_id.strip('[]').split(',', 1)
                sent_timestamp = int(parts[0].strip())
                correlation_id = parts[1].strip().strip("'\"")
                normalized_request_id = correlation_id
            except Exception:
                pass

        if normalized_request_id:
            # Always set tags to maintain CSV column consistency
            tags["request_id"] = str(normalized_request_id)
        else:
            tags["request_id"] = ""
        
        if correlation_id:
            tags["correlation_id"] = correlation_id
        else:
            tags["correlation_id"] = ""

        fields = {key: valor, "received_timestamp": timestamp}
        if sent_timestamp:
            fields["sent_timestamp"] = sent_timestamp
        if extra_fields:
            fields.update(extra_fields)
        data = format_influx_line(measurement, tags, fields, timestamp=timestamp)
        response = requests.post(INFLUXDB_URL, headers=headers, data=data)
        print(f"Response Code: {response.status_code}, Response Text: {response.text} - Data Sent: {data}")

    def write_latency_received(self, request_id=None, correlation_id=None):
        """Registra received_timestamp em latency_measurement (M2S) para pareamento de latência."""
        timestamp = int(time.time() * 1000)
        sensor_id = self.device.identifier
        tags = {"sensor": sensor_id, "source": "middts", "direction": "M2S"}
        
        # Prefer correlation_id over request_id for end-to-end tracing
        if correlation_id:
            tags["correlation_id"] = str(correlation_id)  # format_influx_line handles escaping
        elif request_id is None:
            if hasattr(self, 'last_payload') and isinstance(self.last_payload, dict):
                request_id = self.last_payload.get('request_id')
            if request_id is None and hasattr(self, 'request_id'):
                request_id = getattr(self, 'request_id')
            if request_id:
                tags["request_id"] = str(request_id)
        elif request_id:
            tags["request_id"] = str(request_id)
        
        # Convert value to float to avoid InfluxDB field type conflicts
        value = self.get_value()
        if self.type == 'Boolean' or self.type == 'Integer':
            try:
                ival = int(value)
            except Exception:
                if self.type == 'Boolean':
                    ival = 1 if str(value).lower() in ('true', '1', 'yes') else 0
                else:
                    try:
                        ival = int(float(value))
                    except Exception:
                        ival = 0
            value = float(ival)
        
        fields = {self.name: value, "received_timestamp": timestamp}
        data = format_influx_line("latency_measurement", tags, fields, timestamp=timestamp)
        response = requests.post(INFLUXDB_URL, headers={"Authorization": f"Token {INFLUXDB_TOKEN}", "Content-Type": "text/plain"}, data=data)
        print(f"[M2S-RECEIVED] Logged received_timestamp for {sensor_id} (correlation_id={correlation_id}): {response.status_code}")
    
    def save(self, *args, **kwargs):
        import time
        from datetime import datetime
        
        save_start = time.time()
        property_name = getattr(self, 'name', f'property_{getattr(self, "id", "new")}')
        device_name = getattr(self.device, 'name', 'unknown') if hasattr(self, 'device') and self.device else 'no_device'
        print(f"[{datetime.now().isoformat()}] 🏭 DEVICE PROPERTY SAVE START: '{property_name}' on device '{device_name}'")
        
        # Extract correlation_id from kwargs for end-to-end tracing
        correlation_id = kwargs.pop('correlation_id', None)
        # If True, sent_timestamp was already logged upstream (avoid duplicate write)
        self.m2s_sent_logged = bool(kwargs.pop('m2s_sent_logged', False))
        if correlation_id:
            print(f"[DEBUG] Setting self.correlation_id = {correlation_id} (type: {type(correlation_id)})")
            self.correlation_id = correlation_id  # Store for RPC layer
        
        old_value = ''
        if self.id:
            old_value_start = time.time()
            old_value = Property.objects.get(id=self.id).value
            old_value_time = time.time() - old_value_start
            print(f"[{datetime.now().isoformat()}] 📊 Property '{property_name}' value change: '{old_value}' → '{self.value}' (fetch_time: {old_value_time:.3f}s)")
        
        success = False
        rpc_time = 0
        response = None
        if self.rpc_write_method:
            rpc_start = time.time()
            print(f"[{datetime.now().isoformat()}] 📡 Starting RPC call for '{property_name}' using method '{self.rpc_write_method}'")
            response = self.call_rpc(RPCCallTypes.WRITE)
            rpc_time = time.time() - rpc_start
            success = response.status_code == 200
            print(f"[{datetime.now().isoformat()}] 📡 RPC call completed for '{property_name}' in {rpc_time:.3f}s (status: {response.status_code}, success: {success})")
        else:
            print(f"[{datetime.now().isoformat()}] ⏭️ No RPC write method for '{property_name}' - skipping RPC call")
        
        value_processing_start = time.time()
        if success:
            response_json = response.json()
            new_value = str(response_json.get(self.name))
            print(f"[{datetime.now().isoformat()}] ✅ RPC response for '{property_name}': {new_value}")
            self.value = new_value
        else:
            print(f"[{datetime.now().isoformat()}] ❌ RPC failed for '{property_name}', keeping old value: {old_value}")
            self.value = old_value
        value_processing_time = time.time() - value_processing_start
        
        db_save_start = time.time()
        super().save(*args, **kwargs)
        db_save_time = time.time() - db_save_start
        print(f"[{datetime.now().isoformat()}] 🗃️ Database save for device property '{property_name}' completed in {db_save_time:.3f}s")
        
        influx_time = 0
        if success and USE_INFLUX_TO_EVALUATE and INFLUXDB_TOKEN:
            influx_start = time.time()
            print(f"[{datetime.now().isoformat()}] 📈 Writing to InfluxDB for '{property_name}'")
            # Extrai o request_id do contexto, se disponível
            request_id = None
            if hasattr(self, 'last_payload') and isinstance(self.last_payload, dict):
                request_id = self.last_payload.get('request_id')
            if request_id is None and hasattr(self, 'request_id'):
                request_id = getattr(self, 'request_id')
            self.write_influx(request_id=request_id)
            # Também registra received_timestamp em latency_measurement (M2S) para pareamento
            # Use correlation_id for end-to-end tracing if available
            self.write_latency_received(request_id=request_id, correlation_id=correlation_id)
            influx_time = time.time() - influx_start
            print(f"[{datetime.now().isoformat()}] 📈 InfluxDB write para '{property_name}' e latency_measurement completed in {influx_time:.3f}s")
        
        total_save_time = time.time() - save_start
        print(f"[{datetime.now().isoformat()}] 🏭 DEVICE PROPERTY SAVE COMPLETE: '{property_name}' total time: {total_save_time:.3f}s (rpc: {rpc_time:.3f}s, value_proc: {value_processing_time:.3f}s, db_save: {db_save_time:.3f}s, influx: {influx_time:.3f}s)")
        
        # Log performance warnings
        if total_save_time > 2.0:
            print(f"[{datetime.now().isoformat()}] 🐌 SLOW DEVICE SAVE WARNING: Property '{property_name}' took {total_save_time:.3f}s (threshold: 2.0s)")
        if rpc_time > 1.0:
            print(f"[{datetime.now().isoformat()}] 🐌 SLOW RPC WARNING: Property '{property_name}' RPC took {rpc_time:.3f}s (threshold: 1.0s)")

        return response

    #Para leitura, seria necessário criar um mecanismos no middleware para chamar de forma assincrona esse método de todas as instâncias
    def call_rpc(self,rpc_type:RPCCallTypes):
        """Ultra-fast RPC with 200ms timeout and immediate fallback"""
        import time
        from datetime import datetime
        
        print(f"[{datetime.now().isoformat()}] ⚡ ULTRA-FAST RPC: {rpc_type.name} for {self.device.identifier}")
        
        device = self.device
        gateway = device.gateway
        start_time = time.time()
        correlation_id = getattr(self, 'correlation_id', None)
        property_name = getattr(self, 'name', 'unknown')
        
        # Resolve auth headers for the gateway (Bearer JWT or ApiKey)
        try:
            from core.api import get_gateway_auth_headers
            response, status_code = get_gateway_auth_headers(None, gateway.id)
            if status_code != 200:
                raise Exception(f"Gateway auth failed: {status_code}")
            headers = response['headers']
            print(f"[{datetime.now().isoformat()}] 🔑 Gateway auth: OK")
        except Exception as e:
            print(f"[{datetime.now().isoformat()}] ❌ Gateway auth failed: {e}")
            return self._create_mock_response()
        
        # Setup RPC call - use TWOWAY with fast TB timeout
        urltwoway = f"{gateway.url.rstrip('/')}/api/rpc/twoway/{device.identifier}"
        
        # Helper function to serialize Decimal and other non-JSON types
        def json_serialize_value(value):
            """Convert Decimal and other non-JSON types to JSON-serializable format"""
            from decimal import Decimal
            if isinstance(value, Decimal):
                return float(value)
            elif isinstance(value, dict):
                return {k: json_serialize_value(v) for k, v in value.items()}
            elif isinstance(value, (list, tuple)):
                return [json_serialize_value(item) for item in value]
            return value
        
        # Adaptive RPC timeout based on network profile
        import time
        import os
        
        # Read network profile from environment (set by topology script)
        network_profile = os.getenv('NETWORK_PROFILE', 'urllc').lower()
        m2s_perf_mode = os.getenv('M2S_PERF_MODE', '0').lower() in ('1', 'true', 'yes')
        m2s_perf_timestamps_only = os.getenv('M2S_PERF_TIMESTAMPS_ONLY', '0').lower() in ('1', 'true', 'yes')
        m2s_perf_full = os.getenv('M2S_PERF_FULL', '0').lower() in ('1', 'true', 'yes')
        disable_hotpath_influx = os.getenv('M2S_DISABLE_RPC_INFLUX_HOTPATH', '0').lower() in ('1', 'true', 'yes') or m2s_perf_full
        
        # Adaptive timeout configuration per profile
        # Values consider: network RTT (2 × delay) + ThingsBoard processing overhead (~100ms) + safety margin
        TIMEOUT_CONFIG = {
            'urllc': 0.30,       # 300ms - keep high throughput; eventual delivery is measured separately
            'embb': 0.50,        # 500ms - RTT ~50ms + TB processing + margin
            'best_effort': 1.00  # 1000ms - RTT ~100ms + TB processing + generous margin
        }
        RETRY_CONFIG = {
            'urllc': 1,          # 1 retry - avoid throughput collapse under contention
            'embb': 3,           # 3 retries - standard
            'best_effort': 2,    # 2 retries - less aggressive, accepts loss
        }

        if m2s_perf_mode:
            # Benchmark mode: prioritize low M2S latency over eventual delivery rate
            TIMEOUT_CONFIG['urllc'] = float(os.getenv('M2S_URLLC_TIMEOUT_S', '0.22'))
            RETRY_CONFIG['urllc'] = int(os.getenv('M2S_URLLC_RETRIES', '0'))
        
        retry_count = 0
        max_retries = RETRY_CONFIG.get(network_profile, 2)
        base_timeout = TIMEOUT_CONFIG.get(network_profile, 0.18)
        
        while retry_count <= max_retries:
            try:
                from facade.utils import get_session_for_gateway
                session = get_session_for_gateway(gateway.id)
                
                # Progressive timeout per retry to absorb transient backend slowness
                current_timeout = min(base_timeout * (1 + 0.35 * retry_count), base_timeout + 0.60)
                
                if rpc_type.name == 'WRITE' and self.rpc_write_method:
                    print(f"[{datetime.now().isoformat()}] ⚡ ULTRA-WRITE (try {retry_count+1}): {self.rpc_write_method}={self.get_value()}")
                    
                    # M2S TIMESTAMP: Register when middleware SENDS to simulator
                    if retry_count == 0 and not disable_hotpath_influx:  # Only on first try
                        if not getattr(self, 'm2s_sent_logged', False):
                            try:
                                self._write_m2s_sent_timestamp()
                            except Exception as e:
                                print(f"[{datetime.now().isoformat()}] ⚠️ M2S InfluxDB: {e}")
                        else:
                            print(f"[{datetime.now().isoformat()}] ℹ️ M2S sent_timestamp already logged upstream; skipping duplicate write")

                        if not m2s_perf_timestamps_only:
                            try:
                                self._write_influx_fast()
                            except Exception as e:
                                print(f"[{datetime.now().isoformat()}] ⚠️ InfluxDB: {e}")
                    
                    # Serialize value to JSON-compatible format (convert Decimal, etc)
                    serialized_params = json_serialize_value(self.get_value())
                    print(
                        f"[{datetime.now().isoformat()}] [M2S-RPC] OUTBOUND "
                        f"device={self.device.identifier} property={property_name} method={self.rpc_write_method} "
                        f"params={serialized_params} timeout={current_timeout:.3f}s retry={retry_count} "
                        f"corr={correlation_id}"
                    )
                    
                    response = session.post(
                        urltwoway,
                        json={"method": self.rpc_write_method, "params": serialized_params},
                        headers=headers,
                        timeout=current_timeout
                    )
                elif rpc_type.name == 'READ' and self.rpc_read_method:
                    if retry_count == 0:
                        print(f"[{datetime.now().isoformat()}] ⚡ ULTRA-READ: {self.rpc_read_method}")
                    response = session.post(
                        urltwoway,
                        json={"method": self.rpc_read_method},
                        headers=headers,
                        timeout=current_timeout
                    )
                else:
                    return self._create_mock_response()
                
                elapsed = time.time() - start_time
                try:
                    response_text = (response.text or '')[:180]
                except Exception:
                    response_text = '<unavailable>'
                print(
                    f"[{datetime.now().isoformat()}] [M2S-RPC] INBOUND "
                    f"device={self.device.identifier} property={property_name} method={self.rpc_write_method or self.rpc_read_method} "
                    f"status={response.status_code} elapsed={elapsed:.3f}s retry={retry_count} corr={correlation_id} "
                    f"body={response_text}"
                )
                if response.status_code == 200:
                    if retry_count > 0:
                        print(f"[{datetime.now().isoformat()}] ✅ ULTRA-RPC SUCCESS in {elapsed:.3f}s after {retry_count} retry(ies)")
                    elif elapsed > base_timeout * 0.8:
                        # Log successful but slow requests (>80% of timeout) for monitoring
                        print(f"[{datetime.now().isoformat()}] ⚡ ULTRA-RPC SUCCESS in {elapsed:.3f}s (close to timeout={base_timeout:.2f}s)")
                    return response

                retryable_status = {408, 429, 500, 502, 503, 504}
                if response.status_code in retryable_status and retry_count < max_retries:
                    retry_count += 1
                    print(
                        f"[{datetime.now().isoformat()}] 🔄 ULTRA-RPC RETRY {retry_count}/{max_retries} "
                        f"after HTTP {response.status_code} in {elapsed:.3f}s"
                    )
                    backoff = 0.02 * (2 ** (retry_count - 1))
                    time.sleep(min(0.12, backoff))
                    continue

                print(
                    f"[{datetime.now().isoformat()}] ❌ ULTRA-RPC NON-RETRYABLE/FINAL HTTP "
                    f"{response.status_code} after {elapsed:.3f}s"
                )
                return response
                
            except Exception as e:
                retry_count += 1
                elapsed = time.time() - start_time
                print(
                    f"[{datetime.now().isoformat()}] [M2S-RPC] EXCEPTION "
                    f"device={self.device.identifier} property={property_name} method={self.rpc_write_method or self.rpc_read_method} "
                    f"elapsed={elapsed:.3f}s retry={retry_count-1} corr={correlation_id} err={str(e)[:180]}"
                )
                
                if retry_count <= max_retries:
                    print(f"[{datetime.now().isoformat()}] 🔄 ULTRA-RPC RETRY {retry_count}/{max_retries} after {elapsed:.3f}s (timeout={base_timeout:.2f}s): {str(e)[:100]}")
                    # Exponential backoff: 20ms, 40ms, 80ms for retries 1, 2, 3
                    backoff = 0.02 * (2 ** (retry_count - 1))
                    time.sleep(min(0.10, backoff))
                    print(f"[{datetime.now().isoformat()}] 💤 Retry backoff: {backoff*1000:.0f}ms")
                else:
                    print(f"[{datetime.now().isoformat()}] ❌ ULTRA-RPC FAILED after {max_retries} retries in {elapsed:.3f}s: {str(e)[:100]}")
                    print(f"[{datetime.now().isoformat()}] 🔻 FALLBACK: Returning mock 504 response")
                    return self._create_mock_response(status_code=504)

    def _create_mock_response(self, status_code=504):
        """Create a mock response for fallback/oneway scenarios"""
        class MockResponse:
            def __init__(self, prop, status_code):
                self.status_code = status_code
                self.text = "mock_success" if status_code == 200 else "mock_failure"
                self._prop = prop
            
            def json(self):
                return {self._prop.name: self._prop.get_value()}
        
        return MockResponse(self, status_code)

    def _write_m2s_sent_timestamp(self):
        """Write M2S sent timestamp to InfluxDB for latency measurement"""
        try:
            import time
            from datetime import datetime
            
            if not (USE_INFLUX_TO_EVALUATE and INFLUXDB_TOKEN):
                print(f"[{datetime.now().isoformat()}] ⚠️ M2S: InfluxDB not configured")
                return
            
            sent_ts = int(time.time() * 1000)
            sensor_id = self.device.identifier
            
            tags = {
                "sensor": sensor_id, 
                "source": "middts",
                "direction": "M2S"
            }
            
            # Use correlation_id if available for end-to-end tracing
            correlation_id = getattr(self, 'correlation_id', None)
            if isinstance(correlation_id, (list, tuple)):
                correlation_id = str(correlation_id[1]) if len(correlation_id) > 1 else str(correlation_id[0])
            if correlation_id:
                tags["correlation_id"] = f'"{correlation_id}"'
            
            fields = {"sent_timestamp": sent_ts}
            
            from facade.utils import format_influx_line
            data = format_influx_line("latency_measurement", tags, fields, timestamp=sent_ts)
            
            import requests
            requests.post(
                INFLUXDB_URL,
                headers={"Authorization": f"Token {INFLUXDB_TOKEN}", "Content-Type": "text/plain"},
                data=data,
                timeout=0.5
            )
            print(f"[{datetime.now().isoformat()}] 📈 M2S sent_timestamp logged for {sensor_id}")
        except Exception as e:
            from datetime import datetime
            print(f"[{datetime.now().isoformat()}] ❌ M2S timestamp failed: {e}")

    def _write_influx_fast(self):
        """Fast InfluxDB write with minimal blocking - focusing on property data only"""
        try:
            import time
            from datetime import datetime
            
            if not (USE_INFLUX_TO_EVALUATE and INFLUXDB_TOKEN):
                return
            
            send_ts = int(time.time() * 1000)
            sensor_id = self.device.identifier
            tags = {"sensor": sensor_id, "source": "middts", "direction": "M2S"}
            
            field_value = self.get_value()
            if self.type in ['Boolean', 'Integer']:
                try:
                    ival = int(field_value)
                except:
                    ival = 1 if str(field_value).lower() in ('true', '1', 'yes') else 0
                field_value = float(ival)
            
            # Log property data only - timestamps handled by response endpoints
            fields = {self.name: field_value}
            
            from facade.utils import format_influx_line
            data = format_influx_line("device_data", tags, fields, timestamp=send_ts)
            
            import requests
            requests.post(
                INFLUXDB_URL,
                headers={"Authorization": f"Token {INFLUXDB_TOKEN}", "Content-Type": "text/plain"},
                data=data,
                timeout=0.5  # Very fast timeout
            )
            print(f"[{datetime.now().isoformat()}] 📈 InfluxDB: OK")
        except Exception as e:
            from datetime import datetime
            print(f"[{datetime.now().isoformat()}] 📈 InfluxDB failed: {e}")
    
    def _write_influx_m2s_sent_with_correlation(self):
        """Write M2S sent timestamp when middleware sends RPC to device"""
        try:
            import time
            from datetime import datetime
            if not (USE_INFLUX_TO_EVALUATE and INFLUXDB_TOKEN):
                return
            sent_timestamp = int(time.time() * 1000)
            sensor_id = self.device.identifier
            
            tags = {"sensor": sensor_id, "source": "middts", "direction": "M2S"}
            
            # Prefer correlation_id for end-to-end tracing
            correlation_id = getattr(self, 'correlation_id', None)
            request_id = getattr(self, 'request_id', None)
            if correlation_id:
                tags["correlation_id"] = f'"{correlation_id}"'
            else:
                # Fallback to request_id for backward compatibility
                if request_id:
                    tags["request_id"] = str(request_id)
            
            fields = {"sent_timestamp": sent_timestamp}
            from facade.utils import format_influx_line
            data = format_influx_line("latency_measurement", tags, fields, timestamp=sent_timestamp)
            import requests
            requests.post(
                INFLUXDB_URL,
                headers={"Authorization": f"Token {INFLUXDB_TOKEN}", "Content-Type": "text/plain"},
                data=data,
                timeout=0.5
            )
            print(f"[{datetime.now().isoformat()}] 📡 M2S sent_timestamp: {sent_timestamp} for {sensor_id} request_id={request_id}")
        except Exception as e:
            from datetime import datetime
            print(f"[{datetime.now().isoformat()}] 📡 M2S timestamp failed: {e}")
    
    
    def get_value(self):
        if self.type == 'Boolean':
            return True if self.value in ['True', 'true', True] else False
        elif self.type == 'Integer':
            return int(self.value)
        elif self.type == 'Double':
            return Decimal(self.value)
        else:
            return self.value

def write_inactivity_event(device, inactivity_type: InactivityType, error_message=None):
    """
    Write device inactivity events to InfluxDB with proper formatting
    """
    if not USE_INFLUX_TO_EVALUATE or not INFLUXDB_TOKEN:
        return
        
    timestamp = int(time.time() * 1000)
    headers = {
        "Authorization": f"Token {INFLUXDB_TOKEN}",
        "Content-Type": "text/plain"
    }
    
    # Get timeout value, default to 0 if None
    timeout = device.get_inactivity_timeout() or 0
    
    # Build tags section - move type to tags for better querying
    tags = [
        f"device={device.identifier}",
        f"type={inactivity_type.value}"
    ]
    
    # Build fields section - use numeric floats for aggregation
    fields = [
        f"inactivity_timeout={float(int(timeout))}",
        f"status={float(1.0)}"
    ]
    
    if error_message:
        escaped_message = error_message.replace('"', '\\"')
        fields.append(f'error_message="{escaped_message}"')
    
    # Construct measurement in proper InfluxDB line protocol format using helper
    tags_dict = {"device": device.identifier, "type": inactivity_type.value}
    # Use numeric floats for fields to avoid conflicts (Influx will store as float)
    fields_dict = {"inactivity_timeout": float(int(timeout)), "status": float(1.0)}
    if error_message:
        fields_dict["error_message"] = error_message
    measurement = format_influx_line("device_inactivity", tags_dict, fields_dict, timestamp=timestamp)
    try:
        response = requests.post(INFLUXDB_URL, headers=headers, data=measurement)
        if response.status_code != 204:
            print(f"Failed to write inactivity event: {response.text}")
            print(f"Attempted measurement: {measurement}")
    except Exception as e:
        print(f"Error writing to InfluxDB: {str(e)}")
