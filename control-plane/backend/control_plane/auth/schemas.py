"""Pydantic schemas for authentication."""

from pydantic import BaseModel

from control_plane.auth.roles import Role


class LoginRequest(BaseModel):
    email: str
    password: str
    org_id: str | None = None


class UserResponse(BaseModel):
    id: int
    email: str
    name: str
    role: Role

    model_config = {"from_attributes": True}


class LoginResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: UserResponse


class UserCreate(BaseModel):
    email: str
    name: str
    password: str
    role: Role = "viewer"


class AuthUser(BaseModel):
    """Lightweight user/principal object for request context.

    `id` is an int for local DB users; for external OIDC principals (whose
    subject is a UUID/string) it falls back to 0 and the real identifier is in
    `subject`. `kind` distinguishes an interactive user from a machine (service
    or agent) principal authenticated via client-credentials.
    """
    id: int
    email: str
    role: Role
    subject: str = ""            # raw IdP 'sub' / client_id (esp. for OIDC principals)
    kind: str = "user"           # user | service | agent
    tenant_id: str = "default"   # verified tenant claim or configured single tenant
