import json
from django.conf import settings
from django.contrib.auth import authenticate, get_user_model
from django.http import JsonResponse
from django.shortcuts import get_object_or_404
from ninja import Router, NinjaAPI
from typing import List
import requests
from datetime import datetime, timedelta
from .models import GatewayIOT, Organization, OrganizationMembership
from .schemas import AddOrganizationMemberSchema, CreateGatewayIOTSchema, CreateOrganizationSchema, CreateUserSchema, GatewayIOTSchema, OrganizationSchema
from rest_framework_simplejwt.tokens import RefreshToken


router = Router()
api = NinjaAPI()



def get_user_organizations(user):
    if getattr(user, "is_superuser", False):
        return Organization.objects.all()
    if not user or not getattr(user, "is_authenticated", False):
        return Organization.objects.none()
    return Organization.objects.filter(memberships__user=user).distinct()


def resolve_current_organization(request, organization_id: int = None):
    user = getattr(request, "user", None)
    organizations = get_user_organizations(user)
    if organization_id is not None:
        organization = organizations.filter(id=organization_id).first()
        if not organization:
            return None, api.create_response(request, {"detail": "Organization not accessible"}, status=403)
        return organization, None
    count = organizations.count()
    if count == 1:
        return organizations.first(), None
    if count == 0:
        return None, api.create_response(request, {"detail": "User has no organization"}, status=400)
    return None, api.create_response(request, {"detail": "Multiple organizations available; provide organization_id"}, status=400)


def validate_membership_role(role: str):
    valid_roles = {choice[0] for choice in OrganizationMembership.ROLE_CHOICES}
    return role in valid_roles


@router.post(
    "/users/",
    tags=['Core'],
    openapi_extra={
        "requestBody": {
            "content": {
                "application/json": {
                    "examples": {
                        "default": {
                            "value": {
                                "username": "operator01",
                                "password": "strongpass123",
                                "email": "operator01@example.com",
                                "first_name": "Ana",
                                "last_name": "Silva",
                                "is_staff": True,
                                "organization_id": 1,
                                "role": "member"
                            }
                        }
                    }
                }
            }
        }
    },
)
def create_user(request, payload: CreateUserSchema):
    User = get_user_model()
    if User.objects.filter(username=payload.username).exists():
        return api.create_response(request, {"detail": "username already exists"}, status=400)
    if not validate_membership_role(payload.role):
        return api.create_response(request, {"detail": f"invalid role: {payload.role}"}, status=400)

    target_organization = None
    request_user = getattr(request, "user", None)
    if request_user and getattr(request_user, "is_authenticated", False):
        target_organization, error_response = resolve_current_organization(request, organization_id=payload.organization_id)
        if error_response:
            return error_response
    elif payload.organization_id is not None:
        target_organization = Organization.objects.filter(id=payload.organization_id).first()
    if target_organization is None:
        target_organization = Organization.objects.filter(name="Default").first()

    if payload.role == OrganizationMembership.ROLE_ADMIN:
        if target_organization is None:
            return api.create_response(request, {"detail": "Organization required for admin role"}, status=400)
        if not request_user or not getattr(request_user, "is_authenticated", False):
            return api.create_response(request, {"detail": "Authentication required to assign admin role"}, status=403)
        if not getattr(request_user, "is_superuser", False):
            is_org_admin = OrganizationMembership.objects.filter(
                user=request_user,
                organization=target_organization,
                role=OrganizationMembership.ROLE_ADMIN,
            ).exists()
            if not is_org_admin:
                return api.create_response(request, {"detail": "Only organization admins or superusers can assign admin role"}, status=403)

    user = User.objects.create_user(
        username=payload.username,
        password=payload.password,
        email=payload.email,
        first_name=payload.first_name,
        last_name=payload.last_name,
    )
    user.is_staff = bool(payload.is_staff)
    user.save()

    if target_organization is not None:
        OrganizationMembership.objects.get_or_create(
            user=user,
            organization=target_organization,
            defaults={"role": payload.role},
        )
    return {"id": user.id, "username": user.username, "is_staff": user.is_staff, "organization_id": target_organization.id if target_organization else None, "role": payload.role if target_organization else None}


@router.post(
    "/organizations/",
    response=OrganizationSchema,
    tags=['Core'],
    openapi_extra={
        "requestBody": {
            "content": {
                "application/json": {
                    "examples": {
                        "default": {
                            "value": {
                                "name": "Smart Building Org",
                                "description": "Organization for DT and IoT assets"
                            }
                        }
                    }
                }
            }
        }
    },
)
def create_organization(request, payload: CreateOrganizationSchema):
    user = getattr(request, "user", None)
    if not user or not getattr(user, "is_authenticated", False) or not getattr(user, "is_superuser", False):
        return api.create_response(request, {"detail": "Superuser required"}, status=403)
    organization = Organization.objects.create(created_by=user, **payload.dict())
    OrganizationMembership.objects.get_or_create(
        user=user,
        organization=organization,
        defaults={"role": OrganizationMembership.ROLE_ADMIN},
    )
    return organization


@router.get("/organizations/", response=List[OrganizationSchema], tags=['Core'])
def list_organizations(request):
    return list(get_user_organizations(getattr(request, "user", None)))


@router.post(
    "/organizations/{organization_id}/members/",
    tags=['Core'],
    openapi_extra={
        "requestBody": {
            "content": {
                "application/json": {
                    "examples": {
                        "default": {
                            "value": {
                                "user_id": 2,
                                "role": "member"
                            }
                        }
                    }
                }
            }
        }
    },
)
def add_organization_member(request, organization_id: int, payload: AddOrganizationMemberSchema):
    user = getattr(request, "user", None)
    if not user or not getattr(user, "is_authenticated", False):
        return api.create_response(request, {"detail": "Authentication required"}, status=403)
    organization = get_object_or_404(Organization, id=organization_id)
    if not getattr(user, "is_superuser", False):
        member_exists = OrganizationMembership.objects.filter(user=user, organization=organization).exists()
        if not member_exists:
            return api.create_response(request, {"detail": "Organization not accessible"}, status=403)
    target_user = get_object_or_404(get_user_model(), id=payload.user_id)
    membership, created = OrganizationMembership.objects.update_or_create(
        user=target_user,
        organization=organization,
        defaults={"role": payload.role},
    )
    return {"organization_id": organization.id, "user_id": target_user.id, "role": membership.role, "created": created}

@router.post("/token/", response=dict, tags=['Auth'])
def login(request, username: str, password: str):
    # Deprecated: prefer /token/ handled by SimpleJWT `obtain_token` below.
    user = authenticate(request, username=username, password=password)
    if not user:
        return {"error": "Invalid credentials"}, 400
    # Return a simple acknowledgement; use the SimpleJWT endpoints instead.
    return {"detail": "Use /api/core/token/ to obtain JWT via SimpleJWT."}

@router.post(
    "/gatewaysiot/",
    response=GatewayIOTSchema,
    tags=['Core'],
    openapi_extra={
        "requestBody": {
            "content": {
                "application/json": {
                    "examples": {
                        "jwt_gateway": {
                            "value": {
                                "name": "ThingsBoard Main Gateway",
                                "url": "http://thingsboard:9090",
                                "auth_method": "jwt",
                                "username": "tenant@thingsboard.org",
                                "password": "tenant",
                                "api_key": None
                            }
                        },
                        "api_key_gateway": {
                            "value": {
                                "name": "ThingsBoard Edge Gateway",
                                "url": "http://thingsboard:9090",
                                "auth_method": "api_key",
                                "username": None,
                                "password": None,
                                "api_key": "TB_API_KEY_EXAMPLE"
                            }
                        }
                    }
                }
            }
        }
    },
)
def create_gateway(request, payload: CreateGatewayIOTSchema, organization_id: int = None):
    payload_data = payload.dict()
    user = getattr(request, "user", None)
    if not user or not getattr(user, "is_authenticated", False):
        return api.create_response(request, {"detail": "Authentication required"}, status=403)
    organization, error_response = resolve_current_organization(request, organization_id=organization_id)
    if error_response:
        return error_response
    payload_data['organization'] = organization
    payload_data['created_by'] = user
    gateway = GatewayIOT.objects.create(**payload_data)
    return gateway

@router.get("/gatewayiot/{gatewayiot_id}/", response=GatewayIOTSchema, tags=['Core'])
def get_gatewayiot(request, gatewayiot_id: int):
    user = getattr(request, "user", None)
    queryset = GatewayIOT.objects.all()
    if not getattr(user, "is_superuser", False):
        queryset = queryset.filter(organization__memberships__user=user).distinct()
    gateway = get_object_or_404(queryset, id=gatewayiot_id)
    return gateway

@router.get("/gatewaysiot/", response=List[GatewayIOTSchema], tags=['Core'])
def list_gateways(request):
    user = getattr(request, "user", None)
    gateways = GatewayIOT.objects.all()
    if not getattr(user, "is_superuser", False):
        gateways = gateways.filter(organization__memberships__user=user).distinct()
    return gateways


def get_gateway_auth_headers(request, gateway_id: int):
    gateway_qs = GatewayIOT.objects.all()
    user = getattr(request, "user", None)
    if request is not None and not getattr(user, "is_superuser", False):
        if not user or not getattr(user, "is_authenticated", False):
            gateway_qs = gateway_qs.none()
        else:
            gateway_qs = gateway_qs.filter(organization__memberships__user=user).distinct()
    gateway = get_object_or_404(gateway_qs, id=gateway_id)

    if gateway.auth_method == GatewayIOT.AUTH_METHOD_API_KEY:
        if not gateway.api_key:
            return {"error": "Gateway API key is not configured."}, 400
        return {
            "headers": {
                "Content-Type": "application/json",
                "X-Authorization": f"ApiKey {gateway.api_key}",
            },
            "token_type": "api_key",
        }, 200

    if not gateway.username or not gateway.password:
        return {"error": "Gateway username/password are not configured."}, 400

    url = f"{gateway.url}/api/auth/login"
    payload = {
        "username": gateway.username,
        "password": gateway.password
    }
    headers = {
        "Content-Type": "application/json"
    }
    response = requests.post(url, headers=headers, data=json.dumps(payload))
    if response.status_code != 200:
        return {
            "error": f"Error obtaining JWT token: {response.status_code}, {response.text}"
        }, 400

    token = response.json().get("token")
    if not token:
        return {"error": "ThingsBoard login response has no token."}, 400

    return {
        "headers": {
            "Content-Type": "application/json",
            "X-Authorization": f"Bearer {token}",
        },
        "token": token,
        "token_type": "bearer",
    }, 200

@router.get("/gatewayiot/{gateway_id}/jwt/", response={200: dict, 400: dict}, tags=['Core'])
def get_jwt_token_gateway(request, gateway_id: int):
    auth_response, status_code = get_gateway_auth_headers(request, gateway_id)
    if status_code != 200:
        return auth_response, status_code
    token = auth_response.get("token")
    if not token:
        return {
            "error": "Gateway is configured with API key; no JWT token to return.",
            "token_type": auth_response.get("token_type"),
        }, 400
    return {"token": token, "token_type": auth_response.get("token_type")}, 200


@router.get("/gatewayiot/{gateway_id}/check/", response={200: dict, 400: dict}, tags=['Core'])
def check_gateway_access(request, gateway_id: int):
    user = getattr(request, "user", None)
    queryset = GatewayIOT.objects.all()
    if request is not None and not getattr(user, "is_superuser", False):
        if not user or not getattr(user, "is_authenticated", False):
            queryset = queryset.none()
        else:
            queryset = queryset.filter(organization__memberships__user=user).distinct()
    gateway = get_object_or_404(queryset, id=gateway_id)
    auth_response, status_code = get_gateway_auth_headers(request, gateway_id)
    if status_code != 200:
        return auth_response, status_code

    response = requests.get(f"{gateway.url}/api/auth/user", headers=auth_response["headers"])
    if response.status_code == 200:
        return {
            "ok": True,
            "gateway": gateway.name,
            "auth_method": gateway.auth_method,
        }, 200
    return {
        "ok": False,
        "error": f"Gateway check failed: {response.status_code}, {response.text}",
    }, 400

# Middleware to validate JWT tokens will be implemented separately.
@router.post(
    "/token/",
    tags=["Authentication"],
    summary="Obtain JWT access and refresh tokens",
    description="Example request: POST /api/core/token/?username=middts&password=middts",
)
def obtain_token(request, username: str, password: str):
    user = authenticate(username=username, password=password)
    if user is not None:
        refresh = RefreshToken.for_user(user)
        return {
            "refresh": str(refresh),
            "access": str(refresh.access_token),
        }
    return JsonResponse({"detail": "Invalid credentials"}, status=401)

@router.post(
    "/token/refresh/",
    tags=["Authentication"],
    summary="Refresh JWT access token",
    description="Example request: POST /api/core/token/refresh/?refresh=<refresh_token>",
)
def refresh_token(request, refresh: str):
    try:
        refresh_token = RefreshToken(refresh)
        return {
            "access": str(refresh_token.access_token),
        }
    except Exception:
        return JsonResponse({"detail": "Invalid refresh token"}, status=401)

@router.get("/protected-endpoint/", tags=["Protected"])
def protected_endpoint(request):
    return JsonResponse({"message": "This is a protected endpoint."})