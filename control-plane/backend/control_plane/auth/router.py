"""Auth API endpoints."""

import logging
import os

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from control_plane.auth.dependencies import get_current_user, require_role
from control_plane.auth.login_rate_limit import (
    clear_login_account_window,
    enforce_login_rate_limit,
)
from control_plane.auth.models import User
from control_plane.auth.schemas import (
    AuthUser,
    LoginRequest,
    LoginResponse,
    UserCreate,
    UserResponse,
)
from control_plane.auth.service import (
    create_access_token,
    hash_password,
    validate_local_password,
    verify_password,
)
from control_plane.database import get_db
from control_plane.env import configured_org_id, is_production, tenancy_mode
from control_plane.models.database import Organization

log = logging.getLogger("control_plane.auth")

router = APIRouter(prefix="/api/auth", tags=["auth"])

_seeded = False


async def _seed_admin(db: AsyncSession) -> None:
    """Create default admin user if no users exist."""
    global _seeded
    if _seeded:
        return
    org_id = configured_org_id()
    if await db.get(Organization, org_id) is None:
        db.add(Organization(id=org_id, name=org_id))
        await db.flush()
    result = await db.execute(
        select(User).where(User.org_id == org_id).limit(1)
    )
    if result.scalar_one_or_none() is None:
        # In production the initial admin password must be supplied explicitly —
        # never silently seed the well-known admin/admin credential. In dev/demo
        # we keep 'admin' for convenience.
        admin_email = os.environ.get("OSTIARI_ADMIN_EMAIL", "admin@ostiari.ai")
        admin_password = os.environ.get("OSTIARI_ADMIN_PASSWORD", "").strip()
        if is_production():
            if not admin_password:
                raise RuntimeError(
                    "OSTIARI_ADMIN_PASSWORD must be set in production "
                    "(OSTIARI_ENV=production) — refusing to seed the default admin/admin."
                )
        else:
            admin_password = admin_password or "admin"
        admin = User(
            email=admin_email,
            name="Admin",
            hashed_password=hash_password(admin_password),
            role="admin",
            is_active=True,
            org_id=org_id,
        )
        db.add(admin)
        await db.flush()
        log.info("Seeded initial admin user %s", admin_email)
    _seeded = True


@router.post("/login", response_model=LoginResponse)
async def login(
    body: LoginRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Authenticate user and return JWT."""
    await _seed_admin(db)
    if tenancy_mode() == "multi" and not body.org_id:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="org_id is required for local login in multi-tenant mode",
        )
    login_org = body.org_id or configured_org_id()
    account_key = await enforce_login_rate_limit(
        request,
        f"{login_org}:{body.email}",
        db,
    )
    result = await db.execute(
        select(User).where(
            User.org_id == login_org,
            User.email == body.email,
        )
    )
    user = result.scalar_one_or_none()
    if (
        not user
        or not user.hashed_password
        or not verify_password(body.password, user.hashed_password)
        or not user.is_active
    ):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials")
    await clear_login_account_window(db, account_key)
    token = create_access_token(
        user.id,
        user.email,
        user.role,
        org=user.org_id or configured_org_id(),
    )
    return LoginResponse(
        access_token=token,
        user=UserResponse(id=user.id, email=user.email, name=user.name, role=user.role),
    )


@router.post("/register", response_model=UserResponse, status_code=status.HTTP_201_CREATED)
async def register(
    body: UserCreate,
    user: AuthUser = Depends(require_role("admin")),
    db: AsyncSession = Depends(get_db),
):
    """Create a new user (admin only)."""
    try:
        validate_local_password(body.password)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(exc),
        ) from exc
    user_org = getattr(user, "tenant_id", None) or configured_org_id()
    existing = await db.execute(
        select(User).where(
            User.org_id == user_org,
            User.email == body.email,
        )
    )
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Email already registered")
    new_user = User(
        email=body.email,
        name=body.name,
        hashed_password=hash_password(body.password),
        role=body.role,
        is_active=True,
        # New users join the creating admin's org.
        org_id=user_org,
    )
    db.add(new_user)
    await db.flush()
    return UserResponse(id=new_user.id, email=new_user.email, name=new_user.name, role=new_user.role)


@router.get("/me", response_model=UserResponse)
async def me(
    current_user: AuthUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Get current authenticated user info.

    Local users are looked up in the DB. Externally-authenticated (OIDC)
    principals aren't DB rows, so return the identity straight from the
    validated token instead of 404ing.
    """
    if current_user.id:
        result = await db.execute(
            select(User).where(
                User.id == current_user.id,
                User.org_id == current_user.tenant_id,
            )
        )
        user = result.scalar_one_or_none()
        if user:
            return UserResponse(id=user.id, email=user.email, name=user.name, role=user.role)

    # External / OIDC principal — echo the token-derived identity.
    if current_user.subject:
        return UserResponse(id=0, email=current_user.email,
                            name=current_user.email, role=current_user.role)

    raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")


@router.get("/users", response_model=list[UserResponse])
async def list_users(
    user: AuthUser = Depends(require_role("admin")),
    db: AsyncSession = Depends(get_db),
):
    """List all users (admin only)."""
    result = await db.execute(
        select(User)
        .where(User.org_id == user.tenant_id)
        .order_by(User.id)
    )
    users = result.scalars().all()
    return [UserResponse(id=u.id, email=u.email, name=u.name, role=u.role) for u in users]


@router.delete("/users/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_user(
    user_id: int,
    user: AuthUser = Depends(require_role("admin")),
    db: AsyncSession = Depends(get_db),
):
    """Delete a user (admin only)."""
    if user.id == user_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Cannot delete yourself")
    result = await db.execute(
        select(User).where(
            User.id == user_id,
            User.org_id == user.tenant_id,
        )
    )
    target = result.scalar_one_or_none()
    if not target:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    await db.delete(target)
