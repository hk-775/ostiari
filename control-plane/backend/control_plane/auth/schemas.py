"""Pydantic schemas for authentication."""

from pydantic import BaseModel, EmailStr


class LoginRequest(BaseModel):
    email: str
    password: str


class UserResponse(BaseModel):
    id: int
    email: str
    name: str
    role: str

    model_config = {"from_attributes": True}


class LoginResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: UserResponse


class UserCreate(BaseModel):
    email: str
    name: str
    password: str
    role: str = "viewer"


class AuthUser(BaseModel):
    """Lightweight user object for request context."""
    id: int
    email: str
    role: str
