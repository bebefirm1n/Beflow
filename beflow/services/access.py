from __future__ import annotations
import discord


def clone_overwrites(channel) -> dict:
    return {target: discord.PermissionOverwrite.from_pair(*overwrite.pair())
            for target, overwrite in channel.overwrites.items()}


def identity(target) -> str:
    return ("role" if isinstance(target, discord.Role) else "member") + ":" + str(target.id)


def _target_from_key(guild, key, overwrites):
    for target in overwrites:
        if identity(target) == key:
            return target
    kind, _, ident = key.partition(":")
    try:
        id_ = int(ident)
    except ValueError:
        return None
    return guild.get_role(id_) if kind == "role" else guild.get_member(id_)


def restore_field(overwrites, guild, field: str, snapshots: dict) -> dict:
    for key, previous_value in snapshots.items():
        target = _target_from_key(guild, key, overwrites)
        if target is None:
            continue
        overwrite = overwrites.get(target, discord.PermissionOverwrite())
        setattr(overwrite, field, previous_value)
        if overwrite.is_empty():
            overwrites.pop(target, None)
        else:
            overwrites[target] = overwrite
    return overwrites


def apply_channel_restriction(overwrites: dict, guild, field: str, owner_id: int,
                              exempt_member_ids=None) -> tuple[dict, dict]:
    """Deny everyone, strip explicit allowances on other roles/members.

    Returns (new overwrites, snapshot). To undo, pass snapshot to restore_field.
    """
    snapshot = {}
    exempt_ids = set(exempt_member_ids or ())
    for target, overwrite in list(overwrites.items()):
        if not isinstance(target, discord.Role) and (target.id == owner_id or target.id in exempt_ids):
            continue
        previous = getattr(overwrite, field)
        if target.id == guild.default_role.id or previous is True:
            snapshot[identity(target)] = previous
            # Let @everyone's deny apply instead of an allow overriding it.
            setattr(overwrite, field, False if target.id == guild.default_role.id else None)
            if overwrite.is_empty():
                overwrites.pop(target, None)
            else:
                overwrites[target] = overwrite
    everyone = guild.default_role
    if identity(everyone) not in snapshot:
        ow = overwrites.get(everyone, discord.PermissionOverwrite())
        snapshot[identity(everyone)] = getattr(ow, field)
        setattr(ow, field, False)
        overwrites[everyone] = ow
    return overwrites, snapshot


def restrict_category(overwrites: dict, guild, chosen_roles: list, previous: dict | None) -> tuple[dict, dict]:
    """Apply a role allowlist, preserving original permission bits for rollback.

    Membership explicit allows are neutralized; admins always bypass Discord
    channel denies. Previous managed restrictions are first reverted in-memory.
    """
    if previous:
        for field, snapshots in previous.get("permission_snapshots", {}).items():
            restore_field(overwrites, guild, field, snapshots)

    selected_ids = {r.id for r in chosen_roles}
    snapshots = {"view_channel": {}, "connect": {}}

    def mutate(target, field, value):
        ow = overwrites.get(target, discord.PermissionOverwrite())
        old = getattr(ow, field)
        if old == value:
            return
        snapshots[field][identity(target)] = old
        setattr(ow, field, value)
        if ow.is_empty():
            overwrites.pop(target, None)
        else:
            overwrites[target] = ow

    mutate(guild.default_role, "view_channel", False)
    for target, ow in list(overwrites.items()):
        if target.id == guild.default_role.id and isinstance(target, discord.Role):
            continue
        if isinstance(target, discord.Role) and target.id in selected_ids:
            continue
        if ow.view_channel is True:
            mutate(target, "view_channel", None)
        # Nonselected role connect permission is irrelevant if view is denied.
    for role in chosen_roles:
        mutate(role, "view_channel", True)
        mutate(role, "connect", True)

    return overwrites, snapshots
