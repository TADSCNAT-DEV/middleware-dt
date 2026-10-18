"""Isolated SQLite checks. Run in each service image:
python -c "$(cat scenarios/test_gateway_bootstrap.py)" < scenarios/bootstrap_gateway.py
Set GATEWAY_TEST_APP=core (middleware) or devices (simulator).
"""
import os
from django.conf import settings
app = os.environ['GATEWAY_TEST_APP']
settings.configure(SECRET_KEY='test', INSTALLED_APPS=['django.contrib.auth', 'django.contrib.contenttypes', app], DATABASES={'default': {'ENGINE': 'django.db.backends.sqlite3', 'NAME': ':memory:'}}, DEFAULT_AUTO_FIELD='django.db.models.AutoField')
import django
django.setup()
from django.apps import apps
from django.db import connection
from django.contrib.auth import get_user_model
from django.core.management.base import CommandError
Gateway = apps.get_model(app, 'GatewayIOT')
with connection.schema_editor() as editor:
    if app == 'core':
        editor.create_model(get_user_model())
        editor.create_model(apps.get_model('core', 'Organization'))
        editor.create_model(apps.get_model('core', 'OrganizationMembership'))
    editor.create_model(Gateway)
if app == 'core':
    get_user_model().objects.create(username='tester', is_superuser=True)
os.environ.update(DJANGO_SUPERUSER_USERNAME='tester', SEED_THINGSBOARD_GATEWAY='0')
import sys
source = sys.stdin.read()
exec(source, {})
assert Gateway.objects.count() == 0
os.environ.update(SEED_THINGSBOARD_GATEWAY='1', THINGSBOARD_GATEWAY_NAME='Test Gateway', THINGSBOARD_GATEWAY_URL='https://example.com/', THINGSBOARD_AUTH_METHOD='api_key', THINGSBOARD_API_KEY='test-secret')
os.environ['GATEWAY_SEED_DRY_RUN'] = '1'
exec(source, {})
assert Gateway.objects.count() == 0
os.environ['GATEWAY_SEED_DRY_RUN'] = '0'
exec(source, {})
exec(source, {})
assert Gateway.objects.count() == 1
row = Gateway.objects.get()
assert row.api_key == 'test-secret'
if app == 'devices':
    assert row.is_active
    assert row.base_url == 'https://example.com'
else:
    assert row.organization.name == 'Default'
os.environ.update(THINGSBOARD_API_KEY='rotated-secret')
exec(source, {})
assert Gateway.objects.get().api_key == 'test-secret'
os.environ.update(THINGSBOARD_AUTH_METHOD='user_password', THINGSBOARD_USER='tenant', THINGSBOARD_PASSWORD='new-password')
exec(source, {})
row = Gateway.objects.get()
assert row.api_key == 'test-secret' and row.password is None
os.environ.update(THINGSBOARD_AUTH_METHOD='api_key', THINGSBOARD_API_KEY='')
try:
    exec(source, {})
except CommandError:
    pass
else:
    raise AssertionError('Missing API key accepted')
assert Gateway.objects.get().api_key == 'test-secret'
os.environ.update(THINGSBOARD_GATEWAY_NAME='Invalid Gateway', THINGSBOARD_API_KEY='sensitive-value', THINGSBOARD_GATEWAY_URL='invalid-url')
try:
    exec(source, {})
except CommandError as exc:
    assert 'sensitive-value' not in str(exc)
else:
    raise AssertionError('Invalid URL accepted')
assert Gateway.objects.count() == 1
if app == 'devices':
    original = Gateway.objects.get()
    original.is_active = False
    original.save()
    active = Gateway.objects.create(name='Existing Active', base_url='https://active.example.com', auth_method='api_key', api_key='keep-me', is_active=True)
    os.environ.update(THINGSBOARD_GATEWAY_NAME='Another Gateway', THINGSBOARD_GATEWAY_URL='https://new.example.com')
    exec(source, {})
    active.refresh_from_db()
    assert active.is_active and active.api_key == 'keep-me'
    assert not Gateway.objects.get(name='Another Gateway').is_active
    os.environ['THINGSBOARD_GATEWAY_NAME'] = 'Test Gateway'
    exec(source, {})
    original.refresh_from_db()
    assert not original.is_active
print('PASS:', app, 'disabled, dry-run, create, repeat preserves credentials, auth preserved, validation rollback, active gateway preserved')
