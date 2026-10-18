"""Run through manage.py shell in middleware or simulator; never print secrets."""
import os

from django.apps import apps
from django.contrib.auth import get_user_model
from django.core.management.base import CommandError
from django.db import transaction


def bootstrap_gateway():
    if os.getenv('SEED_THINGSBOARD_GATEWAY', '0').lower() not in ('1', 'true', 'yes'):
        print('Gateway bootstrap desabilitado (SEED_THINGSBOARD_GATEWAY=0).')
        return

    name = os.getenv('THINGSBOARD_GATEWAY_NAME', 'ThingsBoard Demo').strip()
    url = os.getenv('THINGSBOARD_GATEWAY_URL', '').strip().rstrip('/')
    method = os.getenv('THINGSBOARD_AUTH_METHOD', 'api_key').strip()
    key = os.getenv('THINGSBOARD_API_KEY', '').strip()
    username = os.getenv('THINGSBOARD_USER', '').strip()
    password = os.getenv('THINGSBOARD_PASSWORD', '')
    if not name or not url:
        raise CommandError('Defina THINGSBOARD_GATEWAY_NAME e THINGSBOARD_GATEWAY_URL no .env.')
    if method not in ('api_key', 'user_password'):
        raise CommandError('THINGSBOARD_AUTH_METHOD deve ser api_key ou user_password.')
    if method == 'api_key' and not key:
        raise CommandError('Defina THINGSBOARD_API_KEY com a API Key da conta ThingsBoard.')
    if method == 'user_password' and (not username or not password):
        raise CommandError('Defina THINGSBOARD_USER e THINGSBOARD_PASSWORD.')

    if os.getenv('GATEWAY_SEED_DRY_RUN') == '1':
        print('Dry run: gateway seria configurado; nenhuma escrita realizada.')
        return

    simulator = apps.is_installed('devices')
    try:
        Gateway = apps.get_model('devices' if simulator else 'core', 'GatewayIOT')
    except LookupError:
        raise CommandError('Esta versao do simulador nao possui GatewayIOT. Atualize o simulador e refaca o build.') from None

    with transaction.atomic():
        lookup = {'name': name}
        defaults = {
            'auth_method': method,
            'username': username if method == 'user_password' else None,
            'password': password if method == 'user_password' else None,
            'api_key': key if method == 'api_key' else None,
        }
        if simulator:
            defaults.update(base_url=url, is_active=not Gateway.objects.filter(is_active=True).exists())
            try:
                defaults['mqtt_port'] = int(os.getenv('THINGSBOARD_MQTT_PORT', '1883'))
                defaults['mqtt_keep_alive'] = int(os.getenv('THINGSBOARD_MQTT_KEEP_ALIVE', '60'))
            except ValueError:
                raise CommandError('Porta e keep alive MQTT devem ser inteiros.') from None
        else:
            user = get_user_model().objects.filter(
                username=os.getenv('DJANGO_SUPERUSER_USERNAME', 'middts'), is_superuser=True,
            ).first()
            if not user:
                raise CommandError('Crie o superusuario configurado antes de executar o seed.')
            Organization = apps.get_model('core', 'Organization')
            Membership = apps.get_model('core', 'OrganizationMembership')
            org, _ = Organization.objects.get_or_create(name='Default', defaults={'created_by': user})
            Membership.objects.get_or_create(user=user, organization=org, defaults={'role': 'admin'})
            lookup['organization'] = org
            defaults.update(url=url, created_by=user)

        # A name identifies the seed-owned gateway. Refuse ambiguous existing rows.
        if Gateway.objects.filter(**lookup).count() > 1:
            raise CommandError('Ha gateways duplicados com esse nome. Corrija o cadastro antes de executar o seed.')
        gateway = Gateway.objects.filter(**lookup).first()
        if gateway is not None:
            print('Gateway existente preservado; nenhuma credencial ou configuracao alterada.')
            return
        gateway = Gateway(**lookup)
        for field, value in defaults.items():
            setattr(gateway, field, value)
        # Validate separately so Django cannot include credential values in errors.
        try:
            gateway.full_clean()
        except Exception:
            raise CommandError('Configuracao do gateway invalida. Confira URL, credenciais e parametros MQTT.') from None
        gateway.save()
    print('Gateway ThingsBoard configurado no simulador.' if simulator else 'Gateway ThingsBoard configurado no MiddTS.')


bootstrap_gateway()
