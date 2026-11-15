"""Paramètres du panneau membre, indépendants du stockage et de Discord.

Tous les boutons V2.1 sont activés par défaut pour conserver une installation
fonctionnelle sur les serveurs qui utilisaient déjà la V2.0.
"""

PANEL_FEATURES = {
    "autoriser": "Autoriser / retirer un accès privé",
    "expulser": "Expulser un membre du vocal",
    "transferer": "Transférer la propriété",
    "informations": "Afficher les informations du vocal",
}


def panel_features(gconf: dict) -> dict:
    """Valeurs effectives; sans mutation implicite pendant une simple lecture."""
    configured = gconf.get("panel_features") or {}
    return {key: configured.get(key, True) is not False for key in PANEL_FEATURES}


def feature_enabled(gconf: dict, key: str) -> bool:
    if key not in PANEL_FEATURES:
        return False
    return panel_features(gconf)[key]


def toggle_feature(gconf: dict, key: str) -> bool:
    if key not in PANEL_FEATURES:
        raise KeyError(key)
    settings = gconf.setdefault("panel_features", {})
    new_value = not feature_enabled(gconf, key)
    settings[key] = new_value
    return new_value


def guest_access(info: dict) -> dict:
    """Snapshots des autorisations individuelles gérées par le bot."""
    return info.setdefault("guest_overwrites", {})
