"""
Role-Based Access Control.

Roles are coarse (stored on the user row); permissions are fine-grained and
checked by endpoints. Endpoints never test roles directly — they ask for a
permission — so adding a role is a one-line change here.

Resource *ownership* (is this my project?) is a separate check performed in
`app.api.deps`; RBAC answers "may this kind of user do this kind of thing",
ownership answers "on this particular row".
"""
from __future__ import annotations

from enum import StrEnum


class Role(StrEnum):
    USER = "user"
    ORG_MEMBER = "org_member"
    ORG_ADMIN = "org_admin"
    SUPPORT = "support"
    ADMIN = "admin"


class Perm(StrEnum):
    # own content
    PROJECTS_RW_OWN = "projects:rw:own"
    PALETTES_RW_OWN = "palettes:rw:own"
    AI_USE = "ai:use"
    BILLING_MANAGE_OWN = "billing:manage:own"
    # organization scope
    PROJECTS_READ_ORG = "projects:read:org"
    ORG_MANAGE = "org:manage"                 # seats, invites, org billing
    CATALOG_MANAGE_ORG = "catalog:manage:org" # private brand catalogs
    # platform scope
    PROJECTS_READ_ANY = "projects:read:any"
    USERS_READ_ANY = "users:read:any"
    USERS_MANAGE = "users:manage"
    CATALOG_MANAGE_GLOBAL = "catalog:manage:global"


_BASE = {Perm.PROJECTS_RW_OWN, Perm.PALETTES_RW_OWN, Perm.AI_USE, Perm.BILLING_MANAGE_OWN}

ROLE_PERMISSIONS: dict[Role, frozenset[Perm]] = {
    Role.USER: frozenset(_BASE),
    # Org members are billed through the org, so they cannot manage billing themselves.
    Role.ORG_MEMBER: frozenset(_BASE - {Perm.BILLING_MANAGE_OWN}),
    Role.ORG_ADMIN: frozenset(_BASE | {Perm.PROJECTS_READ_ORG, Perm.ORG_MANAGE, Perm.CATALOG_MANAGE_ORG}),
    Role.SUPPORT: frozenset({Perm.PROJECTS_READ_ANY, Perm.USERS_READ_ANY}),
    Role.ADMIN: frozenset(set(Perm)),
}


def has_permission(role: str, perm: Perm) -> bool:
    try:
        return perm in ROLE_PERMISSIONS[Role(role)]
    except ValueError:
        return False
