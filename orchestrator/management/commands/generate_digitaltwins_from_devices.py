"""Management command: generate DigitalTwin instances from existing facade Devices.

Behavior:
- Scans Devices for a given `system` context (optional) and builds a hierarchy
  of DigitalTwinInstance objects mapping device names / metadata into model
  elements.
- Heuristics:
  - Use Device.type.name to find a DTDLModel with same/contains name.
  - Use labels/metadata tokens to infer parent instances (e.g. house 1, room 2).
  - Create instance names from device.name and map properties by name.
- Arguments:
  --system-id: optional SystemContext id to limit which DTDLModel/system to use.
  --dry-run: print what would be created without persisting.

This is intentionally conservative: it will not overwrite existing instances but
will create missing ones and link properties where exact matches are found.
"""
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from facade.models import Device, Property
from orchestrator.models import SystemContext, DTDLModel, DigitalTwinInstance, ModelElement, DigitalTwinInstanceProperty, DigitalTwinInstanceRelationship, ModelRelationship
from orchestrator.utils import normalize_name
from orchestrator.helpers import compute_similarity
import re
import os
from django.conf import settings
from neomodel import db


def verify_neo4j_connection(dry_run):
    """Fail before relational writes when graph authentication is unavailable."""
    if dry_run or not getattr(settings, 'USE_NEO4J', False):
        return
    try:
        db.cypher_query('RETURN 1')
    except Exception:
        raise CommandError(
            'Nao foi possivel conectar ao Neo4j antes de gerar os DTs. '
            'Confira NEO4J_USER, NEO4J_PASSWORD e NEO4J_AUTH; '
            'nenhuma instancia foi criada por esta execucao.'
        ) from None


def build_model_graph(system: SystemContext | None):
    """Return structures to navigate model relationships: (model_by_id, adjacency, incoming_count)
    model_by_id: {dtdl_id: DTDLModel}
    adjacency: {source_dtdl_id: [ (target_dtdl_id, ModelRelationship), ... ] }
    incoming_count: {dtdl_id: int}
    """
    models = DTDLModel.objects.filter(system=system) if system else DTDLModel.objects.all()
    model_by_id = {m.dtdl_id: m for m in models}
    adjacency = {}
    incoming_count = {m.dtdl_id: 0 for m in models}
    for rel in ModelRelationship.objects.filter(dtdl_model__in=models):
        src = rel.dtdl_model.dtdl_id
        tgt = rel.target
        adjacency.setdefault(src, []).append((tgt, rel))
        # increment incoming for target if the target model exists in our set
        if any(tgt in mid for mid in model_by_id.keys()):
            # if exact match not found, use substring match
            for mid in model_by_id.keys():
                if tgt in mid:
                    incoming_count[mid] = incoming_count.get(mid, 0) + 1
    return model_by_id, adjacency, incoming_count


def find_root_model(system: SystemContext | None):
    """Find a root model (no incoming relationships) preferring ones with 'house' in name."""
    model_by_id, adjacency, incoming = build_model_graph(system)
    roots = [model_by_id[mid] for mid, cnt in incoming.items() if cnt == 0]
    if not roots:
        return DTDLModel.objects.filter(system=system).first() if system else DTDLModel.objects.first()
    # prefer models that look like 'House' or 'Condominium'
    for r in roots:
        if 'house' in normalize_name(r.name) or 'condo' in normalize_name(r.name) or 'condominium' in normalize_name(r.name):
            return r
    return roots[0]


def extract_root_key(device: Device):
    """Extract a grouping key for a device, typically the house/condo/apartment number or unique top-level token.
    Returns a string key or device.name if nothing found.
    """
    name = device.name or ''
    metadata = device.metadata or ''
    text = f"{name} {metadata}".lower()
    # patterns like 'house 3', 'home-2', 'apt 12', 'condo 4', 'condominio 2'
    m = re.search(r"\b(house|home|apt|apartment|condo|condominium|unit)[\s_-]?(\d+)\b", text)
    if m:
        return f"{m.group(1)} {m.group(2)}"
    # fallback: if the name contains a leading number or trailing number, use that
    m2 = re.search(r"(\d+)", name)
    if m2:
        return m2.group(1)
    # final fallback: use normalized prefix token
    tokens = [t for t in re.split(r"\W+", name) if t]
    return tokens[0] if tokens else name


def model_path_bfs(start_dtdl_id: str, target_dtdl_id: str, adjacency: dict, model_by_id: dict):
    """Find a model dtdl_id path from start to target using BFS. Returns list of dtdl_ids or empty if none."""
    from collections import deque
    q = deque()
    q.append((start_dtdl_id, [start_dtdl_id]))
    visited = set([start_dtdl_id])
    while q:
        cur, path = q.popleft()
        if cur == target_dtdl_id or (target_dtdl_id in cur):
            return path
        for (tgt, rel) in adjacency.get(cur, []):
            # resolve tgt into an actual key in model_by_id (substring match)
            for mid in model_by_id.keys():
                if tgt in mid and mid not in visited:
                    visited.add(mid)
                    q.append((mid, path + [mid]))
    return []






def find_model_for_device(device: Device, system: SystemContext | None):
    """Heurística simples para escolher DTDLModel correspondente ao device type/name."""
    candidates = DTDLModel.objects.filter(system=system) if system else DTDLModel.objects.all()
    dev_type = normalize_name(device.type.name) if getattr(device, "type", None) and device.type and device.type.name else ''
    dev_name = normalize_name(device.name or '')
    # 1) match model name contains device type token
    for m in candidates:
        if dev_type and dev_type in normalize_name(m.name):
            return m
    # 2) match model name contains parts of device name
    dev_tokens = [t for t in re.split(r"\W+", dev_name) if t]
    for m in candidates:
        mn = normalize_name(m.name)
        if any(tok in mn for tok in dev_tokens):
            return m
    # 3) fallback: return first model in system
    return candidates.first()


def find_model_for_token(token: str, system: SystemContext | None):
    """Tenta mapear um token (como 'house', 'garden', 'pump') para um DTDLModel."""
    tok = normalize_name(re.sub(r"\d+", "", token)).strip()  # remove números para casar somente o tipo
    if not tok:
        return None
    candidates = DTDLModel.objects.filter(system=system) if system else DTDLModel.objects.all()
    # prioridade: nome contendo token (normalizado)
    for m in candidates:
        if tok in normalize_name(m.name):
            return m
    # fallback: contém token como palavra
    for m in candidates:
        if any(tok == normalize_name(part) for part in re.split(r"\W+", m.name) if part):
            return m
    return None


def find_or_create_hierarchy(root_tokens: list[str], system: SystemContext | None, dry_run=False):
    """Cria instâncias de hierarchy tokens ascendentes e retorna instância final.
    Ex: ['house 1', 'room 2', 'light 3'] cria/retorna instância para 'light 3'.
    """
    parent = None
    prev_parent = None
    created_instances = []
    for idx, token in enumerate(root_tokens):
        name = token
        # tenta encontrar instância existente com mesmo nome (normalizado)
        if system:
            existing = DigitalTwinInstance.objects.filter(name__iexact=name, model__system=system).first()
        else:
            existing = DigitalTwinInstance.objects.filter(name__iexact=name).first()
        if existing:
            parent = existing
        else:
            # seleciona modelo baseado no token (prioriza modelos do system)
            model = find_model_for_token(token, system)
            if not model:
                # último recurso: tenta achar model pelo device completo (se disponível)
                model = DTDLModel.objects.filter(system=system).first() if system else DTDLModel.objects.first()

            if dry_run:
                created_instances.append((name, model.name if model else None))
                parent = None
            else:
                # cria a instância com model (pode ser None — mas preferimos ter um model)
                if model is None:
                    # tenta fallback geral (evita criar instância sem modelo que pode quebrar saves posteriores)
                    model = DTDLModel.objects.filter(system=system).first() if system else DTDLModel.objects.first()
                # sanitize name: avoid creating instances with generic suffixes like
                # 'condominium' or with the exact model name (these cause confusing entries)
                try:
                    model_norm = normalize_name(model.name) if model and model.name else ''
                except Exception:
                    model_norm = ''
                name_norm = normalize_name(name) if name else ''
                if model_norm and (name_norm == model_norm or 'condominium' in name_norm or 'condominio' in name_norm):
                    # leave name empty to let DigitalTwinInstance.save() generate a proper name
                    inst = DigitalTwinInstance.objects.create(model=model)
                else:
                    inst = DigitalTwinInstance.objects.create(model=model, name=name)
                created_instances.append((name, model.name if model else None))
                parent = inst

        # cria relação com parent anterior: tenta encontrar ModelRelationship entre os modelos
        if not dry_run and idx > 0 and prev_parent and parent:
            try:
                source_model = prev_parent.model
                target_model = parent.model if parent else None
                rel = resolve_instance_relationship(source_model, target_model)
                if rel:
                    DigitalTwinInstanceRelationship.objects.update_or_create(
                        source_instance=prev_parent,
                        target_instance=parent,
                        defaults={'relationship': rel}
                    )
                else:
                    # Não criar DigitalTwinInstanceRelationship vazio — apenas logar
                    print(f"[WARN] No ModelRelationship found between {source_model.name if source_model else 'N/A'} -> {target_model.name if target_model else 'N/A'}; skipping instance relationship creation.")
            except Exception as e:
                print(f"[ERROR] Failed creating relationship between instances {prev_parent} -> {parent}: {e}")
        prev_parent = parent
    return parent, created_instances


def resolve_instance_relationship(source_model: DTDLModel | None, target_model: DTDLModel | None):
    if not source_model or not target_model:
        return None
    target_id = target_model.dtdl_id or ''
    target_id_nover = re.sub(r";\d+$", "", target_id)
    target_name = normalize_name(target_model.name or '')
    target_token = normalize_name(target_model.name.split()[0]) if target_model.name else ''
    for rel in source_model.model_relationships.all():
        rel_target = rel.target or ''
        rel_name = rel.name or ''
        if target_id and target_id in rel_target:
            return rel
        if target_id_nover and target_id_nover in rel_target:
            return rel
        if target_name and target_name in normalize_name(rel_target):
            return rel
        if target_token and target_token in normalize_name(rel_name):
            return rel
    return None


def extract_house_key_strict(name: str | None):
    if not name:
        return None
    m = re.search(r"\b(house|home|apt|apartment|condo|condominium|unit)\s*(\d+)\b", name.lower())
    if not m:
        return None
    return f"{m.group(1)} {m.group(2)}"


def repair_house_relationships(system: SystemContext | None, dry_run=False):
    house_model = DTDLModel.objects.filter(system=system, name__icontains='house').first() if system else DTDLModel.objects.filter(name__icontains='house').first()
    if not house_model:
        return 0
    target_models = []
    for key in ('room', 'garden', 'pool'):
        tm = DTDLModel.objects.filter(system=system, name__icontains=key).first() if system else DTDLModel.objects.filter(name__icontains=key).first()
        if tm:
            target_models.append(tm)
    if not target_models:
        return 0

    created = 0
    houses = DigitalTwinInstance.objects.filter(model=house_model)
    for house in houses:
        house_key = extract_house_key_strict(house.name)
        if not house_key:
            continue
        for tm in target_models:
            rel = resolve_instance_relationship(house_model, tm)
            if not rel:
                continue
            targets = DigitalTwinInstance.objects.filter(model=tm)
            for target in targets:
                target_key = extract_house_key_strict(target.name)
                if target_key and target_key == house_key:
                    if dry_run:
                        continue
                    _, was_created = DigitalTwinInstanceRelationship.objects.update_or_create(
                        source_instance=house,
                        target_instance=target,
                        defaults={'relationship': rel}
                    )
                    if was_created:
                        created += 1
    return created


def create_full_topology(system: SystemContext | None, dry_run=False):
    """Create one DigitalTwinInstance per DTDLModel and wire ModelRelationships as instance relationships.
    Returns a mapping {dtdl_model.id: instance} (or names in dry-run).
    This is conservative: instances are created with empty names to let the save() generate unique names.
    """
    mapping = {}
    models = DTDLModel.objects.filter(system=system) if system else DTDLModel.objects.all()
    created = []
    # Create instances for all models
    for m in models:
        if dry_run:
            created.append((m.name, 'DRY'))
            mapping[m.id] = None
            continue
        # Create instances with empty names; they will be properly named/linked when devices are processed.
        inst = DigitalTwinInstance.objects.create(model=m, name='')
        mapping[m.id] = inst
    # Create relationships between instances according to ModelRelationship
    if not dry_run:
        for rel in ModelRelationship.objects.filter(dtdl_model__in=models):
            source_model = rel.dtdl_model
            # try to resolve target model by dtdl_id or name
            target_model = DTDLModel.objects.filter(dtdl_id__icontains=rel.target, system=system).first() if system else DTDLModel.objects.filter(dtdl_id__icontains=rel.target).first()
            if not target_model:
                # fallback by name token
                target_model = DTDLModel.objects.filter(name__icontains=rel.target.split()[:1]).first()
            if source_model and target_model and mapping.get(source_model.id) and mapping.get(target_model.id):
                try:
                    DigitalTwinInstanceRelationship.objects.update_or_create(
                        source_instance=mapping[source_model.id],
                        target_instance=mapping[target_model.id],
                        defaults={'relationship': rel}
                    )
                except Exception as e:
                    print(f"[WARN] failed to create instance relationship for model rel {rel}: {e}")
    return mapping, created


class Command(BaseCommand):
    help = 'Generate DigitalTwin hierarchy from existing Devices'

    def add_arguments(self, parser):
        parser.add_argument('--system-id', type=int, help='SystemContext id to limit models')
        parser.add_argument('--dry-run', action='store_true', help='Do not persist changes')

    @transaction.atomic
    def handle(self, *args, **options):
        verify_neo4j_connection(options.get('dry_run', False))
        system = None
        if options.get('system_id'):
            system = SystemContext.objects.filter(pk=options['system_id']).first()
            if not system:
                self.stdout.write(self.style.ERROR(f"SystemContext {options['system_id']} not found"))
                return
        dry_run = options.get('dry_run', False)
        devices = Device.objects.all()
        created = 0
        skipped = 0
        # Group devices by inferred root key (e.g. house 1)
        groups = {}
        for d in devices:
            key = extract_root_key(d)
            groups.setdefault(key, []).append(d)

        # Build model graph to discover topology
        model_by_id, adjacency, incoming = build_model_graph(system)
        root_model = find_root_model(system)
        self.stdout.write(self.style.NOTICE(f"Root model chosen: {root_model.name if root_model else 'N/A'}"))
        # If there are no DigitalTwinInstance entries, we previously created a full
        # topology from the models. That produced placeholder instances with empty
        # names which later remained unassociated when devices were present. To
        # avoid orphan placeholders, only generate the full topology when there
        # are no devices to drive instance creation.
        any_instances = DigitalTwinInstance.objects.exists()
        instance_mapping = {}
        if not any_instances:
            if devices.exists():
                self.stdout.write(self.style.NOTICE("Devices present — skipping full topology pre-creation to avoid placeholder instances."))
            else:
                self.stdout.write(self.style.NOTICE("No existing DigitalTwinInstance found and no devices — generating full topology from DTDL models."))
                mapping, created_list = create_full_topology(system, dry_run=dry_run)
                # mapping keys are model ids -> DigitalTwinInstance (or None in dry-run)
                instance_mapping = {}
                for model_pk, inst in mapping.items():
                    m = DTDLModel.objects.filter(pk=model_pk).first()
                    if m:
                        instance_mapping[m] = inst
                if dry_run:
                    self.stdout.write(self.style.NOTICE(f"[DRY] Would create topology: {created_list}"))
        for group_key, dlist in groups.items():
            self.stdout.write(self.style.NOTICE(f"Processing group {group_key} ({len(dlist)} devices)"))
            for d in dlist:
                device_model = find_model_for_device(d, system)
                path = []
                if root_model and device_model:
                    path = model_path_bfs(root_model.dtdl_id, device_model.dtdl_id, adjacency, model_by_id)
                if not path and device_model:
                    path = [device_model.dtdl_id]

                parent_instance = None
                created_list = []
                # Attempt to extract hierarchical tokens from device name, e.g. 'House 1 - Room 2 - LightBulb'
                name_tokens = [t.strip() for t in re.split(r"\s*-\s*", d.name)] if d.name else []
                for idx, mid in enumerate(path):
                    mdl = model_by_id.get(mid)
                    if not mdl:
                        continue

                    is_leaf = idx == len(path) - 1
                    inst = None

                    if is_leaf:
                        # Leaf must be unique per device (avoid collapsing multiple devices of same type).
                        inst = (
                            DigitalTwinInstance.objects
                            .filter(model=mdl, digitaltwininstanceproperty__device_property__device=d)
                            .distinct()
                            .first()
                        )
                        if not inst:
                            inst = DigitalTwinInstance.objects.filter(model=mdl, name__iexact=d.name).first()
                    else:
                        # Intermediate nodes are shared within the group hierarchy.
                        existing = DigitalTwinInstance.objects.filter(model=mdl)
                        desired_token = name_tokens[idx] if len(name_tokens) > idx else None
                        for ex in existing:
                            try:
                                hierarchy_text = " ".join(ex.get_hierarchy()).lower()
                            except Exception:
                                hierarchy_text = (ex.name or '').lower()
                            # If we have a desired token for this level, only accept an
                            # existing instance that matches that token. Otherwise fall
                            # back to group_key match.
                            if desired_token:
                                if desired_token.lower() in hierarchy_text:
                                    inst = ex
                                    break
                            else:
                                if group_key and group_key.lower() in hierarchy_text:
                                    inst = ex
                                    break

                    if not inst:
                        if dry_run:
                            if is_leaf:
                                # preview uses device name if it already contains group_key
                                if d.name and group_key and group_key.lower() in d.name.lower():
                                    preview_name = d.name
                                else:
                                    preview_name = f"{group_key} - {d.name}" if group_key else d.name
                            else:
                                # try to use the token for this level from device name when available
                                token = name_tokens[idx] if len(name_tokens) > idx else mdl.name
                                preview_name = f"{group_key} - {token}" if group_key else token
                            created_list.append((mdl.name, preview_name))
                            inst = None
                        else:
                            # For leaf instances prefer the device's full name when it already
                            # encodes the hierarchy (avoids collisions between rooms).
                            if is_leaf:
                                if d.name and group_key and group_key.lower() in d.name.lower():
                                    instance_name = d.name
                                else:
                                    instance_name = f"{group_key} - {d.name}" if group_key else d.name
                            else:
                                # Use device tokens to generate more specific intermediate names
                                token = name_tokens[idx] if len(name_tokens) > idx else mdl.name
                                instance_name = f"{group_key} - {token}" if group_key else token
                            inst = DigitalTwinInstance.objects.create(model=mdl, name=instance_name)
                            created_list.append((mdl.name, inst.name))

                    if parent_instance and inst:
                        rel = resolve_instance_relationship(parent_instance.model, mdl)
                        if rel:
                            DigitalTwinInstanceRelationship.objects.update_or_create(
                                source_instance=parent_instance,
                                target_instance=inst,
                                defaults={'relationship': rel}
                            )
                    parent_instance = inst

                instance = parent_instance
                if dry_run:
                    self.stdout.write(self.style.NOTICE(f"[DRY] Device {d.name} -> would create/attach to instance {created_list}"))
                    skipped += 1
                    continue
                if instance is None:
                    instance, created_list = find_or_create_hierarchy([d.name], system, dry_run=dry_run)
                if instance is None:
                    self.stdout.write(self.style.WARNING(f"Could not determine instance for device {d.name}. Skipping."))
                    skipped += 1
                    continue

                with transaction.atomic():
                    for prop in Property.objects.filter(device=d):
                        if not prop.name:
                            continue
                        norm_prop = normalize_name(prop.name)
                        elems_qs = ModelElement.objects.filter(dtdl_model=instance.model) if instance and instance.model else ModelElement.objects.all()
                        el = None
                        for e in elems_qs:
                            if normalize_name(e.name) == norm_prop:
                                el = e
                                break
                        if not el:
                            best = None
                            best_score = 0.0
                            for e in elems_qs:
                                score = compute_similarity(prop.name, e.name)
                                if score > best_score:
                                    best_score = score
                                    best = e
                            threshold = float(os.environ.get('ASSOC_SIM_THRESHOLD', 0.7))
                            # Accept match either by similarity OR by token/substring overlap
                            def norm(s):
                                return normalize_name(s or '')
                            accepted = False
                            if best and best_score >= threshold:
                                accepted = True
                            else:
                                # token / substring fallback: humidity <-> soilHumidity
                                prop_norm = norm(prop.name)
                                if best:
                                    best_norm = norm(best.name)
                                    # substring match in either direction
                                    if prop_norm and (prop_norm in best_norm or best_norm in prop_norm):
                                        accepted = True
                                    else:
                                        # token overlap (split on non-word)
                                        prop_tokens = set([t for t in re.split(r"\W+", prop_norm) if t])
                                        best_tokens = set([t for t in re.split(r"\W+", best_norm) if t])
                                        if prop_tokens & best_tokens:
                                            # accept if some token overlaps and score is reasonably close
                                            if best_score >= 0.45:
                                                accepted = True

                                # If best candidate didn't match, try scanning all elements for substring/token overlap
                                if not accepted:
                                    for e in elems_qs:
                                        e_norm = norm(e.name)
                                        if prop_norm and (prop_norm in e_norm or e_norm in prop_norm):
                                            # accept this element as the match
                                            best = e
                                            accepted = True
                                            break

                            if accepted and best:
                                el = best
                            else:
                                print(f"[WARN] No ModelElement found for property '{prop.name}' in model '{instance.model.name if instance and instance.model else 'N/A'}' - skipping binding.")
                                continue
                        dtip, _ = DigitalTwinInstanceProperty.objects.get_or_create(dtinstance=instance, property=el)
                        # Use update() to avoid triggering model save side-effects/RPC during binding.
                        DigitalTwinInstanceProperty.objects.filter(pk=dtip.pk).update(device_property=prop)
                created += 1
                self.stdout.write(self.style.SUCCESS(f"Created/updated DT instance for device {d.name}: {instance}"))

        if not dry_run:
            added = repair_house_relationships(system, dry_run=dry_run)
            if added:
                self.stdout.write(self.style.SUCCESS(f"Repaired {added} house-level relationships"))
