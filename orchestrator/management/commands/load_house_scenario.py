"""
Management command: load_house_scenario
========================================
Carrega o cenário House 2.0 no middleware-dt chamando os endpoints REST da
própria API, exercitando o stack completo:

  Django views → Ninja API → DTDLModel.save() → parser microservice (DTDL_PARSER_URL)

Uso:
    python manage.py load_house_scenario
    python manage.py load_house_scenario --base-url http://localhost:8000
    python manage.py load_house_scenario --reset   # recria exemplos sem instâncias
    python manage.py load_house_scenario --system-name "Casa 2.0"

Equivalente via Make (dentro do container middleware):
    make seed-house
"""
import json
import os
import sys
from pathlib import Path
from urllib.parse import urlsplit

import requests
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.test import Client
from django.db import transaction
from django.db.models.deletion import Collector

# Ordem de criação garante que modelos sem dependências venham antes.
# O parser lida com relacionamentos entre modelos na mesma especificação,
# mas criamos na ordem lógica para facilitar depuração.
MODEL_ORDER = [
    "LightBulb.json",
    "AirConditioner.json",
    "Irrigation.json",
    "Pump.json",
    "Garden.json",
    "Pool.json",
    "Room.json",
    "House.json",
]

# commands/ -> management/ -> orchestrator/ -> <project_root>
SCENARIOS_DIR = Path(__file__).resolve().parents[3] / "scenarios" / "house2.0"

SYSTEM_NAME = "House 2.0 Condominium"
SYSTEM_DESCRIPTION = (
    "Cenário House 2.0 — modelos DTDL de condomínio residencial com cômodos, "
    "piscina, jardim, irrigação, iluminação e ar condicionado. "
    "Fonte: https://github.com/Digital-Twins-RSBR/HouseScenario/tree/main/models/House2.0"
)


class _DjangoClientSession:
    def __init__(self, client):
        self.client = client

    def _path(self, url):
        parsed = urlsplit(url)
        path = parsed.path or "/"
        if parsed.query:
            path = f"{path}?{parsed.query}"
        return path

    def get(self, url, timeout=None):
        return _DjangoClientResponse(self.client.get(self._path(url)))

    def post(self, url, json=None, timeout=None):
        return _DjangoClientResponse(
            self.client.post(
                self._path(url),
                data=__import__("json").dumps(json or {}),
                content_type="application/json",
            )
        )


class _DjangoClientResponse:
    def __init__(self, response):
        self._response = response
        self.status_code = response.status_code
        self.text = response.content.decode("utf-8", errors="replace")

    def json(self):
        return json.loads(self.text or "null")

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}: {self.text}")


class Command(BaseCommand):
    help = "Carrega o cenário House 2.0 (SystemContext + DTDLModels) via API REST"

    def add_arguments(self, parser):
        parser.add_argument(
            "--base-url",
            default="http://localhost:8000",
            help="URL base da API (default: http://localhost:8000)",
        )
        parser.add_argument(
            "--system-name",
            default=SYSTEM_NAME,
            help=f"Nome do SystemContext a criar (default: '{SYSTEM_NAME}')",
        )
        parser.add_argument(
            "--reset",
            action="store_true",
            help="Recria apenas modelos originais da demo, recusando modelos editados ou em uso",
        )
        parser.add_argument(
            "--force",
            action="store_true",
            help="Alias obsoleto de --reset; use --reset",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Mostra o que seria criado sem fazer chamadas de escrita",
        )
        parser.add_argument(
            "--username",
            default=os.getenv("DJANGO_SUPERUSER_USERNAME", "middts"),
            help="Usuário Django usado para autenticar as chamadas da API (default: DJANGO_SUPERUSER_USERNAME ou middts)",
        )

    # ------------------------------------------------------------------
    def handle(self, *args, **options):
        base_url = options["base_url"].rstrip("/")
        system_name = options["system_name"]
        reset = options.get("reset", False) or options["force"]
        force = False  # Existing models are always checked; never blindly POST duplicates.
        if options["force"]:
            self.stdout.write(self.style.WARNING("--force foi substituido por --reset (com protecoes)."))
        dry_run = options["dry_run"]
        username = options["username"]
        session = self._build_authenticated_session(username)

        self.stdout.write(self.style.MIGRATE_HEADING("\n=== load_house_scenario ==="))
        self.stdout.write(f"  API base URL : {base_url}")
        self.stdout.write(f"  Sistema      : {system_name}")
        self.stdout.write(f"  Scenarios dir: {SCENARIOS_DIR}")
        if dry_run:
            self.stdout.write(self.style.WARNING("  ** DRY RUN — nenhuma escrita será feita **"))
        self.stdout.write("")

        # --- 1. Verificar acessibilidade da API ---
        self._check_api(session, base_url)

        # --- 2. Obter ou criar SystemContext ---
        system_id = self._get_or_create_system(
            session, base_url, system_name, force, dry_run
        )
        if dry_run and system_id is None:
            system_id = "<dry-run-id>"

        # Deletion and recreation share a transaction. A parser/API failure rolls back.
        with transaction.atomic():
            if reset and system_id != "<dry-run-id>":
                self._reset_example_models(system_id, username, dry_run)
            results = self._load_models(
                session, base_url, system_id, force, dry_run
            )
            self._print_summary(results, dry_run)

    def _reset_example_models(self, system_id, username, dry_run):
        from orchestrator.models import SystemContext, DTDLModel

        system = SystemContext.objects.select_for_update().get(pk=system_id)
        if system.description != SYSTEM_DESCRIPTION or not system.created_by or system.created_by.username != username:
            raise CommandError("Reset recusado: sistema nao identificado como demo criada por este usuario.")
        specs = {}
        for filename in MODEL_ORDER:
            path = SCENARIOS_DIR / filename
            if not path.exists():
                raise CommandError("Reset recusado: arquivos originais da demo incompletos.")
            spec = json.loads(path.read_text())
            specs[spec['@id']] = spec

        models = list(DTDLModel.objects.select_for_update().filter(
            system=system, dtdl_id__in=specs,
        ))
        if any(model.specification != specs[model.dtdl_id] for model in models):
            raise CommandError("Reset recusado: um modelo da demo foi editado. Nenhum modelo removido.")
        queryset = DTDLModel.objects.filter(pk__in=[model.pk for model in models])
        collector = Collector(using=queryset.db)
        collector.collect(queryset)
        allowed = {'orchestrator.dtdlmodel', 'orchestrator.modelelement', 'orchestrator.modelrelationship'}
        affected = {model._meta.label_lower for model, rows in collector.data.items() if rows}
        affected.update(qs.model._meta.label_lower for qs in collector.fast_deletes if qs.exists())
        if affected - allowed:
            raise CommandError("Reset recusado: modelos possuem instancias ou vinculos. Nenhum modelo removido.")
        self.stdout.write(f"Reset da demo: {len(models)} modelos originais; outros cadastros preservados.")
        if not dry_run:
            queryset.delete()

    # ------------------------------------------------------------------
    def _check_api(self, session, base_url):
        """Faz uma requisição GET simples para verificar que a API responde."""
        url = f"{base_url}/api/orchestrator/systems/"
        self.stdout.write(f"Verificando API em {url} ...")
        try:
            resp = session.get(url, timeout=10)
            resp.raise_for_status()
            self.stdout.write(self.style.SUCCESS(f"  ✓ API acessível (HTTP {resp.status_code})"))
        except requests.ConnectionError as exc:
            raise CommandError(
                f"Não foi possível conectar à API em {base_url}. "
                f"Certifique-se de que o middleware está rodando. Detalhe: {exc}"
            )
        except requests.HTTPError as exc:
            raise CommandError(f"API retornou erro: {exc}")

    def _build_authenticated_session(self, username):
        user = get_user_model().objects.filter(username=username).first()
        if not user:
            raise CommandError(
                f"Usuário '{username}' não encontrado. Ajuste --username ou garanta que o superuser exista."
            )
        client = Client()
        client.force_login(user)
        return _DjangoClientSession(client)

    # ------------------------------------------------------------------
    def _get_or_create_system(self, session, base_url, name, force, dry_run):
        """Retorna o ID do sistema existente ou cria um novo."""
        list_url = f"{base_url}/api/orchestrator/systems/"
        resp = session.get(list_url, timeout=10)
        resp.raise_for_status()
        systems = resp.json()

        matches = [s for s in systems if s["name"] == name]
        if len(matches) > 1:
            raise CommandError("Ha sistemas com nomes duplicados. Resolva a ambiguidade antes de carregar ou resetar a demo.")
        existing = matches[0] if matches else None

        if existing:
            system_id = existing["id"]
            if force:
                self.stdout.write(
                    self.style.WARNING(
                        f"Sistema '{name}' já existe (id={system_id}). "
                        "--force ativo: modelos serão recriados."
                    )
                )
            else:
                self.stdout.write(
                    self.style.SUCCESS(
                        f"Sistema '{name}' já existe (id={system_id}). "
                        "Use --reset para recriar os exemplos sem instancias."
                    )
                )
            return system_id

        self.stdout.write(f"Criando SystemContext '{name}' ...")
        if dry_run:
            self.stdout.write(self.style.WARNING("  [dry-run] POST /api/orchestrator/systems/ ignorado"))
            return None

        payload = {"name": name, "description": SYSTEM_DESCRIPTION}
        resp = session.post(
            f"{base_url}/api/orchestrator/systems/",
            json=payload,
            timeout=15,
        )
        if resp.status_code not in (200, 201):
            raise CommandError(
                f"Falha ao criar sistema: HTTP {resp.status_code} — {resp.text}"
            )
        system = resp.json()
        self.stdout.write(
            self.style.SUCCESS(f"  ✓ Sistema criado: {system['name']} (id={system['id']})")
        )
        return system["id"]

    # ------------------------------------------------------------------
    def _load_models(self, session, base_url, system_id, force, dry_run):
        """Itera sobre os JSONs e cria cada DTDLModel via API."""
        results = []

        for filename in MODEL_ORDER:
            json_path = SCENARIOS_DIR / filename
            if not json_path.exists():
                self.stdout.write(
                    self.style.WARNING(f"  ⚠ Arquivo não encontrado: {json_path} — pulando")
                )
                results.append({"file": filename, "status": "missing"})
                continue

            spec = json.loads(json_path.read_text())
            display_name = spec.get("displayName") or spec.get("@id", filename)

            self.stdout.write(f"\nProcessando {filename} ({display_name}) ...")

            if dry_run:
                self.stdout.write(
                    self.style.WARNING(
                        f"  [dry-run] POST /api/orchestrator/systems/{system_id}/dtdlmodels/ ignorado"
                    )
                )
                results.append({"file": filename, "model": display_name, "status": "dry-run"})
                continue

            # Verifica se já existe (GET list) — evita duplicata quando --force não passado
            if not force:
                existing_id = self._find_existing_model(session, base_url, system_id, spec.get("@id"))
                if existing_id is not None:
                    self.stdout.write(
                        self.style.SUCCESS(
                            f"  → já existe (id={existing_id}), pulando. Use --reset para recriar os exemplos sem instancias."
                        )
                    )
                    results.append({"file": filename, "model": display_name, "status": "skipped", "id": existing_id})
                    continue

            payload = {"name": display_name, "specification": spec}
            url = f"{base_url}/api/orchestrator/systems/{system_id}/dtdlmodels/"

            try:
                resp = session.post(url, json=payload, timeout=30)
            except requests.Timeout:
                self.stdout.write(
                    self.style.ERROR(f"  ✗ Timeout ao processar {filename}. Parser demorou demais?")
                )
                results.append({"file": filename, "model": display_name, "status": "timeout"})
                continue

            if resp.status_code not in (200, 201):
                self.stdout.write(
                    self.style.ERROR(
                        f"  ✗ HTTP {resp.status_code}: {resp.text[:300]}"
                    )
                )
                results.append(
                    {"file": filename, "model": display_name, "status": "error", "detail": resp.text[:300]}
                )
                continue

            model = resp.json()
            parsed = model.get("parsed_specification") or {}
            n_elements = len(parsed.get("modelElements", [])) if isinstance(parsed, dict) else "?"
            n_relations = len(parsed.get("modelRelationships", [])) if isinstance(parsed, dict) else "?"
            self.stdout.write(
                self.style.SUCCESS(
                    f"  ✓ {display_name} criado (id={model['id']}) — "
                    f"{n_elements} elements, {n_relations} relationships"
                )
            )
            results.append(
                {"file": filename, "model": display_name, "status": "created", "id": model["id"]}
            )

        return results

    # ------------------------------------------------------------------
    def _find_existing_model(self, session, base_url, system_id, dtdl_id):
        """Retorna o id do DTDLModel se já existir no sistema, ou None."""
        if not dtdl_id:
            return None
        url = f"{base_url}/api/orchestrator/systems/{system_id}/dtdlmodels/"
        try:
            resp = session.get(url, timeout=10)
            resp.raise_for_status()
            for m in resp.json():
                if m.get("dtdl_id") == dtdl_id:
                    return m["id"]
        except (requests.RequestException, ValueError, KeyError, TypeError) as exc:
            raise CommandError(
                "Nao foi possivel conferir os modelos existentes; carga interrompida para evitar duplicatas."
            ) from exc
        return None

    # ------------------------------------------------------------------
    def _print_summary(self, results, dry_run):
        self.stdout.write(self.style.MIGRATE_HEADING("\n=== Sumário ==="))
        counts = {"created": 0, "skipped": 0, "error": 0, "timeout": 0, "missing": 0, "dry-run": 0}
        for r in results:
            status = r["status"]
            counts[status] = counts.get(status, 0) + 1
            icon = {"created": "✓", "skipped": "→", "error": "✗", "timeout": "⏱", "missing": "⚠", "dry-run": "○"}.get(status, "?")
            colour = {
                "created": self.style.SUCCESS,
                "skipped": self.style.SUCCESS,
                "error": self.style.ERROR,
                "timeout": self.style.ERROR,
                "missing": self.style.WARNING,
                "dry-run": self.style.WARNING,
            }.get(status, str)
            self.stdout.write(colour(f"  {icon} [{status:8s}] {r.get('model', r['file'])}"))
        self.stdout.write("")
        self.stdout.write(f"  Total: {len(results)} modelos | "
                          f"criados={counts['created']} skipped={counts['skipped']} "
                          f"errors={counts['error'] + counts['timeout'] + counts['missing']}")
        if dry_run:
            self.stdout.write(self.style.WARNING("\n  (dry-run — nenhuma mudança persistida)"))
        elif counts["error"] + counts["timeout"] + counts["missing"] > 0:
            self.stdout.write(self.style.ERROR("\n  Atenção: alguns modelos falharam. Verifique os logs acima."))
            sys.exit(1)
        else:
            self.stdout.write(self.style.SUCCESS("\n  Cenário House 2.0 carregado com sucesso!"))
