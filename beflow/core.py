import discord
from discord import app_commands
from discord.ext import commands
import os
import re
import asyncio
import logging
from dotenv import load_dotenv
from beflow.storage import ConfigStore
from beflow.services.lifecycle import creation_locks, channel_locks, creation_cooldowns
from beflow.services.access import (clone_overwrites, apply_channel_restriction, restore_field,
                                    restrict_category, identity)

logging.basicConfig(level=logging.INFO)

try:
    from keep_alive import keep_alive
    KEEP_ALIVE_AVAILABLE = True
except ImportError:
    KEEP_ALIVE_AVAILABLE = False

load_dotenv()
TOKEN = os.getenv("TOKEN_BOT")

CLIENT_ID = os.getenv("CLIENT_ID", "1495103873373704472")
PERMISSIONS_INVITATION = discord.Permissions(
    manage_channels=True, manage_roles=True, move_members=True,
    view_channel=True, connect=True, send_messages=True,
    embed_links=True, read_message_history=True, manage_messages=True,
)
LIEN_INVITATION = (
    f"https://discord.com/oauth2/authorize?client_id={CLIENT_ID}"
    f"&permissions={PERMISSIONS_INVITATION.value}"
    "&integration_type=0&scope=bot+applications.commands"
)

# ═══════════════════════════════════════════════════════════
#  RÉGLAGES
# ═══════════════════════════════════════════════════════════
DUREE_CHOIX = 60            # secondes laissées au membre pour choisir un type
DELAI_NETTOYAGE = 3         # secondes d'attente avant de vérifier si un vocal temp est vide
DELAI_SUPPRESSION_MSG = 20  # secondes avant de supprimer le message une fois le résultat connu
CONFIG_PATH = os.getenv("LEGACY_CONFIG_PATH", "vocaux_config.json")
DATABASE_PATH = os.getenv("DATABASE_PATH", "data/beflow.sqlite3")
STORE = ConfigStore(DATABASE_PATH, CONFIG_PATH)

# ═══════════════════════════════════════════════════════════
#  PERSISTENCE (fichier JSON simple : hubs, types, vocaux temp)
#
#  Structure d'un serveur :
#  {
#    "hubs": {"<channel_id>": {"types": ["Duo", "Trio"], "categorie_cible": "<id>"}, ...},
#    "types": {"<nom>": {"limite":..., "modifiable_nom":..., "modifiable_limite":...,
#                        "masque_defaut":..., "format_nom":..., "emoji":...}, ...},
#    "temp_channels": {"<channel_id>": {"owner_id":..., "type":..., "masque":..., "verrou":...}, ...},
#    "categories": {"<category_id>": {"roles": ["<role_id>", ...]}, ...},
#    "banwords": ["mot1", "mot2"],
#  }
# ═══════════════════════════════════════════════════════════
def charger_configuration():
    return STORE.load()


def sauvegarder_configuration():
    STORE.save(configuration)


configuration = charger_configuration()


def migrer_configuration():
    """Convertit l'ancien format (hubs = liste de salons, types partagés par tout
    le serveur) vers le nouveau format (hubs = dict avec la liste des types
    autorisés dans chaque hub). Les hubs existants récupèrent tous les types déjà
    définis, pour ne rien casser au premier démarrage après mise à jour."""
    modifie = False
    for gconf in configuration.values():
        if isinstance(gconf.get("hubs"), list):
            types_existants = list(gconf.get("types", {}).keys())
            gconf["hubs"] = {cid: {"types": list(types_existants)} for cid in gconf["hubs"]}
            modifie = True
    if modifie:
        sauvegarder_configuration()


migrer_configuration()


def config_serveur(guild_id: int) -> dict:
    """Retourne (et initialise si besoin) la config d'un serveur."""
    gid = str(guild_id)
    if gid not in configuration:
        configuration[gid] = {"hubs": {}, "types": {}, "temp_channels": {}, "categories": {}, "banwords": []}
    gconf = configuration[gid]
    gconf.setdefault("hubs", {})
    gconf.setdefault("types", {})
    gconf.setdefault("temp_channels", {})
    gconf.setdefault("categories", {})
    gconf.setdefault("banwords", [])
    if isinstance(gconf["hubs"], list):
        gconf["hubs"] = {cid: {"types": list(gconf["types"].keys())} for cid in gconf["hubs"]}
    return gconf


# ═══════════════════════════════════════════════════════════
#  UTILITAIRES
# ═══════════════════════════════════════════════════════════
RE_EMOJI_CUSTOM = re.compile(r"^<a?:\w{2,32}:\d{17,20}>$")


def emoji_valide(v: str) -> bool:
    """Vrai si v est vide, un emoji personnalisé <:nom:id>, ou un emoji Unicode (non ASCII)."""
    return not v or bool(RE_EMOJI_CUSTOM.match(v)) or (not v.isascii() and len(v) <= 16)


def emoji_type(info: dict) -> str:
    """Emoji configuré pour un type de vocal, avec repli sur 🔊 si aucun n'est défini
    (ou s'il est invalide, pour ne jamais casser un menu Discord)."""
    e = info.get("emoji")
    return e if e and emoji_valide(e) else "🔊"


def copie_overwrites(categorie):
    """Copie indépendante des overwrites d'une catégorie
    (sinon on modifierait directement ceux de la catégorie)."""
    if not categorie:
        return {}
    return clone_overwrites(categorie)


def valeur_categorie(salon, cible, attr):
    """Valeur de base héritée de la catégorie du salon (None si aucune)."""
    if not salon.category:
        return None
    return getattr(salon.category.overwrites_for(cible), attr)


def hubs_valides(guild: discord.Guild, gconf: dict) -> list:
    """Liste (id, salon) des hubs dont le salon existe encore."""
    resultat = []
    for cid in gconf["hubs"]:
        salon = guild.get_channel(int(cid))
        if salon:
            resultat.append((cid, salon))
    return resultat


# ═══════════════════════════════════════════════════════════
#  BOT — pas besoin d'intents privilégiés (members/presence)
# ═══════════════════════════════════════════════════════════
intents = discord.Intents.default()
intents.voice_states = True

bot = commands.Bot(command_prefix="!", intents=intents, help_command=None,
                   allowed_mentions=discord.AllowedMentions.none())
arbre = bot.tree

# Menus de choix de type en attente : (guild_id, member_id) -> ChoixTypeView
choix_en_cours: dict[tuple[int, int], "ChoixTypeView"] = {}


async def setup_hook():
    # Vue persistante : nécessaire pour que les boutons du panneau vocal
    # continuent de fonctionner après un redémarrage du bot.
    # setup_hook n'est appelé qu'une seule fois (contrairement à on_ready).
    bot.add_view(PanelVocalView())
    if os.getenv("SYNC_COMMANDS", "1") == "1":
        await arbre.sync()


bot.setup_hook = setup_hook


@bot.event
async def on_ready():
    # on_ready peut se déclencher plusieurs fois (reconnexions) : on n'initialise qu'une fois
    if getattr(bot, "_init_ok", False):
        return
    bot._init_ok = True

    # Nettoyage au démarrage : hubs/vocaux supprimés pendant que le bot était offline
    for gid, gconf in list(configuration.items()):
        guild = bot.get_guild(int(gid))
        if not guild:
            continue
        gconf = config_serveur(int(gid))
        gconf["hubs"] = {
            cid: info for cid, info in gconf["hubs"].items() if guild.get_channel(int(cid))
        }
        gconf["categories"] = {
            cid: info for cid, info in gconf["categories"].items() if guild.get_channel(int(cid))
        }
        for cid in list(gconf["temp_channels"].keys()):
            salon = guild.get_channel(int(cid))
            if not salon:
                gconf["temp_channels"].pop(cid, None)
            elif not salon.members:
                try:
                    await salon.delete(reason="Nettoyage au démarrage — vocal vide")
                except discord.HTTPException:
                    logging.exception("Impossible de supprimer le vocal vide %s", cid)
                else:
                    gconf["temp_channels"].pop(cid, None)
            else:
                # A previously crashed transfer must not leave the owner absent.
                info = gconf["temp_channels"][cid]
                if not any(m.id == info.get("owner_id") for m in salon.members):
                    try:
                        await transfert_proprietaire(salon, info, salon.members[0])
                    except discord.HTTPException:
                        logging.exception("Reprise transfert de propriété du vocal %s", cid)
    sauvegarder_configuration()

    # Ready may fire after reconnect; the job is intentionally idempotent.
    await bot.change_presence(
        activity=discord.Activity(type=discord.ActivityType.watching, name="Nadouja & Mitteg & Not Feller")
    )
    print(f"[✓] {bot.user} connecté — Slash commands synchronisées.")


@bot.event
async def on_guild_channel_delete(channel):
    """Nettoie la config quand un salon / une catégorie est supprimé(e) à la main."""
    gconf = config_serveur(channel.guild.id)
    cid = str(channel.id)
    modifie = False
    for section in ("hubs", "temp_channels", "categories"):
        if gconf[section].pop(cid, None) is not None:
            modifie = True
    if modifie:
        sauvegarder_configuration()


# ═══════════════════════════════════════════════════════════
#  MENU DE CHOIX DU TYPE DE VOCAL (affiché dans le chat vocal)
# ═══════════════════════════════════════════════════════════
class ChoixTypeSelect(discord.ui.Select):
    def __init__(self, membre: discord.Member, types_disponibles: dict):
        self.membre = membre
        options = [
            discord.SelectOption(
                label=nom,
                description=f"Limite : {info.get('limite')} membre(s)" if info.get("limite") else "Limite : illimitée",
                emoji=emoji_type(info),
            )
            for nom, info in list(types_disponibles.items())[:25]
        ]
        super().__init__(
            placeholder="Choisis le type de vocal à créer...",
            options=options,
            min_values=1,
            max_values=1,
        )

    async def callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.membre.id:
            await interaction.response.send_message(
                "❌ Ce choix ne t'est pas destiné.", ephemeral=True
            )
            return
        choix_en_cours.pop((self.membre.guild.id, self.membre.id), None)
        self.view.stop()
        await creer_vocal_temporaire(interaction, self.membre, self.values[0])


class ChoixTypeView(discord.ui.View):
    def __init__(self, membre: discord.Member, types_disponibles: dict, hub_id: int):
        super().__init__(timeout=DUREE_CHOIX)
        self.membre = membre
        self.hub_id = hub_id
        self.message: discord.Message | None = None
        self.add_item(ChoixTypeSelect(membre, types_disponibles))

    async def on_timeout(self):
        key = (self.membre.guild.id, self.membre.id)
        if choix_en_cours.get(key) is not self:
            return
        choix_en_cours.pop(key, None)
        voice = self.membre.voice
        # On ne déconnecte que si le membre est TOUJOURS dans le hub
        if voice and voice.channel and voice.channel.id == self.hub_id:
            try:
                await self.membre.move_to(None)
            except discord.HTTPException:
                pass
        if self.message:
            embed = discord.Embed(
                title="⏰ Temps écoulé",
                description=f"{self.membre.mention} n'a pas choisi de type de vocal à temps et a été déconnecté.",
                color=0xFF4444,
            )
            try:
                await self.message.edit(content=None, embed=embed, view=None)
                asyncio.create_task(supprimer_message_plus_tard(self.message))
            except discord.HTTPException:
                pass


async def supprimer_message_plus_tard(message: discord.Message, delai: int = DELAI_SUPPRESSION_MSG):
    """Attend `delai` secondes puis supprime le message (silencieux si déjà supprimé)."""
    await asyncio.sleep(delai)
    try:
        await message.delete()
    except discord.HTTPException:
        pass


# ═══════════════════════════════════════════════════════════
#  PANNEAU DE GESTION DU VOCAL (affiché dans le chat du vocal créé)
#  Vue persistante : les boutons fonctionnent même après un redémarrage
#  du bot, et la permission (nom/limite) est vérifiée à chaque clic.
# ═══════════════════════════════════════════════════════════
def autorisation_vocal(interaction: discord.Interaction, channel_id: int, flag: str) -> bool:
    """Recheck an action at final submission (modal may have been open for minutes)."""
    if not interaction.guild or interaction.channel_id != channel_id:
        return False
    gconf = config_serveur(interaction.guild.id)
    info = gconf["temp_channels"].get(str(channel_id))
    if not info or info.get("owner_id") != interaction.user.id:
        return False
    if not getattr(interaction.user, "voice", None) or interaction.user.voice.channel != interaction.channel:
        return False
    type_info = gconf["types"].get(info.get("type"), {})
    return bool(type_info.get(flag))


def staff_autorise(interaction: discord.Interaction) -> bool:
    return bool(interaction.guild and interaction.user.guild_permissions.manage_channels)


def staff_roles_autorise(interaction: discord.Interaction) -> bool:
    return staff_autorise(interaction) and interaction.user.guild_permissions.manage_roles


class RenommerVocalModal(discord.ui.Modal, title="Renommer le vocal"):
    nouveau_nom = discord.ui.TextInput(label="Nouveau nom", max_length=100)

    def __init__(self, channel_id: int, guild_id: int):
        super().__init__()
        self.channel_id = channel_id
        self.guild_id = guild_id

    async def on_submit(self, interaction: discord.Interaction):
        if not autorisation_vocal(interaction, self.channel_id, "modifiable_nom"):
            await interaction.response.send_message("❌ Tu n'es plus autorisé à renommer ce vocal.", ephemeral=True)
            return
        # Le renommage est limité par Discord et peut être lent → defer
        await interaction.response.defer(ephemeral=True, thinking=True)

        salon = interaction.guild.get_channel(self.channel_id)
        if not salon:
            await interaction.followup.send("❌ Salon introuvable.", ephemeral=True)
            return

        nom = self.nouveau_nom.value.strip()[:100]
        if not nom:
            await interaction.followup.send("❌ Le nom ne peut pas être vide.", ephemeral=True)
            return

        gconf = config_serveur(self.guild_id)
        nom_lower = nom.lower()
        if any(mot in nom_lower for mot in gconf.get("banwords", []) if mot):
            await interaction.followup.send(
                "❌ Ce nom contient un mot interdit. Choisis un autre nom.", ephemeral=True
            )
            return

        try:
            await salon.edit(name=nom)
        except discord.HTTPException:
            await interaction.followup.send("❌ Impossible de renommer le vocal pour le moment.", ephemeral=True)
            return
        await interaction.followup.send(f"✅ Vocal renommé en **{nom}**.", ephemeral=True)


class LimiteVocalModal(discord.ui.Modal, title="Changer la limite"):
    nombre = discord.ui.TextInput(label="Nouvelle limite (0 = illimité)", default="0", max_length=2)

    def __init__(self, channel_id: int):
        super().__init__()
        self.channel_id = channel_id

    async def on_submit(self, interaction: discord.Interaction):
        if not autorisation_vocal(interaction, self.channel_id, "modifiable_limite"):
            await interaction.response.send_message("❌ Tu n'es plus autorisé à modifier ce vocal.", ephemeral=True)
            return
        try:
            valeur = max(0, min(99, int(self.nombre.value.strip())))
        except ValueError:
            await interaction.response.send_message("❌ La limite doit être un nombre entier.", ephemeral=True)
            return
        salon = interaction.guild.get_channel(self.channel_id)
        if not salon:
            await interaction.response.send_message("❌ Salon introuvable.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            await salon.edit(user_limit=valeur)
        except discord.HTTPException:
            await interaction.followup.send("❌ Impossible de modifier la limite.", ephemeral=True)
            return
        await interaction.followup.send(
            f"✅ Limite mise à jour : **{valeur if valeur else 'illimitée'}**.", ephemeral=True
        )


def embed_panel_vocal(type_nom: str, masque: bool, verrou: bool, emoji: str = "🔊") -> discord.Embed:
    embed = discord.Embed(
        title="🎛️ Gestion de ton vocal",
        description=(
            "Utilise les boutons ci-dessous pour personnaliser ton vocal "
            "(disponible seulement si le type le permet)."
        ),
        color=0xFFD700,
    )
    embed.add_field(name="Type", value=f"{emoji} {type_nom}", inline=True)
    embed.add_field(name="Visibilité", value="🙈 Masqué" if masque else "👁️ Visible", inline=True)
    embed.add_field(name="Accès", value="🔒 Verrouillé" if verrou else "🔓 Ouvert", inline=True)
    return embed


class PanelVocalView(discord.ui.View):
    """Vue du panneau affiché dans chaque vocal temporaire.

    Les custom_id sont fixes pour rester utilisables après un redémarrage du bot
    (une instance par défaut est enregistrée comme vue persistante dans setup_hook).
    À chaque envoi ou édition réelle, on reconstruit une instance avec l'état
    courant (masque/verrou) pour que les libellés des boutons restent corrects.
    """

    def __init__(self, masque: bool = False, verrou: bool = False):
        super().__init__(timeout=None)

        bouton_renommer = discord.ui.Button(
            label="Renommer", style=discord.ButtonStyle.primary, emoji="✏️",
            custom_id="panel_vocal_renommer",
        )
        bouton_renommer.callback = self.bouton_renommer
        self.add_item(bouton_renommer)

        bouton_limite = discord.ui.Button(
            label="Limite", style=discord.ButtonStyle.primary, emoji="👥",
            custom_id="panel_vocal_limite",
        )
        bouton_limite.callback = self.bouton_limite
        self.add_item(bouton_limite)

        bouton_masquer = discord.ui.Button(
            label="Afficher" if masque else "Masquer",
            style=discord.ButtonStyle.secondary,
            emoji="👁️" if masque else "🙈",
            custom_id="panel_vocal_masquer",
        )
        bouton_masquer.callback = self.bouton_masquer
        self.add_item(bouton_masquer)

        bouton_verrou = discord.ui.Button(
            label="Déverrouiller" if verrou else "Verrouiller",
            style=discord.ButtonStyle.danger if verrou else discord.ButtonStyle.secondary,
            emoji="🔓" if verrou else "🔒",
            custom_id="panel_vocal_verrou",
        )
        bouton_verrou.callback = self.bouton_verrou
        self.add_item(bouton_verrou)

    @staticmethod
    def _info(interaction: discord.Interaction):
        gconf = config_serveur(interaction.guild_id)
        return gconf, gconf["temp_channels"].get(str(interaction.channel_id))

    async def _verifier(self, interaction: discord.Interaction, permission: str | None = None):
        """Vérifie que le salon est temporaire, que l'utilisateur en est le créateur et,
        si demandé, que le type autorise l'action. Retourne (gconf, info) ou None
        (après avoir répondu à l'utilisateur)."""
        gconf, info = self._info(interaction)
        if not info:
            await interaction.response.send_message("❌ Ce salon n'est plus un vocal temporaire.", ephemeral=True)
            return None
        if info["owner_id"] != interaction.user.id or not getattr(interaction.user, "voice", None) \
                or interaction.user.voice.channel != interaction.channel:
            await interaction.response.send_message("❌ Seul le propriétaire présent dans le vocal peut le modifier.", ephemeral=True)
            return None
        if permission:
            type_info = gconf["types"].get(info["type"])
            if not type_info or not type_info.get(permission):
                texte = (
                    "❌ Le nom de ce vocal ne peut pas être modifié."
                    if permission == "modifiable_nom"
                    else "❌ La limite de ce vocal ne peut pas être modifiée."
                )
                await interaction.response.send_message(texte, ephemeral=True)
                return None
        return gconf, info

    async def bouton_renommer(self, interaction: discord.Interaction):
        if not await self._verifier(interaction, "modifiable_nom"):
            return
        await interaction.response.send_modal(RenommerVocalModal(interaction.channel_id, interaction.guild_id))

    async def bouton_limite(self, interaction: discord.Interaction):
        if not await self._verifier(interaction, "modifiable_limite"):
            return
        await interaction.response.send_modal(LimiteVocalModal(interaction.channel_id))

    async def _changer_etat(self, interaction: discord.Interaction, etat: str, permission: str):
        # Serialize all overwrites updates on a channel: no lost updates when two
        # buttons are clicked simultaneously.
        await interaction.response.defer(ephemeral=True)
        async with channel_locks.hold(interaction.channel_id):
            gconf = config_serveur(interaction.guild_id)
            info = gconf["temp_channels"].get(str(interaction.channel_id))
            if not info or info.get("owner_id") != interaction.user.id:
                await interaction.followup.send("❌ Tu n'es plus propriétaire de ce salon.", ephemeral=True)
                return
            try:
                salon = await interaction.guild.fetch_channel(interaction.channel_id)
            except discord.HTTPException:
                await interaction.followup.send("❌ Impossible de récupérer le salon.", ephemeral=True)
                return
            nouvelle_valeur = not info.get(etat, False)
            overwrites = copie_overwrites(salon)
            snapshots = info.setdefault("permission_snapshots", {})
            if nouvelle_valeur:
                overwrites, state = apply_channel_restriction(
                    overwrites, interaction.guild, permission, info["owner_id"])
                snapshots[permission] = state
            else:
                restore_field(overwrites, interaction.guild, permission,
                              snapshots.get(permission, {}))
            try:
                await salon.edit(overwrites=overwrites, reason="Beflow : changement de confidentialité")
            except discord.HTTPException:
                await interaction.followup.send("❌ Impossible de modifier les permissions.", ephemeral=True)
                return
            if not nouvelle_valeur:
                snapshots.pop(permission, None)
            info[etat] = nouvelle_valeur
            sauvegarder_configuration()
            try:
                await interaction.message.edit(
                    embed=embed_panel_vocal(
                        info["type"], info.get("masque", False), info.get("verrou", False),
                        emoji=emoji_type(gconf["types"].get(info["type"], {}))),
                    view=PanelVocalView(info.get("masque", False), info.get("verrou", False)))
            except discord.HTTPException:
                logging.exception("Panneau impossible à actualiser : %s", salon.id)
        await interaction.followup.send("✅ Paramètre du vocal mis à jour.", ephemeral=True)

    async def bouton_masquer(self, interaction: discord.Interaction):
        if await self._verifier(interaction):
            await self._changer_etat(interaction, "masque", "view_channel")

    async def bouton_verrou(self, interaction: discord.Interaction):
        if await self._verifier(interaction):
            await self._changer_etat(interaction, "verrou", "connect")


async def nettoyer_orphelin(salon: discord.VoiceChannel, gconf: dict, nom_type: str):
    """Remember failed-deletion channels so a restart or retry can remove them."""
    cid = str(salon.id)
    gconf["temp_channels"][cid] = {
        "owner_id": 0, "type": nom_type, "masque": False, "verrou": False,
        "permission_snapshots": {},
    }
    sauvegarder_configuration()
    for attempt in range(3):
        await asyncio.sleep(10 * (attempt + 1))
        if salon.members:
            logging.warning("Vocal orphelin %s occupé ; suppression ignorée", cid)
            return
        try:
            await salon.delete(reason="Nettoyage d'un vocal orphelin")
        except discord.HTTPException:
            logging.exception("Tentative %s de nettoyage %s impossible", attempt + 1, cid)
        else:
            gconf["temp_channels"].pop(cid, None)
            sauvegarder_configuration()
            return


async def creer_vocal_temporaire(interaction: discord.Interaction, membre: discord.Member, nom_type: str):
    # Acknowledge before waiting for the per-member lock (Discord's 3s limit).
    await interaction.response.defer()
    async with creation_locks.hold((membre.guild.id, membre.id)):
        await _creer_vocal_temporaire_verrouille(interaction, membre, nom_type)


async def _creer_vocal_temporaire_verrouille(interaction: discord.Interaction, membre: discord.Member, nom_type: str):
    msg = await interaction.original_response()

    gconf = config_serveur(membre.guild.id)
    type_info = gconf["types"].get(nom_type)
    hub = interaction.channel

    async def echec(texte: str):
        embed = discord.Embed(description=texte, color=0xFF4444)
        try:
            await interaction.edit_original_response(content=None, embed=embed, view=None)
            asyncio.create_task(supprimer_message_plus_tard(msg))
        except discord.HTTPException:
            pass

    if not type_info:
        return await echec("❌ Ce type de vocal n'existe plus.")
    hub_info_now = gconf["hubs"].get(str(hub.id))
    if not hub_info_now or nom_type not in hub_info_now.get("types", []):
        return await echec("❌ Ce type n'est plus autorisé dans ce hub.")
    cooldown_key = (membre.guild.id, membre.id)
    secondes = creation_cooldowns.remaining(cooldown_key)
    if secondes:
        return await echec(f"⌛ Réessaie dans {secondes:.0f} secondes.")
    # Le membre a pu quitter le hub entre-temps → sinon on créerait un salon orphelin jamais supprimé
    if not membre.voice or not membre.voice.channel or membre.voice.channel.id != hub.id:
        return await echec("❌ Tu as quitté le hub, demande annulée.")

    try:
        nom_salon = type_info.get("format_nom", "🔊 {type} de {pseudo}").format(
            pseudo=membre.display_name, type=nom_type
        )
    except (KeyError, IndexError, ValueError):  # ex. format personnalisé contenant {truc}
        nom_salon = f"🔊 {nom_type} de {membre.display_name}"
    nom_salon = nom_salon[:100]

    # Catégorie cible : celle configurée pour le hub si elle existe encore, sinon la catégorie du hub
    hub_info = gconf["hubs"].get(str(hub.id), {})
    categorie_cible = None
    if hub_info.get("categorie_cible"):
        categorie_cible = membre.guild.get_channel(int(hub_info["categorie_cible"]))
    if not isinstance(categorie_cible, discord.CategoryChannel):
        categorie_cible = hub.category

    masque_initial = type_info.get("masque_defaut", False)
    if categorie_cible:
        permissions = categorie_cible.permissions_for(membre)
        if not (permissions.view_channel and permissions.connect):
            return await echec("❌ Tu n'as pas accès à la catégorie cible de ce hub.")

    # Le vocal créé reprend les restrictions de rôle éventuelles de sa catégorie cible
    overwrites = copie_overwrites(categorie_cible)
    snapshots_creation = {}
    if masque_initial:
        overwrites, snapshots_creation = apply_channel_restriction(
            overwrites, membre.guild, "view_channel", membre.id)
    # Le créateur garde toujours l'accès (posé directement à la création : un appel API en moins)
    ow_membre = overwrites.get(membre, discord.PermissionOverwrite())
    ow_membre.view_channel = True
    ow_membre.connect = True
    overwrites[membre] = ow_membre

    try:
        nouveau_salon = await membre.guild.create_voice_channel(
            name=nom_salon,
            category=categorie_cible,
            user_limit=type_info.get("limite", 0),
            overwrites=overwrites,
        )
    except discord.HTTPException:
        return await echec("❌ Impossible de créer le vocal (permissions ou limite de salons atteinte ?).")

    # A user may have left the hub while Discord created the channel.
    if not membre.voice or membre.voice.channel != hub:
        try:
            await nouveau_salon.delete(reason="Créateur parti du hub pendant la création")
        except discord.HTTPException:
            logging.exception("Salon orphelin potentiel %s", nouveau_salon.id)
            asyncio.create_task(nettoyer_orphelin(nouveau_salon, gconf, nom_type))
        return await echec("❌ Tu as quitté le hub pendant la création.")
    try:
        await membre.move_to(nouveau_salon)
    except discord.HTTPException:
        # Impossible de le déplacer : on supprime le salon, sinon il resterait vide pour toujours
        try:
            await nouveau_salon.delete(reason="Déplacement impossible")
        except discord.HTTPException:
            logging.exception("Suppression d'un salon après déplacement impossible : %s", nouveau_salon.id)
            asyncio.create_task(nettoyer_orphelin(nouveau_salon, gconf, nom_type))
        return await echec("❌ Impossible de te déplacer dans le nouveau vocal.")

    gconf["temp_channels"][str(nouveau_salon.id)] = {
        "owner_id": membre.id,
        "type": nom_type,
        "masque": masque_initial,
        "verrou": False,
        "permission_snapshots": {"view_channel": snapshots_creation} if masque_initial else {},
    }
    sauvegarder_configuration()
    creation_cooldowns.mark(cooldown_key)

    embed = discord.Embed(
        title="✅ Vocal créé !",
        description=f"{membre.mention}, ton vocal **{nom_salon}** a été créé et tu y as été déplacé.",
        color=0x00FF88,
    )
    try:
        await interaction.edit_original_response(content=None, embed=embed, view=None)
        asyncio.create_task(supprimer_message_plus_tard(msg))
    except discord.HTTPException:
        pass

    try:
        await nouveau_salon.send(
            embed=embed_panel_vocal(nom_type, masque_initial, False, emoji=emoji_type(type_info)),
            view=PanelVocalView(masque=masque_initial, verrou=False),
        )
    except discord.HTTPException:
        pass


# ═══════════════════════════════════════════════════════════
#  ÉVÉNEMENTS VOCAUX : rejoindre un hub / vider un vocal temp
# ═══════════════════════════════════════════════════════════
async def transfert_proprietaire(salon: discord.VoiceChannel, info: dict, nouveau: discord.Member):
    """Atomically change managed owner overwrites before committing state."""
    ancien_id = info.get("owner_id")
    salon = await salon.guild.fetch_channel(salon.id)
    overwrites = copie_overwrites(salon)
    for target, overwrite in list(overwrites.items()):
        if target.id == ancien_id and not isinstance(target, discord.Role):
            overwrite.view_channel = None
            overwrite.connect = None
            if overwrite.is_empty():
                overwrites.pop(target, None)
            else:
                overwrites[target] = overwrite
    owner_overwrite = overwrites.get(nouveau, discord.PermissionOverwrite())
    owner_overwrite.view_channel = True
    owner_overwrite.connect = True
    overwrites[nouveau] = owner_overwrite
    await salon.edit(overwrites=overwrites, reason="Beflow : changement de propriétaire")
    info["owner_id"] = nouveau.id
    sauvegarder_configuration()
    try:
        await salon.send(f"👑 {nouveau.display_name} est maintenant propriétaire de ce vocal.",
                         allowed_mentions=discord.AllowedMentions.none())
    except discord.HTTPException:
        logging.exception("Annonce de transfert impossible : %s", salon.id)


@bot.event
async def on_voice_state_update(member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
    if member.bot:
        return

    gconf = config_serveur(member.guild.id)
    a_change_de_salon = before.channel != after.channel

    # ── Quitte un hub sans avoir choisi → on annule le menu en attente ──
    if before.channel and a_change_de_salon and str(before.channel.id) in gconf["hubs"]:
        vue = choix_en_cours.pop((member.guild.id, member.id), None)
        if vue:
            vue.stop()
            if vue.message:
                try:
                    await vue.message.delete()
                except discord.HTTPException:
                    pass

    # ── Rejoint un salon hub ────────────────────────────────
    if after.channel and a_change_de_salon and str(after.channel.id) in gconf["hubs"]:
        types_du_hub = gconf["hubs"][str(after.channel.id)].get("types", [])
        types_disponibles = {n: gconf["types"][n] for n in types_du_hub if n in gconf["types"]}

        if not types_disponibles:
            logging.warning(
                f"Hub {after.channel.id} rejoint par {member.id} mais aucun type de vocal "
                f"n'est disponible dans ce hub sur le serveur {member.guild.id} (utilise /vocal config)."
            )
        else:
            embed = discord.Embed(
                title="🎙️ Choisis ton type de vocal",
                description=(
                    f"{member.mention}, sélectionne le type de vocal que tu veux créer ci-dessous.\n"
                    f"⏱️ Tu as **{DUREE_CHOIX} secondes**, sinon tu seras déconnecté."
                ),
                color=0xFFD700,
            )
            view = ChoixTypeView(member, types_disponibles, after.channel.id)
            try:
                msg = await after.channel.send(
                    content=member.mention,
                    embed=embed,
                    view=view,
                    allowed_mentions=discord.AllowedMentions(users=[member]),
                )
                view.message = msg
                choix_en_cours[(member.guild.id, member.id)] = view
            except discord.Forbidden:
                logging.warning(
                    f"Permission refusée pour envoyer un message dans le hub {after.channel.id} "
                    f"(serveur {member.guild.id}) — vérifie que le bot peut écrire dans ce salon vocal."
                )
            except discord.HTTPException:
                logging.exception("Impossible d'envoyer le menu de choix dans le hub.")

    # ── Quitte un vocal temporaire : serialize deletion and ownership ────
    if before.channel and a_change_de_salon and str(before.channel.id) in gconf["temp_channels"]:
        await asyncio.sleep(DELAI_NETTOYAGE)
        cid = str(before.channel.id)
        async with channel_locks.hold(before.channel.id):
            info = gconf["temp_channels"].get(cid)
            if not info:
                return
            salon = member.guild.get_channel(before.channel.id)
            if not salon:
                gconf["temp_channels"].pop(cid, None)
                sauvegarder_configuration()
                return
            if not salon.members:
                try:
                    await salon.delete(reason="Vocal temporaire vide")
                except discord.HTTPException:
                    logging.exception("Suppression impossible du vocal vide %s", cid)
                else:
                    gconf["temp_channels"].pop(cid, None)
                    sauvegarder_configuration()
                return
            if info.get("owner_id") == member.id and member not in salon.members:
                candidats = [m for m in salon.members if not m.bot]
                if candidats:
                    try:
                        await transfert_proprietaire(salon, info, candidats[0])
                    except discord.HTTPException:
                        logging.exception("Transfert de propriété impossible : %s", cid)




# ═══════════════════════════════════════════════════════════
#  GESTION DES ERREURS DE PERMISSIONS
# ═══════════════════════════════════════════════════════════
@arbre.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.MissingPermissions):
        msg = "❌ Tu n'as pas la permission d'utiliser cette commande."
    else:
        logging.error("Erreur commande slash serveur=%s utilisateur=%s", interaction.guild_id,
                      interaction.user.id, exc_info=(type(error), error, error.__traceback__))
        msg = "❌ Une erreur est survenue en exécutant cette commande."

    if interaction.response.is_done():
        await interaction.followup.send(msg, ephemeral=True)
    else:
        await interaction.response.send_message(msg, ephemeral=True)


# ═══════════════════════════════════════════════════════════
#  GROUPE DE COMMANDES  /vocal ...
#  (toute la gestion des types/hubs passe par /vocal config)
#  guild_only : en MP, guild_id vaut None et casserait la config
# ═══════════════════════════════════════════════════════════
groupe_vocal = app_commands.Group(
    name="vocal", description="Système de vocaux temporaires", guild_only=True
)


# ═══════════════════════════════════════════════════════════
#  PANNEAU DE CONFIGURATION INTERACTIF  —  /vocal config
# ═══════════════════════════════════════════════════════════
def embed_config(gconf: dict) -> discord.Embed:
    types_txt = "\n".join(
        f"{emoji_type(i)} **{n}** — limite {i.get('limite') or '∞'} · nom {'✅' if i.get('modifiable_nom') else '❌'} · "
        f"limite {'✅' if i.get('modifiable_limite') else '❌'} · masqué par défaut {'✅' if i.get('masque_defaut') else '❌'}"
        for n, i in gconf["types"].items()
    ) or "_Aucun type créé_"
    hubs_txt = "\n".join(
        f"🎙️ <#{cid}> — {len(info.get('types', []))} type(s)"
        + (f" · catégorie cible : <#{info['categorie_cible']}>" if info.get("categorie_cible") else "")
        for cid, info in gconf["hubs"].items()
    ) or "_Aucun hub créé_"
    categories_txt = "\n".join(
        f"📁 <#{cid}> — restreinte à {len(info.get('roles', []))} rôle(s)"
        for cid, info in gconf.get("categories", {}).items()
    ) or "_Aucune catégorie restreinte_"
    banwords_txt = ", ".join(gconf.get("banwords", [])) or "_Aucun mot interdit_"

    embed = discord.Embed(
        title="⚙️ Configuration — Vocaux temporaires",
        description="Utilise les boutons ci-dessous pour tout configurer, sans taper d'autre commande.",
        color=0xFFD700,
    )
    embed.add_field(name=f"Types ({len(gconf['types'])})", value=types_txt[:1024], inline=False)
    embed.add_field(name=f"Hubs ({len(gconf['hubs'])})", value=hubs_txt[:1024], inline=False)
    embed.add_field(name=f"Catégories restreintes ({len(gconf.get('categories', {}))})", value=categories_txt[:1024], inline=False)
    embed.add_field(name="Mots interdits", value=banwords_txt[:1024], inline=False)
    embed.set_footer(text="Panneau valable 5 minutes • Visible uniquement par toi")
    return embed


async def maj_panneau(interaction: discord.Interaction, embed: discord.Embed, vue: "VueBase"):
    """Met à jour le message du panneau, que l'interaction ait été différée (defer) ou non,
    et mémorise le message dans la vue pour le désactiver au timeout."""
    if interaction.response.is_done():
        vue.message = await interaction.edit_original_response(embed=embed, view=vue)
    else:
        await interaction.response.edit_message(embed=embed, view=vue)
        vue.message = interaction.message


async def retour_config(interaction: discord.Interaction, guild_id: int, auteur_id: int):
    gconf = config_serveur(guild_id)
    await maj_panneau(interaction, embed_config(gconf), VueConfig(guild_id, auteur_id))


class VueBase(discord.ui.View):
    """Base commune : restreint le panneau à son auteur et le désactive au timeout."""

    def __init__(self, guild_id: int, auteur_id: int, timeout: int = 300):
        super().__init__(timeout=timeout)
        self.guild_id = guild_id
        self.auteur_id = auteur_id
        self.message: discord.Message | None = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.auteur_id or not staff_autorise(interaction):
            await interaction.response.send_message("❌ Tu n'es pas autorisé à utiliser ce panneau.", ephemeral=True)
            return False
        return True

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True
        if self.message:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass


# ── Étape : options du type (boutons oui/non), puis modal pour le texte ─
class TypeModal(discord.ui.Modal, title="Nouveau type de vocal"):
    nom = discord.ui.TextInput(label="Nom du type", placeholder="Duo", max_length=50)
    limite = discord.ui.TextInput(label="Limite de membres (0 = illimité)", default="0", max_length=2)
    emoji = discord.ui.TextInput(
        label="Emoji (colle un emoji Discord, perso ou non)",
        required=False,
        max_length=100,
    )
    format_nom = discord.ui.TextInput(
        label="Format du nom ({pseudo} et {type})",
        default="🔊 {type} de {pseudo}",
        max_length=100,
        required=False,
    )

    def __init__(
        self,
        guild_id: int,
        auteur_id: int,
        modifiable_nom: bool,
        modifiable_limite: bool,
        masque_defaut: bool,
        type_existant: str | None = None,
        gconf: dict | None = None,
    ):
        titre = f"Modifier « {type_existant} »"[:45] if type_existant else "Nouveau type de vocal"
        super().__init__(title=titre)
        self.guild_id = guild_id
        self.auteur_id = auteur_id
        self.modifiable_nom = modifiable_nom
        self.modifiable_limite = modifiable_limite
        self.masque_defaut = masque_defaut
        self.type_existant = type_existant

        if type_existant and gconf:
            info = gconf["types"].get(type_existant, {})
            self.nom.default = type_existant
            self.limite.default = str(info.get("limite", 0))
            self.emoji.default = info.get("emoji", "")
            self.format_nom.default = info.get("format_nom", "🔊 {type} de {pseudo}")

    async def on_submit(self, interaction: discord.Interaction):
        if not staff_autorise(interaction) or interaction.user.id != self.auteur_id or interaction.guild_id != self.guild_id:
            await interaction.response.send_message("❌ Tu n'es plus autorisé à utiliser ce formulaire.", ephemeral=True)
            return
        gconf = config_serveur(self.guild_id)
        nom_val = self.nom.value.strip()
        if not nom_val:
            await interaction.response.send_message("❌ Le nom du type ne peut pas être vide.", ephemeral=True)
            return

        conflit = any(
            t.lower() == nom_val.lower() and t != self.type_existant
            for t in gconf["types"]
        )
        if conflit:
            await interaction.response.send_message(f"❌ Le type **{nom_val}** existe déjà.", ephemeral=True)
            return
        try:
            limite_val = max(0, min(99, int(self.limite.value.strip())))
        except ValueError:
            await interaction.response.send_message("❌ La limite doit être un nombre entier.", ephemeral=True)
            return

        format_val = self.format_nom.value.strip() or "🔊 {type} de {pseudo}"
        emoji_val = self.emoji.value.strip()

        # Un emoji invalide ferait refuser le menu de choix par Discord (400 Bad Request)
        if not emoji_valide(emoji_val):
            await interaction.response.send_message(
                "❌ Emoji invalide : utilise un emoji Unicode ou un emoji personnalisé `<:nom:id>`.",
                ephemeral=True,
            )
            return

        # Un format contenant un autre {champ} ferait planter la création du vocal
        try:
            format_val.format(pseudo="test", type="test")
        except (KeyError, IndexError, ValueError):
            await interaction.response.send_message(
                "❌ Format du nom invalide : seuls `{pseudo}` et `{type}` sont autorisés.",
                ephemeral=True,
            )
            return

        # Si le type est renommé, on met à jour les hubs qui le référencent
        if self.type_existant and self.type_existant != nom_val:
            gconf["types"].pop(self.type_existant, None)
            for hub_info in gconf["hubs"].values():
                types_hub = hub_info.get("types", [])
                if self.type_existant in types_hub:
                    types_hub[types_hub.index(self.type_existant)] = nom_val
            for temp_info in gconf["temp_channels"].values():
                if temp_info.get("type") == self.type_existant:
                    temp_info["type"] = nom_val

        gconf["types"][nom_val] = {
            "limite": limite_val,
            "modifiable_nom": self.modifiable_nom,
            "modifiable_limite": self.modifiable_limite,
            "masque_defaut": self.masque_defaut,
            "format_nom": format_val,
            "emoji": emoji_val,
        }
        sauvegarder_configuration()

        await retour_config(interaction, self.guild_id, self.auteur_id)


class OptionsTypeView(VueBase):
    """Première étape de création/édition d'un type : options oui/non par boutons.
    On ne peut pas tout mettre dans le modal (limite de 5 champs texte), donc
    les bascules oui/non sont réglées ici avant d'ouvrir le formulaire de texte."""

    def __init__(
        self,
        guild_id: int,
        auteur_id: int,
        type_existant: str | None = None,
        modifiable_nom: bool = False,
        modifiable_limite: bool = False,
        masque_defaut: bool = False,
    ):
        super().__init__(guild_id, auteur_id, timeout=180)
        self.type_existant = type_existant
        self.modifiable_nom = modifiable_nom
        self.modifiable_limite = modifiable_limite
        self.masque_defaut = masque_defaut
        self._construire_boutons()

    def _construire_boutons(self):
        self.clear_items()

        bouton_nom = discord.ui.Button(
            label=f"Nom modifiable : {'Oui' if self.modifiable_nom else 'Non'}",
            style=discord.ButtonStyle.success if self.modifiable_nom else discord.ButtonStyle.secondary,
            emoji="✏️", row=0,
        )
        bouton_nom.callback = self._toggle_nom
        self.add_item(bouton_nom)

        bouton_limite = discord.ui.Button(
            label=f"Limite modifiable : {'Oui' if self.modifiable_limite else 'Non'}",
            style=discord.ButtonStyle.success if self.modifiable_limite else discord.ButtonStyle.secondary,
            emoji="👥", row=0,
        )
        bouton_limite.callback = self._toggle_limite
        self.add_item(bouton_limite)

        bouton_masque = discord.ui.Button(
            label=f"Masqué par défaut : {'Oui' if self.masque_defaut else 'Non'}",
            style=discord.ButtonStyle.success if self.masque_defaut else discord.ButtonStyle.secondary,
            emoji="🙈", row=1,
        )
        bouton_masque.callback = self._toggle_masque
        self.add_item(bouton_masque)

        bouton_continuer = discord.ui.Button(label="Continuer", style=discord.ButtonStyle.primary, emoji="➡️", row=2)
        bouton_continuer.callback = self._continuer
        self.add_item(bouton_continuer)

        bouton_annuler = discord.ui.Button(label="Annuler", style=discord.ButtonStyle.secondary, emoji="✖️", row=2)
        bouton_annuler.callback = self._annuler
        self.add_item(bouton_annuler)

    async def _toggle_nom(self, interaction: discord.Interaction):
        self.modifiable_nom = not self.modifiable_nom
        self._construire_boutons()
        await interaction.response.edit_message(view=self)

    async def _toggle_limite(self, interaction: discord.Interaction):
        self.modifiable_limite = not self.modifiable_limite
        self._construire_boutons()
        await interaction.response.edit_message(view=self)

    async def _toggle_masque(self, interaction: discord.Interaction):
        self.masque_defaut = not self.masque_defaut
        self._construire_boutons()
        await interaction.response.edit_message(view=self)

    async def _continuer(self, interaction: discord.Interaction):
        gconf = config_serveur(self.guild_id)
        await interaction.response.send_modal(
            TypeModal(
                self.guild_id,
                self.auteur_id,
                self.modifiable_nom,
                self.modifiable_limite,
                self.masque_defaut,
                type_existant=self.type_existant,
                gconf=gconf,
            )
        )

    async def _annuler(self, interaction: discord.Interaction):
        await retour_config(interaction, self.guild_id, self.auteur_id)


# ── Étape : suppression d'un type (Select) ──────────────────
class SelectTypeASupprimer(discord.ui.Select):
    def __init__(self, guild_id: int, gconf: dict):
        self.guild_id = guild_id
        options = [discord.SelectOption(label=n, value=n, emoji=emoji_type(i)) for n, i in gconf["types"].items()][:25]
        super().__init__(placeholder="Choisis le type à supprimer...", options=options, min_values=1, max_values=1)

    async def callback(self, interaction: discord.Interaction):
        gconf = config_serveur(self.guild_id)
        gconf["types"].pop(self.values[0], None)
        for hub_info in gconf["hubs"].values():
            if self.values[0] in hub_info.get("types", []):
                hub_info["types"].remove(self.values[0])
        sauvegarder_configuration()
        await retour_config(interaction, self.guild_id, self.view.auteur_id)


class VueSupprimerType(VueBase):
    def __init__(self, guild_id: int, auteur_id: int, gconf: dict):
        super().__init__(guild_id, auteur_id)
        self.add_item(SelectTypeASupprimer(guild_id, gconf))

    @discord.ui.button(label="Retour", style=discord.ButtonStyle.secondary, emoji="⬅️", row=1)
    async def retour(self, interaction: discord.Interaction, button: discord.ui.Button):
        await retour_config(interaction, self.guild_id, self.auteur_id)


# ── Étape : modifier un type existant ───────────────────────
class SelectTypeAModifier(discord.ui.Select):
    def __init__(self, guild_id: int, gconf: dict):
        self.guild_id = guild_id
        options = [discord.SelectOption(label=n, value=n, emoji=emoji_type(i)) for n, i in gconf["types"].items()][:25]
        super().__init__(placeholder="Choisis le type à modifier...", options=options, min_values=1, max_values=1)

    async def callback(self, interaction: discord.Interaction):
        gconf = config_serveur(self.guild_id)
        nom = self.values[0]
        info = gconf["types"].get(nom)
        if not info:
            await interaction.response.send_message("❌ Ce type n'existe plus.", ephemeral=True)
            return

        vue = OptionsTypeView(
            self.guild_id,
            self.view.auteur_id,
            type_existant=nom,
            modifiable_nom=info.get("modifiable_nom", False),
            modifiable_limite=info.get("modifiable_limite", False),
            masque_defaut=info.get("masque_defaut", False),
        )
        embed = discord.Embed(
            title=f"✏️ Modifier « {nom} » — options",
            description=(
                "Ajuste les options ci-dessous, puis clique sur **Continuer** pour modifier "
                "le nom, la limite, l'emoji et le format de nom du type."
            ),
            color=0xFFD700,
        )
        await maj_panneau(interaction, embed, vue)


class VueModifierType(VueBase):
    def __init__(self, guild_id: int, auteur_id: int, gconf: dict):
        super().__init__(guild_id, auteur_id)
        self.add_item(SelectTypeAModifier(guild_id, gconf))

    @discord.ui.button(label="Retour", style=discord.ButtonStyle.secondary, emoji="⬅️", row=1)
    async def retour(self, interaction: discord.Interaction, button: discord.ui.Button):
        await retour_config(interaction, self.guild_id, self.auteur_id)


# ── Étape : nouveau hub (salon existant OU modal) ───────────
class HubModal(discord.ui.Modal, title="Nouveau salon hub"):
    nom = discord.ui.TextInput(label="Nom du salon", default="➕ Créer un vocal", max_length=100)

    def __init__(self, guild_id: int, auteur_id: int, categorie_id: int | None = None):
        super().__init__()
        self.guild_id = guild_id
        self.auteur_id = auteur_id
        self.categorie_id = categorie_id

    async def on_submit(self, interaction: discord.Interaction):
        if not staff_autorise(interaction) or interaction.user.id != self.auteur_id or interaction.guild_id != self.guild_id:
            await interaction.response.send_message("❌ Tu n'es plus autorisé à utiliser ce formulaire.", ephemeral=True)
            return
        await interaction.response.defer()
        gconf = config_serveur(self.guild_id)
        categorie = interaction.guild.get_channel(self.categorie_id) if self.categorie_id else None
        nom = self.nom.value.strip()[:100] or "➕ Créer un vocal"
        try:
            salon = await interaction.guild.create_voice_channel(name=nom, category=categorie)
        except discord.HTTPException:
            await interaction.followup.send("❌ Impossible de créer le salon (permissions ?).", ephemeral=True)
            return
        gconf["hubs"][str(salon.id)] = {"types": list(gconf["types"].keys())}
        sauvegarder_configuration()

        await retour_config(interaction, self.guild_id, self.auteur_id)


class SelectSalonExistant(discord.ui.ChannelSelect):
    def __init__(self, guild_id: int):
        self.guild_id = guild_id
        super().__init__(
            placeholder="...ou choisis un salon vocal existant à transformer",
            channel_types=[discord.ChannelType.voice],
            min_values=1,
            max_values=1,
            row=0,
        )

    async def callback(self, interaction: discord.Interaction):
        gconf = config_serveur(self.guild_id)
        salon = interaction.guild.get_channel(self.values[0].id)
        if not salon:
            await interaction.response.send_message("❌ Salon introuvable.", ephemeral=True)
            return
        if str(salon.id) in gconf["hubs"]:
            await interaction.response.send_message(f"❌ {salon.mention} est déjà un hub.", ephemeral=True)
            return
        if str(salon.id) in gconf["temp_channels"]:
            await interaction.response.send_message(
                f"❌ {salon.mention} est un vocal temporaire, il ne peut pas devenir un hub.", ephemeral=True
            )
            return

        gconf["hubs"][str(salon.id)] = {"types": list(gconf["types"].keys())}
        sauvegarder_configuration()

        await retour_config(interaction, self.guild_id, self.view.auteur_id)


class SelectCategorieHub(discord.ui.ChannelSelect):
    """Choix de la catégorie du salon qui sera créé via le bouton 'Créer un nouveau salon'."""

    def __init__(self):
        super().__init__(
            placeholder="Catégorie du nouveau salon (optionnel)...",
            channel_types=[discord.ChannelType.category],
            min_values=1,
            max_values=1,
            row=1,
        )

    async def callback(self, interaction: discord.Interaction):
        self.view.categorie_id = self.values[0].id
        await interaction.response.send_message(
            f"📁 Catégorie sélectionnée : **{self.values[0].name}** (pour le prochain salon créé).",
            ephemeral=True,
        )


class VueNouveauHub(VueBase):
    def __init__(self, guild_id: int, auteur_id: int, guild: discord.Guild):
        super().__init__(guild_id, auteur_id)
        self.categorie_id: int | None = None
        self.add_item(SelectSalonExistant(guild_id))
        if guild.categories:
            self.add_item(SelectCategorieHub())

    @discord.ui.button(label="Créer un nouveau salon", style=discord.ButtonStyle.success, emoji="➕", row=2)
    async def nouveau_salon(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(HubModal(self.guild_id, self.auteur_id, self.categorie_id))

    @discord.ui.button(label="Retour", style=discord.ButtonStyle.secondary, emoji="⬅️", row=2)
    async def retour(self, interaction: discord.Interaction, button: discord.ui.Button):
        await retour_config(interaction, self.guild_id, self.auteur_id)


# ── Étape : suppression d'un hub (Select) ───────────────────
class SelectHubASupprimer(discord.ui.Select):
    def __init__(self, guild_id: int, gconf: dict, guild: discord.Guild):
        self.guild_id = guild_id
        options = [
            discord.SelectOption(label=salon.name[:100], value=cid, emoji="🎙️")
            for cid, salon in hubs_valides(guild, gconf)
        ]
        super().__init__(placeholder="Choisis le hub à supprimer...", options=options[:25], min_values=1, max_values=1)

    async def callback(self, interaction: discord.Interaction):
        gconf = config_serveur(self.guild_id)
        gconf["hubs"].pop(self.values[0], None)
        sauvegarder_configuration()
        await retour_config(interaction, self.guild_id, self.view.auteur_id)


class VueSupprimerHub(VueBase):
    def __init__(self, guild_id: int, auteur_id: int, gconf: dict, guild: discord.Guild):
        super().__init__(guild_id, auteur_id)
        self.add_item(SelectHubASupprimer(guild_id, gconf, guild))

    @discord.ui.button(label="Retour", style=discord.ButtonStyle.secondary, emoji="⬅️", row=1)
    async def retour(self, interaction: discord.Interaction, button: discord.ui.Button):
        await retour_config(interaction, self.guild_id, self.auteur_id)


# ── Étape : types disponibles + catégorie cible pour un hub ────
class SelectTypesHub(discord.ui.Select):
    def __init__(self, guild_id: int, hub_id: str, gconf: dict):
        self.guild_id = guild_id
        self.hub_id = hub_id
        actuels = set(gconf["hubs"].get(hub_id, {}).get("types", []))
        options = [
            discord.SelectOption(label=n, value=n, emoji=emoji_type(info), default=(n in actuels))
            for n, info in gconf["types"].items()
        ][:25]
        super().__init__(
            placeholder="Choisis les types disponibles pour ce hub...",
            options=options,
            min_values=0,
            max_values=len(options) if options else 1,
            row=0,
        )

    async def callback(self, interaction: discord.Interaction):
        gconf = config_serveur(self.guild_id)
        gconf["hubs"].setdefault(self.hub_id, {})["types"] = list(self.values)
        sauvegarder_configuration()
        await retour_config(interaction, self.guild_id, self.view.auteur_id)


class SelectCategorieCible(discord.ui.ChannelSelect):
    def __init__(self, guild_id: int, hub_id: str):
        self.guild_id = guild_id
        self.hub_id = hub_id
        super().__init__(
            placeholder="Catégorie cible pour les vocaux créés (sinon, catégorie du hub)...",
            channel_types=[discord.ChannelType.category],
            min_values=1,
            max_values=1,
            row=1,
        )

    async def callback(self, interaction: discord.Interaction):
        gconf = config_serveur(self.guild_id)
        gconf["hubs"].setdefault(self.hub_id, {})["categorie_cible"] = str(self.values[0].id)
        sauvegarder_configuration()
        await interaction.response.send_message(
            f"📁 Les vocaux créés depuis ce hub seront désormais placés dans **{self.values[0].name}**.",
            ephemeral=True,
        )


class VueTypesHub(VueBase):
    def __init__(self, guild_id: int, auteur_id: int, hub_id: str, gconf: dict):
        super().__init__(guild_id, auteur_id)
        self.hub_id = hub_id
        if gconf["types"]:
            self.add_item(SelectTypesHub(guild_id, hub_id, gconf))
        self.add_item(SelectCategorieCible(guild_id, hub_id))

    @discord.ui.button(label="Catégorie du hub (réinitialiser)", style=discord.ButtonStyle.secondary, emoji="↩️", row=2)
    async def reset_categorie(self, interaction: discord.Interaction, button: discord.ui.Button):
        gconf = config_serveur(self.guild_id)
        gconf["hubs"].setdefault(self.hub_id, {}).pop("categorie_cible", None)
        sauvegarder_configuration()
        await interaction.response.send_message(
            "↩️ Les vocaux créés depuis ce hub utiliseront de nouveau la catégorie du hub.", ephemeral=True
        )

    @discord.ui.button(label="Retour", style=discord.ButtonStyle.secondary, emoji="⬅️", row=2)
    async def retour(self, interaction: discord.Interaction, button: discord.ui.Button):
        await retour_config(interaction, self.guild_id, self.auteur_id)


class SelectHubPourTypes(discord.ui.Select):
    def __init__(self, guild_id: int, gconf: dict, guild: discord.Guild):
        self.guild_id = guild_id
        options = [
            discord.SelectOption(label=salon.name[:100], value=cid, emoji="🎙️")
            for cid, salon in hubs_valides(guild, gconf)
        ]
        super().__init__(placeholder="Choisis le hub à configurer...", options=options[:25], min_values=1, max_values=1)

    async def callback(self, interaction: discord.Interaction):
        gconf = config_serveur(self.guild_id)
        salon = interaction.guild.get_channel(int(self.values[0]))
        cat_cible_id = gconf["hubs"].get(self.values[0], {}).get("categorie_cible")
        cat_cible = interaction.guild.get_channel(int(cat_cible_id)) if cat_cible_id else None
        vue = VueTypesHub(self.guild_id, self.view.auteur_id, self.values[0], gconf)
        embed = discord.Embed(
            title=f"🔀 Configuration — {salon.name if salon else '#' + self.values[0]}",
            description=(
                "Sélectionne les types de vocaux disponibles dans ce hub (les types non cochés "
                "n'apparaîtront pas dans son menu), et choisis dans quelle catégorie Discord "
                "les vocaux créés depuis ce hub doivent être placés.\n\n"
                f"**Catégorie cible actuelle :** {cat_cible.mention if cat_cible else 'catégorie du hub (par défaut)'}"
            ),
            color=0xFFD700,
        )
        await maj_panneau(interaction, embed, vue)


class VueChoixHubTypes(VueBase):
    def __init__(self, guild_id: int, auteur_id: int, gconf: dict, guild: discord.Guild):
        super().__init__(guild_id, auteur_id)
        self.add_item(SelectHubPourTypes(guild_id, gconf, guild))

    @discord.ui.button(label="Retour", style=discord.ButtonStyle.secondary, emoji="⬅️", row=1)
    async def retour(self, interaction: discord.Interaction, button: discord.ui.Button):
        await retour_config(interaction, self.guild_id, self.auteur_id)


# ═══════════════════════════════════════════════════════════
#  GESTION DES CATÉGORIES DISCORD (renommage + accès par rôle)
# ═══════════════════════════════════════════════════════════
def embed_categorie(categorie: discord.CategoryChannel, gconf: dict) -> discord.Embed:
    info = gconf.get("categories", {}).get(str(categorie.id))
    if info and info.get("roles"):
        roles_txt = ", ".join(f"<@&{rid}>" for rid in info["roles"])
        acces = f"Restreinte à : {roles_txt}"
    else:
        acces = "Aucune restriction (accès normal du serveur)"

    embed = discord.Embed(
        title=f"📁 {categorie.name}",
        description=(
            "Gère cette catégorie : renommage et accès par rôle.\n"
            "Les hubs Beflow sont synchronisés avec les nouveaux droits. "
            "Les salons non gérés et les vocaux temporaires gardent leurs permissions spécifiques."
        ),
        color=0xFFD700,
    )
    embed.add_field(name="Accès", value=acces, inline=False)
    return embed


async def synchroniser_vocaux_categorie(categorie: discord.CategoryChannel, gconf: dict):
    """Resynchronise les permissions des vocaux de la catégorie, sauf les vocaux temporaires
    (sinon on écraserait les droits du créateur et l'état masqué/verrouillé)."""
    for salon in categorie.voice_channels:
        if str(salon.id) in gconf["hubs"]:
            try:
                await salon.edit(sync_permissions=True)
            except discord.HTTPException:
                logging.exception("Synchronisation hub %s impossible", salon.id)


class RenommerCategorieModal(discord.ui.Modal, title="Renommer la catégorie"):
    nouveau_nom = discord.ui.TextInput(label="Nouveau nom", max_length=100)

    def __init__(self, guild_id: int, auteur_id: int, categorie_id: int):
        super().__init__()
        self.guild_id = guild_id
        self.auteur_id = auteur_id
        self.categorie_id = categorie_id

    async def on_submit(self, interaction: discord.Interaction):
        if not staff_autorise(interaction) or interaction.user.id != self.auteur_id or interaction.guild_id != self.guild_id:
            await interaction.response.send_message("❌ Tu n'es plus autorisé à utiliser ce formulaire.", ephemeral=True)
            return
        await interaction.response.defer()
        categorie = interaction.guild.get_channel(self.categorie_id)
        if not categorie:
            await interaction.followup.send("❌ Catégorie introuvable.", ephemeral=True)
            return
        nom = self.nouveau_nom.value.strip()[:100]
        if not nom:
            await interaction.followup.send("❌ Le nom ne peut pas être vide.", ephemeral=True)
            return
        try:
            await categorie.edit(name=nom)
        except discord.HTTPException:
            await interaction.followup.send("❌ Impossible de renommer la catégorie.", ephemeral=True)
            return

        gconf = config_serveur(self.guild_id)
        vue = VueGererCategorie(self.guild_id, self.auteur_id, str(self.categorie_id))
        await maj_panneau(interaction, embed_categorie(categorie, gconf), vue)


class SelectRolesCategorie(discord.ui.RoleSelect):
    def __init__(self, guild_id: int, categorie_id: str):
        self.guild_id = guild_id
        self.categorie_id = categorie_id
        super().__init__(
            placeholder="Choisis le(s) rôle(s) autorisé(s) à voir cette catégorie...",
            min_values=1,
            max_values=10,
            row=1,
        )

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer()  # plusieurs appels API → on évite l'expiration des 3 s
        gconf = config_serveur(self.guild_id)
        categorie = interaction.guild.get_channel(int(self.categorie_id))
        if not categorie:
            await interaction.followup.send("❌ Catégorie introuvable.", ephemeral=True)
            return

        if not staff_roles_autorise(interaction):
            await interaction.followup.send("❌ Permissions Gérer les salons et Gérer les rôles requises.", ephemeral=True)
            return
        previous = gconf["categories"].get(self.categorie_id)
        overwrites, snapshots = restrict_category(
            copie_overwrites(categorie), interaction.guild, list(self.values), previous)
        try:
            await categorie.edit(overwrites=overwrites)
        except discord.HTTPException:
            await interaction.followup.send("❌ Impossible de modifier les permissions.", ephemeral=True)
            return
        gconf["categories"][self.categorie_id] = {
            "roles": [str(r.id) for r in self.values],
            "permission_snapshots": snapshots,
        }
        sauvegarder_configuration()
        await synchroniser_vocaux_categorie(categorie, gconf)

        vue = VueGererCategorie(self.guild_id, self.view.auteur_id, self.categorie_id)
        await maj_panneau(interaction, embed_categorie(categorie, gconf), vue)


class VueGererCategorie(VueBase):
    def __init__(self, guild_id: int, auteur_id: int, categorie_id: str):
        super().__init__(guild_id, auteur_id)
        self.categorie_id = categorie_id
        self.add_item(SelectRolesCategorie(guild_id, categorie_id))

    @discord.ui.button(label="Renommer", style=discord.ButtonStyle.primary, emoji="✏️", row=0)
    async def bouton_renommer(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(
            RenommerCategorieModal(self.guild_id, self.auteur_id, int(self.categorie_id))
        )

    @discord.ui.button(label="Retirer la restriction", style=discord.ButtonStyle.danger, emoji="🔓", row=0)
    async def bouton_retirer(self, interaction: discord.Interaction, button: discord.ui.Button):
        gconf = config_serveur(self.guild_id)
        categorie = interaction.guild.get_channel(int(self.categorie_id))
        if not categorie:
            await interaction.response.send_message("❌ Catégorie introuvable.", ephemeral=True)
            return

        if not staff_roles_autorise(interaction):
            await interaction.response.send_message(
                "❌ Permissions Gérer les salons et Gérer les rôles requises.", ephemeral=True)
            return
        info = gconf.get("categories", {}).get(self.categorie_id)
        if not info:
            await interaction.response.send_message("❌ Cette catégorie n'a pas de restriction configurée.", ephemeral=True)
            return

        await interaction.response.defer()

        snapshots = info.get("permission_snapshots")
        if snapshots is None:
            await interaction.followup.send(
                "⚠️ Ancienne restriction V1 sans sauvegarde des permissions. "
                "Ne peut pas être retirée automatiquement sans risquer de perdre les droits existants. "
                "Reconfigure d'abord les rôles avec la V2, ou corrige manuellement.", ephemeral=True)
            return
        overwrites = copie_overwrites(categorie)
        for field, snapshot in snapshots.items():
            restore_field(overwrites, interaction.guild, field, snapshot)
        try:
            await categorie.edit(overwrites=overwrites)
        except discord.HTTPException:
            await interaction.followup.send("❌ Impossible de modifier les permissions.", ephemeral=True)
            return
        gconf["categories"].pop(self.categorie_id, None)
        sauvegarder_configuration()
        await synchroniser_vocaux_categorie(categorie, gconf)

        vue = VueGererCategorie(self.guild_id, self.auteur_id, self.categorie_id)
        await maj_panneau(interaction, embed_categorie(categorie, gconf), vue)

    @discord.ui.button(label="Retour", style=discord.ButtonStyle.secondary, emoji="⬅️", row=2)
    async def retour(self, interaction: discord.Interaction, button: discord.ui.Button):
        await retour_config(interaction, self.guild_id, self.auteur_id)


class SelectCategorieAGerer(discord.ui.ChannelSelect):
    def __init__(self, guild_id: int):
        self.guild_id = guild_id
        super().__init__(
            placeholder="Choisis la catégorie à gérer...",
            channel_types=[discord.ChannelType.category],
            min_values=1,
            max_values=1,
        )

    async def callback(self, interaction: discord.Interaction):
        gconf = config_serveur(self.guild_id)
        categorie = interaction.guild.get_channel(self.values[0].id)
        if not categorie:
            await interaction.response.send_message("❌ Catégorie introuvable.", ephemeral=True)
            return
        vue = VueGererCategorie(self.guild_id, self.view.auteur_id, str(categorie.id))
        await maj_panneau(interaction, embed_categorie(categorie, gconf), vue)


class VueChoixCategorie(VueBase):
    def __init__(self, guild_id: int, auteur_id: int):
        super().__init__(guild_id, auteur_id)
        self.add_item(SelectCategorieAGerer(guild_id))

    @discord.ui.button(label="Retour", style=discord.ButtonStyle.secondary, emoji="⬅️", row=1)
    async def retour(self, interaction: discord.Interaction, button: discord.ui.Button):
        await retour_config(interaction, self.guild_id, self.auteur_id)


# ═══════════════════════════════════════════════════════════
#  MOTS INTERDITS (bloque le renommage si présents dans le nouveau nom)
# ═══════════════════════════════════════════════════════════
class BanwordsModal(discord.ui.Modal, title="Mots interdits (renommage)"):
    mots = discord.ui.TextInput(
        label="Mots interdits, séparés par des virgules",
        style=discord.TextStyle.paragraph,
        required=False,
        max_length=500,
    )

    def __init__(self, guild_id: int, auteur_id: int, gconf: dict):
        super().__init__()
        self.guild_id = guild_id
        self.auteur_id = auteur_id
        self.mots.default = ", ".join(gconf.get("banwords", []))

    async def on_submit(self, interaction: discord.Interaction):
        if not staff_autorise(interaction) or interaction.user.id != self.auteur_id or interaction.guild_id != self.guild_id:
            await interaction.response.send_message("❌ Tu n'es plus autorisé à utiliser ce formulaire.", ephemeral=True)
            return
        gconf = config_serveur(self.guild_id)
        gconf["banwords"] = [m.strip().lower() for m in self.mots.value.split(",") if m.strip()]
        sauvegarder_configuration()

        await retour_config(interaction, self.guild_id, self.auteur_id)


# ── Panneau principal ────────────────────────────────────────
class VueConfig(VueBase):
    def __init__(self, guild_id: int, auteur_id: int):
        super().__init__(guild_id, auteur_id)

    @discord.ui.button(label="Nouveau type", style=discord.ButtonStyle.success, emoji="➕", row=0)
    async def bouton_nouveau_type(self, interaction: discord.Interaction, button: discord.ui.Button):
        vue = OptionsTypeView(self.guild_id, self.auteur_id)
        embed = discord.Embed(
            title="➕ Nouveau type — options",
            description=(
                "Règle les options ci-dessous, puis clique sur **Continuer** pour saisir "
                "le nom, la limite, l'emoji et le format de nom du type."
            ),
            color=0xFFD700,
        )
        await maj_panneau(interaction, embed, vue)

    @discord.ui.button(label="Modifier un type", style=discord.ButtonStyle.primary, emoji="✏️", row=0)
    async def bouton_modifier_type(self, interaction: discord.Interaction, button: discord.ui.Button):
        gconf = config_serveur(self.guild_id)
        if not gconf["types"]:
            await interaction.response.send_message("❌ Aucun type à modifier.", ephemeral=True)
            return
        vue = VueModifierType(self.guild_id, self.auteur_id, gconf)
        embed = discord.Embed(title="✏️ Modifier un type", description="Choisis le type à modifier.", color=0xFFD700)
        await maj_panneau(interaction, embed, vue)

    @discord.ui.button(label="Supprimer un type", style=discord.ButtonStyle.danger, emoji="🗑️", row=0)
    async def bouton_supprimer_type(self, interaction: discord.Interaction, button: discord.ui.Button):
        gconf = config_serveur(self.guild_id)
        if not gconf["types"]:
            await interaction.response.send_message("❌ Aucun type à supprimer.", ephemeral=True)
            return
        vue = VueSupprimerType(self.guild_id, self.auteur_id, gconf)
        embed = discord.Embed(title="🗑️ Supprimer un type", description="Choisis le type à retirer.", color=0xFF4444)
        await maj_panneau(interaction, embed, vue)

    @discord.ui.button(label="Nouveau hub", style=discord.ButtonStyle.success, emoji="🎙️", row=1)
    async def bouton_nouveau_hub(self, interaction: discord.Interaction, button: discord.ui.Button):
        gconf = config_serveur(self.guild_id)
        if not gconf["types"]:
            await interaction.response.send_message(
                "❌ Crée d'abord au moins un type de vocal avant de créer un hub.", ephemeral=True
            )
            return
        vue = VueNouveauHub(self.guild_id, self.auteur_id, interaction.guild)
        embed = discord.Embed(
            title="🎙️ Nouveau hub",
            description=(
                "Choisis un salon vocal existant à transformer, une catégorie pour le nouveau "
                "salon (optionnel), ou crée-en un nouveau directement. Le nouveau hub reçoit "
                "automatiquement tous les types existants — modifiable ensuite via **Configurer un hub**."
            ),
            color=0x00FF88,
        )
        await maj_panneau(interaction, embed, vue)

    @discord.ui.button(label="Supprimer un hub", style=discord.ButtonStyle.danger, emoji="🚫", row=1)
    async def bouton_supprimer_hub(self, interaction: discord.Interaction, button: discord.ui.Button):
        gconf = config_serveur(self.guild_id)
        if not hubs_valides(interaction.guild, gconf):
            await interaction.response.send_message("❌ Aucun hub à supprimer.", ephemeral=True)
            return
        vue = VueSupprimerHub(self.guild_id, self.auteur_id, gconf, interaction.guild)
        embed = discord.Embed(title="🚫 Supprimer un hub", description="Choisis le hub à retirer.", color=0xFF4444)
        await maj_panneau(interaction, embed, vue)

    @discord.ui.button(label="Configurer un hub", style=discord.ButtonStyle.primary, emoji="🔀", row=1)
    async def bouton_types_hub(self, interaction: discord.Interaction, button: discord.ui.Button):
        gconf = config_serveur(self.guild_id)
        if not hubs_valides(interaction.guild, gconf):
            await interaction.response.send_message("❌ Crée d'abord au moins un hub.", ephemeral=True)
            return
        if not gconf["types"]:
            await interaction.response.send_message("❌ Crée d'abord au moins un type de vocal.", ephemeral=True)
            return
        vue = VueChoixHubTypes(self.guild_id, self.auteur_id, gconf, interaction.guild)
        embed = discord.Embed(
            title="🔀 Configurer un hub",
            description="Choisis le hub à configurer (types disponibles et catégorie cible).",
            color=0xFFD700,
        )
        await maj_panneau(interaction, embed, vue)

    @discord.ui.button(label="Gérer une catégorie", style=discord.ButtonStyle.primary, emoji="📁", row=2)
    async def bouton_gerer_categorie(self, interaction: discord.Interaction, button: discord.ui.Button):
        vue = VueChoixCategorie(self.guild_id, self.auteur_id)
        embed = discord.Embed(
            title="📁 Gérer une catégorie",
            description="Choisis la catégorie Discord à renommer ou à restreindre par rôle.",
            color=0xFFD700,
        )
        await maj_panneau(interaction, embed, vue)

    @discord.ui.button(label="Mots interdits", style=discord.ButtonStyle.danger, emoji="🚫", row=2)
    async def bouton_banwords(self, interaction: discord.Interaction, button: discord.ui.Button):
        gconf = config_serveur(self.guild_id)
        await interaction.response.send_modal(BanwordsModal(self.guild_id, self.auteur_id, gconf))

    @discord.ui.button(label="Actualiser", style=discord.ButtonStyle.secondary, emoji="🔄", row=3)
    async def bouton_actualiser(self, interaction: discord.Interaction, button: discord.ui.Button):
        gconf = config_serveur(self.guild_id)
        await interaction.response.edit_message(embed=embed_config(gconf), view=self)

    @discord.ui.button(label="Fermer", style=discord.ButtonStyle.secondary, emoji="✖️", row=3)
    async def bouton_fermer(self, interaction: discord.Interaction, button: discord.ui.Button):
        embed = discord.Embed(description="Panneau de configuration fermé.", color=0x2B2D31)
        await interaction.response.edit_message(embed=embed, view=None)
        self.stop()


@groupe_vocal.command(name="config", description="Panneau de configuration interactif (types & hubs) en un seul endroit")
@app_commands.checks.has_permissions(manage_channels=True)
async def vocal_config(interaction: discord.Interaction):
    gconf = config_serveur(interaction.guild_id)
    vue = VueConfig(interaction.guild_id, interaction.user.id)
    await interaction.response.send_message(embed=embed_config(gconf), view=vue, ephemeral=True)
    vue.message = await interaction.original_response()


# ── /vocal renommer et /vocal limite ────────────────────────
# Retirés : ces actions se font désormais via le panneau de boutons
# envoyé automatiquement dans chaque vocal temporaire créé.

# ═══════════════════════════════════════════════════════════
#  PANEL D'INVITATION
# ═══════════════════════════════════════════════════════════
class InviteView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

        self.add_item(
            discord.ui.Button(
                label="Inviter Beflow",
                emoji="<:love:1486416128254152826>",
                style=discord.ButtonStyle.link,
                url=LIEN_INVITATION,
            )
        )


# ═══════════════════════════════════════════════════════════
#  AIDE — /help avec panel interactif
# ═══════════════════════════════════════════════════════════
CMDS = {
    "vocal config": {
        "emoji": "⚙️",
        "titre": "/vocal config",
        "categorie": "Staff",
        "desc": "Panneau de configuration interactif tout-en-un.",
        "details": (
            "Ouvre un panneau avec des boutons pour tout gérer sans taper d'autres commandes : "
            "créer/modifier/supprimer un type (nom, limite, emoji, format de nom, options), "
            "créer/supprimer un hub, configurer un hub (types disponibles + catégorie cible), "
            "gérer une catégorie Discord (renommage, accès par rôle) et gérer la liste des mots "
            "interdits.\n\n"
            "**Permission requise :** Gérer les salons"
        ),
    },
    "types de vocaux": {
        "emoji": "🔊",
        "titre": "Créer / modifier un type",
        "categorie": "Staff",
        "desc": "Configure le nom, l'emoji, la limite et les options d'un type de vocal.",
        "details": (
            "**Nouveau type** et **Modifier un type** ouvrent le même formulaire en deux temps : "
            "d'abord les options oui/non (nom modifiable, limite modifiable, masqué par défaut), "
            "puis le nom, la limite, l'emoji et le format de nom.\n\n"
            "Le champ **Emoji** accepte un emoji Unicode classique ou un emoji personnalisé du "
            "serveur — colle-le simplement dans le champ. Il apparaît dans le menu de choix du "
            "type et dans le panneau du vocal créé.\n\n"
            "Modifier le nom d'un type met automatiquement à jour les hubs qui le référencent.\n\n"
            "**Limite Discord :** un emoji personnalisé peut apparaître dans les menus et messages "
            "du bot, mais jamais dans le nom du salon vocal lui-même — Discord n'affiche que les "
            "emojis Unicode classiques dans les noms de salons et de catégories, quel que soit le "
            "bot utilisé."
        ),
    },
    "configurer un hub": {
        "emoji": "🔀",
        "titre": "Configurer un hub",
        "categorie": "Staff",
        "desc": "Choisis les types disponibles et la catégorie cible de chaque hub.",
        "details": (
            "Un hub ne propose que les types qui lui sont explicitement assignés. Un nouveau hub "
            "reçoit tous les types existants au moment de sa création ; tu peux ensuite en retirer "
            "certains (par exemple pour garder un type de test uniquement disponible dans un hub "
            "privé, sans qu'il apparaisse dans les autres hubs).\n\n"
            "Tu peux aussi choisir une **catégorie cible** : les vocaux créés depuis ce hub seront "
            "placés dans cette catégorie plutôt que dans celle du hub. Un bouton permet de revenir "
            "au comportement par défaut."
        ),
    },
    "gérer une catégorie": {
        "emoji": "📁",
        "titre": "Gérer une catégorie",
        "categorie": "Staff",
        "desc": "Renomme une catégorie Discord ou restreint son accès par rôle.",
        "details": (
            "Choisis une catégorie existante pour la renommer, ou pour la restreindre à un ou "
            "plusieurs rôles : la catégorie, les hubs gérés qui s'y trouvent et les futurs "
            "vocaux ciblant cette catégorie suivent la restriction ; les salons non gérés "
            "et les vocaux temporaires déjà en cours gardent leurs permissions. "
            "**Retirer la restriction** rétablit l'accès normal du serveur."
        ),
    },
    "mots interdits": {
        "emoji": "🚫",
        "titre": "Mots interdits",
        "categorie": "Staff",
        "desc": "Bloque le renommage d'un vocal si son nouveau nom contient un mot interdit.",
        "details": (
            "Liste de mots (séparés par des virgules) que les membres ne peuvent pas utiliser en "
            "renommant leur vocal depuis le panneau. Si le nouveau nom contient un des mots de la "
            "liste, le renommage est refusé."
        ),
    },
    "panneau vocal": {
        "emoji": "🎛️",
        "titre": "Panneau dans ton vocal",
        "categorie": "Membres",
        "desc": "Gère ton vocal temporaire directement depuis ses boutons.",
        "details": (
            "Dès qu'un vocal temporaire est créé, un message avec des boutons apparaît dans son "
            "chat :\n"
            "**✏️ Renommer** et **👥 Limite** — utilisables seulement si le staff a autorisé cette "
            "modification pour ce type de vocal (réglable indépendamment via `/vocal config`). Le "
            "renommage est refusé s'il contient un mot interdit.\n"
            "**🙈 Masquer / 👁️ Afficher** — cache le vocal pour tout le monde sauf toi.\n"
            "**🔒 Verrouiller / 🔓 Déverrouiller** — empêche quiconque de te rejoindre (sans "
            "déconnecter les membres déjà présents).\n\n"
            "Ces boutons sont utilisables uniquement par le créateur du vocal. Si le créateur "
            "quitte le vocal alors qu'il reste du monde, la propriété est transférée "
            "automatiquement au premier membre présent."
        ),
    },
}


def embed_apercu() -> discord.Embed:
    embed = discord.Embed(
        title="❓ Aide — Vocaux temporaires",
        description="Sélectionne une commande dans le menu ci-dessous pour voir son détail.",
        color=0xFFD700,
    )
    embed.add_field(
        name="🛠️ Staff",
        value=(
            "`/vocal config` — ⭐ Panneau interactif tout-en-un\n"
            "🔊 Créer / modifier un type — nom, emoji, limite, options\n"
            "🔀 Configurer un hub — types disponibles et catégorie cible\n"
            "📁 Gérer une catégorie — renommage et accès par rôle\n"
            "🚫 Mots interdits — bloquer certains mots au renommage"
        ),
        inline=False,
    )
    embed.add_field(
        name="👤 Membres",
        value="🎛️ Un panneau de boutons (renommer / limite / masquer / verrouiller) apparaît directement dans chaque vocal créé.",
        inline=False,
    )
    embed.set_footer(text="Vocaux temporaires • Menu valable 3 minutes")
    return embed


class AideSelect(discord.ui.Select):
    def __init__(self):
        options = [
            discord.SelectOption(
                label="Vue d'ensemble", value="apercu",
                description="Retour à la liste des commandes", emoji="📋",
            )
        ]
        for cle, info in CMDS.items():
            options.append(
                discord.SelectOption(
                    label=info["titre"], value=cle,
                    description=info["desc"][:100], emoji=info["emoji"],
                )
            )
        super().__init__(
            placeholder="Sélectionne une commande pour en savoir plus...",
            options=options[:25], min_values=1, max_values=1,
        )

    async def callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.view.auteur_id:
            await interaction.response.send_message("❌ Ce menu ne t'est pas destiné.", ephemeral=True)
            return

        if self.values[0] == "apercu":
            embed = embed_apercu()
        else:
            info = CMDS[self.values[0]]
            embed = discord.Embed(
                title=f"{info['emoji']} {info['titre']}",
                description=info["desc"],
                color=0xFFD700,
            )
            embed.add_field(name="📖 Détails", value=info["details"][:1024], inline=False)
            embed.set_footer(text=f"Catégorie : {info['categorie']} • Reviens à la vue d'ensemble dans le menu")

        await interaction.response.edit_message(embed=embed, view=self.view)


class AideView(discord.ui.View):
    def __init__(self, auteur_id: int):
        super().__init__(timeout=180)
        self.auteur_id = auteur_id
        self.message: discord.Message | None = None
        self.add_item(AideSelect())

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True
        if self.message:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass


@arbre.command(name="help", description="Affiche l'aide du bot vocaux temporaires")
async def help_cmd(interaction: discord.Interaction):
    view = AideView(interaction.user.id)
    await interaction.response.send_message(embed=embed_apercu(), view=view, ephemeral=True)
    view.message = await interaction.original_response()


arbre.add_command(groupe_vocal)


@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return

    if bot.user in message.mentions:
        # Les emojis personnalisés ne s'affichent pas dans les titres d'embed :
        # on les place dans la description et les champs.
        embed = discord.Embed(
            title="Beflow",
            description=(
                "<a:r2:1485268957664383038> Salut ! Je suis **Beflow**.\n\n"
                "Je crée automatiquement des **vocaux temporaires** entièrement configurables.\n\n"
                "Clique sur le bouton ci-dessous pour m'ajouter à ton serveur."
            ),
            color=0x5865F2,
        )

        embed.add_field(
            name="Fonctionnalités",
            value=(
                "<a:ar:1485250353854288035> Vocaux temporaires\n"
                "<a:ar:1485250353854288035> Types de salons\n"
                "<a:ar:1485250353854288035> Hubs\n"
                "<a:ar:1485250353854288035> Configuration interactive\n"
                "<a:ar:1485250353854288035> Gestion des vocs"
            ),
            inline=False,
        )

        embed.set_footer(text="Développé par Bebefirm1")

        try:
            await message.reply(
                embed=embed,
                view=InviteView(),
                mention_author=False,
            )
        except discord.HTTPException:
            pass

    await bot.process_commands(message)


# The entry point is ../bot.py, so importing this module never connects to Discord.