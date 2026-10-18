"""SQLite-only reset checks; feed load_house_scenario.py on stdin in a middleware image."""
import io
import json
import sys
from pathlib import Path
from django.conf import settings
settings.configure(
    SECRET_KEY='test', USE_NEO4J=False, USE_INFLUX_TO_EVALUATE=False,
    INFLUXDB_HOST='unused', INFLUXDB_PORT=8086, INFLUXDB_BUCKET='unused',
    INFLUXDB_ORGANIZATION='unused', INFLUXDB_TOKEN='unused',
    INSTALLED_APPS=['django.contrib.auth', 'django.contrib.contenttypes', 'core', 'facade', 'orchestrator'],
    DATABASES={'default': {'ENGINE': 'django.db.backends.sqlite3', 'NAME': ':memory:'}},
    DEFAULT_AUTO_FIELD='django.db.models.AutoField',
)
import django
django.setup()
from django.apps import apps
from django.db import connection, transaction, models
from django.contrib.auth import get_user_model
from django.core.management.base import CommandError
from orchestrator.models import SystemContext, DTDLModel, DigitalTwinInstance
with connection.schema_editor() as editor:
    for model in apps.get_models():
        # PostgreSQL accepts unbounded varchar; SQLite needs a length in Django 5.1.
        for field in model._meta.local_fields:
            if isinstance(field, models.CharField) and field.max_length is None:
                field.max_length = 255
        editor.create_model(model)
ns = {'__file__': str(Path.cwd() / 'orchestrator/management/commands/load_house_scenario.py'), '__name__': 'isolated_house_command'}
exec(compile(sys.stdin.read(), ns['__file__'], 'exec'), ns)
command = ns['Command'](stdout=io.StringIO())
user = get_user_model().objects.create(username='tester')
system = SystemContext.objects.create(name=ns['SYSTEM_NAME'], description=ns['SYSTEM_DESCRIPTION'], created_by=user)
spec = json.loads((ns['SCENARIOS_DIR'] / ns['MODEL_ORDER'][0]).read_text())

def add_model(system, specification):
    # Bypass the parser; reset tests must not contact external services.
    model = DTDLModel(system=system, name='Test', dtdl_id=specification['@id'], specification=specification)
    DTDLModel.objects.bulk_create([model])
    return model

def reset(dry_run=False):
    with transaction.atomic():
        command._reset_example_models(system.pk, 'tester', dry_run)

original = add_model(system, spec)
custom = add_model(system, {'@id': 'dtmi:custom:model;1'})
other = SystemContext.objects.create(name='Other', description='User system', created_by=user)
other_model = add_model(other, spec)
reset(dry_run=True)
assert DTDLModel.objects.filter(pk=original.pk).exists()
original.specification = {**spec, 'displayName': 'User edited'}
DTDLModel.objects.filter(pk=original.pk).update(specification=original.specification)
try:
    reset()
except CommandError:
    pass
else:
    raise AssertionError('Edited model deleted')
DTDLModel.objects.filter(pk=original.pk).update(specification=spec)
instance = DigitalTwinInstance(model=original, name='User instance')
DigitalTwinInstance.objects.bulk_create([instance])
try:
    reset()
except CommandError:
    pass
else:
    raise AssertionError('In-use model deleted')
assert DigitalTwinInstance.objects.filter(pk=instance.pk).exists()
DigitalTwinInstance.objects.filter(pk=instance.pk).delete()
# Simulate recreation failure; transaction must restore removed examples.
try:
    with transaction.atomic():
        reset()
        raise RuntimeError('Simulated parser failure')
except RuntimeError:
    pass
assert DTDLModel.objects.filter(pk=original.pk).exists()
reset()
assert not DTDLModel.objects.filter(pk=original.pk).exists()
assert DTDLModel.objects.filter(pk__in=[custom.pk, other_model.pk]).count() == 2
assert SystemContext.objects.count() == 2
system.description = 'Modified by user'
system.save()
try:
    reset()
except CommandError:
    pass
else:
    raise AssertionError('Unrecognized system accepted')
print('PASS: reset dry-run, edited-model protection, instance protection, rollback, custom models and other systems preserved')
