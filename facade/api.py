# facade/views.py
import json

from core.api import get_gateway_auth_headers
from ninja import Query
from core.models import GatewayIOT
from .models import Device, DeviceType, Property
from .schemas import DeviceDiscoveryParams, DeviceRPCView, DeviceSchema
import requests
from django.shortcuts import get_object_or_404
from ninja import Router, NinjaAPI
from django.contrib.auth import get_user_model
import json
from pathlib import Path
from django.conf import settings

# Load device type mappings (optional). If present, used as fallback to create
# properties and RPC metadata when ThingsBoard doesn't provide them. This can
# be disabled via the `DEVICE_TYPE_MAPPING_ENABLED` Django setting so the
# system falls back to telemetry-only inference.
_MAPPINGS_PATH = Path(__file__).resolve().parents[1] / "orchestrator" / "config" / "device_type_mappings.json"
if getattr(settings, 'DEVICE_TYPE_MAPPING_ENABLED', True):
    try:
        if _MAPPINGS_PATH.exists():
            with open(_MAPPINGS_PATH, 'r', encoding='utf-8') as _f:
                DEVICE_TYPE_MAPPINGS = json.load(_f)
        else:
            DEVICE_TYPE_MAPPINGS = {}
    except Exception:
        DEVICE_TYPE_MAPPINGS = {}
else:
    DEVICE_TYPE_MAPPINGS = {}


def _normalize_mapping_key(value):
    return " ".join(str(value or "").strip().lower().replace("_", " ").split())


def _get_type_mapping(dtype_name):
    if not dtype_name:
        return None
    if dtype_name in DEVICE_TYPE_MAPPINGS:
        return DEVICE_TYPE_MAPPINGS[dtype_name]
    normalized = _normalize_mapping_key(dtype_name)
    for key, value in DEVICE_TYPE_MAPPINGS.items():
        if _normalize_mapping_key(key) == normalized:
            return value
    return None

router = Router()
api = NinjaAPI()

def _scope_to_organization(queryset, request):
    user = getattr(request, "user", None)
    if not user or not getattr(user, "is_authenticated", False):
        return queryset.none()
    if getattr(user, "is_superuser", False):
        return queryset
    return queryset.filter(organization__memberships__user=user).distinct()


@router.get(
    "/devices/",
    response={200: list[DeviceSchema]},
    tags=['Facade'],
    summary="List devices in organization scope",
)
def list_devices(request):
    return _scope_to_organization(Device.objects.all(), request)

@router.post(
    "/devices/{device_id}/rpc/",
    response={200: dict},
    tags=['Facade'],
    summary="Send RPC command to a device",
    openapi_extra={
        "requestBody": {
            "content": {
                "application/json": {
                    "examples": {
                        "toggle": {
                            "value": {
                                "method": "setStatus",
                                "params": {"status": True}
                            }
                        }
                    }
                }
            }
        }
    },
)
def call_device_rpc(request, device_id: int, payload: DeviceRPCView):
    import uuid

    def _parse_gateway_response(resp):
        try:
            return resp.json()
        except ValueError:
            body = (resp.text or "").strip()
            if not body:
                return None
            return {"detail": body}

    user = getattr(request, "user", None)
    if not user or not getattr(user, "is_authenticated", False):
        return api.create_response(request, {"detail": "Authentication required"}, status=403)
    qs = _scope_to_organization(Device.objects.all(), request)
    device = get_object_or_404(qs, id=device_id)
    gateway = device.gateway
    auth_response, status_code = get_gateway_auth_headers(request, gateway.id)
    if status_code != 200:
        return api.create_response(request, auth_response, status=status_code)
    url = f"{gateway.url.rstrip('/')}/api/plugins/rpc/oneway/{device.identifier}"
    headers = auth_response["headers"]
    # Gerar request_id único
    request_id = str(uuid.uuid4())
    # Incluir request_id no payload.params
    params = dict(payload.params) if payload.params else {}
    params["request_id"] = request_id
    # Chamar o RPC com request_id propagado
    response = requests.post(
        url, json={"method": payload.method, "params": params}, headers=headers
    )
    # Atribuir request_id ao device para uso posterior (ex: gravação sent_timestamp)
    setattr(device, 'request_id', request_id)
    response_payload = _parse_gateway_response(response)
    if response.status_code == 200:
        if response_payload is not None:
            return response_payload
        return {"status": "accepted", "request_id": request_id}
    if response_payload is None:
        response_payload = {"detail": response.reason or "Gateway RPC request failed"}
    return api.create_response(request, response_payload, status=response.status_code)


@router.get(
    "/gatewaysiot/{gateway_id}/discover-devices/",
    response={200: dict},
    tags=['Facade'],
    summary="Discover devices from ThingsBoard gateway",
)
def discover_devices(request, gateway_id: int, params: DeviceDiscoveryParams = Query(...)):
    User = get_user_model()
    # Allow anonymous API calls to trigger discovery for testing: fall back
    # to a system user (prefer a superuser) when the request is unauthenticated.
    if getattr(request, "user", None) and request.user.is_authenticated:
        user = request.user
    else:
        user = User.objects.filter(is_superuser=True).first() or User.objects.first()
    auth_response, status_code = get_gateway_auth_headers(request, gateway_id)
    if status_code != 200:
        return api.create_response(request, auth_response, status=status_code)
    gateway_qs = GatewayIOT.objects.all()
    req_user = getattr(request, "user", None)
    if getattr(req_user, "is_authenticated", False) and not getattr(req_user, "is_superuser", False):
        gateway_qs = gateway_qs.filter(organization__memberships__user=req_user).distinct()
    elif request is not None and req_user and not getattr(req_user, "is_authenticated", False):
        gateway_qs = gateway_qs.none()
    gateway = get_object_or_404(gateway_qs, id=gateway_id)
    headers = auth_response["headers"]
    url = f"{gateway.url.rstrip('/')}/api/tenant/devices"
    # Helper functions (defined here so they are available for both created and updated devices)
    def try_sync_telemetry_keys_to_properties(dev_obj):
        try:
            keys_url = f"{gateway.url.rstrip('/')}/api/plugins/telemetry/DEVICE/{dev_obj.identifier}/keys/timeseries"
            kresp = requests.get(keys_url, headers=headers, timeout=10)
            if kresp.status_code == 200:
                keys = kresp.json()
                for key in keys:
                    # create without side-effects
                    prop, created = Property.objects.get_or_create(device=dev_obj, name=key, defaults={"type": "Double"})
                    if created:
                        try:
                            prop.save_base(raw=True)
                        except Exception:
                            pass
        except Exception:
            pass

    def apply_type_mapping(dev_obj, dtype_name):
        try:
            mapping = _get_type_mapping(dtype_name)
            if not mapping:
                return
            for p in mapping:
                # Create property without rpc methods first to avoid triggering RPC on save
                defaults = {"type": p.get("type", "Double")}
                prop, created = Property.objects.get_or_create(device=dev_obj, name=p["name"], defaults=defaults)
                # Shared attributes are authoritative; type mappings only fill gaps.
                updated = False
                if not prop.type and p.get("type"):
                    prop.type = p.get("type")
                    updated = True
                if not prop.rpc_read_method and p.get("rpc_read_method"):
                    prop.rpc_read_method = p.get("rpc_read_method")
                    updated = True
                if not prop.rpc_write_method and p.get("rpc_write_method"):
                    prop.rpc_write_method = p.get("rpc_write_method")
                    updated = True
                if updated:
                    try:
                        prop.save_base(raw=True)
                    except Exception:
                        pass
        except Exception:
            pass

    def infer_properties_from_telemetry(dev_obj):
        try:
            keys_url = f"{gateway.url.rstrip('/')}/api/plugins/telemetry/DEVICE/{dev_obj.identifier}/keys/timeseries"
            kresp = requests.get(keys_url, headers=headers, timeout=10)
            if kresp.status_code != 200:
                return
            keys = kresp.json() or []
            if not keys:
                return
            for key in keys:
                val_url = f"{gateway.url.rstrip('/')}/api/plugins/telemetry/DEVICE/{dev_obj.identifier}/values/timeseries?keys={key}&limit=1"
                vresp = requests.get(val_url, headers=headers, timeout=10)
                if vresp.status_code != 200:
                    continue
                try:
                    vjson = vresp.json()
                except Exception:
                    continue
                val = None
                if isinstance(vjson, dict):
                    for k, arr in vjson.items():
                        if isinstance(arr, list) and arr:
                            val = arr[0].get("value")
                            break
                ptype = "String"
                if val is None:
                    ptype = "String"
                else:
                    s = str(val).lower()
                    if s in ("true", "false"):
                        ptype = "Boolean"
                    else:
                        try:
                            int(val)
                            ptype = "Integer"
                        except Exception:
                            try:
                                float(val)
                                ptype = "Double"
                            except Exception:
                                ptype = "String"
                Property.objects.get_or_create(device=dev_obj, name=key, defaults={"type": ptype})
        except Exception:
            pass

    response = requests.get(url, headers=headers, params=params.dict())
    if response.status_code == 200:
        devices_data = response.json()['data']
        created_objs = []
        updated_count = 0
        for device_data in devices_data:
            obj = Device.objects.filter(identifier=device_data['id']['id'], gateway=gateway).first()
            # Ensure device type exists (create if necessary)
            dtype_name = device_data.get('type') or 'Unknown'
            dtype, _ = DeviceType.objects.get_or_create(
                name=dtype_name,
                defaults={"organization": gateway.organization, "created_by": user},
            )
            if gateway.organization and dtype.organization_id is None:
                dtype.organization = gateway.organization
            if user and dtype.created_by_id is None:
                dtype.created_by = user
            if dtype.organization_id or dtype.created_by_id:
                dtype.save(update_fields=[field for field in ['organization', 'created_by'] if getattr(dtype, f'{field}_id', None)])
            if obj:
                # Atualiza campos e persiste (usando update_fields para evitar bypass)
                obj.name = device_data['name']
                obj.status = 'unknown'
                obj.type = dtype
                obj.organization = gateway.organization
                obj.user = user
                if obj.created_by_id is None:
                    obj.created_by = user
                try:
                    obj.save(update_fields=['name', 'status', 'type', 'organization', 'user', 'created_by'])
                except Exception:
                    obj.save()
                updated_count += 1
                # Se não houver propriedades locais, sincroniza a partir do ThingsBoard
                try:
                    if not obj.property_set.exists():
                        obj.sync_properties_from_thingsboard()
                        obj.sync_metadata_from_thingsboard()
                        # Apply mapping fallback and telemetry inference if still empty
                        if not obj.property_set.exists():
                            apply_type_mapping(obj, dtype_name)
                        if not obj.property_set.exists():
                            try_sync_telemetry_keys_to_properties(obj)
                        if not obj.property_set.exists():
                            infer_properties_from_telemetry(obj)
                except Exception:
                    pass
            else:
                created_objs.append(Device(
                    name=device_data['name'],
                    identifier=device_data['id']['id'],
                    status='unknown',
                    type=dtype,
                    gateway=gateway,
                    organization=gateway.organization,
                    created_by=user,
                    user=user
                ))
        created_count = 0
        if created_objs:
            # Bulk create then post-process to sync properties/metadata
            Device.objects.bulk_create(created_objs)
            created_count = len(created_objs)
            # Requery created devices to trigger property/metadata sync
            identifiers = [d.identifier for d in created_objs]
            created_devices = list(Device.objects.filter(identifier__in=identifiers, gateway=gateway))
            for dev in created_devices:
                try:
                    # Ensure type exists (should already) and sync properties/metadata
                    dev.sync_properties_from_thingsboard()
                    dev.sync_metadata_from_thingsboard()
                except Exception:
                    # Don't fail the entire discovery if a device sync fails
                    pass
            # Apply mapping/inference for created devices
            for dev in created_devices:
                try:
                    # 1) attempt mapping by device type
                    dtype_name = dev.type.name if dev.type else ''
                    apply_type_mapping(dev, dtype_name)
                    # 2) if still no properties, try telemetry-based inference
                    if not dev.property_set.exists():
                        try_sync_telemetry_keys_to_properties(dev)
                    if not dev.property_set.exists():
                        infer_properties_from_telemetry(dev)
                except Exception:
                    pass
        # Final pass: ensure all devices for this gateway have properties via mapping/inference
        try:
            for dev in Device.objects.filter(gateway=gateway):
                try:
                    if not dev.property_set.exists():
                        dtype_name = dev.type.name if dev.type else ''
                        apply_type_mapping(dev, dtype_name)
                        if not dev.property_set.exists():
                            try_sync_telemetry_keys_to_properties(dev)
                        if not dev.property_set.exists():
                            infer_properties_from_telemetry(dev)
                except Exception:
                    pass
        except Exception:
            pass
        return {"created": created_count, "updated": updated_count}
    # Some ThingsBoard cloud responses may be empty or non-JSON (HTML error pages).
    # Avoid raising an internal server error when `.json()` fails — return a
    # readable payload with the original status and body text instead.
    try:
        payload = response.json()
    except Exception:
        body = (response.text or "").strip()
        if not body:
            body = response.reason or "Gateway error"
        payload = {"detail": body}
    if response.status_code == 401 and gateway.auth_method == GatewayIOT.AUTH_METHOD_API_KEY:
        # ThingsBoard may say "Invalid username or password" even for ApiKey requests.
        # Keep the upstream fields and identify the authentication actually used.
        if not isinstance(payload, dict):
            payload = {"upstream_response": payload}
        payload["gateway_auth_method"] = "api_key"
        payload["detail"] = (
            "ThingsBoard rejected the API Key during device discovery. "
            "No username/password login was attempted by discovery. "
            "Check that the key is active, belongs to this instance and is a user API Key."
        )
    return api.create_response(request, payload, status=response.status_code)

@router.get(
    "/devices/{device_id}/rpc-methods/",
    response={200: list},
    tags=['Facade'],
    summary="List read and write RPC methods mapped for the device",
)
def list_device_rpc_methods(request, device_id: int):
    qs = _scope_to_organization(Device.objects.all(), request)
    device = qs.filter(id=device_id).first()
    if not device:
        return api.create_response(request, {"detail": "Not found"}, status=404)
    rpc_methods = device.property_set.filter(rpc_read_method__isnull=False).values_list('name', 'rpc_read_method', 'rpc_write_method')
    return list(rpc_methods)




 