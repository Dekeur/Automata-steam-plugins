"""
Automata (farm d'heures Steam / Steam hours farm)
--------------------------------------
Fait tourner tes jeux Steam « à vide » : Steam croit que le jeu est lancé et
compte le temps de jeu (et les drops de cartes), sans rien exécuter du jeu.
Interface en français ou en anglais (choix au premier lancement, puis dans Options).

Chaque jeu farmé est un petit processus qui s'annonce à Steam via
steam_api64.dll (la DLL officielle de Valve, copiée depuis un de tes jeux).

Lancement : double-clic sur Lancer.bat (installe Pillow et pystray au premier lancement)
            ou : .venv\\Scripts\\pythonw.exe farm.py   — option --auto : caché + farm auto
            ou : SteamHoursFarm.exe (version compilée, voir construire_exe.bat)
Steam doit être ouvert et connecté.
"""
from __future__ import annotations

import ctypes
import datetime as dt
import hashlib
import json
import math
import os
import queue
import random
import re
import shutil
import struct
import subprocess
import sys
import threading
import time
import traceback
import winreg
from ctypes import wintypes
from pathlib import Path

# Pillow, pystray et tkinter ne sont importés que pour la fenêtre (voir interface()) :
# les processus de farm restent ainsi très légers.

NOM = "Automata"                  # nom affiché de l'appli
VERSION = "1.6.0"
PROJET = "https://github.com/Dekeur/Automata-steam-plugins"
MAJ_API = "https://api.github.com/repos/Dekeur/Automata-steam-plugins/releases/latest"
GELE = getattr(sys, "frozen", False)          # True dans le .exe construit avec PyInstaller
ICI = Path(sys.executable).resolve().parent if GELE else Path(__file__).resolve().parent
SCRIPT = "" if GELE else str(Path(__file__).resolve())
DLL = ICI / "steam_api64.dll"
CONFIG = ICI / "config.json"
CONFIG_SECOURS = ICI / "config.json.bak"   # dernière config lisible, recopiée à chaque lancement
JOURNAL = ICI / "journal.txt"
JOURNAL_MAX = 1_000_000                    # au-delà, journal.txt ne garde que sa fin
SIGNAL = ICI / ".afficher"        # un 2e lancement demande à la fenêtre déjà ouverte de se montrer
CLE_RUN = r"Software\Microsoft\Windows\CurrentVersion\Run"
NOM_RUN = "FarmHeuresSteam"
CLE_APPLI = r"Software\FarmHeuresSteam"   # langue retenue pour tout le PC (valeur « Langue »)
MAX_SIMULTANES = 32               # Steam refuse au-delà de 32 jeux lancés en même temps
APP_TEST = 480                    # Spacewar, gratuit pour tous : sert à interroger Steam
APP_REDIST = 228980               # « Steamworks Common Redistributables » : installé par Steam, pas un jeu
SANS_FENETRE = 0x08000000         # CREATE_NO_WINDOW
REMB_JOURS, REMB_SECONDES = 14, 2 * 3600   # règle de remboursement de Steam
OBJECTIF_REMB = 110 / 60          # « farmer jusqu'à 1 h 50 » pour garder le remboursement
ESSAIS_MAX, DELAI_ESSAI = 3, 30   # nouvel essai quand Steam refuse un jeu (souvent : Steam pas prêt)
PAUSE_STEAM = 20                  # secondes d'attente après le retour de Steam (ou la fin d'un jeu) avant de relancer
VERIF_JEU = 5                     # secondes entre deux recherches d'un vrai jeu lancé
GRACE_ARRET = 60                  # Steam marque encore « lancé » un jeu farmé quelques secondes après son arrêt
STATUTS = {0: "offline", 1: "online", 3: "away", 4: "snooze", 7: "invisible"}   # EPersonaState -> steam://friends/status/…
INVISIBLE = 7

FOND, PANNEAU, BORD = "#121419", "#1b1f27", "#2a303b"
TEXTE, DOUX, GRIS = "#eef0f4", "#c3c8d2", "#8b93a3"
ACCENT, ACCENT_CLAIR, ENCRE = "#66c0f4", "#93d5fb", "#0b1220"
VERT, ROUGE, ORANGE, SELECTION = "#7bd88f", "#f08080", "#f0b45a", "#263850"
BARRE = "#3b8fd0"                 # barres des statistiques (validée pour le fond sombre)
EGO = "#ff374d"                   # rouge du logo Automata
OR = "#f0c75a"                    # bouton Succès (trophée)
FILTRES = (("tous", "Tous les jeux"), ("coches", "Cochés"), ("encours", "En cours"), ("objectif", "Avec objectif"),
           ("remb", "Remboursables"), ("jamais", "Jamais lancés"), ("installes", "Installés"),
           ("presque", "Succès presque finis"))
PRESQUE = 0.8                     # filtre « Succès presque finis » : au moins 80 % des succès, sans les avoir tous
FILTRES_SUCCES = (("tous", "Tous les succès"), ("verrouilles", "Verrouillés"), ("debloques", "Débloqués"))
FILTRES_VALEUR = (("tous", "Tous les jeux payants"), ("jamais", "Jamais lancés"), ("promo", "En promo en ce moment"))
HASARD = (("jamais", "Jamais lancés"), ("peu", "Joués moins de 2 h"), ("installes", "Installés"),
          ("tous", "Tous mes jeux"))
# menu Outils : boutique Steam (prix, noms) et liste de souhaits. Ces adresses ne reçoivent que des numéros de jeux,
# sauf la liste de souhaits, qui reçoit l'identifiant public du compte.
BOUTIQUE = "https://store.steampowered.com/api/appdetails"
API_ARTICLES = "https://api.steampowered.com/IStoreBrowseService/GetItems/v1/"
API_SOUHAITS = "https://api.steampowered.com/IWishlistService/GetWishlist/v1/"
LOT_PRIX = 400                    # jeux par requête de prix (la boutique en accepte plus de 500)
GRATUIT = "gratuit"               # prix_steam : jeu sans prix dans la boutique
MAX_CAPTURES = 240                # vignettes affichées par jeu dans la fenêtre Captures d'écran
SYMBOLES = {"EUR": "€", "USD": "$", "GBP": "£", "JPY": "¥", "CNY": "¥", "KRW": "₩", "INR": "₹", "UAH": "₴",
            "PLN": "zł", "BRL": "R$", "CAD": "CA$", "AUD": "A$", "RUB": "₽", "TRY": "₺"}
JOURS = {"fr": ("lun.", "mar.", "mer.", "jeu.", "ven.", "sam.", "dim."),
         "en": ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")}
MOIS_EN = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
LANGUES = (("fr", "Français"), ("en", "English"))
LANGUE = "fr"                     # "fr" ou "en" : lue dans config.json au démarrage


# ───────────────────────── Langue ─────────────────────────

def T(s: str, **kw) -> str:
    """Traduit un texte écrit en français (voir le dictionnaire EN en bas du fichier)."""
    if LANGUE == "en":
        s = EN.get(s, s)
    return s.format(**kw) if kw else s


def pluriel(n: int, un: str, plusieurs: str, **kw) -> str:
    """En français 0 et 1 sont au singulier, en anglais seulement 1."""
    singulier = n <= 1 if LANGUE == "fr" else n == 1
    return T(un if singulier else plusieurs, n=n, **kw)


def langue_systeme() -> str:
    try:
        return "fr" if ctypes.windll.kernel32.GetUserDefaultUILanguage() & 0x3FF == 0x0C else "en"
    except Exception:
        return "en"


def langue_retenue() -> str | None:
    """Langue déjà choisie sur ce PC, même par une autre copie de l'appli (une nouvelle version dézippée
    ailleurs, par exemple) : elle est gardée dans le registre en plus de config.json."""
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, CLE_APPLI) as k:
            v = winreg.QueryValueEx(k, "Langue")[0]
    except OSError:
        return None
    return v if v in ("fr", "en") else None


def retenir_langue(code: str) -> None:
    try:
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, CLE_APPLI) as k:
            winreg.SetValueEx(k, "Langue", 0, winreg.REG_SZ, code)
    except OSError:
        pass


def langue_config() -> str | None:
    try:
        choix = json.loads(CONFIG.read_text(encoding="utf-8")).get("langue")
    except (OSError, ValueError, AttributeError):
        return None
    return choix if choix in ("fr", "en") else None


def charger_langue() -> None:
    global LANGUE
    LANGUE = langue_config() or langue_retenue() or langue_systeme()


def journal(msg: str) -> None:
    try:
        if JOURNAL.exists() and JOURNAL.stat().st_size > JOURNAL_MAX:
            fin = JOURNAL.read_bytes()[-JOURNAL_MAX // 5:]
            JOURNAL.write_bytes(fin[fin.find(b"\n") + 1:])
        with JOURNAL.open("a", encoding="utf-8") as f:
            f.write(time.strftime("%Y-%m-%d %H:%M:%S  ") + msg + "\n")
    except OSError:
        pass


def ecrire_atomique(chemin: Path, texte: str) -> None:
    """Écrit un fichier .tmp puis le met à la place de l'ancien : une coupure de courant pendant l'écriture
    laisse l'ancien fichier intact au lieu d'un fichier à moitié écrit."""
    tmp = chemin.with_name(chemin.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(texte)
        f.flush()
        os.fsync(f.fileno())
    for essai in range(5):
        try:
            os.replace(tmp, chemin)
            return
        except PermissionError:          # fichier ouvert un instant par un antivirus, une sauvegarde…
            time.sleep(0.1 * (essai + 1))
    os.replace(tmp, chemin)


def lire_config() -> dict:
    """config.json. S'il est abîmé : la copie de secours, et le fichier abîmé est mis de côté (jamais écrasé).
    Une config lisible est recopiée dans config.json.bak à chaque lancement."""
    try:
        brut = CONFIG.read_bytes()
    except OSError:
        return {}
    try:
        cfg = json.loads(brut.decode("utf-8"))
        if not isinstance(cfg, dict):
            raise ValueError("pas un objet JSON")
    except ValueError as e:
        abime = CONFIG.with_name(time.strftime("config.abime-%Y%m%d-%H%M%S.json"))
        try:
            shutil.copy2(CONFIG, abime)
        except OSError:
            pass
        try:
            cfg = json.loads(CONFIG_SECOURS.read_text(encoding="utf-8"))
            if not isinstance(cfg, dict):
                raise ValueError
        except (OSError, ValueError):
            journal(T("config.json illisible ({e}) et pas de copie de secours : réglages remis à zéro, "
                      "fichier abîmé gardé dans {f}", e=e, f=abime.name))
            return {}
        journal(T("config.json illisible ({e}) : copie de secours reprise, fichier abîmé gardé dans {f}",
                  e=e, f=abime.name))
        return cfg
    try:
        ecrire_atomique(CONFIG_SECOURS, json.dumps(cfg))
    except OSError:
        pass
    return cfg


def interpreteur_sans_console(base: bool) -> str:
    """pythonw.exe du venv (base=False) ou de l'installation Python d'origine (base=True)."""
    exe = Path(getattr(sys, "_base_executable", sys.executable) if base else sys.executable)
    w = exe.with_name("pythonw.exe")
    return str(w if w.exists() else exe)


def commande(*args: str, fenetre: bool = False) -> list[str]:
    """Ligne de commande pour relancer ce programme.

    fenetre=False : processus de farm / vérification (Python de base, sans Pillow).
    fenetre=True  : l'appli elle-même (Python du venv, avec Pillow et pystray).
    """
    if GELE:
        return [sys.executable, *args]
    return [interpreteur_sans_console(base=not fenetre), SCRIPT, *args]


# ───────────────────────── Lecture des fichiers de Steam ─────────────────────────

def dossier_steam() -> Path:
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam") as k:
        return Path(winreg.QueryValueEx(k, "SteamPath")[0])


def _processus_actif(pid: int) -> bool:
    k32 = ctypes.WinDLL("kernel32")
    k32.OpenProcess.restype = wintypes.HANDLE
    k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    h = k32.OpenProcess(0x1000, False, pid)        # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        return False
    code = wintypes.DWORD()
    ok = k32.GetExitCodeProcess(h, ctypes.byref(code))
    k32.CloseHandle(h)
    return bool(ok) and code.value == 259          # STILL_ACTIVE


def etat_steam() -> tuple[int, int]:
    """(compte connecté, pid de Steam) d'après le registre ; 0 si inconnu."""
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam\ActiveProcess") as k:
            uid = int(winreg.QueryValueEx(k, "ActiveUser")[0])
            try:
                pid = int(winreg.QueryValueEx(k, "pid")[0])
            except OSError:
                pid = 0
            return uid, pid
    except OSError:
        return 0, 0


def utilisateur_actif() -> int:
    return etat_steam()[0]


def steam_pret() -> bool:
    """Steam tourne et un compte est connecté."""
    uid, pid = etat_steam()
    return bool(uid) and (not pid or _processus_actif(pid))


def jeu_lance(exclure: set[int]) -> tuple[int, str] | None:
    """Un jeu que Steam marque comme lancé (Apps\\<appid>\\Running) et qui n'est pas dans `exclure`."""
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam\Apps") as k:
            i = 0
            while True:
                try:
                    nom = winreg.EnumKey(k, i)
                except OSError:
                    return None
                i += 1
                if not nom.isdigit() or int(nom) in exclure:
                    continue
                try:
                    with winreg.OpenKey(k, nom) as s:
                        if winreg.QueryValueEx(s, "Running")[0] != 1:
                            continue
                        try:
                            titre = str(winreg.QueryValueEx(s, "Name")[0])
                        except OSError:
                            titre = f"App {nom}"
                        return int(nom), titre
                except OSError:
                    continue
    except OSError:
        return None


class _DerniereSaisie(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]


def inactivite() -> float:
    """Secondes depuis la dernière action au clavier ou à la souris."""
    info = _DerniereSaisie(ctypes.sizeof(_DerniereSaisie), 0)
    if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(info)):
        return 0.0
    return ((ctypes.windll.kernel32.GetTickCount() - info.dwTime) & 0xFFFFFFFF) / 1000


def statut_steam(steam: Path) -> int | None:
    """Statut choisi dans la liste d'amis de Steam (1 en ligne, 3 absent, 7 invisible…), d'après localconfig.vdf."""
    uid = utilisateur_actif()
    try:
        texte = (steam / "userdata" / str(uid) / "config" / "localconfig.vdf").read_text(encoding="utf-8",
                                                                                         errors="replace")
    except OSError:
        return None
    m = re.search(r'"FriendStoreLocalPrefs_\d+"\s+"[^\n]*?ePersonaState\\?"\s*:\s*(\d+)', texte)
    return int(m.group(1)) if m else None


def changer_statut(code: int) -> None:
    """Change le statut Steam (liste d'amis) par le protocole steam://."""
    try:
        os.startfile("steam://friends/status/" + STATUTS.get(code, "online"))
    except OSError as e:
        journal(T("changement de statut Steam impossible : {e}", e=e))


_JETON = re.compile(r'"((?:[^"\\]|\\.)*)"|([{}])')


def lire_vdf(chemin: Path) -> dict:
    """VDF texte de Steam -> dict (clés en minuscules)."""
    racine: dict = {}
    pile, cle = [racine], None
    for m in _JETON.finditer(chemin.read_text(encoding="utf-8", errors="replace")):
        texte, accolade = m.groups()
        if accolade == "{":
            d: dict = {}
            pile[-1][(cle or "").lower()] = d
            pile.append(d)
            cle = None
        elif accolade == "}":
            if len(pile) > 1:
                pile.pop()
        elif cle is None:
            cle = texte
        else:
            pile[-1][cle.lower()] = texte.replace("\\\\", "\\")
            cle = None
    return racine


def bibliotheques(steam: Path) -> list[Path]:
    libs = [steam]
    try:
        for v in lire_vdf(steam / "steamapps" / "libraryfolders.vdf").get("libraryfolders", {}).values():
            if isinstance(v, dict) and v.get("path") and Path(v["path"]) not in libs:
                libs.append(Path(v["path"]))
    except OSError:
        pass
    return [p for p in libs if p.exists()]


def apps_locales(steam: Path) -> dict:
    """Section « apps » du localconfig.vdf du compte connecté (temps de jeu, dernier lancement…)."""
    uid = utilisateur_actif()
    dossiers = [steam / "userdata" / str(uid)] if uid else []
    if (steam / "userdata").exists():
        dossiers += sorted((steam / "userdata").iterdir(), key=lambda p: p.stat().st_mtime, reverse=True)
    for d in dossiers:
        f = d / "config" / "localconfig.vdf"
        if f.exists():
            return lire_vdf(f).get("userlocalconfigstore", {}).get("software", {}) \
                .get("valve", {}).get("steam", {}).get("apps", {})
    return {}


def temps_de_jeu(steam: Path, apps: dict | None = None) -> dict[int, int]:
    """appid -> minutes jouées, d'après le localconfig.vdf du compte connecté."""
    apps = apps_locales(steam) if apps is None else apps
    return {int(k): int(v.get("playtime", "0") or 0)
            for k, v in apps.items() if k.isdigit() and isinstance(v, dict)}


def lire_kv_binaire(data: bytes, p: int = 0, chaines: list[str] | None = None) -> tuple[dict, int]:
    """KeyValues binaire de Steam (appinfo.vdf, appcache/stats) -> (dictionnaire aux clés en minuscules, position
    après la fin). chaines : table des clés du format v29 d'appinfo.vdf (les clés y sont des index)."""
    def cstr(p: int) -> tuple[str, int]:
        fin = data.index(b"\0", p)
        return data[p:fin].decode("utf-8", "replace"), fin + 1

    d: dict = {}
    while p < len(data):
        t = data[p]
        p += 1
        if t in (8, 11):
            break
        if chaines is None:
            k, p = cstr(p)
        else:
            k = chaines[struct.unpack_from("<i", data, p)[0]]
            p += 4
        if t == 0:
            v, p = lire_kv_binaire(data, p, chaines)
        elif t == 1:
            v, p = cstr(p)
        elif t == 2:
            v, p = struct.unpack_from("<i", data, p)[0], p + 4
        elif t == 3:
            v, p = struct.unpack_from("<f", data, p)[0], p + 4
        elif t in (4, 6):
            v, p = struct.unpack_from("<I", data, p)[0], p + 4
        elif t in (7, 10):
            v, p = struct.unpack_from("<q", data, p)[0], p + 8
        else:
            raise ValueError(f"type {t}")
        d[k.lower()] = v
    return d, p


def compte_utilisateur(steam: Path) -> int:
    """Compte Steam connecté, sinon le dernier connecté sur ce PC (loginusers.vdf). 0 si aucun."""
    uid = utilisateur_actif()
    if uid:
        return uid
    try:
        users = lire_vdf(steam / "config" / "loginusers.vdf").get("users", {})
    except (OSError, ValueError):
        return 0
    # « MostRecent » n'est plus écrit par les Steam récents : sinon, la dernière connexion (« Timestamp »)
    comptes = []
    for sid, u in users.items():
        if isinstance(u, dict) and sid.isdigit():
            t = str(u.get("timestamp", ""))
            comptes.append((u.get("mostrecent") == "1", int(t) if t.isdigit() else 0, int(sid)))
    return max(comptes)[2] & 0xFFFFFFFF if comptes else 0


def succes_locaux(steam: Path, uid: int) -> dict[int, tuple[int, int]]:
    """appid -> (succès débloqués, total), lus dans le cache de Steam (appcache/stats) sans lancer le jeu.
    Le schéma donne les succès (bits des stats de type ACHIEVEMENTS), le fichier du joueur les bits débloqués.
    Steam tient ce cache à jour quand le jeu ou la page du jeu lit les succès : il peut avoir du retard."""
    out: dict[int, tuple[int, int]] = {}
    dossier = steam / "appcache" / "stats"
    if not uid or not dossier.is_dir():
        return out
    for f in dossier.glob("UserGameStatsSchema_*.bin"):
        appid = f.stem.rpartition("_")[2]
        if not appid.isdigit():
            continue
        try:
            schema, _ = lire_kv_binaire(f.read_bytes())
            racine = next((v for v in schema.values() if isinstance(v, dict)), {})
            joueur = steam / "appcache" / "stats" / f"UserGameStats_{uid}_{appid}.bin"
            cache = lire_kv_binaire(joueur.read_bytes())[0].get("cache", {}) if joueur.exists() else {}
            faits = total = 0
            for sid, st in (racine.get("stats") or {}).items():
                if not isinstance(st, dict) or not isinstance(st.get("bits"), dict):
                    continue
                donnees = cache.get(sid)
                masque = int(donnees.get("data", 0) or 0) & 0xFFFFFFFF if isinstance(donnees, dict) else 0
                for b in st["bits"]:
                    total += 1
                    faits += (masque >> int(b)) & 1 if b.isdigit() and int(b) < 32 else 0
        except (OSError, ValueError, IndexError, struct.error):
            continue
        if total:
            out[int(appid)] = (faits, total)
    return out


def infos_applis(steam: Path) -> dict[int, tuple[str, str, bool]]:
    """appid -> (nom, type, gratuit) lus dans appcache/appinfo.vdf (format binaire de Steam)."""
    data = (steam / "appcache" / "appinfo.vdf").read_bytes()
    magique = struct.unpack_from("<I", data)[0]
    if magique not in (0x07564427, 0x07564428, 0x07564429):
        raise ValueError(T("appinfo.vdf : format inconnu {m}", m=f"{magique:#x}"))
    chaines: list[str] | None = None
    pos = 8
    if magique == 0x07564429:          # v29 : les clés sont des index dans une table de chaînes
        table = struct.unpack_from("<Q", data, 8)[0]
        pos = 16
        n = struct.unpack_from("<I", data, table)[0]
        p, chaines = table + 4, []
        for _ in range(n):
            fin = data.index(b"\0", p)
            chaines.append(data[p:fin].decode("utf-8", "replace"))
            p = fin + 1

    # après appid+taille : état, maj, jeton(8), sha1(20), n° de changement, [sha1 binaire(20)]
    entete = 40 + (0 if magique == 0x07564427 else 20)
    out: dict[int, tuple[str, str, bool]] = {}
    while True:
        appid, taille = struct.unpack_from("<II", data, pos)
        if appid == 0:
            break
        try:
            d, _ = lire_kv_binaire(data, pos + 8 + entete, chaines)
            ai = d.get("appinfo") or {}
            c, ext = ai.get("common") or {}, ai.get("extended") or {}
            if c.get("name"):
                gratuit = str(ext.get("isfreeapp", "0")) not in ("0", "")
                out[appid] = (c["name"], (c.get("type") or "").lower(), gratuit)
        except (ValueError, IndexError, struct.error):
            pass
        pos += 8 + taille
    return out


def chemins_images(steam: Path, appid: int) -> dict[str, Path]:
    """Images du jeu déjà en cache chez Steam (icône, bannière, logo…)."""
    racine = steam / "appcache" / "librarycache" / str(appid)
    out: dict[str, Path] = {}
    if not racine.is_dir():
        return out
    for dossier, _, fichiers in os.walk(racine):
        for f in fichiers:
            n, p = f.lower(), Path(dossier) / f
            if Path(dossier) == racine and re.fullmatch(r"[0-9a-f]{40}\.jpg", n):
                out["icone"] = p
            elif n in ("header.jpg", "library_header.jpg"):
                out.setdefault("entete", p)
            elif n == "library_hero.jpg":
                out["hero"] = p
            elif n == "logo.png":
                out["logo"] = p
            elif n == "library_600x900.jpg":
                out["capsule"] = p
    return out


def preparer_dll(steam: Path) -> bool:
    """Copie à côté du programme une steam_api64.dll prise dans un jeu installé, si besoin."""
    if DLL.exists():
        return True
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.LoadLibraryExW.restype = wintypes.HMODULE
    k32.LoadLibraryExW.argtypes = [wintypes.LPCWSTR, wintypes.HANDLE, wintypes.DWORD]
    k32.GetProcAddress.restype = ctypes.c_void_p
    k32.GetProcAddress.argtypes = [wintypes.HMODULE, ctypes.c_char_p]
    k32.FreeLibrary.argtypes = [wintypes.HMODULE]
    besoins = [("SteamAPI_InitFlat", "SteamAPI_Init"), ("SteamAPI_RunCallbacks",),
               ("SteamAPI_IsSteamRunning",), ("SteamAPI_ISteamApps_BIsSubscribedApp",),
               ("SteamAPI_SteamApps_v008", "SteamAPI_SteamApps_v009")]
    for lib in bibliotheques(steam):
        racine = lib / "steamapps" / "common"
        for dossier, sous, fichiers in os.walk(racine):
            if len(Path(dossier).relative_to(racine).parts) >= 6:
                sous.clear()
            nom = next((f for f in fichiers if f.lower() == "steam_api64.dll"), None)
            if not nom:
                continue
            f = Path(dossier) / nom
            h = k32.LoadLibraryExW(str(f), None, 0x1)   # DONT_RESOLVE_DLL_REFERENCES
            if not h:
                continue
            ok = all(any(k32.GetProcAddress(h, n.encode()) for n in g) for g in besoins)
            k32.FreeLibrary(h)
            if ok:
                shutil.copy2(f, DLL)
                journal(T("DLL copiée depuis {f}", f=f))
                return True
    return False


# ───────────────────────── Processus enfants (parlent à Steam) ─────────────────────────

def _steam_api():
    api = ctypes.CDLL(str(DLL))
    if hasattr(api, "SteamAPI_InitFlat"):
        err = ctypes.create_string_buffer(1024)
        api.SteamAPI_InitFlat.restype = ctypes.c_int
        r = api.SteamAPI_InitFlat(err)
        ok, msg = r == 0, err.value.decode("utf-8", "replace") or f"code {r}"
    else:
        api.SteamAPI_Init.restype = ctypes.c_bool
        ok, msg = bool(api.SteamAPI_Init()), T("SteamAPI_Init a échoué")
    api.SteamAPI_IsSteamRunning.restype = ctypes.c_bool
    return api, ok, msg


def mode_farm(appid: int, parent: int) -> int:
    """Se fait passer pour le jeu `appid` jusqu'à ce que la fenêtre principale se ferme."""
    os.environ["SteamAppId"] = os.environ["SteamGameId"] = str(appid)
    api, ok, msg = _steam_api()
    if not ok:
        journal(T("[{a}] Steam a refusé : {m}", a=appid, m=msg))
        return 4
    k32 = ctypes.WinDLL("kernel32")
    k32.OpenProcess.restype = wintypes.HANDLE
    k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k32.WaitForSingleObject.restype = wintypes.DWORD
    k32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    h = k32.OpenProcess(0x00100000, False, parent)   # SYNCHRONIZE
    code = 0
    while True:
        if h:
            if k32.WaitForSingleObject(h, 1000) == 0:   # la fenêtre principale a disparu
                break
        else:
            time.sleep(1)
        api.SteamAPI_RunCallbacks()
        if not api.SteamAPI_IsSteamRunning():
            journal(T("[{a}] Steam s'est fermé", a=appid))
            code = 5
            break
    api.SteamAPI_Shutdown()
    return code


def mode_verif(entree: Path, sortie: Path) -> int:
    """Demande à Steam quels appids le compte possède, et depuis quand (pour les remboursements)."""
    os.environ["SteamAppId"] = os.environ["SteamGameId"] = str(APP_TEST)
    api, ok, msg = _steam_api()
    if not ok:
        sortie.write_text(json.dumps({"erreur": msg}), encoding="utf-8")
        return 4
    nom = next(n for n in ("SteamAPI_SteamApps_v008", "SteamAPI_SteamApps_v009") if hasattr(api, n))
    fabrique = getattr(api, nom)
    fabrique.restype = ctypes.c_void_p
    apps = fabrique()

    def fonction(n: str, restype):
        if not hasattr(api, n):
            return None
        f = getattr(api, n)
        f.restype, f.argtypes = restype, [ctypes.c_void_p, ctypes.c_uint32]
        return f

    possede = fonction("SteamAPI_ISteamApps_BIsSubscribedApp", ctypes.c_bool)
    achat = fonction("SteamAPI_ISteamApps_GetEarliestPurchaseUnixTime", ctypes.c_uint32)
    famille = fonction("SteamAPI_ISteamApps_BIsSubscribedFromFamilySharing", ctypes.c_bool)
    ids = json.loads(entree.read_text(encoding="utf-8"))
    possedes = [i for i in ids if possede(apps, i)]
    res = {"possedes": possedes,
           "achats": {str(i): achat(apps, i) for i in possedes} if achat else {},
           "famille": [i for i in possedes if famille(apps, i)] if famille else []}
    sortie.write_text(json.dumps(res), encoding="utf-8")
    api.SteamAPI_Shutdown()
    return 0


def verifier_possession(ids: list[int]) -> dict | None:
    entree, sortie = ICI / "_verif_in.json", ICI / "_verif_out.json"
    entree.write_text(json.dumps(ids), encoding="utf-8")
    sortie.unlink(missing_ok=True)
    env = dict(os.environ, SteamAppId=str(APP_TEST), SteamGameId=str(APP_TEST))
    try:
        subprocess.run(commande("--verif", str(entree), str(sortie)),
                       env=env, cwd=ICI, creationflags=SANS_FENETRE, timeout=60,
                       stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        res = json.loads(sortie.read_text(encoding="utf-8"))
    except (OSError, subprocess.TimeoutExpired, ValueError) as e:
        journal(T("vérification des jeux possédés impossible : {e}", e=e))
        return None
    finally:
        entree.unlink(missing_ok=True)
        sortie.unlink(missing_ok=True)
    if "erreur" in res:
        journal(T("vérification des jeux possédés : Steam a refusé ({e})", e=res["erreur"]))
        return None
    return {"possedes": set(res["possedes"]), "achats": {int(a): t for a, t in res.get("achats", {}).items() if t},
            "famille": set(res.get("famille", []))}


# ───────────────────────── Succès (achievements) ─────────────────────────

def _attendre_steam(api, test, delai: float, pas: float = 0.05):
    """Fait tourner les rappels de Steam jusqu'à ce que test() soit vrai (ou que le délai soit écoulé)."""
    fin = time.time() + delai
    while True:
        api.SteamAPI_RunCallbacks()
        v = test()
        if v or time.time() >= fin:
            return v
        time.sleep(pas)


def mode_succes(appid: int, entree: Path, sortie: Path) -> int:
    """Lit les succès du jeu `appid`, et les débloque ou les re-verrouille si on le demande (fenêtre Succès)."""
    os.environ["SteamAppId"] = os.environ["SteamGameId"] = str(appid)
    demande = json.loads(entree.read_text(encoding="utf-8"))
    res: dict = {}
    api, ok, msg = _steam_api()
    if not ok:
        res.update(erreur="init", detail=msg)
        code = 4
    else:
        try:
            code = _succes(api, demande, res)
        except Exception as e:  # noqa: BLE001 — renvoyé à la fenêtre
            res.update(erreur="exception", detail=repr(e))
            code = 6
        api.SteamAPI_Shutdown()
    sortie.write_text(json.dumps(res), encoding="utf-8")
    return code


def _succes(api, demande: dict, res: dict) -> int:
    c, P = ctypes, ctypes.c_void_p

    def f(nom, restype, *argtypes):
        if not nom or not hasattr(api, nom):
            return None
        fn = getattr(api, nom)
        fn.restype, fn.argtypes = restype, list(argtypes)
        return fn

    fab = next((n for n in ("SteamAPI_SteamUserStats_v013", "SteamAPI_SteamUserStats_v012",
                            "SteamAPI_SteamUserStats_v011") if hasattr(api, n)), None)
    s = "SteamAPI_ISteamUserStats_"
    nombre_succes = f(s + "GetNumAchievements", c.c_uint32, P)
    nom_succes = f(s + "GetAchievementName", c.c_char_p, P, c.c_uint32)
    etat = f(s + "GetAchievementAndUnlockTime", c.c_bool, P, c.c_char_p, c.POINTER(c.c_bool), c.POINTER(c.c_uint32))
    if not (fab and nombre_succes and nom_succes and etat):
        res["erreur"] = "dll"
        return 7
    stats = f(fab, P)()
    demander = f(s + "RequestCurrentStats", c.c_bool, P)
    attribut = f(s + "GetAchievementDisplayAttribute", c.c_char_p, P, c.c_char_p, c.c_char_p)
    poser = f(s + "SetAchievement", c.c_bool, P, c.c_char_p)
    effacer = f(s + "ClearAchievement", c.c_bool, P, c.c_char_p)
    stocker = f(s + "StoreStats", c.c_bool, P)
    icone = f(s + "GetAchievementIcon", c.c_int, P, c.c_char_p)
    demander_pct = f(s + "RequestGlobalAchievementPercentages", c.c_uint64, P)
    pct = f(s + "GetAchievementAchievedPercent", c.c_bool, P, c.c_char_p, c.POINTER(c.c_float))
    fab_utils = next((n for n in ("SteamAPI_SteamUtils_v010", "SteamAPI_SteamUtils_v009") if hasattr(api, n)), None)
    utils = f(fab_utils, P)() if fab_utils else None
    taille_image = f("SteamAPI_ISteamUtils_GetImageSize", c.c_bool, P, c.c_int, c.POINTER(c.c_uint32),
                     c.POINTER(c.c_uint32))
    image_rgba = f("SteamAPI_ISteamUtils_GetImageRGBA", c.c_bool, P, c.c_int, c.c_void_p, c.c_int)

    def lu(n: bytes):
        fait, quand = c.c_bool(), c.c_uint32()
        if not etat(stats, n, c.byref(fait), c.byref(quand)):
            return None                                    # stats du joueur pas encore reçues
        return bool(fait.value), int(quand.value)

    # 1) liste des succès (schéma du jeu) puis état du joueur, envoyés par le client Steam
    if demander:
        demander(stats)
    if not _attendre_steam(api, lambda: nombre_succes(stats) > 0, 8):
        res["succes"] = []                                 # pas de succès dans ce jeu
        return 0
    noms = [nom_succes(stats, i) for i in range(nombre_succes(stats))]
    noms = [n for n in noms if n]
    if not _attendre_steam(api, lambda: lu(noms[0]) is not None, 15):
        res["erreur"] = "stats"
        return 8

    # 2) débloquer ou re-verrouiller
    action = demande.get("action", "lire")
    texte = {n.decode("utf-8", "replace"): n for n in noms}
    voulus = [texte[n] for n in demande.get("noms", []) if n in texte]
    if action in ("debloquer", "verrouiller") and voulus and poser and effacer and stocker:
        attendu = action == "debloquer"
        changes = [n for n in voulus if (lu(n) or (None,))[0] != attendu and (poser if attendu else effacer)(stats, n)]
        if changes:
            res["stockage"] = bool(stocker(stats))
            _attendre_steam(api, lambda: False, 5)                 # le serveur de Steam confirme ou annule
            if demander:
                demander(stats)
                _attendre_steam(api, lambda: False, 2)

    # 3) état, date, nom et description de chaque succès
    res["succes"] = []
    for n in noms:
        fait, quand = lu(n) or (False, 0)

        def attr(cle):
            v = attribut(stats, n, cle) if attribut else None
            return v.decode("utf-8", "replace") if v else ""
        ident = n.decode("utf-8", "replace")
        res["succes"].append({"id": ident, "nom": attr(b"name") or ident, "desc": attr(b"desc"),
                              "cache": attr(b"hidden") == "1", "fait": fait, "date": quand if fait else 0})
    if action in ("debloquer", "verrouiller"):
        cibles = {n.decode("utf-8", "replace") for n in voulus}
        res["refuses"] = [x["id"] for x in res["succes"] if x["id"] in cibles and x["fait"] != (action == "debloquer")]

    # 4) pourcentage des joueurs qui ont chaque succès
    if demander_pct and pct:
        demander_pct(stats)
        p = c.c_float()
        if _attendre_steam(api, lambda: pct(stats, noms[0], c.byref(p)), 6):
            for n, x in zip(noms, res["succes"]):
                if pct(stats, n, c.byref(p)):
                    x["pct"] = round(float(p.value), 1)

    # 5) icônes (en couleur si débloqué, grisées sinon) : 0 tant que Steam les télécharge
    if demande.get("icones") and demande.get("fichier_icones") and icone and utils and taille_image and image_rgba:
        poignees: dict[bytes, int] = {}

        def charger():
            for n in noms:
                if n not in poignees:
                    h = icone(stats, n)
                    if h > 0:
                        poignees[n] = h
            return len(poignees) == len(noms)
        _attendre_steam(api, charger, 8, pas=0.3)
        blob, index = bytearray(), {}
        for n, h in poignees.items():
            l, ht = c.c_uint32(), c.c_uint32()
            if taille_image(utils, h, c.byref(l), c.byref(ht)) and 0 < l.value <= 256 and 0 < ht.value <= 256:
                w, hh = l.value, ht.value
                buf = c.create_string_buffer(w * hh * 4)
                if image_rgba(utils, h, buf, len(buf)):
                    brut, saut = buf.raw, max(1, max(w, hh) // 64)
                    if saut > 1:                           # 256 px -> 64 px (1 pixel sur `saut`)
                        px = memoryview(brut).cast("I")
                        brut = b"".join(px[y * w:(y + 1) * w:saut].tobytes() for y in range(0, hh, saut))
                        w, hh = len(range(0, w, saut)), len(range(0, hh, saut))
                    index[n.decode("utf-8", "replace")] = [len(blob), w, hh]
                    blob += brut
        Path(demande["fichier_icones"]).write_bytes(bytes(blob))
        res["icones"] = index
    return 0


def succes_steam(appid: int, action: str = "lire", noms=(), icones: bool = True) -> dict:
    """Lance le processus --succes et renvoie sa réponse (avec les icônes brutes dans "_icones")."""
    entree, sortie, fichier = ICI / "_succes_in.json", ICI / "_succes_out.json", ICI / "_succes_icones.bin"
    entree.write_text(json.dumps({"action": action, "noms": list(noms), "icones": icones,
                                  "fichier_icones": str(fichier)}), encoding="utf-8")
    sortie.unlink(missing_ok=True)
    fichier.unlink(missing_ok=True)
    env = dict(os.environ, SteamAppId=str(appid), SteamGameId=str(appid))
    try:
        subprocess.run(commande("--succes", str(appid), str(entree), str(sortie)),
                       env=env, cwd=ICI, creationflags=SANS_FENETRE, timeout=120,
                       stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        res = json.loads(sortie.read_text(encoding="utf-8"))
        if res.get("icones") and fichier.exists():
            res["_icones"] = fichier.read_bytes()
    except (OSError, subprocess.TimeoutExpired, ValueError) as e:
        res = {"erreur": "processus", "detail": repr(e)}
    finally:
        for p in (entree, sortie, fichier):
            p.unlink(missing_ok=True)
    if res.get("erreur"):
        journal(T("[{a}] succès : {e} {d}", a=appid, e=res["erreur"], d=res.get("detail", "")))
    return res


# ───────────────────────── Petits outils ─────────────────────────

# ───────────────────────── Outils Steam : données locales et boutique ─────────────────────────

def collections_steam(steam: Path, uid: int) -> list[tuple[str, str, set[int]]]:
    """Collections classiques de la bibliothèque Steam : (identifiant, nom, jeux), lues dans la copie locale du
    stockage cloud de Steam. Les collections dynamiques (Steam les calcule lui-même à partir de filtres, parfois
    d'après les jeux de tes amis) et « Masqués » sont laissées de côté, comme les collections vides."""
    fichier = steam / "userdata" / str(uid) / "config" / "cloudstorage" / "cloud-storage-namespace-1.json"
    out: list[tuple[str, str, set[int]]] = []
    try:
        entrees = json.loads(fichier.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return out
    for e in entrees if isinstance(entrees, list) else []:
        try:
            cle, v = e
            if not str(cle).startswith("user-collections.") or v.get("is_deleted") or not v.get("value"):
                continue
            c = json.loads(v["value"])
            ident, nom = str(c.get("id", "")), str(c.get("name") or "").strip()
            if "filterSpec" in c or ident == "hidden":
                continue
            jeux = {int(a) for a in c.get("added", []) if str(a).isdigit()}
            if jeux and (nom or ident == "favorite"):
                out.append((ident, nom, jeux))
        except (ValueError, TypeError, AttributeError):
            continue
    return sorted(out, key=lambda c: (c[0] != "favorite", c[1].lower()))


def captures_steam(steam: Path, uid: int) -> dict[int, list[Path]]:
    """appid -> captures d'écran Steam du compte (touche F12 en jeu), les plus récentes d'abord."""
    racine = steam / "userdata" / str(uid) / "760" / "remote"
    out: dict[int, list[Path]] = {}
    if not uid or not racine.is_dir():
        return out
    for d in racine.iterdir():
        dossier = d / "screenshots"
        if not d.name.isdigit() or not dossier.is_dir():
            continue
        try:
            fichiers = sorted((p for p in dossier.iterdir()
                               if p.suffix.lower() in (".jpg", ".jpeg", ".png") and p.is_file()),
                              key=lambda p: p.name, reverse=True)
        except OSError:
            continue
        if fichiers:
            out[int(d.name)] = fichiers
    return out


def date_capture(p: Path) -> float:
    """Instant d'une capture : Steam la nomme AAAAMMJJHHMMSS_n.jpg ; sinon, la date du fichier."""
    m = re.match(r"(\d{14})", p.name)
    try:
        if m:
            return dt.datetime.strptime(m[1], "%Y%m%d%H%M%S").timestamp()
        return p.stat().st_mtime
    except (ValueError, OSError, OverflowError):
        return 0.0


def jeux_installes(steam: Path) -> dict[int, dict]:
    """appid -> {"nom", "taille" (octets), "lance" (dernier lancement), "disque"} d'après les appmanifest_*.acf
    de chaque bibliothèque Steam."""
    out: dict[int, dict] = {}
    for lib in bibliotheques(steam):
        for f in (lib / "steamapps").glob("appmanifest_*.acf"):
            if not f.stem[12:].isdigit():
                continue
            try:
                d = lire_vdf(f).get("appstate", {})
            except OSError:
                d = {}

            def n(cle: str) -> int:
                v = str(d.get(cle, ""))
                return int(v) if v.isdigit() else 0

            out[int(f.stem[12:])] = {"nom": str(d.get("name", "")), "taille": n("sizeondisk"),
                                     "lance": n("lastplayed"), "disque": (lib.anchor or str(lib)).upper()}
    return out


def derniers_lancements(apps: dict) -> dict[int, int]:
    """appid -> instant du dernier lancement (jeux installés ou non), d'après le localconfig.vdf du compte."""
    out = {}
    for k, v in apps.items():
        if k.isdigit() and isinstance(v, dict) and str(v.get("lastplayed", "")).isdigit() and int(v["lastplayed"]):
            out[int(k)] = int(v["lastplayed"])
    return out


def requete_json(url: str, delai: float = 20):
    """GET d'une adresse publique de Steam -> JSON décodé, ou None sans réponse. La requête ne porte que l'adresse
    et un User-Agent générique : aucun cookie, aucune session Steam."""
    import urllib.request
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "SteamHoursFarm", "Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=delai) as rep:
            return json.loads(rep.read(30_000_000).decode("utf-8"))
    except Exception:  # noqa: BLE001 — pas de réseau, Steam indisponible… : l'appelant affiche un message
        return None


def prix_steam(ids) -> dict | None:
    """Prix du moment dans la boutique Steam : appid -> (devise, prix normal, prix actuel) en centimes, GRATUIT
    pour un jeu sans prix (gratuit ou pas encore sorti), None pour un jeu retiré de la boutique. Renvoie None si
    la boutique ne répond pas. Elle ne reçoit que des numéros de jeux ; la devise est celle du pays d'où part
    la requête."""
    ids = sorted(set(ids))
    out: dict = {}
    for i in range(0, len(ids), LOT_PRIX):
        lot = ids[i:i + LOT_PRIX]
        d = requete_json(BOUTIQUE + "?filters=price_overview&appids=" + ",".join(map(str, lot)))
        if not isinstance(d, dict):
            return None
        for a in lot:
            x = d.get(str(a))
            if not isinstance(x, dict) or not x.get("success"):
                out[a] = None
                continue
            p = x.get("data")
            p = p.get("price_overview") if isinstance(p, dict) else None
            if isinstance(p, dict) and isinstance(p.get("final"), int):
                out[a] = (str(p.get("currency", "")), int(p.get("initial") or p["final"]), int(p["final"]))
            else:
                out[a] = GRATUIT
    return out


def noms_boutique(ids, langue: str) -> dict[int, str]:
    """Noms de jeux demandés à la boutique Steam, pour ceux que le cache local ne connaît pas (jeux de la liste
    de souhaits que tu ne possèdes pas)."""
    import urllib.parse
    ids, out = sorted(set(ids)), {}
    for i in range(0, len(ids), 100):
        q = {"ids": [{"appid": a} for a in ids[i:i + 100]],
             "context": {"language": "french" if langue == "fr" else "english", "country_code": "US"},
             "data_request": {"include_basic_info": True}}
        d = requete_json(API_ARTICLES + "?input_json=" + urllib.parse.quote(json.dumps(q, separators=(",", ":"))))
        articles = d.get("response", {}).get("store_items", []) if isinstance(d, dict) else []
        for x in articles if isinstance(articles, list) else []:
            if isinstance(x, dict) and x.get("name") and str(x.get("appid", "")).isdigit() and int(x["appid"]):
                out[int(x["appid"])] = str(x["name"])
    return out


def liste_souhaits(uid: int) -> list[int] | None:
    """Jeux de la liste de souhaits du compte : None si Steam ne répond pas, liste vide si elle est vide ou privée.
    Steam reçoit l'identifiant public du compte (SteamID) : la liste n'est lisible que si les « Détails du jeu »
    du profil sont publics."""
    if not uid:
        return None
    d = requete_json(f"{API_SOUHAITS}?steamid={76561197960265728 + uid}")
    if not isinstance(d, dict) or not isinstance(d.get("response"), dict):
        return None
    articles = d["response"].get("items", [])
    return [int(x["appid"]) for x in articles if isinstance(x, dict) and str(x.get("appid", "")).isdigit()]


def souhaits_steam(uid: int, noms: dict[int, str], langue: str) -> list[dict] | None:
    """Liste de souhaits avec les prix du moment : [{"a", "nom", "prix"}] (prix comme dans prix_steam).
    None si Steam ne répond pas."""
    ids = liste_souhaits(uid)
    if ids is None:
        return None
    if not ids:
        return []
    prix = prix_steam(ids)
    if prix is None:
        return None
    manquants = [a for a in ids if a not in noms]
    autres = noms_boutique(manquants, langue) if manquants else {}
    return [{"a": a, "nom": noms.get(a) or autres.get(a) or f"App {a}", "prix": prix.get(a)} for a in ids]


def remise(p) -> int:
    """Pourcentage de promo d'un prix (devise, normal, actuel) ; 0 sans promo ou sans prix."""
    if not isinstance(p, tuple) or p[1] <= 0 or p[2] >= p[1]:
        return 0
    return round(100 - 100 * p[2] / p[1])


def argent(centimes: float, devise: str) -> str:
    """Somme lisible : « 1 234,56 € » (fr) / « €1,234.56 » (en). Une devise sans symbole connu garde son code."""
    v = f"{centimes / 100:,.2f}"
    sym = SYMBOLES.get(devise)
    if LANGUE == "en":
        return f"{sym}{v}" if sym else f"{v} {devise}".strip()
    return f"{v.replace(',', ' ').replace('.', ',')} {sym or devise}".strip()


def taille_txt(octets: float) -> str:
    """Taille lisible : « 95,4 Go » (fr) / « 95.4 GB » (en)."""
    unites = ("B", "KB", "MB", "GB", "TB") if LANGUE == "en" else ("o", "Ko", "Mo", "Go", "To")
    v, i = float(octets), 0
    while v >= 1024 and i < len(unites) - 1:
        v, i = v / 1024, i + 1
    txt = f"{v:.0f}" if i < 2 or v >= 100 else f"{v:.1f}"
    return f"{txt if LANGUE == 'en' else txt.replace('.', ',')} {unites[i]}"


def depuis_txt(ts: float) -> str:
    """Ancienneté d'un instant passé : « aujourd'hui », « hier », « il y a 12 jours », « il y a 3 mois »,
    « il y a 2 ans » ; « jamais » pour 0."""
    if not ts:
        return T("jamais")
    try:
        jours = (dt.date.today() - dt.date.fromtimestamp(ts)).days
    except (ValueError, OSError, OverflowError):
        return T("jamais")
    if jours <= 0:
        return T("aujourd'hui")
    if jours == 1:
        return T("hier")
    if jours < 60:
        return T("il y a {n} jours", n=jours)
    if jours < 730:
        return T("il y a {n} mois", n=jours // 30)
    return T("il y a {n} ans", n=jours // 365)


def heure_plage(texte: str) -> int:
    """Heure saisie dans Options (0 à 23) ; une saisie invalide vaut 0."""
    v = nombre(texte)
    return int(v) if 0 <= v < 24 else 0


def dans_plage(heure: float, debut: int, fin: int) -> bool:
    """heure (0 à 24, minutes en décimales) dans la plage [debut, fin[. La plage peut passer minuit (23 h → 8 h) ;
    debut == fin veut dire toute la journée."""
    if debut == fin:
        return True
    return debut <= heure < fin if debut < fin else heure >= debut or heure < fin


def version_num(v: str) -> tuple[int, ...]:
    return tuple(int(x) for x in re.findall(r"\d+", v)[:3])


def derniere_version() -> str | None:
    """Numéro de la dernière version publiée sur GitHub, None sans réponse. La requête ne contient que l'adresse
    MAJ_API et un User-Agent générique : ni cookie, ni compte Steam, ni liste de jeux, ni info sur le PC."""
    import urllib.request
    req = urllib.request.Request(MAJ_API, headers={"User-Agent": "SteamHoursFarm",
                                                   "Accept": "application/vnd.github+json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as rep:
            tag = json.loads(rep.read(500_000).decode("utf-8")).get("tag_name", "")
    except Exception:  # noqa: BLE001 — pas de réseau, GitHub indisponible… : on réessaiera plus tard
        return None
    tag = str(tag).strip().lstrip("vV")
    return tag if re.fullmatch(r"\d+(\.\d+){0,3}", tag) else None


def hms(s: float) -> str:
    s = int(s)
    return f"{s // 3600}:{s // 60 % 60:02d}:{s % 60:02d}"


def heures(minutes: float, vide: str | None = "—") -> str:
    """Durée en heures et minutes : « 312 h 46 » (fr) ou « 312h 46m » (en). vide=None affiche aussi le zéro."""
    minutes = int(minutes)
    if not minutes and vide is not None:
        return vide
    h, m = divmod(minutes, 60)
    return f"{h}h {m:02d}m" if LANGUE == "en" else f"{h} h {m:02d}"


def nombre(texte: str) -> float:
    try:
        return max(0.0, float(str(texte).replace(",", ".").strip() or 0))
    except ValueError:
        return 0.0


def court(v: float) -> str:
    """1.8333 -> '1.83', 500.0 -> '500'."""
    return f"{v:.2f}".rstrip("0").rstrip(".")


def nombre_txt(v: float) -> str:
    """Nombre à afficher : virgule en français, point en anglais."""
    return court(v) if LANGUE == "en" else court(v).replace(".", ",")


def objectif_txt(h: float) -> str:
    """Objectif en heures, lisible : 500 -> '500 h', 1.8333 -> '1 h 50'."""
    if abs(h - round(h)) < 1e-6:
        return f"{round(h)}h" if LANGUE == "en" else f"{round(h)} h"
    return heures(round(h * 60), None)


def pct(p: float) -> str:
    """Pourcentage : « 9,5 % » (fr) ou « 9.5% » (en)."""
    return f"{nombre_txt(p)}%" if LANGUE == "en" else f"{nombre_txt(p)} %"


def jour_court(d) -> str:
    return JOURS[LANGUE][d.weekday()]


def date_courte(d) -> str:
    return f"{MOIS_EN[d.month - 1]} {d.day}" if LANGUE == "en" else d.strftime("%d/%m")


def date_longue(ts: float) -> str:
    """Date d'un instant passé, avec l'année : « 15/11/2026 » / « Nov 15, 2026 »."""
    d = dt.datetime.fromtimestamp(ts)
    return f"{MOIS_EN[d.month - 1]} {d.day}, {d.year}" if LANGUE == "en" else d.strftime("%d/%m/%Y")


def moment(ts: float) -> str:
    """Jour et heure d'un instant à venir : « sam. 22/11 à 14 h 05 » / « Sat Nov 22, 14:05 »."""
    d = dt.datetime.fromtimestamp(ts)
    h = f"{d:%H:%M}" if LANGUE == "en" else f"{d:%H} h {d:%M}"
    ecart = (d.date() - dt.date.today()).days
    if ecart == 0:
        return T("aujourd'hui à {h}", h=h)
    if ecart == 1:
        return T("demain à {h}", h=h)
    if LANGUE == "en":
        return f"{jour_court(d)} {date_courte(d)}, {h}"
    return f"{jour_court(d)} {date_courte(d)} à {h}"


def garder_eveille(oui: bool) -> None:
    # ES_CONTINUOUS (| ES_SYSTEM_REQUIRED) : empêche la mise en veille tant que le farm tourne
    ctypes.windll.kernel32.SetThreadExecutionState(0x80000000 | (0x1 if oui else 0))


def demarrage_windows() -> bool:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, CLE_RUN) as k:
            winreg.QueryValueEx(k, NOM_RUN)
            return True
    except OSError:
        return False


def regler_demarrage_windows(oui: bool) -> None:
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, CLE_RUN) as k:
        if oui:
            winreg.SetValueEx(k, NOM_RUN, 0, winreg.REG_SZ,
                              subprocess.list2cmdline(commande("--auto", fenetre=True)))
        else:
            try:
                winreg.DeleteValue(k, NOM_RUN)
            except FileNotFoundError:
                pass


_MUTEX = None


def deja_ouvert() -> bool:
    """Une seule fenêtre par dossier d'installation."""
    global _MUTEX
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateMutexW.restype = wintypes.HANDLE
    k32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
    nom = "Local\\FarmHeuresSteam_" + hashlib.md5(str(ICI).lower().encode()).hexdigest()[:12]
    _MUTEX = k32.CreateMutexW(None, False, nom)
    return ctypes.get_last_error() == 183   # ERROR_ALREADY_EXISTS


# ───────────────────────── Images (Pillow) ─────────────────────────

def _police(taille: int, gras: bool = True):
    from PIL import ImageFont
    try:
        return ImageFont.truetype("segoeuib.ttf" if gras else "segoeui.ttf", taille)
    except OSError:
        return ImageFont.load_default(taille)


def arrondir(im, rayon_rel: float = 0.2):
    """Coins arrondis (anticrénelés) sur une image RGBA."""
    from PIL import Image, ImageChops, ImageDraw
    w, h = im.size
    m = Image.new("L", (w * 4, h * 4), 0)
    ImageDraw.Draw(m).rounded_rectangle((0, 0, w * 4 - 1, h * 4 - 1), int(min(w, h) * 4 * rayon_rel), fill=255)
    m = m.resize((w, h), Image.LANCZOS)
    im.putalpha(ImageChops.multiply(im.getchannel("A"), m))
    return im


def dessiner_logo(n: int):
    """Logo de l'appli : logo.png (rond rouge), à côté du script ou embarqué dans le .exe. Rond rouge de secours."""
    from PIL import Image, ImageDraw
    for dossier in (Path(getattr(sys, "_MEIPASS", ICI)), ICI):
        try:
            return Image.open(dossier / "logo.png").convert("RGBA").resize((n, n), Image.LANCZOS)
        except OSError:
            pass
    t = n * 4
    im = Image.new("RGBA", (t, t), (0, 0, 0, 0))
    ImageDraw.Draw(im).ellipse((0, 0, t - 1, t - 1), fill=EGO)
    return im.resize((n, n), Image.LANCZOS)


def dessiner_trophee(n: int, couleur: str = OR):
    """Petit trophée doré du bouton Succès."""
    from PIL import Image, ImageDraw
    t = n * 4
    im = Image.new("RGBA", (t, t), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    e = max(4, int(t * .085))
    d.ellipse((t * .04, t * .12, t * .42, t * .50), outline=couleur, width=e)     # anses
    d.ellipse((t * .58, t * .12, t * .96, t * .50), outline=couleur, width=e)
    d.pieslice((t * .20, -t * .32, t * .80, t * .64), 0, 180, fill=couleur)     # coupe
    d.rectangle((t * .20, t * .08, t * .80, t * .17), fill=couleur)
    d.rectangle((t * .44, t * .60, t * .56, t * .78), fill=couleur)              # pied
    d.rounded_rectangle((t * .26, t * .78, t * .74, t * .92), int(t * .04), fill=couleur)
    return im.resize((n, n), Image.LANCZOS)


def icone_jeu(chemin: Path | None, nom: str, s: int):
    from PIL import Image, ImageDraw
    im = None
    if chemin:
        try:
            im = Image.open(chemin).convert("RGBA").resize((s, s), Image.LANCZOS)
        except OSError:
            im = None
    if im is None:                       # pas d'icône en cache : pastille avec l'initiale
        im = Image.new("RGBA", (s, s), BORD)
        lettre = next((c for c in nom.upper() if c.isalnum()), "?")
        ImageDraw.Draw(im).text((s / 2, s / 2), lettre, font=_police(int(s * .55)), fill=GRIS, anchor="mm")
    return arrondir(im, 0.22)


def case_a_cocher(etat: str, c: int):
    """off = vide, on = coché, run = en cours (lecture)."""
    from PIL import Image, ImageDraw
    t = c * 4
    im = Image.new("RGBA", (t, t), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    r = int(t * .24)
    if etat == "off":
        e = max(4, int(t * .09))
        d.rounded_rectangle((e // 2, e // 2, t - 1 - e // 2, t - 1 - e // 2), r, outline=GRIS, width=e)
    else:
        d.rounded_rectangle((0, 0, t - 1, t - 1), r, fill=VERT if etat == "run" else ACCENT)
        if etat == "run":
            d.polygon([(t * .36, t * .26), (t * .36, t * .74), (t * .76, t * .5)], fill=ENCRE)
        else:
            d.line([(t * .25, t * .52), (t * .43, t * .70), (t * .76, t * .32)], fill=ENCRE,
                   width=int(t * .13), joint="curve")
    return im.resize((c, c), Image.LANCZOS)


def couvrir(img, w: int, h: int):
    from PIL import Image
    r = max(w / img.width, h / img.height)
    img = img.resize((max(w, round(img.width * r)), max(h, round(img.height * r))), Image.LANCZOS)
    x, y = (img.width - w) // 2, (img.height - h) // 2
    return img.crop((x, y, x + w, y + h))


def barre_arrondie(c, x0: float, y0: float, x1: float, y1: float, r: float, **kw):
    """Barre verticale au sommet arrondi, posée sur la ligne de base (y1)."""
    r = min(r, (x1 - x0) / 2, max(0.0, y1 - y0))
    if r < 1:
        return c.create_rectangle(x0, y0, x1, y1, width=0, **kw)
    pts = [x0, y1, x0, y1, x0, y0 + r, x0, y0, x0 + r, y0, x1 - r, y0, x1, y0, x1, y0 + r, x1, y1, x1, y1]
    return c.create_polygon(pts, smooth=True, **kw)


# ───────────────────────── Fenêtre ─────────────────────────

def interface(auto: bool, apres=None) -> None:
    """Ouvre la fenêtre. `apres(appli, racine)` est appelé une fois la fenêtre prête (tests)."""
    import tkinter as tk
    from tkinter import messagebox, ttk

    from PIL import Image, ImageChops, ImageFilter, ImageOps, ImageTk
    try:
        import pystray
    except ImportError:
        pystray = None

    class Appli:
        def __init__(self, r: tk.Tk):
            global LANGUE
            self.r = r
            self.cfg = lire_config()
            c = self.cfg
            self.k = r.winfo_fpixels("1i") / 96          # facteur d'échelle (écrans à 125 %, 150 %…)
            self.steam: Path | None = None
            self.jeux: dict[int, dict] = {}               # appid -> {"nom", "min"}
            self.infos: dict[int, tuple] = {}
            self.installes: set[int] = set()
            self.succes_cache: dict[int, tuple[int, int]] = {}   # appid -> (débloqués, total), cache de Steam
            self.gratuits: set[int] = set()
            self.chemins: dict[int, dict] = {}
            self.icones: dict[int, Image.Image] = {}
            self.images: dict[tuple, ImageTk.PhotoImage] = {}
            # la file du farm : l'ordre des jeux cochés décide qui passe en premier
            self.coches: dict[int, None] = dict.fromkeys(int(a) for a in c.get("coches", []))
            self.objectifs: dict[int, float] = {int(a): float(h) for a, h in c.get("objectifs", {}).items()}
            self.atteints: set[int] = set(c.get("atteints", []))
            self.farme: dict[int, float] = {int(a): s for a, s in c.get("farme", {}).items()}
            self.achats: dict[int, int] = {int(a): t for a, t in c.get("achats", {}).items()}
            self.famille: set[int] = set(c.get("famille", []))
            self.remb_ok: set[int] = set(c.get("remb_ok", []))
            self.historique: dict[str, dict[str, float]] = c.get("historique", {})
            self.groupes: dict[str, list[int]] = c.get("groupes", {})
            self.base_farme: dict[int, float] = {}
            self.procs: dict[int, tuple[subprocess.Popen, float]] = {}
            self.echecs: dict[int, str | int] = {}          # "refus" ou code de sortie
            self.essais: dict[int, tuple[int, float]] = {}
            self.arretes: dict[int, float] = {}             # jeux arrêtés récemment -> heure de l'arrêt
            self.actif, self.debut, self.total_depart = False, 0.0, 0.0
            self.pause: str | None = None   # "steam", "jeu" (vrai jeu lancé), "pc" (PC utilisé), "horaire"
            self.reprise_depuis: float | None = None
            self.jeu_reel: tuple[int, str] | None = None
            self.derniere_verif_jeu = 0.0
            self.statut_avant: int | None = c.get("statut_avant")
            self.derniere_rotation = 0.0
            self.dernier_tic = time.time()
            tri = c.get("tri")
            self.tri = ((tri[0], bool(tri[1])) if isinstance(tri, list) and len(tri) == 2
                        and tri[0] in ("nom", "temps", "objectif", "farme", "succes") else ("nom", False))
            self.sel: int | None = None
            self.chargement = None
            self.q: queue.Queue = queue.Queue()
            self.auto, self.steam_pret_depuis = auto, None
            self.tray, self.notifie = None, False
            self.fen_stats = self.fen_options = self.fen_succes = None
            self.fen_valeur = self.fen_stockage = self.fen_souhaits = self.fen_captures = self.fen_hasard = None
            # menu Outils : données lues dans les fichiers de Steam, prix de la boutique, liste de souhaits
            self.installations: dict[int, dict] = {}        # jeux installés : nom, taille, dernier lancement, disque
            self.lances: dict[int, int] = {}                # appid -> instant du dernier lancement
            self.collections: list[tuple[str, str, set[int]]] = []
            self.captures: dict[int, list[Path]] = {}
            self.prix: dict | None = None                   # prix de la boutique, gardés une heure
            self.prix_date, self.prix_occupe = 0.0, False
            self.souhaits: list[dict] | None = None
            self.souhaits_date, self.souhaits_occupe = 0.0, False
            self.tables: dict[str, tuple] = {}
            self.tris_outils = {"valeur": ("prix", True), "stockage": ("taille", True), "souhaits": ("promo", True)}
            self.h_jeu: int | None = None                   # « À quoi je joue ? » : jeu tiré
            self.c_jeu: int | None = None                   # fenêtre Captures : jeu affiché
            self.c_gen, self.c_cases, self.c_tk, self.c_ids = 0, [], {}, []
            self._icones_tk: dict[int, ImageTk.PhotoImage] = {}
            self._derniere_veille = time.time()
            self.succes_jeu: int | None = None              # fenêtre Succès : jeu affiché
            self.succes_liste: list[dict] = []
            self.succes_icones: dict[str, Image.Image] = {}
            self.succes_occupe = False                      # un processus --succes tourne
            self._rendu_prevu = None
            self._masque = None
            self._maj_objectif = False
            self._note = ""
            self._derniere_sauvegarde = time.time()
            self.duree = tk.StringVar(value=str(c.get("heures", "0")))
            self.rotation = tk.StringVar(value=str(c.get("rotation", "2")))
            self.eveille = tk.BooleanVar(value=c.get("eveille", True))
            self.au_demarrage = tk.BooleanVar(value=demarrage_windows())
            self.invisible = tk.BooleanVar(value=c.get("invisible", False))
            self.pause_jeu = tk.BooleanVar(value=c.get("pause_jeu", True))
            self.si_inactif = tk.BooleanVar(value=c.get("si_inactif", False))
            self.inactif_min = tk.StringVar(value=str(c.get("inactif_min", "10")))
            self.maj_auto = tk.BooleanVar(value=c.get("maj_auto", True))
            self.plage = tk.BooleanVar(value=c.get("plage", False))
            self.plage_debut = tk.StringVar(value=str(c.get("plage_debut", "23")))
            self.plage_fin = tk.StringVar(value=str(c.get("plage_fin", "8")))
            self.promos_auto = tk.BooleanVar(value=c.get("promos_auto", False))
            self._style()
            # la langue n'est demandée qu'une fois : config.json de ce dossier, sinon celle déjà choisie sur ce PC
            langue = c.get("langue") if c.get("langue") in ("fr", "en") else langue_retenue()
            if langue:
                LANGUE = c["langue"] = langue
                if langue_retenue() is None:     # choisie avant la v1.3 : la retenir aussi pour tout le PC
                    retenir_langue(langue)
            elif auto:                       # démarrage caché : pas de question, langue de Windows
                LANGUE = langue_systeme()
            else:
                self._choisir_langue()
            r.title(NOM)
            self._construire()
            self._creer_tray()
            self._recharger()
            r.protocol("WM_DELETE_WINDOW", self._fermer)
            r.after(150, self._pomper)
            r.after(500, self._tic)
            r.after(4000, self._verifier_maj)
            r.after(9000, self._verifier_promos)
            if auto:
                r.withdraw()

        # ── apparence ──
        def _style(self):
            r, k = self.r, self.k
            self._taille(r, 1000, 740)
            r.minsize(*self._taille_min())
            self._restaurer_fenetre()
            r.configure(bg=FOND)
            self._logo_tk = ImageTk.PhotoImage(dessiner_logo(64))
            r.iconphoto(True, self._logo_tk)
            s = ttk.Style(r)
            s.theme_use("clam")
            s.configure(".", background=FOND, foreground=TEXTE, fieldbackground=PANNEAU,
                        bordercolor=BORD, lightcolor=BORD, darkcolor=BORD, font=("Segoe UI", 10))
            s.configure("Treeview", background=FOND, fieldbackground=FOND, foreground=TEXTE,
                        rowheight=int(34 * k), borderwidth=0)
            s.map("Treeview", background=[("selected", SELECTION)], foreground=[("selected", TEXTE)])
            s.layout("Treeview.Item", [("Treeitem.padding", {"sticky": "nswe", "children": [
                ("Treeitem.image", {"side": "left", "sticky": ""}),
                ("Treeitem.text", {"side": "left", "sticky": ""})]})])
            s.configure("Treeview.Heading", background=FOND, foreground=GRIS, relief="flat",
                        font=("Segoe UI", 9, "bold"), padding=(int(10 * k), 6))
            s.map("Treeview.Heading", background=[("active", PANNEAU)])
            s.configure("TEntry", fieldbackground=PANNEAU, foreground=TEXTE, insertcolor=TEXTE, padding=6)
            s.configure("TSpinbox", fieldbackground=PANNEAU, foreground=TEXTE, arrowcolor=TEXTE,
                        insertcolor=TEXTE, padding=4)
            s.configure("Ban.TSpinbox", fieldbackground="#262c36", foreground=TEXTE, arrowcolor=TEXTE,
                        insertcolor=TEXTE, padding=3, bordercolor="#323a47")
            s.configure("TCombobox", fieldbackground=PANNEAU, background=PANNEAU, foreground=TEXTE,
                        arrowcolor=TEXTE, padding=5)
            s.map("TCombobox", fieldbackground=[("readonly", PANNEAU)], foreground=[("readonly", TEXTE)],
                  selectbackground=[("readonly", PANNEAU)], selectforeground=[("readonly", TEXTE)],
                  background=[("active", BORD)])
            r.option_add("*TCombobox*Listbox.background", PANNEAU)
            r.option_add("*TCombobox*Listbox.foreground", TEXTE)
            r.option_add("*TCombobox*Listbox.selectBackground", SELECTION)
            r.option_add("*TCombobox*Listbox.selectForeground", TEXTE)
            r.option_add("*TCombobox*Listbox.font", ("Segoe UI", 10))
            s.configure("TButton", background=PANNEAU, foreground=TEXTE, padding=(12, 6), borderwidth=0)
            s.map("TButton", background=[("active", BORD)], foreground=[("disabled", GRIS)])
            s.configure("Accent.TButton", background=ACCENT, foreground=ENCRE,
                        font=("Segoe UI", 10, "bold"), padding=(18, 8))
            s.map("Accent.TButton", background=[("disabled", PANNEAU), ("active", ACCENT_CLAIR)],
                  foreground=[("disabled", GRIS)])
            s.configure("Succes.Treeview", rowheight=int(54 * k))
            s.configure("Arret.TButton", background=ROUGE, foreground=ENCRE,
                        font=("Segoe UI", 10, "bold"), padding=(18, 8))
            s.map("Arret.TButton", background=[("active", "#f5a3a3")])
            s.configure("Verre.TButton", background="#262c36", foreground=TEXTE, padding=(14, 7))
            s.map("Verre.TButton", background=[("active", "#323a47")])
            s.configure("Succes.TButton", background="#3a3220", foreground="#f6d987", padding=(12, 7))
            s.map("Succes.TButton", background=[("active", "#4b412a")])
            s.configure("BanPetit.TButton", background="#323a47", foreground=TEXTE, padding=(8, 2))
            s.map("BanPetit.TButton", background=[("active", "#3d4655")])
            s.configure("TCheckbutton", background=FOND, foreground=DOUX, indicatorbackground=PANNEAU,
                        indicatorforeground=ENCRE, upperbordercolor=GRIS, lowerbordercolor=GRIS)
            s.map("TCheckbutton", background=[("active", FOND)], foreground=[("active", TEXTE)],
                  indicatorbackground=[("selected", ACCENT)])
            s.configure("Vertical.TScrollbar", background=PANNEAU, troughcolor=FOND, arrowcolor=GRIS, borderwidth=0)
            s.configure("Titre.TLabel", font=("Segoe UI Semibold", 13))
            s.configure("Section.TLabel", font=("Segoe UI Semibold", 11), foreground=TEXTE)
            s.configure("Gris.TLabel", foreground=GRIS)
            s.configure("Doux.TLabel", foreground=DOUX)
            s.configure("Lien.TLabel", foreground=ACCENT, font=("Segoe UI Semibold", 10))
            s.configure("Ban.TLabel", background="#262c36", foreground=DOUX)
            s.configure("Ban.TFrame", background="#262c36")
            s.configure("Ligne.TFrame", background=BORD)
            s.configure("Tuile.TFrame", background=PANNEAU)
            s.configure("TuileTitre.TLabel", background=PANNEAU, foreground=GRIS, font=("Segoe UI", 9))
            s.configure("TuileValeur.TLabel", background=PANNEAU, foreground=TEXTE, font=("Segoe UI Semibold", 20))
            self._barre_sombre(r)

        def _barre_sombre(self, fenetre):
            try:  # barre de titre sombre sous Windows 11
                fenetre.update_idletasks()
                v = ctypes.c_int(1)
                ctypes.windll.dwmapi.DwmSetWindowAttribute(ctypes.windll.user32.GetParent(fenetre.winfo_id()),
                                                           20, ctypes.byref(v), 4)
            except Exception:
                pass

        def _taille(self, fenetre, l: int, h: int):
            """Taille voulue, réduite si l'écran est trop petit (portable en 1366x768, par exemple)."""
            l = min(int(l * self.k), fenetre.winfo_screenwidth() - int(40 * self.k))
            h = min(int(h * self.k), fenetre.winfo_screenheight() - int(110 * self.k))
            fenetre.geometry(f"{l}x{h}")

        def _restaurer_fenetre(self):
            """Taille et position de la dernière fois, si la fenêtre retombe bien sur un des écrans actuels."""
            m = re.fullmatch(r"(\d+)x(\d+)\+(-?\d+)\+(-?\d+)", str(self.cfg.get("fenetre", "")))
            if m:
                l, h, x, y = map(int, m.groups())
                u = ctypes.windll.user32
                vx, vy, vl, vh = (u.GetSystemMetrics(i) for i in (76, 77, 78, 79))   # bureau virtuel (tous les écrans)
                lmin, hmin = self._taille_min()
                l, h = max(lmin, min(l, vl)), max(hmin, min(h, vh))
                # la barre de titre doit rester attrapable : sinon, seule la taille est reprise
                if vx - l + 150 <= x <= vx + vl - 150 and vy <= y <= vy + vh - 80:
                    self.r.geometry(f"{l}x{h}+{x}+{y}")
                else:
                    self.r.geometry(f"{l}x{h}")
            if self.cfg.get("agrandie"):
                self.r.state("zoomed")

        def _taille_min(self) -> tuple[int, int]:
            r, k = self.r, self.k
            return (min(int(840 * k), r.winfo_screenwidth() - int(40 * k)),
                    min(int(580 * k), r.winfo_screenheight() - int(110 * k)))

        def _choisir_langue(self):
            """Premier lancement : choix de la langue, avant de construire la fenêtre."""
            global LANGUE
            r, k = self.r, self.k
            r.minsize(1, 1)
            r.resizable(False, False)
            r.geometry(f"{int(440 * k)}x{int(300 * k)}")
            r.title(NOM)
            f = ttk.Frame(r, padding=int(26 * k))
            f.pack(fill="both", expand=True)
            self._logo_choix = ImageTk.PhotoImage(dessiner_logo(int(56 * k)))
            ttk.Label(f, image=self._logo_choix).pack(pady=(0, int(12 * k)))
            ttk.Label(f, text="Choisis ta langue", style="Titre.TLabel").pack()
            ttk.Label(f, text="Choose your language", style="Gris.TLabel").pack(pady=(2, int(18 * k)))
            choix = tk.StringVar()
            defaut = langue_systeme()
            bas = ttk.Frame(f)
            bas.pack()
            for code, nom in LANGUES:
                b = ttk.Button(bas, text=nom, width=12, style="Accent.TButton" if code == defaut else "TButton",
                               command=lambda c=code: choix.set(c))
                b.pack(side="left", padx=int(6 * k))
                if code == defaut:
                    b.focus_set()
            # Entrée valide le bouton qui a le focus (Tab pour passer de l'un à l'autre)
            r.bind("<Return>", lambda e: e.widget.invoke() if isinstance(e.widget, ttk.Button) else choix.set(defaut))
            r.protocol("WM_DELETE_WINDOW", lambda: choix.set(defaut))
            self._choix_langue = choix                     # pour les tests
            r.wait_variable(choix)
            r.unbind("<Return>")
            LANGUE = choix.get() if choix.get() in ("fr", "en") else defaut
            self.cfg["langue"] = LANGUE
            retenir_langue(LANGUE)
            f.destroy()
            r.resizable(True, True)
            r.minsize(*self._taille_min())
            self._taille(r, 1000, 740)
            self._sauver()

        def _construire(self):
            r, k = self.r, self.k
            m18 = int(18 * k)
            # barre du haut
            haut = ttk.Frame(r, padding=(m18, int(10 * k), m18, int(8 * k)))
            haut.pack(fill="x")
            self._logo_haut = ImageTk.PhotoImage(dessiner_logo(int(26 * k)))
            ttk.Label(haut, image=self._logo_haut).pack(side="left", padx=(0, int(8 * k)))
            ttk.Label(haut, text=NOM, style="Titre.TLabel").pack(side="left")
            ttk.Button(haut, text=T("↻  Recharger"), command=self._recharger).pack(side="right")
            ttk.Button(haut, text=T("Options"), command=self._ouvrir_options).pack(side="right", padx=(0, 8))
            ttk.Button(haut, text=T("Statistiques"), command=self._ouvrir_stats).pack(side="right", padx=(0, 8))
            self.b_outils = ttk.Button(haut, text=T("Outils  ▾"), command=self._menu_outils)
            self.b_outils.pack(side="right", padx=(0, 8))
            # « Nouvelle version x.y.z » : vide tant qu'il n'y en a pas (rempli par _montrer_maj)
            self.l_maj = ttk.Label(haut, style="Lien.TLabel", cursor="hand2")
            self.l_maj.pack(side="right", pady=(3, 0))
            self.l_maj.bind("<Button-1>", lambda e: os.startfile(PROJET + "/releases/latest"))
            # placé en dernier : si la fenêtre est étroite, c'est le sous-titre qui est rogné, pas les boutons
            self.sous_titre = ttk.Label(haut, style="Gris.TLabel")
            self.sous_titre.pack(side="left", padx=(14, 0), pady=(3, 0))

            # bannière du jeu sélectionné
            self.ban = tk.Canvas(r, height=int(250 * k), bg=FOND, highlightthickness=0, bd=0)
            self.ban.pack(fill="x")
            self.ban.bind("<Configure>", lambda e: self._planifier_banniere())
            self.b_img = self.ban.create_image(0, 0, anchor="nw")
            self.b_nom = self.ban.create_text(0, 0, anchor="sw", fill=TEXTE, font=("Segoe UI Semibold", 22))
            self.b_stats = self.ban.create_text(0, 0, anchor="nw", fill=DOUX, font=("Segoe UI", 10))
            self.b_etat = self.ban.create_text(0, 0, anchor="nw", fill=GRIS, font=("Segoe UI Semibold", 10))
            self.b_piste = self.ban.create_rectangle(0, 0, 0, 0, fill="#323a47", width=0)
            self.b_jauge = self.ban.create_rectangle(0, 0, 0, 0, fill=ACCENT, width=0)
            self.b_pct = self.ban.create_text(0, 0, anchor="w", fill=DOUX, font=("Segoe UI", 9))
            # objectif d'heures du jeu sélectionné
            obj = ttk.Frame(self.ban, style="Ban.TFrame", padding=(10, 4))
            ttk.Label(obj, text=T("Objectif"), style="Ban.TLabel").pack(side="left", padx=(0, 6))
            self.obj_var = tk.StringVar(value="0")
            self.obj_spin = ttk.Spinbox(obj, from_=0, to=100000, increment=10, width=6, style="Ban.TSpinbox",
                                        textvariable=self.obj_var, command=self._regler_objectif)
            self.obj_spin.pack(side="left")
            self.obj_spin.bind("<Return>", lambda e: (self._regler_objectif(), self.arbre.focus_set()))
            self.obj_spin.bind("<FocusOut>", lambda e: self._regler_objectif())
            ttk.Label(obj, text=T("h de jeu au total"), style="Ban.TLabel").pack(side="left", padx=(6, 8))
            ttk.Button(obj, text=T("＋ X h…"), style="BanPetit.TButton",
                       command=lambda: self._encore_dialogue([self.sel])).pack(side="left")
            self.b_obj = obj
            self.b_w_obj = self.ban.create_window(0, 0, anchor="sw", window=obj)
            self.b_farm = ttk.Button(self.ban, style="Accent.TButton", width=17, text=T("＋  Ajouter au farm"),
                                     command=self._basculer_selection)
            self.b_steam = ttk.Button(self.ban, text=T("Ouvrir dans Steam"), style="Verre.TButton",
                                      command=self._bouton_steam)
            self._trophee_tk = ImageTk.PhotoImage(dessiner_trophee(int(18 * k)))
            self.b_succes = ttk.Button(self.ban, text=T("Succès"), image=self._trophee_tk, compound="left",
                                       style="Succes.TButton", command=lambda: self._ouvrir_succes())
            self.b_w_farm = self.ban.create_window(0, 0, anchor="se", window=self.b_farm)
            self.b_w_steam = self.ban.create_window(0, 0, anchor="se", window=self.b_steam)
            self.b_w_succes = self.ban.create_window(0, 0, anchor="se", window=self.b_succes)

            # recherche + filtre + groupes
            barre = ttk.Frame(r, padding=(m18, int(4 * k), m18, int(8 * k)))
            barre.pack(fill="x")
            self.recherche = tk.StringVar()
            self.recherche.trace_add("write", lambda *_: self._remplir())
            ttk.Label(barre, text=T("Rechercher"), style="Doux.TLabel").pack(side="left", padx=(0, 8))
            self.champ = ttk.Entry(barre, textvariable=self.recherche)
            self.champ.pack(side="left", fill="x", expand=True)
            self.filtre = tk.StringVar(value=T(FILTRES[0][1]))
            valeurs = self._valeurs_filtre()                 # filtres + collections Steam
            cb = self.cb_filtre = ttk.Combobox(barre, textvariable=self.filtre, values=valeurs, state="readonly",
                                               width=max(15, max(map(len, valeurs)) + 1))
            cb.pack(side="left", padx=(10, 0))
            cb.bind("<<ComboboxSelected>>", lambda e: (self._remplir(), self.arbre.focus_set()))
            self.b_groupes = ttk.Button(barre, text=T("Groupes  ▾"), command=self._menu_groupes)
            self.b_groupes.pack(side="left", padx=(10, 0))
            ttk.Button(barre, text=T("Tout décocher"), command=self._tout_decocher).pack(side="left", padx=(10, 0))

            # pied (placé avant la liste pour qu'il reste visible quand on rétrécit la fenêtre)
            pied = ttk.Frame(r, padding=(m18, int(10 * k), m18, int(14 * k)))
            pied.pack(side="bottom", fill="x")
            self.etat = ttk.Label(pied, style="Doux.TLabel")
            self.etat.pack(side="left", fill="x", expand=True)
            self.bouton = ttk.Button(pied, text=T("▶  Démarrer le farm"), style="Accent.TButton",
                                     command=self._marche_arret)
            self.bouton.pack(side="right")
            ttk.Frame(r, style="Ligne.TFrame", height=1).pack(side="bottom", fill="x", padx=m18)

            # liste
            milieu = ttk.Frame(r, padding=(m18, 0, m18, 0))
            milieu.pack(fill="both", expand=True)
            self.arbre = ttk.Treeview(milieu, columns=("temps", "objectif", "farme", "succes"),
                                      show="tree headings",
                                      selectmode="extended")
            self.arbre.heading("#0", text=T("Jeu"), anchor="w", command=lambda: self._trier("nom"))
            self.arbre.column("#0", width=int(420 * k), stretch=True)
            for col, titre, larg in (("temps", "Temps Steam", 120), ("objectif", "Objectif", 140),
                                     ("farme", "Farmé ici", 140), ("succes", "Succès", 125)):
                self.arbre.heading(col, text=T(titre), anchor="e", command=lambda c=col: self._trier(c))
                self.arbre.column(col, width=int(larg * k), anchor="e", stretch=False)
            sb = ttk.Scrollbar(milieu, orient="vertical", command=self.arbre.yview)
            self.arbre.configure(yscrollcommand=sb.set)
            self.arbre.pack(side="left", fill="both", expand=True)
            sb.pack(side="right", fill="y")
            self.arbre.tag_configure("encours", foreground=VERT)
            self.arbre.tag_configure("echec", foreground=ROUGE)
            self.arbre.tag_configure("remb", foreground=ORANGE)
            self.arbre.tag_configure("attente", foreground=GRIS)
            self.arbre.tag_configure("off", foreground=DOUX)
            self.arbre.bind("<Button-1>", self._clic)
            self.arbre.bind("<Double-1>", self._double)
            self.arbre.bind("<Button-3>", self._menu_contextuel)
            self.arbre.bind("<space>", lambda e: (self._basculer_jeux(map(int, self.arbre.selection())), "break")[1])
            self.arbre.bind("<<TreeviewSelect>>", self._selection)
            r.bind("<Control-f>", lambda e: (self.champ.focus_set(), self.champ.select_range(0, "end")))
            self.champ.bind("<Escape>", lambda e: self.recherche.set(""))

            options_menu = dict(tearoff=0, bg=PANNEAU, fg=TEXTE, activebackground=SELECTION,
                                activeforeground=TEXTE, bd=0, relief="flat", font=("Segoe UI", 10),
                                disabledforeground=GRIS)
            self.menu = tk.Menu(r, **options_menu)
            self.menu_sup = tk.Menu(self.menu, **options_menu)
            self.menu_col = tk.Menu(self.menu, **options_menu)

        def _changer_langue(self, code: str):
            """Reconstruit toute la fenêtre dans l'autre langue, sans arrêter le farm."""
            global LANGUE
            if code == LANGUE or code not in ("fr", "en"):
                return
            LANGUE = code
            self.cfg["langue"] = code
            retenir_langue(code)
            sel = self.sel
            for w in self.r.winfo_children():
                w.destroy()
            self.fen_stats = self.fen_options = self.fen_succes = None
            self.fen_valeur = self.fen_stockage = self.fen_souhaits = self.fen_captures = self.fen_hasard = None
            self.r.title(NOM)
            self._construire()
            if self.actif:
                self.bouton.config(text=T("■  Arrêter le farm"), style="Arret.TButton")
            self.sel = None
            self._maj_sous_titre()
            self._remplir()
            if sel and self.arbre.exists(str(sel)):
                self.arbre.selection_set(str(sel))
                self.arbre.see(str(sel))
            self._planifier_banniere()
            if self.tray:
                self.tray.title = NOM
            self._maj_menu_tray()
            self._montrer_maj()
            self._sauver()
            self._ouvrir_options()

        # ── petites fenêtres ──
        def _dialogue(self, titre: str, message: str, boutons, saisie: str | None = None, parent=None):
            """Fenêtre modale sombre. Renvoie (index du bouton cliqué ou None, texte saisi)."""
            k = self.k
            if self.r.state() != "normal":     # fenêtre cachée (zone de notification) : la montrer d'abord
                self._montrer()
            parent = parent or self.r
            d = tk.Toplevel(parent)
            d.title(titre)
            d.configure(bg=FOND)
            d.transient(parent)
            d.resizable(False, False)
            f = ttk.Frame(d, padding=int(20 * k))
            f.pack(fill="both", expand=True)
            ttk.Label(f, text=message, wraplength=int(440 * k), justify="left").pack(anchor="w")
            var = tk.StringVar(value=saisie or "")
            if saisie is not None:
                e = ttk.Entry(f, textvariable=var, width=40)
                e.pack(fill="x", pady=(12, 0))
                e.focus_set()
                e.select_range(0, "end")
            res = [None]

            def choisir(i):
                res[0] = i
                d.destroy()

            bas = ttk.Frame(f)
            bas.pack(fill="x", pady=(18, 0))
            for i, (txt, style) in reversed(list(enumerate(boutons))):
                ttk.Button(bas, text=txt, style=style, command=lambda i=i: choisir(i)).pack(side="right", padx=(8, 0))
            d.bind("<Escape>", lambda e: d.destroy())
            if saisie is not None:
                d.bind("<Return>", lambda e: choisir(len(boutons) - 1))
            self._barre_sombre(d)
            d.update_idletasks()
            x = parent.winfo_rootx() + (parent.winfo_width() - d.winfo_width()) // 2
            y = parent.winfo_rooty() + (parent.winfo_height() - d.winfo_height()) // 3
            d.geometry(f"+{max(0, x)}+{max(0, y)}")
            d.grab_set()
            self.r.wait_window(d)
            return res[0], var.get().strip()

        def _fenetre(self, attr: str, titre: str, taille: tuple[int, int]):
            ancienne = getattr(self, attr)
            if ancienne is not None and ancienne.winfo_exists():
                ancienne.deiconify()
                ancienne.lift()
                return None
            w = tk.Toplevel(self.r)
            w.title(titre)
            w.configure(bg=FOND)
            self._taille(w, *taille)
            setattr(self, attr, w)
            self._barre_sombre(w)
            return w

        def _ouvrir_options(self):
            w = self._fenetre("fen_options", T("Options"), (620, 930))
            if w is None:
                return
            k = self.k
            f = ttk.Frame(w, padding=(int(22 * k), int(16 * k)))
            f.pack(fill="both", expand=True)
            # boutons du bas placés en premier : sur un petit écran, c'est le contenu qui rétrécit, pas eux
            bas = ttk.Frame(f)
            bas.pack(fill="x", side="bottom")
            ttk.Label(bas, text=T("Version {v}", v=VERSION), style="Gris.TLabel").pack(side="left")
            ttk.Button(bas, text=T("Fermer"), command=w.destroy).pack(side="right")
            ttk.Button(bas, text=T("Page du projet"), command=lambda: os.startfile(PROJET)).pack(side="right", padx=8)

            def titre(texte, trait=True):
                if trait:
                    ttk.Frame(f, style="Ligne.TFrame", height=1).pack(fill="x", pady=(int(2 * k), int(12 * k)))
                ttk.Label(f, text=texte, style="Section.TLabel").pack(anchor="w", pady=(0, int(8 * k)))

            def spin(parent, var, de, a):
                sp = ttk.Spinbox(parent, from_=de, to=a, increment=1, width=5, textvariable=var, command=self._sauver)
                sp.bind("<FocusOut>", lambda e: self._sauver())
                return sp

            def ligne(avant, var, apres):
                l = ttk.Frame(f)
                l.pack(fill="x", pady=(0, 6))
                ttk.Label(l, text=avant).pack(side="left")
                spin(l, var, 0, 9999).pack(side="left", padx=6)
                ttk.Label(l, text=apres, style="Doux.TLabel").pack(side="left")

            def case(texte, var, cmd=None):
                ttk.Checkbutton(f, text=texte, variable=var, command=cmd or self._sauver).pack(anchor="w", pady=(0, 4))

            def note(texte):
                ttk.Label(f, text=texte, style="Gris.TLabel", wraplength=int(560 * k),
                          justify="left").pack(anchor="w", pady=(0, int(10 * k)))

            titre(T("Farm"), trait=False)
            ligne(T("Arrêter le farm automatiquement après"), self.duree, T("h  (0 = jamais)"))
            ligne(T("Rotation : changer de groupe de jeux toutes les"), self.rotation, T("h  (0 = jamais)"))
            note(T("Steam accepte {n} jeux à la fois. Si tu en coches plus, l'appli les fait tourner par groupes "
                   "et passe au groupe suivant à chaque rotation.", n=MAX_SIMULTANES))
            case(T("Garder le PC éveillé pendant le farm"), self.eveille)

            titre(T("Quand farmer"))
            l = ttk.Frame(f)
            l.pack(fill="x", pady=(0, 4))
            ttk.Checkbutton(l, text=T("Farmer seulement quand je n'utilise pas le PC, après"),
                            variable=self.si_inactif, command=self._sauver).pack(side="left")
            spin(l, self.inactif_min, 1, 600).pack(side="left", padx=6)
            ttk.Label(l, text=T("min sans activité"), style="Doux.TLabel").pack(side="left")
            l = ttk.Frame(f)
            l.pack(fill="x", pady=(0, 2))
            ttk.Checkbutton(l, text=T("Farmer seulement entre"), variable=self.plage,
                            command=self._sauver).pack(side="left")
            spin(l, self.plage_debut, 0, 23).pack(side="left", padx=6)
            ttk.Label(l, text=T("h et"), style="Doux.TLabel").pack(side="left")
            spin(l, self.plage_fin, 0, 23).pack(side="left", padx=6)
            ttk.Label(l, text=T("h"), style="Doux.TLabel").pack(side="left")
            note(T("Par exemple de 23 h à 8 h pour farmer la nuit. En dehors de ces heures, le farm se met en pause "
                   "et reprend tout seul."))
            case(T("Mettre le farm en pause quand je lance un vrai jeu"), self.pause_jeu)
            case(T("Passer en invisible sur Steam pendant le farm"), self.invisible, self._regler_invisible)
            note(T("En invisible, tes amis ne te voient plus « En jeu ». Ton statut d'avant revient quand le farm "
                   "s'arrête."))
            case(T("Lancer avec Windows"), self.au_demarrage, self._regler_demarrage)
            note(T("Au démarrage du PC, l'appli s'ouvre cachée dans la zone de notification, attend que Steam "
                   "soit connecté, puis farme tes jeux cochés."))

            titre(T("Langue"))
            langue = tk.StringVar(value=dict(LANGUES)[LANGUE])
            cb = ttk.Combobox(f, textvariable=langue, values=[n for _, n in LANGUES], state="readonly", width=14)
            cb.pack(anchor="w", pady=(0, int(12 * k)))
            cb.bind("<<ComboboxSelected>>", lambda e: self.r.after(
                10, lambda: self._changer_langue(next(c for c, n in LANGUES if n == langue.get()))))

            titre(T("Mises à jour"))
            case(T("Me prévenir quand une nouvelle version sort"), self.maj_auto, self._regler_maj)
            note(T("Une fois par jour au plus, l'appli demande à GitHub le numéro de la dernière version. "
                   "Rien d'autre n'est envoyé : ni ton compte Steam, ni tes jeux."))

            titre(T("Ajouter un jeu par son AppID"))
            l = ttk.Frame(f)
            l.pack(fill="x")
            self.ajout = tk.StringVar()
            e = ttk.Entry(l, textvariable=self.ajout, width=12)
            e.pack(side="left")
            e.bind("<Return>", lambda _: self._ajouter())
            ttk.Button(l, text=T("Ajouter"), command=self._ajouter).pack(side="left", padx=(8, 0))
            note(T("Pour un jeu absent de la liste. L'AppID est le nombre dans l'adresse de sa page Steam "
                   "(store.steampowered.com/app/730 → 730)."))

        # ── statistiques ──
        def _ouvrir_stats(self):
            w = self._fenetre("fen_stats", T("Statistiques"), (800, 760))
            if w is None:
                return
            k = self.k
            m18 = int(18 * k)
            haut = ttk.Frame(w, padding=(m18, int(16 * k), m18, 0))
            haut.pack(fill="x")
            self.tuiles = {}
            for i, (cle, titre) in enumerate((("jour", "Aujourd'hui"), ("semaine", "7 derniers jours"),
                                              ("total", "Total farmé avec l'appli"))):
                t = ttk.Frame(haut, style="Tuile.TFrame", padding=(int(14 * k), int(10 * k)))
                t.pack(side="left", fill="x", expand=True, padx=(0 if i == 0 else int(10 * k), 0))
                ttk.Label(t, text=T(titre), style="TuileTitre.TLabel").pack(anchor="w")
                self.tuiles[cle] = ttk.Label(t, style="TuileValeur.TLabel")
                self.tuiles[cle].pack(anchor="w")
            ttk.Label(w, text=T("Heures farmées par jour · 30 derniers jours"), style="Section.TLabel",
                      padding=(m18, int(18 * k), m18, int(6 * k))).pack(anchor="w")
            self.graphe = tk.Canvas(w, height=int(220 * k), bg=FOND, highlightthickness=0)
            self.graphe.pack(fill="x", padx=m18)
            self.graphe.bind("<Configure>", lambda e: self._dessiner_graphe())
            self.graphe.bind("<Motion>", self._survol_graphe)
            self.graphe.bind("<Leave>", lambda e: self._survol_graphe(None))
            ttk.Label(w, text=T("Jeux les plus farmés"), style="Section.TLabel",
                      padding=(m18, int(18 * k), m18, int(6 * k))).pack(anchor="w")
            self.top = tk.Canvas(w, bg=FOND, highlightthickness=0)
            self.top.pack(fill="both", expand=True, padx=m18, pady=(0, m18))
            self.top.bind("<Configure>", lambda e: self._dessiner_top())
            self._rafraichir_stats()

        def _jours(self, n: int = 30):
            aujourd = dt.date.today()
            out = []
            for i in range(n - 1, -1, -1):
                d = aujourd - dt.timedelta(days=i)
                h = self.historique.get(d.isoformat(), {})
                out.append((d, sum(h.values()), len(h)))
            return out

        def _rafraichir_stats(self):
            w = self.fen_stats
            if w is None or not w.winfo_exists():
                return
            jours = self._jours(30)
            self.tuiles["jour"].config(text=heures(jours[-1][1] / 60, None))
            self.tuiles["semaine"].config(text=heures(sum(j[1] for j in jours[-7:]) / 60, None))
            total = sum(self._total(a) for a in set(self.farme) | set(self.procs))
            self.tuiles["total"].config(text=heures(total / 60, None))
            self._dessiner_graphe()
            self._dessiner_top()
            if getattr(self, "_stats_apres", None):
                self.r.after_cancel(self._stats_apres)
            self._stats_apres = self.r.after(5000, self._rafraichir_stats)

        def _dessiner_graphe(self):
            c, k = self.graphe, self.k
            c.delete("all")
            self._barres = []
            W, H = c.winfo_width(), c.winfo_height()
            if W < 100:
                return
            jours = self._jours(30)
            vmax = max(v for _, v, _ in jours) / 3600
            g, b, t = int(46 * k), int(24 * k), int(10 * k)
            if vmax <= 0:
                c.create_line(g, H - b, W, H - b, fill=BORD)
                c.create_text((g + W) / 2, (H - b) / 2, fill=GRIS, font=("Segoe UI", 10),
                              text=T("Pas encore d'heures farmées. Lance le farm pour voir tes stats ici."))
                return
            pas = next(p for p in (0.25, 0.5, 1, 2, 5, 10, 20, 25, 50, 100, 200, 250, 500, 1000)
                       if vmax / p <= 4)
            ymax = pas * math.ceil(vmax / pas)
            hauteur = H - b - t
            for i in range(int(round(ymax / pas)) + 1):
                y = H - b - i * pas / ymax * hauteur
                c.create_line(g, y, W, y, fill=BORD if i else "#3a4150")
                c.create_text(g - int(8 * k), y, text=nombre_txt(i * pas) + ("h" if LANGUE == "en" else " h"),
                              anchor="e", fill=GRIS, font=("Segoe UI", 8))
            fente = (W - g) / len(jours)
            larg = max(3.0, min(fente * 0.62, 26 * k))
            for i, (d, v, n) in enumerate(jours):
                x0 = g + i * fente + (fente - larg) / 2
                hpx = v / 3600 / ymax * hauteur
                barre = barre_arrondie(c, x0, H - b - hpx, x0 + larg, H - b, 4 * k, fill=BARRE) if v > 0 else None
                self._barres.append((g + i * fente, g + (i + 1) * fente, d, v, n, barre, x0 + larg / 2, H - b - hpx))
                if i % 7 == len(jours) % 7 - 1 or i == len(jours) - 1:
                    it = c.create_text(x0 + larg / 2, H - b + int(12 * k), fill=GRIS, font=("Segoe UI", 8),
                                       text=T("auj.") if i == len(jours) - 1 else date_courte(d))
                    bx = c.bbox(it)
                    if bx and bx[2] > W - 1:          # étiquette du dernier jour : ne pas dépasser le bord
                        c.move(it, W - 1 - bx[2], 0)

        def _survol_graphe(self, e):
            c = self.graphe
            c.delete("bulle")
            for x0, x1, d, v, n, barre, xc, ytop in getattr(self, "_barres", []):
                if barre:
                    c.itemconfig(barre, fill=BARRE)
            if e is None:
                return
            for x0, x1, d, v, n, barre, xc, ytop in getattr(self, "_barres", []):
                if x0 <= e.x < x1:
                    if barre:
                        c.itemconfig(barre, fill=ACCENT)
                    texte = T("{j} {d} : {h}", j=jour_court(d), d=date_courte(d), h=heures(v / 60, None))
                    if n:
                        texte += pluriel(n, " · {n} jeu", " · {n} jeux")
                    W = c.winfo_width()
                    t = c.create_text(0, 0, text=texte, fill=TEXTE, font=("Segoe UI", 9), anchor="s", tags="bulle")
                    bx = c.bbox(t)
                    demi = (bx[2] - bx[0]) / 2 + 8
                    x = min(max(xc, demi), W - demi)
                    y = max(ytop - 8, bx[3] - bx[1] + 10)
                    c.coords(t, x, y - 4)
                    bx = c.bbox(t)
                    fond = c.create_rectangle(bx[0] - 8, bx[1] - 4, bx[2] + 8, bx[3] + 4, fill=PANNEAU,
                                              outline=BORD, tags="bulle")
                    c.tag_raise(t, fond)
                    return

        def _dessiner_top(self):
            c, k = self.top, self.k
            c.delete("all")
            W = c.winfo_width()
            totaux = sorted(((self._total(a), a) for a in set(self.farme) | set(self.procs) if self._total(a) >= 60),
                            reverse=True)[:10]
            if not totaux:
                c.create_text(0, int(10 * k), anchor="nw", fill=GRIS, font=("Segoe UI", 10),
                              text=T("Aucun jeu farmé pour l'instant."))
                return
            self._top_imgs = []
            pas, nom_l, val_l = int(28 * k), int(250 * k), int(80 * k)
            vmax = totaux[0][0]
            for i, (v, a) in enumerate(totaux):
                y = i * pas + pas / 2
                if a in self.icones:
                    im = ImageTk.PhotoImage(self.icones[a].resize((int(20 * k), int(20 * k))))
                    self._top_imgs.append(im)
                    c.create_image(0, y, image=im, anchor="w")
                nom = self._nom(a)
                c.create_text(int(30 * k), y, text=nom if len(nom) < 30 else nom[:28] + "…", anchor="w",
                              fill=TEXTE, font=("Segoe UI", 10))
                larg = max(4 * k, (W - nom_l - val_l) * v / vmax)
                c.create_rectangle(nom_l, y - 6 * k, nom_l + larg, y + 6 * k, fill=BARRE, width=0)
                c.create_text(nom_l + larg + int(8 * k), y, text=heures(v / 60), anchor="w", fill=DOUX,
                              font=("Segoe UI", 9))

        # ── succès ──
        # Lire les succès passe par un processus --succes qui se présente à Steam comme le jeu : Steam affiche
        # « En jeu » quelques secondes. Rien n'est lu tant que l'utilisateur n'ouvre pas la fenêtre.
        def _ouvrir_succes(self, a: int | None = None):
            a = a or self.sel
            if a not in self.jeux:
                return
            ancienne = self.fen_succes
            if ancienne is not None and ancienne.winfo_exists():
                if self.succes_jeu == a or self.succes_occupe:   # même jeu, ou une opération est en cours
                    ancienne.deiconify()
                    ancienne.lift()
                    return
                ancienne.destroy()
            self.fen_succes = None
            w = self._fenetre("fen_succes", T("Succès · {j}", j=self._nom(a)), (760, 700))
            k = self.k
            m18 = int(18 * k)
            self.succes_jeu, self.succes_liste, self.succes_icones, self._succes_tk = a, [], {}, {}
            haut = ttk.Frame(w, padding=(m18, int(16 * k), m18, int(4 * k)))
            haut.pack(fill="x")
            ttk.Label(haut, text=self._nom(a), style="Titre.TLabel").pack(anchor="w")
            ligne = ttk.Frame(haut)
            ligne.pack(fill="x", pady=(int(6 * k), 0))
            self.s_resume = ttk.Label(ligne, style="Doux.TLabel")
            self.s_resume.pack(side="left")
            self.s_jauge = tk.Canvas(ligne, height=max(4, int(6 * k)), width=int(180 * k), bg=FOND,
                                     highlightthickness=0)
            self.s_jauge.pack(side="left", padx=(int(12 * k), 0))
            outils = ttk.Frame(w, padding=(m18, int(10 * k), m18, int(8 * k)))
            outils.pack(fill="x")
            self.s_filtre = tk.StringVar(value=T(FILTRES_SUCCES[0][1]))
            cb = ttk.Combobox(outils, textvariable=self.s_filtre, values=[T(l) for _, l in FILTRES_SUCCES],
                              state="readonly", width=16)
            cb.pack(side="left")
            cb.bind("<<ComboboxSelected>>", lambda e: (self._remplir_succes(), self.s_arbre.focus_set()))
            ttk.Button(outils, text=T("Tout sélectionner"), command=self._tout_selectionner_succes).pack(
                side="left", padx=(10, 0))
            self.s_recharger = ttk.Button(outils, text=T("↻  Recharger"), command=lambda: self._lancer_succes("lire"))
            self.s_recharger.pack(side="right")
            # pied (avant la liste, pour qu'il reste visible si la fenêtre rétrécit)
            pied = ttk.Frame(w, padding=(m18, int(10 * k), m18, int(14 * k)))
            pied.pack(side="bottom", fill="x")
            self.s_debloquer = ttk.Button(pied, style="Accent.TButton", command=lambda: self._agir_succes("debloquer"))
            self.s_debloquer.pack(side="right")
            self.s_verrouiller = ttk.Button(pied, command=lambda: self._agir_succes("verrouiller"))
            self.s_verrouiller.pack(side="right", padx=(0, 8))
            self.s_etat = ttk.Label(pied, style="Doux.TLabel", wraplength=int(400 * k), justify="left")
            self.s_etat.pack(side="left", fill="x", expand=True)
            ttk.Frame(w, style="Ligne.TFrame", height=1).pack(side="bottom", fill="x", padx=m18)
            milieu = ttk.Frame(w, padding=(m18, 0, m18, 0))
            milieu.pack(fill="both", expand=True)
            arbre = self.s_arbre = ttk.Treeview(milieu, columns=("etat", "pct"), show="tree headings",
                                                selectmode="extended", style="Succes.Treeview")
            arbre.heading("#0", text=T("Succès"), anchor="w")
            arbre.column("#0", width=int(440 * k), stretch=True)
            arbre.heading("etat", text=T("Débloqué le"), anchor="e")
            arbre.column("etat", width=int(130 * k), anchor="e", stretch=False)
            arbre.heading("pct", text=T("Joueurs"), anchor="e")
            arbre.column("pct", width=int(80 * k), anchor="e", stretch=False)
            sb = ttk.Scrollbar(milieu, orient="vertical", command=arbre.yview)
            arbre.configure(yscrollcommand=sb.set)
            arbre.pack(side="left", fill="both", expand=True)
            sb.pack(side="right", fill="y")
            arbre.tag_configure("verrou", foreground=DOUX)
            arbre.bind("<<TreeviewSelect>>", lambda e: self._maj_boutons_succes())
            arbre.bind("<Control-a>", lambda e: (self._tout_selectionner_succes(), "break")[1])
            w.bind("<Escape>", lambda e: w.destroy())
            self._maj_boutons_succes()
            self._lancer_succes("lire")

        def _maj_bouton_succes(self, variante: str = "compte"):
            """Bouton de la bannière : « Succès 12/76 » si on a déjà lu les succès de ce jeu, « Succès » sinon,
            ou le trophée seul (variante "icone") quand la place manque."""
            if variante == "icone":
                self.b_succes.config(text="", compound="image")
                return
            vu = self._succes_compte(self.sel)
            texte = T("Succès  {f}/{n}", f=vu[0], n=vu[1]) if variante == "compte" and vu else T("Succès")
            self.b_succes.config(text=texte, compound="left")

        def _succes_ouverte(self) -> bool:
            w = self.fen_succes
            return w is not None and w.winfo_exists()

        def _etat_succes(self, texte: str, couleur: str = DOUX):
            if self._succes_ouverte():
                self.s_etat.config(text=texte, foreground=couleur)

        def _lancer_succes(self, action: str, noms=()):
            """Lit (ou débloque / re-verrouille) les succès du jeu de la fenêtre, dans un fil à part."""
            a = self.succes_jeu
            if self.succes_occupe or a is None:
                return
            if not steam_pret():
                self._etat_succes(T("Ouvre Steam et connecte-toi pour voir les succès."), ORANGE)
                return
            self.succes_occupe = True
            self._maj_boutons_succes()
            self._etat_succes({"lire": T("Lecture des succès dans Steam…"),
                               "debloquer": T("Déblocage en cours…"),
                               "verrouiller": T("Re-verrouillage en cours…")}[action])
            if not self.succes_liste:
                self.s_resume.config(text=T("Chargement…"))
            # pendant ce temps, le registre de Steam dit que le jeu tourne : ce n'est pas un vrai jeu lancé
            self.arretes[a] = time.time()

            def tache():
                res = succes_steam(a, action, noms)
                self.q.put(lambda: self._succes_recus(a, action, list(noms), res))
            threading.Thread(target=tache, daemon=True).start()

        def _succes_recus(self, a: int, action: str, noms: list[str], res: dict):
            self.succes_occupe = False
            self.arretes[a] = time.time()
            if not self._succes_ouverte() or self.succes_jeu != a:
                return
            erreur = res.get("erreur")
            if erreur:
                textes = {"init": T("Steam a refusé d'ouvrir les succès de ce jeu ({d}).", d=res.get("detail", "")),
                          "stats": T("Steam n'a pas envoyé tes succès. Réessaie dans un instant."),
                          "dll": T("La DLL de Steam trouvée est trop ancienne pour lire les succès.")}
                self._etat_succes(textes.get(erreur, T("Lecture impossible ({e}). Détails dans journal.txt.", e=erreur)),
                                  ROUGE)
                if not self.succes_liste:
                    self.s_resume.config(text="")
                self._maj_boutons_succes()
                return
            self.succes_liste = res.get("succes", [])
            # compte gardé en config pour l'afficher sur le bouton de la bannière
            compte = [sum(x["fait"] for x in self.succes_liste), len(self.succes_liste)]
            self.cfg.setdefault("succes_vus", {})[str(a)] = compte
            if compte[1]:
                self.succes_cache[a] = tuple(compte)
            self._sauver()
            self._maj_ligne(a)
            if self.sel == a:
                self._planifier_banniere()
            self.succes_icones, self._succes_tk = {}, {}
            blob = res.get("_icones", b"")
            for ident, (debut, iw, ih) in res.get("icones", {}).items():
                try:
                    self.succes_icones[ident] = Image.frombytes("RGBA", (iw, ih), blob[debut:debut + iw * ih * 4])
                except (ValueError, TypeError):
                    pass
            garder = set(self.s_arbre.selection())
            self._remplir_succes()
            garder = [i for i in garder if self.s_arbre.exists(i)]
            if garder:
                self.s_arbre.selection_set(garder)
            if action == "lire":
                self._etat_succes(T("Ce jeu n'a pas de succès Steam.") if not self.succes_liste else
                                  T("Sélectionne des succès (Ctrl+clic ou Maj+clic pour en prendre plusieurs)."),
                                  GRIS)
                return
            refuses = res.get("refuses", [])
            faits = len(noms) - len(refuses)
            if action == "debloquer":
                texte = pluriel(faits, "{n} succès débloqué.", "{n} succès débloqués.")
            else:
                texte = pluriel(faits, "{n} succès re-verrouillé.", "{n} succès re-verrouillés.")
            if refuses:
                texte += " " + pluriel(len(refuses), "Steam en a refusé {n} : le jeu le protège.",
                                       "Steam en a refusé {n} : le jeu les protège.")
            self._etat_succes(texte, ORANGE if refuses else VERT)

        def _remplir_succes(self):
            arbre = self.s_arbre
            arbre.delete(*arbre.get_children())
            filtre = next((c for c, l in FILTRES_SUCCES if T(l) == self.s_filtre.get()), "tous")
            for x in self.succes_liste:
                if (filtre == "verrouilles" and x["fait"]) or (filtre == "debloques" and not x["fait"]):
                    continue
                desc = x["desc"] if len(x["desc"]) <= 72 else x["desc"][:70].rstrip() + "…"
                if x["fait"]:
                    etat = date_longue(x["date"]) if x["date"] else T("Débloqué")
                else:
                    etat = T("Caché") if x["cache"] else T("Verrouillé")
                arbre.insert("", "end", iid=x["id"], text=f'{x["nom"]}\n{desc}' if desc else x["nom"],
                             image=self._image_succes(x), values=(etat, pct(x["pct"]) if "pct" in x else ""),
                             tags=() if x["fait"] else ("verrou",))
            self._maj_resume_succes()
            self._maj_boutons_succes()

        def _image_succes(self, x: dict):
            if x["id"] not in self._succes_tk:
                k = self.k
                s = int(40 * k)
                im = self.succes_icones.get(x["id"])
                im = arrondir(im.resize((s, s), Image.LANCZOS), 0.14) if im else icone_jeu(None, x["nom"], s)
                cadre = Image.new("RGBA", (s + int(12 * k), s), (0, 0, 0, 0))   # marge avant le texte
                cadre.alpha_composite(im)
                self._succes_tk[x["id"]] = ImageTk.PhotoImage(cadre)
            return self._succes_tk[x["id"]]

        def _maj_resume_succes(self):
            n, k = len(self.succes_liste), self.k
            f = sum(x["fait"] for x in self.succes_liste)
            c = self.s_jauge
            c.delete("all")
            if not n:
                self.s_resume.config(text=T("Aucun succès"))
                return
            self.s_resume.config(text=T("{f} sur {n} débloqués", f=f, n=n) + "  ·  " + pct(int(100 * f / n)))
            L, H = int(180 * k), max(4, int(6 * k))
            c.create_rectangle(0, 0, L, H, fill="#323a47", width=0)
            if f:
                c.create_rectangle(0, 0, max(2, L * f / n), H, fill=VERT if f == n else ACCENT, width=0)

        def _selection_succes(self) -> list[dict]:
            par_id = {x["id"]: x for x in self.succes_liste}
            return [par_id[i] for i in self.s_arbre.selection() if i in par_id]

        def _maj_boutons_succes(self):
            if not self._succes_ouverte():
                return
            sel = self._selection_succes()
            n_deb, n_ver = sum(not x["fait"] for x in sel), sum(x["fait"] for x in sel)
            occupe = self.succes_occupe
            self.s_debloquer.config(text=T("Débloquer ({n})", n=n_deb) if n_deb else T("Débloquer"),
                                    state="disabled" if occupe or not n_deb else "normal")
            self.s_verrouiller.config(text=T("Re-verrouiller ({n})", n=n_ver) if n_ver else T("Re-verrouiller"),
                                      state="disabled" if occupe or not n_ver else "normal")
            self.s_recharger.config(state="disabled" if occupe else "normal")

        def _tout_selectionner_succes(self):
            self.s_arbre.selection_set(self.s_arbre.get_children())

        def _agir_succes(self, action: str):
            if self.succes_occupe:
                return
            debloquer = action == "debloquer"
            cibles = [x["id"] for x in self._selection_succes() if x["fait"] != debloquer]
            if not cibles:
                return
            jeu, n = self._nom(self.succes_jeu), len(cibles)
            if debloquer:
                texte = pluriel(n, "Débloquer {n} succès de {j} ?", "Débloquer les {n} succès de {j} ?", j=jeu)
                texte += "\n\n" + T("Ils apparaîtront sur ton profil Steam avec la date d'aujourd'hui.")
                if not self.cfg.get("succes_averti"):
                    texte += "\n\n" + T("À savoir : ces succès comptent comme les autres sur ton profil. Des sites "
                                        "de classement peuvent repérer ceux débloqués tous d'un coup, et les jeux en "
                                        "ligne avec anti-triche ont parfois leurs propres règles. À tes risques.")
                bouton = T("Débloquer")
            else:
                texte = pluriel(n, "Re-verrouiller {n} succès de {j} ?", "Re-verrouiller les {n} succès de {j} ?",
                                j=jeu)
                texte += "\n\n" + T("Leur date de déblocage sera perdue. Si tu les débloques à nouveau, ils auront "
                                    "la date de ce jour-là.")
                bouton = T("Re-verrouiller")
            choix, _ = self._dialogue(T("Succès"), texte, [(T("Annuler"), "TButton"), (bouton, "Accent.TButton")],
                                      parent=self.fen_succes)
            if choix != 1 or not self._succes_ouverte():
                return
            if debloquer and not self.cfg.get("succes_averti"):
                self.cfg["succes_averti"] = True
                self._sauver()
            self._lancer_succes(action, cibles)

        # ── zone de notification ──
        def _creer_tray(self):
            if pystray is None:
                return
            try:
                M = pystray.MenuItem
                menu = pystray.Menu(
                    M(lambda _: T("Ouvrir"), lambda: self.q.put(self._montrer), default=True),
                    M(lambda _: T("Arrêter le farm") if self.actif else T("Démarrer le farm"),
                      lambda: self.q.put(self._marche_arret)),
                    M(lambda _: T("Statistiques"), lambda: self.q.put(lambda: (self._montrer(), self._ouvrir_stats()))),
                    pystray.Menu.SEPARATOR,
                    M(lambda _: T("Quitter"), lambda: self.q.put(self._quitter)))
                self.tray = pystray.Icon(NOM_RUN, dessiner_logo(64), NOM, menu)
                self.tray.run_detached()
            except Exception as e:  # noqa: BLE001
                journal(T("icône de notification impossible : {e}", e=repr(e)))
                self.tray = None

        # ───────────── Outils Steam (menu Outils) ─────────────
        def _menu_outils(self):
            m = self.menu
            m.delete(0, "end")
            m.add_command(label=T("Valeur de la bibliothèque…"), command=self._ouvrir_valeur)
            m.add_command(label=T("Stockage des jeux installés…"), command=self._ouvrir_stockage)
            m.add_command(label=T("Promos de ma liste de souhaits…"), command=self._ouvrir_souhaits)
            m.add_command(label=T("Captures d'écran…"), command=self._ouvrir_captures)
            m.add_separator()
            m.add_command(label=T("À quoi je joue ?…"), command=self._ouvrir_hasard)
            b = self.b_outils
            m.tk_popup(b.winfo_rootx(), b.winfo_rooty() + b.winfo_height())

        def _ouverte(self, attr: str) -> bool:
            w = getattr(self, attr, None)
            return w is not None and bool(w.winfo_exists())

        def _ouvrir_lien(self, cible: str):
            try:
                os.startfile(cible)
            except OSError as e:
                self.etat.config(text=T("Impossible d'ouvrir : {e}", e=e))

        def _magasin_ligne(self, arbre, e):
            ligne = arbre.identify_row(e.y)
            if ligne:
                self._ouvrir("https://store.steampowered.com/app/{}", int(ligne))

        def _icone_tk(self, a: int, nom: str):
            """Icône d'un jeu pour les listes des fenêtres Outils (aussi pour un jeu absent de la liste principale)."""
            if a not in self._icones_tk:
                s = int(24 * self.k)
                if a not in self.icones:
                    ch = self._chemins(a) if a in self.jeux else (chemins_images(self.steam, a) if self.steam else {})
                    self.icones[a] = icone_jeu(ch.get("icone"), nom, s)
                im = Image.new("RGBA", (s + int(10 * self.k), s), (0, 0, 0, 0))
                im.alpha_composite(self.icones[a], (0, 0))
                self._icones_tk[a] = ImageTk.PhotoImage(im)
            return self._icones_tk[a]

        def _tuiles(self, parent, titres) -> dict:
            """Rangée de tuiles (titre, grande valeur, petite note), comme dans Statistiques."""
            k, out = self.k, {}
            for i, (cle, titre) in enumerate(titres):
                t = ttk.Frame(parent, style="Tuile.TFrame", padding=(int(14 * k), int(10 * k)))
                t.pack(side="left", fill="x", expand=True, padx=(0 if i == 0 else int(10 * k), 0))
                ttk.Label(t, text=titre, style="TuileTitre.TLabel").pack(anchor="w")
                out[cle] = ttk.Label(t, style="TuileValeur.TLabel", text="—")
                out[cle].pack(anchor="w")
                out[cle + "_note"] = ttk.Label(t, style="TuileTitre.TLabel")
                out[cle + "_note"].pack(anchor="w")
            return out

        def _table(self, parent, nom: str, colonnes):
            """Liste d'une fenêtre Outils. colonnes : (identifiant, titre déjà traduit, largeur) ; la première est
            le nom du jeu. Un clic sur un en-tête trie la liste (self.tris_outils[nom])."""
            k = self.k
            cadre = ttk.Frame(parent)
            arbre = ttk.Treeview(cadre, columns=[c[0] for c in colonnes[1:]], show="tree headings",
                                 selectmode="browse")
            for i, (col, _titre, larg) in enumerate(colonnes):
                ident = "#0" if i == 0 else col
                arbre.heading(ident, anchor="w" if i == 0 else "e", command=lambda c=col: self._trier_table(nom, c))
                arbre.column(ident, width=int(larg * k), anchor="w" if i == 0 else "e", stretch=i == 0)
            sb = ttk.Scrollbar(cadre, orient="vertical", command=arbre.yview)
            arbre.configure(yscrollcommand=sb.set)
            arbre.pack(side="left", fill="both", expand=True)
            sb.pack(side="right", fill="y")
            arbre.tag_configure("promo", foreground=VERT)
            arbre.tag_configure("doux", foreground=DOUX)
            self.tables[nom] = (arbre, colonnes)
            self._entetes(nom)
            return cadre, arbre

        def _entetes(self, nom: str):
            arbre, colonnes = self.tables[nom]
            col_tri, inverse = self.tris_outils[nom]
            for i, (col, titre, _larg) in enumerate(colonnes):
                fleche = (" ▼" if inverse else " ▲") if col == col_tri else ""
                arbre.heading("#0" if i == 0 else col, text=titre + fleche)

        def _trier_table(self, nom: str, col: str):
            ancien = self.tris_outils[nom]
            self.tris_outils[nom] = (col, not ancien[1] if ancien[0] == col else col != "nom")
            self._entetes(nom)
            getattr(self, "_remplir_" + nom)()

        # ── valeur de la bibliothèque ──
        def _ouvrir_valeur(self):
            w = self._fenetre("fen_valeur", T("Valeur de la bibliothèque"), (900, 720))
            if w is None:
                return
            k = self.k
            m18 = int(18 * k)
            haut = ttk.Frame(w, padding=(m18, int(16 * k), m18, 0))
            haut.pack(fill="x")
            self.v_tuiles = self._tuiles(haut, (("total", T("Valeur au prix normal")),
                                                ("jamais", T("Payants, jamais lancés")),
                                                ("heure", T("Coût moyen par heure jouée"))))
            outils = ttk.Frame(w, padding=(m18, int(12 * k), m18, int(8 * k)))
            outils.pack(fill="x")
            self.v_filtre = tk.StringVar(value=T(FILTRES_VALEUR[0][1]))
            cb = ttk.Combobox(outils, textvariable=self.v_filtre, values=[T(l) for _, l in FILTRES_VALEUR],
                              state="readonly", width=max(len(T(l)) for _, l in FILTRES_VALEUR) + 1)
            cb.pack(side="left")
            cb.bind("<<ComboboxSelected>>", lambda e: (self._remplir_valeur(), self.v_arbre.focus_set()))
            self.v_actualiser = ttk.Button(outils, text=T("↻  Actualiser"), command=lambda: self._charger_prix(True))
            self.v_actualiser.pack(side="right")
            pied = ttk.Frame(w, padding=(m18, int(10 * k), m18, int(14 * k)))
            pied.pack(side="bottom", fill="x")
            self.v_etat = ttk.Label(pied, style="Gris.TLabel", wraplength=int(860 * k), justify="left")
            self.v_etat.pack(side="left", fill="x", expand=True)
            ttk.Frame(w, style="Ligne.TFrame", height=1).pack(side="bottom", fill="x", padx=m18)
            cadre, self.v_arbre = self._table(w, "valeur", (("nom", T("Jeu"), 330), ("prix", T("Prix normal"), 115),
                                                             ("actuel", T("En ce moment"), 150),
                                                             ("temps", T("Temps de jeu"), 115),
                                                             ("heure", T("Prix par heure"), 125)))
            cadre.pack(fill="both", expand=True, padx=m18)
            self.v_arbre.bind("<Double-1>", lambda e: self._magasin_ligne(self.v_arbre, e))
            w.bind("<Escape>", lambda e: w.destroy())
            self._charger_prix(False)

        def _etat_valeur(self, texte: str, couleur: str = GRIS):
            if self._ouverte("fen_valeur"):
                self.v_etat.config(text=texte, foreground=couleur)

        def _charger_prix(self, forcer: bool):
            """Demande à la boutique Steam le prix de chaque jeu de la liste, dans un fil à part. Les prix sont
            gardés une heure en mémoire."""
            if self.prix_occupe:
                return
            if self.prix is not None and not forcer and time.time() - self.prix_date < 3600:
                self._remplir_valeur()
                return
            ids = [a for a in self.jeux if a not in self.famille]
            if not ids:
                self._etat_valeur(T("Ta liste de jeux est vide."))
                return
            self.prix_occupe = True
            self.v_actualiser.state(["disabled"])
            self._etat_valeur(T("Lecture des prix dans la boutique Steam…"))

            def tache():
                res = prix_steam(ids)
                self.q.put(lambda: self._prix_recus(res))
            threading.Thread(target=tache, daemon=True).start()

        def _prix_recus(self, res: dict | None):
            self.prix_occupe = False
            if res is not None:
                self.prix, self.prix_date = res, time.time()
            if not self._ouverte("fen_valeur"):
                return
            self.v_actualiser.state(["!disabled"])
            if res is None:
                self._etat_valeur(T("La boutique Steam ne répond pas. Vérifie ta connexion, puis clique sur "
                                    "Actualiser."), ROUGE)
            if self.prix is not None:
                self._remplir_valeur(garder_etat=res is None)

        def _remplir_valeur(self, garder_etat: bool = False):
            if not self._ouverte("fen_valeur") or self.prix is None:
                return
            arbre = self.v_arbre
            arbre.delete(*arbre.get_children())
            filtre = next((c for c, l in FILTRES_VALEUR if T(l) == self.v_filtre.get()), "tous")
            lignes, devise, gratuits, retires = [], "", 0, 0
            for a, j in self.jeux.items():
                if a in self.famille or a not in self.prix:
                    continue
                p = self.prix[a]
                if p == GRATUIT:
                    gratuits += 1
                elif not p:
                    retires += 1
                else:
                    devise = devise or p[0]
                    lignes.append({"a": a, "nom": j["nom"], "normal": p[1], "actuel": p[2], "promo": remise(p),
                                   "sec": self._temps_steam(a)})
            total, actuel = sum(x["normal"] for x in lignes), sum(x["actuel"] for x in lignes)
            jamais = [x for x in lignes if x["sec"] < 60]
            joue = sum(x["sec"] for x in lignes) / 3600
            t = self.v_tuiles
            t["total"].config(text=argent(total, devise) if lignes else "—")
            t["total_note"].config(text=T("{p} avec les promos du moment", p=argent(actuel, devise)) if actuel < total
                                   else pluriel(len(lignes), "{n} jeu payant", "{n} jeux payants"))
            t["jamais"].config(text=argent(sum(x["normal"] for x in jamais), devise) if lignes else "—")
            t["jamais_note"].config(text=pluriel(len(jamais), "{n} jeu", "{n} jeux"))
            t["heure"].config(text=T("{p} / h", p=argent(total / joue, devise)) if joue >= 1 else "—")
            t["heure_note"].config(text=T("{h} jouées sur ces jeux", h=heures(joue * 60, None)))
            if filtre == "jamais":
                lignes = jamais
            elif filtre == "promo":
                lignes = [x for x in lignes if x["promo"]]
            col, inverse = self.tris_outils["valeur"]
            cles = {"nom": lambda x: x["nom"].lower(), "prix": lambda x: x["normal"], "actuel": lambda x: x["promo"],
                    "temps": lambda x: x["sec"],
                    "heure": lambda x: x["normal"] / (x["sec"] / 3600) if x["sec"] >= 60 else math.inf}
            for x in sorted(lignes, key=cles[col], reverse=inverse):
                promo = f"{argent(x['actuel'], devise)}   −{pct(x['promo'])}" if x["promo"] else ""
                if x["sec"] >= 60:
                    temps, par_heure = heures(x["sec"] / 60), T("{p} / h", p=argent(x["normal"] / (x["sec"] / 3600),
                                                                                    devise))
                else:
                    temps, par_heure = T("jamais lancé"), "—"
                arbre.insert("", "end", iid=str(x["a"]), text="  " + x["nom"], image=self._icone_tk(x["a"], x["nom"]),
                             values=(argent(x["normal"], devise), promo, temps, par_heure),
                             tags=("promo",) if x["promo"] else ())
            if not garder_etat:
                self._etat_valeur(T("Prix du moment dans la boutique Steam, pas ce que tu as payé. Double-clic sur un "
                                    "jeu : page du magasin.") + "  " +
                                  T("Non comptés : {g} gratuits, {r} retirés de la boutique, {f} partagés par ta "
                                    "famille.", g=gratuits, r=retires,
                                    f=sum(1 for a in self.jeux if a in self.famille)))

        # ── stockage ──
        def _ouvrir_stockage(self):
            w = self._fenetre("fen_stockage", T("Stockage des jeux installés"), (840, 660))
            if w is None:
                return
            k = self.k
            m18 = int(18 * k)
            self.st_haut = ttk.Frame(w, padding=(m18, int(16 * k), m18, 0))
            self.st_haut.pack(fill="x")
            outils = ttk.Frame(w, padding=(m18, int(12 * k), m18, int(8 * k)))
            outils.pack(fill="x")
            ttk.Label(outils, text=T("Clique sur « Dernier lancement » pour voir en premier les jeux que tu ne lances "
                                     "plus."), style="Gris.TLabel").pack(side="left")
            ttk.Button(outils, text=T("↻  Actualiser"), command=self._actualiser_stockage).pack(side="right")
            pied = ttk.Frame(w, padding=(m18, int(10 * k), m18, int(14 * k)))
            pied.pack(side="bottom", fill="x")
            self.st_desinstaller = ttk.Button(pied, text=T("Désinstaller…"), command=self._desinstaller_selection)
            self.st_desinstaller.pack(side="right")
            self.st_steam = ttk.Button(pied, text=T("Ouvrir dans Steam"), command=lambda: self._ouvrir(
                "steam://nav/games/details/{}", self._selection_stockage()))
            self.st_steam.pack(side="right", padx=(0, 8))
            self.st_etat = ttk.Label(pied, style="Gris.TLabel", wraplength=int(480 * k), justify="left")
            self.st_etat.pack(side="left", fill="x", expand=True)
            ttk.Frame(w, style="Ligne.TFrame", height=1).pack(side="bottom", fill="x", padx=m18)
            cadre, self.st_arbre = self._table(w, "stockage", (("nom", T("Jeu"), 380), ("taille", T("Taille"), 110),
                                                                ("lance", T("Dernier lancement"), 170),
                                                                ("disque", T("Disque"), 90)))
            cadre.pack(fill="both", expand=True, padx=m18)
            self.st_arbre.bind("<<TreeviewSelect>>", lambda e: self._maj_boutons_stockage())
            w.bind("<Escape>", lambda e: w.destroy())
            self._actualiser_stockage()

        def _etat_stockage(self, texte: str, couleur: str = GRIS):
            if self._ouverte("fen_stockage"):
                self.st_etat.config(text=texte, foreground=couleur)

        def _selection_stockage(self) -> int | None:
            s = self.st_arbre.selection() if self._ouverte("fen_stockage") else ()
            return int(s[0]) if s else None

        def _maj_boutons_stockage(self):
            etat = ["!disabled"] if self._selection_stockage() else ["disabled"]
            self.st_desinstaller.state(etat)
            self.st_steam.state(etat)

        def _actualiser_stockage(self):
            """Relit les jeux installés (après une installation ou une désinstallation dans Steam)."""
            if self.steam:
                self.installations = jeux_installes(self.steam)
                self.installes = set(self.installations)
                self._planifier_banniere()
                if self._filtre_cle() == "installes":
                    self._remplir()
            self._remplir_stockage()
            self._etat_stockage("")

        def _remplir_stockage(self):
            if not self._ouverte("fen_stockage"):
                return
            arbre = self.st_arbre
            garde = arbre.selection()
            arbre.delete(*arbre.get_children())
            lignes = []
            for a, x in self.installations.items():
                if a == APP_REDIST:
                    continue
                nom = self.jeux[a]["nom"] if a in self.jeux else (self.infos.get(a, ("",))[0] or x["nom"] or f"App {a}")
                lignes.append({"a": a, "nom": nom, "taille": x["taille"], "disque": x["disque"].rstrip("\\/"),
                               "lance": max(x["lance"], self.lances.get(a, 0))})
            col, inverse = self.tris_outils["stockage"]
            cles = {"nom": lambda x: x["nom"].lower(), "taille": lambda x: x["taille"], "lance": lambda x: x["lance"],
                    "disque": lambda x: (x["disque"], x["nom"].lower())}
            for x in sorted(lignes, key=cles[col], reverse=inverse):
                arbre.insert("", "end", iid=str(x["a"]), text="  " + x["nom"], image=self._icone_tk(x["a"], x["nom"]),
                             values=(taille_txt(x["taille"]), depuis_txt(x["lance"]), x["disque"]))
            garde = [i for i in garde if arbre.exists(i)]
            if garde:
                arbre.selection_set(garde)
            for enfant in self.st_haut.winfo_children():
                enfant.destroy()
            disques = sorted({x["disque"] for x in lignes})[:3]
            tuiles = self._tuiles(self.st_haut, [("jeux", T("Espace pris par tes jeux"))] +
                                  [(f"d{i}", T("Disque {d}", d=d)) for i, d in enumerate(disques)])
            tuiles["jeux"].config(text=taille_txt(sum(x["taille"] for x in lignes)))
            tuiles["jeux_note"].config(text=pluriel(len(lignes), "{n} jeu installé", "{n} jeux installés"))
            for i, d in enumerate(disques):
                try:
                    usage = shutil.disk_usage(d + "\\")
                except OSError:
                    continue
                tuiles[f"d{i}"].config(text=T("{t} libres", t=taille_txt(usage.free)))
                tuiles[f"d{i}_note"].config(text=T("sur {t}", t=taille_txt(usage.total)))
            self._maj_boutons_stockage()

        def _desinstaller_selection(self):
            a = self._selection_stockage()
            if a:
                self._desinstaller(a)

        def _desinstaller(self, a: int):
            """Ouvre la fenêtre de désinstallation de Steam : c'est Steam qui demande confirmation."""
            if a in self.procs:
                msg = T("{nom} est en cours de farm : retire-le du farm avant de le désinstaller.", nom=self._nom(a))
                self.etat.config(text=msg)
                self._etat_stockage(msg, ORANGE)
                return
            self._ouvrir("steam://uninstall/{}", a)
            self._etat_stockage(T("Steam te demande de confirmer. Clique ensuite sur Actualiser."))

        # ── promos de la liste de souhaits ──
        def _ouvrir_souhaits(self):
            w = self._fenetre("fen_souhaits", T("Promos de ma liste de souhaits"), (800, 620))
            if w is None:
                return
            k = self.k
            m18 = int(18 * k)
            haut = ttk.Frame(w, padding=(m18, int(16 * k), m18, int(4 * k)))
            haut.pack(fill="x")
            ttk.Label(haut, text=T("Liste de souhaits"), style="Titre.TLabel").pack(anchor="w")
            self.p_resume = ttk.Label(haut, style="Doux.TLabel")
            self.p_resume.pack(anchor="w", pady=(int(4 * k), 0))
            outils = ttk.Frame(w, padding=(m18, int(10 * k), m18, int(8 * k)))
            outils.pack(fill="x")
            ttk.Button(outils, text=T("Ouvrir ma liste dans Steam"), command=lambda: self._ouvrir_lien(
                "steam://openurl/https://store.steampowered.com/wishlist/")).pack(side="left")
            self.p_actualiser = ttk.Button(outils, text=T("↻  Actualiser"),
                                           command=lambda: self._charger_souhaits(True))
            self.p_actualiser.pack(side="right")
            pied = ttk.Frame(w, padding=(m18, int(10 * k), m18, int(14 * k)))
            pied.pack(side="bottom", fill="x")
            ttk.Checkbutton(pied, text=T("Vérifier chaque jour et me prévenir des nouvelles promos"),
                            variable=self.promos_auto, command=self._sauver).pack(anchor="w")
            self.p_etat = ttk.Label(pied, style="Gris.TLabel", wraplength=int(760 * k), justify="left")
            self.p_etat.pack(anchor="w", pady=(int(4 * k), 0))
            ttk.Frame(w, style="Ligne.TFrame", height=1).pack(side="bottom", fill="x", padx=m18)
            cadre, self.p_arbre = self._table(w, "souhaits", (("nom", T("Jeu"), 380), ("prix", T("Prix"), 115),
                                                               ("promo", T("Promo"), 95),
                                                               ("normal", T("Prix normal"), 115)))
            cadre.pack(fill="both", expand=True, padx=m18)
            self.p_arbre.bind("<Double-1>", lambda e: self._magasin_ligne(self.p_arbre, e))
            w.bind("<Escape>", lambda e: w.destroy())
            self._charger_souhaits(False)

        def _etat_souhaits(self, texte: str, couleur: str = GRIS):
            if self._ouverte("fen_souhaits"):
                self.p_etat.config(text=texte, foreground=couleur)

        def _charger_souhaits(self, forcer: bool, notifier: bool = False):
            """Demande à Steam la liste de souhaits du compte puis les prix du moment, dans un fil à part.
            notifier : vérification quotidienne, qui prévient des promos pas encore vues."""
            if self.souhaits_occupe:
                return
            if self.souhaits is not None and not forcer and time.time() - self.souhaits_date < 1800:
                self._remplir_souhaits()
                return
            uid = compte_utilisateur(self.steam) if self.steam else 0
            if not uid:
                self._etat_souhaits(T("Aucun compte Steam trouvé sur ce PC."), ORANGE)
                return
            self.souhaits_occupe = True
            if self._ouverte("fen_souhaits"):
                self.p_actualiser.state(["disabled"])
                self._etat_souhaits(T("Lecture de ta liste de souhaits et des prix…"))
            noms, langue = {a: i[0] for a, i in self.infos.items()}, LANGUE

            def tache():
                res = souhaits_steam(uid, noms, langue)
                self.q.put(lambda: self._souhaits_recus(res, notifier))
            threading.Thread(target=tache, daemon=True).start()

        def _souhaits_recus(self, res: list | None, notifier: bool):
            self.souhaits_occupe = False
            if res is not None:
                self.souhaits, self.souhaits_date = res, time.time()
                vues = self.cfg.get("promos_vues", {})
                nouvelles = [x for x in res if remise(x["prix"]) > int(vues.get(str(x["a"]), 0) or 0)]
                # promos déjà signalées (ou vues dans la fenêtre) : une promo terminée sort de la liste
                self.cfg["promos_vues"] = {str(x["a"]): remise(x["prix"]) for x in res if remise(x["prix"])}
                if notifier:
                    self.cfg["promos_verifiees"] = time.time()
                    if len(nouvelles) == 1:
                        p = nouvelles[0]["prix"]
                        self._notifier(T("Promo sur ta liste de souhaits : {nom} à −{p} ({prix}).",
                                         nom=nouvelles[0]["nom"], p=pct(remise(p)), prix=argent(p[2], p[0])))
                    elif nouvelles:
                        noms = ", ".join(x["nom"] for x in nouvelles[:3]) + ("…" if len(nouvelles) > 3 else "")
                        self._notifier(T("{n} promos sur ta liste de souhaits : {noms}", n=len(nouvelles), noms=noms))
                self._sauver()
            if not self._ouverte("fen_souhaits"):
                return
            self.p_actualiser.state(["!disabled"])
            if res is None:
                self._etat_souhaits(T("Steam ne répond pas. Vérifie ta connexion, puis clique sur Actualiser."), ROUGE)
            if self.souhaits is not None:
                self._remplir_souhaits(garder_etat=res is None)

        def _remplir_souhaits(self, garder_etat: bool = False):
            if not self._ouverte("fen_souhaits") or self.souhaits is None:
                return
            arbre = self.p_arbre
            arbre.delete(*arbre.get_children())
            col, inverse = self.tris_outils["souhaits"]

            def prix(x, i):          # un jeu sans prix (pas encore sorti…) se range après les autres
                return x["prix"][i] if isinstance(x["prix"], tuple) else (-1 if inverse else math.inf)

            cles = {"nom": lambda x: x["nom"].lower(), "prix": lambda x: prix(x, 2), "normal": lambda x: prix(x, 1),
                    "promo": lambda x: remise(x["prix"])}
            ordre = sorted(self.souhaits, key=lambda x: x["nom"].lower())
            for x in sorted(ordre, key=cles[col], reverse=inverse):
                p, r = x["prix"], remise(x["prix"])
                if isinstance(p, tuple):
                    valeurs = (argent(p[2], p[0]), "−" + pct(r) if r else "", argent(p[1], p[0]) if r else "")
                else:
                    valeurs = ("—", "", "")
                arbre.insert("", "end", iid=str(x["a"]), text="  " + x["nom"], image=self._icone_tk(x["a"], x["nom"]),
                             values=valeurs, tags=("promo",) if r else ())
            n, promos = len(self.souhaits), sum(1 for x in self.souhaits if remise(x["prix"]))
            if not n:
                self.p_resume.config(text=T("Ta liste de souhaits est vide, ou elle n'est pas publique."))
                if not garder_etat:
                    self._etat_souhaits(T("Pour que l'appli la voie : dans Steam, ton profil → Modifier le profil → "
                                          "Confidentialité → « Détails du jeu » : Public."))
                return
            self.p_resume.config(text=pluriel(n, "{n} jeu dans ta liste de souhaits", "{n} jeux dans ta liste de "
                                              "souhaits") + "  ·  " + T("{n} en promo", n=promos))
            if not garder_etat:
                self._etat_souhaits(T("L'appli demande ta liste à Steam avec ton identifiant Steam public, puis les prix "
                                      "à la boutique. Double-clic sur un jeu : page du magasin."))

        def _verifier_promos(self):
            """Si l'option est cochée : une fois par jour, cherche les promos de la liste de souhaits et prévient."""
            if not self.promos_auto.get() or self.souhaits_occupe:
                return
            if time.time() - float(self.cfg.get("promos_verifiees", 0) or 0) < 24 * 3600:
                return
            self._charger_souhaits(True, notifier=True)

        # ── captures d'écran ──
        def _taille_vignette(self) -> tuple[int, int, int]:
            k = self.k
            return int(200 * k), int(112 * k), int(12 * k)

        def _ouvrir_captures(self, a: int | None = None):
            if self.steam:           # relues à chaque ouverture : une capture vient peut-être d'être prise
                self.captures = captures_steam(self.steam, compte_utilisateur(self.steam))
            deja = self._ouverte("fen_captures")
            w = self._fenetre("fen_captures", T("Captures d'écran"), (960, 720))
            if w is None and not deja:
                return
            if w is not None:
                k = self.k
                m18 = int(18 * k)
                haut = ttk.Frame(w, padding=(m18, int(16 * k), m18, int(12 * k)))
                haut.pack(fill="x")
                self.c_choix = tk.StringVar()
                self.c_cb = ttk.Combobox(haut, textvariable=self.c_choix, state="readonly", width=42)
                self.c_cb.pack(side="left")
                self.c_cb.bind("<<ComboboxSelected>>", lambda e: (self._choisir_captures(
                    self.c_ids[self.c_cb.current()]), self.c_toile.focus_set()))
                self.c_dossier = ttk.Button(haut, text=T("Ouvrir le dossier"), command=self._dossier_captures)
                self.c_dossier.pack(side="left", padx=(10, 0))
                self.c_resume = ttk.Label(haut, style="Gris.TLabel")
                self.c_resume.pack(side="right")
                corps = ttk.Frame(w, padding=(m18, 0, m18, m18))
                corps.pack(fill="both", expand=True)
                self.c_toile = tk.Canvas(corps, bg=FOND, highlightthickness=0, bd=0, yscrollincrement=int(40 * k))
                sb = ttk.Scrollbar(corps, orient="vertical", command=self.c_toile.yview)
                self.c_toile.configure(yscrollcommand=sb.set)
                self.c_toile.pack(side="left", fill="both", expand=True)
                sb.pack(side="right", fill="y")
                self.c_toile.bind("<Configure>", lambda e: self._placer_captures())
                w.bind("<MouseWheel>", lambda e: self.c_toile.yview_scroll(-3 if e.delta > 0 else 3, "units"))
                w.bind("<Escape>", lambda e: w.destroy())
            self.c_ids = sorted(self.captures, key=lambda i: self._nom(i).lower())
            self.c_cb["values"] = [f"{self._nom(i)}   ({len(self.captures[i])})" for i in self.c_ids]
            if a not in self.captures:
                a = self.sel if self.sel in self.captures else None
            if a is None and self.captures:
                a = max(self.captures, key=lambda i: len(self.captures[i]))
            self._choisir_captures(a)

        def _choisir_captures(self, a: int | None):
            self.c_gen += 1
            toile = self.c_toile
            toile.delete("all")
            toile.yview_moveto(0)
            self.c_jeu, self.c_tk, self.c_cases = a, {}, []
            self.c_dossier.state(["disabled"] if a is None else ["!disabled"])
            if a is None:
                self.c_choix.set("")
                self.c_resume.config(text="")
                self.c_vide = toile.create_text(0, 0, fill=GRIS, font=("Segoe UI", 11), justify="center",
                                                text=T("Aucune capture d'écran Steam sur ce PC.\nDans un jeu, appuie "
                                                       "sur F12 pour en prendre une."))
                self._placer_captures()
                return
            self.c_cb.current(self.c_ids.index(a))
            toutes = self.captures[a]
            fichiers = toutes[:MAX_CAPTURES]
            resume = pluriel(len(toutes), "{n} capture", "{n} captures")
            if len(toutes) > len(fichiers):
                resume += "  ·  " + T("les {n} plus récentes affichées", n=len(fichiers))
            self.c_resume.config(text=resume)
            for p in fichiers:
                cadre = toile.create_rectangle(0, 0, 0, 0, fill=PANNEAU, width=0)
                image = toile.create_image(0, 0, anchor="nw")
                ts = date_capture(p)
                texte = toile.create_text(0, 0, anchor="nw", fill=GRIS, font=("Segoe UI", 9),
                                          text=date_longue(ts) if ts else "")
                for objet in (cadre, image):
                    toile.tag_bind(objet, "<Button-1>", lambda e, p=p: self._ouvrir_lien(str(p)))
                self.c_cases.append((p, cadre, image, texte))
            self._placer_captures()
            self._charger_vignettes(self.c_gen, 0)

        def _placer_captures(self):
            """Range les vignettes en colonnes selon la largeur de la fenêtre."""
            if not self._ouverte("fen_captures"):
                return
            toile, k = self.c_toile, self.k
            large, haut = toile.winfo_width(), toile.winfo_height()
            if large < 50:
                return
            if self.c_jeu is None:
                toile.coords(self.c_vide, large / 2, haut / 2)
                toile.configure(scrollregion=(0, 0, large, haut))
                return
            tw, th, g = self._taille_vignette()
            colonnes = max(1, (large + g) // (tw + g))
            # les vignettes sont réparties sur toute la largeur (l'écart s'agrandit un peu plutôt qu'un vide à droite)
            ecart = (large - colonnes * tw) / (colonnes - 1) if colonnes > 1 else g
            pas = th + int(22 * k) + g
            for i, (_p, cadre, image, texte) in enumerate(self.c_cases):
                x, y = int((i % colonnes) * (tw + ecart)), (i // colonnes) * pas
                toile.coords(cadre, x, y, x + tw, y + th)
                toile.coords(image, x, y)
                toile.coords(texte, x, y + th + int(3 * k))
            lignes = (len(self.c_cases) + colonnes - 1) // colonnes
            toile.configure(scrollregion=(0, 0, large, max(lignes * pas, haut)))

        def _charger_vignettes(self, gen: int, debut: int):
            """Charge les vignettes par petits lots, pour que la fenêtre reste fluide."""
            if gen != self.c_gen or not self._ouverte("fen_captures"):
                return
            tw, th, _g = self._taille_vignette()
            for i in range(debut, min(debut + 8, len(self.c_cases))):
                p, _cadre, image, _texte = self.c_cases[i]
                try:
                    mini = p.parent / "thumbnails" / p.name        # vignette déjà faite par Steam (200 x 112)
                    src = Image.open(mini if self.k <= 1 and mini.exists() else p)
                    src.draft("RGB", (tw, th))
                    self.c_tk[i] = ImageTk.PhotoImage(couvrir(src.convert("RGB"), tw, th))
                    self.c_toile.itemconfig(image, image=self.c_tk[i])
                except (OSError, ValueError):
                    pass
            if debut + 8 < len(self.c_cases):
                self.r.after(15, lambda: self._charger_vignettes(gen, debut + 8))

        def _dossier_captures(self):
            if self.c_jeu in self.captures:
                self._ouvrir_lien(str(self.captures[self.c_jeu][0].parent))

        # ── « À quoi je joue ? » ──
        def _ouvrir_hasard(self):
            w = self._fenetre("fen_hasard", T("À quoi je joue ?"), (600, 500))
            if w is None:
                return
            k = self.k
            m18 = int(18 * k)
            haut = ttk.Frame(w, padding=(m18, int(16 * k), m18, int(12 * k)))
            haut.pack(fill="x")
            ttk.Label(haut, text=T("Tirer au sort parmi"), style="Doux.TLabel").pack(side="left", padx=(0, 8))
            self.h_choix = tk.StringVar(value=T(HASARD[0][1]))
            cb = ttk.Combobox(haut, textvariable=self.h_choix, values=[T(l) for _, l in HASARD], state="readonly",
                              width=max(len(T(l)) for _, l in HASARD) + 1)
            cb.pack(side="left")
            cb.bind("<<ComboboxSelected>>", lambda e: (self._tirer(), self.h_autre.focus_set()))
            pied = ttk.Frame(w, padding=(m18, int(10 * k), m18, int(16 * k)))
            pied.pack(side="bottom", fill="x")
            self.h_lancer = ttk.Button(pied, style="Accent.TButton", width=14, command=self._lancer_hasard)
            self.h_lancer.pack(side="right")
            self.h_autre = ttk.Button(pied, text=T("↻  Un autre"), command=self._tirer)
            self.h_autre.pack(side="right", padx=(0, 8))
            self.h_magasin = ttk.Button(pied, text=T("Page du magasin"), command=lambda: self._ouvrir(
                "https://store.steampowered.com/app/{}", self.h_jeu))
            self.h_magasin.pack(side="left")
            self.h_image = tk.Label(w, bg=PANNEAU, bd=0)
            self.h_image.pack(padx=m18)
            self.h_nom = ttk.Label(w, style="Titre.TLabel", wraplength=int(560 * k), justify="center")
            self.h_nom.pack(pady=(int(12 * k), int(2 * k)))
            self.h_infos = ttk.Label(w, style="Doux.TLabel", wraplength=int(560 * k), justify="center")
            self.h_infos.pack()
            w.bind("<Escape>", lambda e: w.destroy())
            self._tirer()

        def _tirer(self):
            """Tire un jeu au sort dans la catégorie choisie (jamais le même deux fois de suite)."""
            if not self._ouverte("fen_hasard"):
                return
            k = self.k
            cle = next((c for c, l in HASARD if T(l) == self.h_choix.get()), "tous")
            lot = [a for a, j in self.jeux.items()
                   if cle == "tous" or (cle == "jamais" and not j["min"]) or (cle == "peu" and 0 < j["min"] < 120)
                   or (cle == "installes" and a in self.installes)]
            if len(lot) > 1 and self.h_jeu in lot:
                lot.remove(self.h_jeu)
            large, haut = int(560 * k), int(262 * k)
            base = None
            a = self.h_jeu = random.choice(lot) if lot else None
            if a is not None:
                ch = self._chemins(a)
                for c in ("entete", "hero", "capsule"):
                    if ch.get(c):
                        try:
                            src = Image.open(ch[c])
                            src.draft("RGB", (large, haut))
                            base = couvrir(src.convert("RGB"), large, haut)
                            break
                        except OSError:
                            base = None
            self._h_tk = ImageTk.PhotoImage(base or Image.new("RGB", (large, haut), PANNEAU))
            self.h_image.config(image=self._h_tk)
            for b in (self.h_lancer, self.h_magasin):
                b.state(["disabled"] if a is None else ["!disabled"])
            if a is None:
                self.h_nom.config(text=T("Aucun jeu dans cette catégorie."))
                self.h_infos.config(text="")
                self.h_lancer.config(text=T("▶  Lancer"))
                return
            sec = self._temps_steam(a)
            infos = [T("Temps de jeu : {t}", t=heures(sec / 60)) if sec >= 60 else T("Jamais lancé"),
                     T("Installé") if a in self.installes else T("Pas installé")]
            n = self._succes_compte(a)
            if n:
                infos.append(T("Succès  {f}/{n}", f=n[0], n=n[1]))
            if sec >= 60 and self.lances.get(a):
                infos.append(T("Dernier lancement : {d}", d=depuis_txt(self.lances[a])))
            self.h_nom.config(text=self.jeux[a]["nom"])
            self.h_infos.config(text="   ·   ".join(infos))
            self.h_lancer.config(text=T("▶  Lancer") if a in self.installes else T("Installer"))

        def _lancer_hasard(self):
            a = self.h_jeu
            if a is None:
                return
            if a not in self.installes:
                self._installer(a)
                return
            delai = 0
            if a in self.coches:     # jeu dans le farm : Steam refuserait de le lancer (« déjà en cours d'exécution »)
                delai = 1500 if a in self.procs else 0
                self._basculer_jeux([a], demander=False)
                self.etat.config(text=T("{nom} est retiré du farm pour que tu puisses y jouer.", nom=self.jeux[a]["nom"]))
            self.r.after(delai, lambda: self._ouvrir("steam://rungameid/{}", a))

        # ── collections Steam ──
        def _nom_collection(self, c) -> str:
            nom = T("Favoris") if c[0] == "favorite" else c[1]
            return "▸ " + (nom if len(nom) <= 24 else nom[:23] + "…")

        def _valeurs_filtre(self) -> list[str]:
            return [T(l) for _, l in FILTRES] + [self._nom_collection(c) for c in self.collections]

        def _appliquer_collection(self, i: int):
            c = self.collections[i]
            cible = [a for a in self.jeux if a in c[2]]
            self._basculer_jeux([a for a in self.coches if a not in cible] + [a for a in cible if a not in self.coches])
            self.etat.config(text=pluriel(len(cible), "Collection « {nom} » : {n} jeu coché.",
                                          "Collection « {nom} » : {n} jeux cochés.", nom=self._nom_collection(c)[2:]))

        # ── nouvelle version ──
        def _verifier_maj(self, forcer: bool = False):
            """Demande à GitHub la dernière version (une fois par jour au plus), dans un fil à part."""
            self._montrer_maj()                  # version trouvée lors d'une vérification précédente
            if not self.maj_auto.get():
                return
            if not forcer and time.time() - float(self.cfg.get("maj_verifiee", 0)) < 24 * 3600:
                return

            def tache():
                v = derniere_version()
                self.q.put(lambda: self._maj_recue(v))
            threading.Thread(target=tache, daemon=True).start()

        def _maj_recue(self, v: str | None):
            if v is None:
                return
            self.cfg["maj_verifiee"] = time.time()
            self.cfg["maj_version"] = v
            self._sauver()
            self._montrer_maj()

        def _montrer_maj(self):
            v = str(self.cfg.get("maj_version", ""))
            if self.maj_auto.get() and v and version_num(v) > version_num(VERSION):
                self.l_maj.config(text=T("Nouvelle version {v}  ↗", v=v))
                self.l_maj.pack_configure(padx=(0, int(16 * self.k)))
                if self.cfg.get("maj_notifiee") != v:      # une seule notification par version
                    self.cfg["maj_notifiee"] = v
                    self._sauver()
                    self._notifier(T("La version {v} est disponible sur GitHub.", v=v))
            else:
                self.l_maj.config(text="")
                self.l_maj.pack_configure(padx=0)

        def _regler_maj(self):
            self._sauver()
            self._verifier_maj()

        def _notifier(self, texte: str):
            journal(texte)
            if self.tray:
                try:
                    self.tray.notify(texte, NOM)
                except Exception:
                    pass

        def _pomper(self):
            try:
                while True:
                    self.q.get_nowait()()
            except queue.Empty:
                pass
            self.r.after(150, self._pomper)

        def _montrer(self):
            self.r.deiconify()
            self.r.state("normal")
            self.r.lift()
            self.r.focus_force()

        def _noter_fenetre(self):
            """Taille, position et état agrandi, seulement quand la fenêtre est affichée (pas cachée ni réduite)."""
            try:
                etat = self.r.state()
                if etat == "normal":
                    self.cfg["fenetre"] = self.r.geometry()
                if etat in ("normal", "zoomed"):
                    self.cfg["agrandie"] = etat == "zoomed"
            except tk.TclError:
                pass

        def _fermer(self):
            self._noter_fenetre()                # avant de la cacher dans la zone de notification
            if self.actif and self.tray:
                self.r.withdraw()
                if not self.notifie:
                    self.notifie = True
                    self._notifier(T("Le farm continue. Clic sur l'icône pour rouvrir, clic droit pour quitter."))
                return
            self._quitter()

        def _quitter(self):
            self._tout_arreter("")
            self._sauver()
            if self.tray:
                try:
                    self.tray.stop()
                except Exception:
                    pass
            self.r.destroy()

        def _regler_demarrage(self):
            try:
                regler_demarrage_windows(self.au_demarrage.get())
                self.etat.config(text=T("Au démarrage de Windows, l'appli s'ouvre cachée et farme les jeux cochés.")
                                 if self.au_demarrage.get() else T("Ne se lance plus avec Windows."))
            except OSError as e:
                self.au_demarrage.set(demarrage_windows())
                self.etat.config(text=T("Impossible de régler le démarrage : {e}", e=e))

        # ── statut Steam (mode invisible) ──
        # Un lien steam:// ouvre Steam s'il est fermé : on ne touche au statut que si Steam tourne et est connecté.
        def _passer_invisible(self):
            if not steam_pret():
                return                           # refait à la reprise, quand Steam sera revenu
            if self.statut_avant is None:
                try:
                    steam = self.steam or dossier_steam()
                except OSError:
                    steam = None
                avant = statut_steam(steam) if steam else None
                if avant in (INVISIBLE, 0):
                    return                       # déjà invisible ou hors ligne : rien à faire, rien à remettre
                self.statut_avant = avant if avant is not None else 1
            changer_statut(INVISIBLE)
            self._sauver()

        def _restaurer_statut(self):
            if self.statut_avant is None or not steam_pret():
                return                           # Steam fermé : statut_avant reste enregistré, remis plus tard
            changer_statut(self.statut_avant)
            self.statut_avant = None
            self._sauver()

        def _appliquer_statut(self):
            """Statut voulu pour l'état actuel : invisible pendant le farm si l'option est cochée, sinon celui d'avant."""
            if self.actif and self.invisible.get():
                self._passer_invisible()
            else:
                self._restaurer_statut()

        def _regler_invisible(self):
            self._sauver()
            if self.actif:
                self._appliquer_statut()

        # ── chargement de la bibliothèque (dans un fil à part) ──
        def _recharger(self):
            self.sous_titre.config(text=T("lecture de ta bibliothèque…"))
            threading.Thread(target=self._charger, daemon=True).start()

        def _charger(self):
            try:
                steam = dossier_steam()
                infos = infos_applis(steam)
                apps = apps_locales(steam)
                temps = temps_de_jeu(steam, apps)
                installations = jeux_installes(steam)
                candidats, installes = set(temps), set(installations)
                cache = steam / "appcache" / "librarycache"
                if cache.exists():
                    candidats |= {int(p.name) for p in cache.iterdir() if p.name.isdigit()}
                candidats |= installes
                jeux = {a for a in candidats if a in infos and infos[a][1] == "game"}
                note, verif = "", None
                if preparer_dll(steam):
                    verif = verifier_possession(sorted(jeux))
                    if verif is None:
                        note = ("Steam fermé ? liste du dernier lancement" if self.cfg.get("possedes")
                                else "Steam fermé ? liste non vérifiée")
                else:
                    note = "steam_api64.dll introuvable : le farm ne pourra pas démarrer"
                filtre = verif["possedes"] if verif else set(self.cfg.get("possedes", [])) or None
                if filtre is not None:
                    jeux &= filtre
                jeux |= set(self.cfg.get("manuels", []))
                s = int(24 * self.k)
                chemins = {a: chemins_images(steam, a) for a in jeux}
                icones = {a: icone_jeu(chemins[a].get("icone"), infos.get(a, ("?",))[0], s) for a in jeux}
                uid = compte_utilisateur(steam)
                succes = succes_locaux(steam, uid)
                outils = {"installations": installations, "lances": derniers_lancements(apps),
                          "collections": collections_steam(steam, uid), "captures": captures_steam(steam, uid)}
                self.chargement = (steam, jeux, infos, temps, installes, verif, chemins, icones, succes, outils,
                                   note, None)
            except Exception as e:  # noqa: BLE001 — affiché dans la fenêtre
                journal(T("chargement : {e}", e=repr(e)))
                self.chargement = (None, set(), {}, {}, set(), None, {}, {}, {}, {}, "", repr(e))

        def _appliquer_chargement(self):
            (steam, jeux, infos, temps, installes, verif, chemins, icones, succes, outils, note,
             err) = self.chargement
            self.chargement = None
            if err:
                self.sous_titre.config(text=T("impossible de lire Steam : {e}", e=err))
                return
            self.steam, self.infos, self.installes = steam, infos, installes
            self.succes_cache = succes
            self.installations, self.lances, self.captures = outils["installations"], outils["lances"], outils["captures"]
            self.gratuits = {a for a, i in infos.items() if i[2]}
            self.chemins.update(chemins)
            self.icones.update(icones)
            self.images.clear()
            self.jeux = {a: {"nom": infos.get(a, (f"App {a}",))[0], "min": temps.get(a, 0)} for a in jeux}
            self.base_farme = {a: self._total(a) for a in self.jeux}
            # collections Steam qui contiennent au moins un jeu de la liste : ajoutées aux filtres
            self.collections = [c for c in outils["collections"] if any(a in self.jeux for a in c[2])]
            valeurs = self._valeurs_filtre()
            self.cb_filtre.config(values=valeurs, width=max(15, max(map(len, valeurs)) + 1))
            if self.filtre.get() not in valeurs:
                self.filtre.set(valeurs[0])
            self._icones_tk.clear()
            if verif is not None:
                self.cfg["possedes"] = sorted(verif["possedes"])
                self.achats.update(verif["achats"])
                self.famille = verif["famille"]
                self._sauver()
            self._maj_sous_titre(note)
            self._remplir()
            self._planifier_banniere()           # installé ou non, compte des succès : le bouton peut changer
            if self.sel not in self.jeux:
                premier = next((a for a in self.arbre.get_children() if int(a) in self.coches), None) \
                          or next(iter(self.arbre.get_children()), None)
                if premier:
                    self.arbre.selection_set(premier)
                    self.arbre.see(premier)

        def _maj_sous_titre(self, note: str | None = None):
            """note : message du dernier chargement ("" l'efface) ; None garde le message actuel."""
            n = sum(1 for a in self.coches if a in self.jeux)
            txt = pluriel(len(self.jeux), "{n} jeu", "{n} jeux") + "  ·  " + pluriel(n, "{n} coché", "{n} cochés")
            if note is not None:
                self._note = note
            if self._note:
                txt += "  ·  " + T(self._note)
            self.sous_titre.config(text=txt)

        # ── calculs ──
        def _nom(self, a: int | None) -> str:
            if a is None:
                return ""
            return self.jeux.get(a, {}).get("nom") or self.infos.get(a, (f"App {a}",))[0]

        def _total(self, a: int) -> float:
            """Secondes farmées avec l'appli (depuis toujours), session en cours comprise."""
            p = self.procs.get(a)
            return self.farme.get(a, 0) + (time.time() - p[1] if p else 0)

        def _temps_steam(self, a: int) -> float:
            """Temps de jeu Steam estimé en direct (secondes) : fichier de Steam + farm depuis sa lecture."""
            j = self.jeux.get(a)
            if not j:
                return 0.0
            return j["min"] * 60 + max(0.0, self._total(a) - self.base_farme.get(a, 0))

        def _objectif_atteint(self, a: int) -> bool:
            h = self.objectifs.get(a, 0)
            return h > 0 and self._temps_steam(a) >= h * 3600

        def _fin_estimee(self, a: int) -> tuple[float, float | None] | None:
            """(secondes restantes, heure de fin estimée ou None si on ne peut pas l'estimer)."""
            obj = self.objectifs.get(a, 0)
            reste = obj * 3600 - self._temps_steam(a)
            if obj <= 0 or reste <= 0:
                return None
            vitesse = 0.0
            if self.actif and not self.pause and a in self.coches and a not in self.echecs and not self._a_confirmer(a):
                n = sum(1 for b in self.coches if b in self.jeux and b not in self.echecs
                        and not self._objectif_atteint(b) and not self._a_confirmer(b))
                if n <= MAX_SIMULTANES:
                    vitesse = 1.0
                elif nombre(self.rotation.get()) > 0:
                    vitesse = MAX_SIMULTANES / n     # le jeu ne tourne qu'une partie du temps
            return reste, (time.time() + reste / vitesse if vitesse else None)

        def _remboursable(self, a: int) -> bool:
            t = self.achats.get(a)
            return (bool(t) and a not in self.gratuits and a not in self.famille
                    and time.time() - t < REMB_JOURS * 86400 and self._temps_steam(a) < REMB_SECONDES)

        def _a_confirmer(self, a: int) -> bool:
            return self._remboursable(a) and a not in self.remb_ok

        def _texte_remb(self, a: int) -> str:
            jours = int((time.time() - self.achats[a]) // 86400)
            reste = heures(max(0, REMB_SECONDES - self._temps_steam(a)) / 60, None)
            return pluriel(jours, "acheté il y a {n} jour, encore {r} avant la limite de 2 h",
                           "acheté il y a {n} jours, encore {r} avant la limite de 2 h", r=reste)

        def _texte_echec(self, a: int) -> str:
            v = self.echecs.get(a)
            return T("refusé par Steam") if v == "refus" else T("arrêté (code {c})", c=v)

        def _plage(self) -> tuple[int, int]:
            return heure_plage(self.plage_debut.get()), heure_plage(self.plage_fin.get())

        def _texte_pause(self) -> str:
            if self.pause == "horaire":
                return T("⏸  En dehors de ta plage horaire : le farm reprend à {h}", h=T("{h} h", h=self._plage()[0]))
            if self.pause == "steam":
                return T("⏸  Steam est fermé : le farm reprendra tout seul dès que Steam sera de retour")
            if self.pause == "jeu":
                return T("⏸  En pause pendant que tu joues à {nom}", nom=self.jeu_reel[1] if self.jeu_reel else "?")
            return T("⏸  En pause : tu utilises le PC. Le farm reprend après {n} min sans activité",
                     n=nombre_txt(nombre(self.inactif_min.get())))

        # ── images ──
        def _chemins(self, a: int) -> dict:
            if a not in self.chemins and self.steam:
                self.chemins[a] = chemins_images(self.steam, a)
            return self.chemins.get(a, {})

        def _image_ligne(self, a: int):
            etat = "run" if a in self.procs else "on" if a in self.coches else "off"
            cle = (a, etat)
            if cle not in self.images:
                k = self.k
                c, g, s = int(16 * k), int(12 * k), int(24 * k)
                im = Image.new("RGBA", (c + g + s + int(10 * k), max(c, s)), (0, 0, 0, 0))
                im.alpha_composite(case_a_cocher(etat, c), (0, (im.height - c) // 2))
                if a not in self.icones:
                    self.icones[a] = icone_jeu(self._chemins(a).get("icone"), self.jeux[a]["nom"], s)
                ic = self.icones[a]
                if etat == "off":   # icône un peu éteinte quand le jeu n'est pas coché
                    ic = ic.copy()
                    ic.putalpha(ic.getchannel("A").point(lambda v: v * 150 // 255))
                im.alpha_composite(ic, (c + g, (im.height - s) // 2))
                self.images[cle] = ImageTk.PhotoImage(im)
            return self.images[cle]

        def _planifier_banniere(self):
            if self._rendu_prevu:
                self.r.after_cancel(self._rendu_prevu)
            self._rendu_prevu = self.r.after(60, self._rendre_banniere)

        def _masque_degrade(self, w: int, h: int):
            if self._masque and self._masque[0] == (w, h):
                return self._masque[1]
            horiz = ImageOps.invert(Image.linear_gradient("L").rotate(90)).resize((w, h))
            horiz = horiz.point(lambda v: 60 + v * 175 // 255)
            vert = Image.linear_gradient("L").resize((w, h)).point(lambda v: max(0, v - 110) * 255 // 145)
            m = ImageChops.lighter(horiz, vert)
            self._masque = ((w, h), m)
            return m

        def _rendre_banniere(self):
            self._rendu_prevu = None
            c, k = self.ban, self.k
            if not c.winfo_exists():
                return
            w, h = c.winfo_width(), c.winfo_height()
            if w < 50:
                return
            a = self.sel
            ch = self._chemins(a) if a else {}
            base = None
            for cle, flou in (("hero", 0), ("entete", 14), ("capsule", 20)):
                if ch.get(cle):
                    try:
                        src = Image.open(ch[cle])
                        src.draft("RGB", (w, h))
                        base = couvrir(src.convert("RGB"), w, h)
                        if flou:
                            base = base.filter(ImageFilter.GaussianBlur(flou * k))
                        break
                    except OSError:
                        base = None
            if base is None:
                base = Image.new("RGB", (w, h), PANNEAU)
            base = Image.composite(Image.new("RGB", (w, h), FOND), base, self._masque_degrade(w, h))
            marge = int(28 * k)
            bas_logo = int(h * .46)
            a_logo = False
            if ch.get("logo"):
                try:
                    lg = Image.open(ch["logo"]).convert("RGBA")
                    lg.thumbnail((int(w * .32), bas_logo - int(16 * k)), Image.LANCZOS)
                    base.paste(lg, (marge, bas_logo - lg.height), lg)
                    a_logo = True
                except OSError:
                    pass
            self._ban_tk = ImageTk.PhotoImage(base)
            c.itemconfig(self.b_img, image=self._ban_tk)
            nom = self.jeux[a]["nom"] if a in self.jeux else (NOM if not a else f"App {a}")
            c.itemconfig(self.b_nom, text="" if a_logo else nom, width=int(w * .6))
            c.coords(self.b_nom, marge, bas_logo)
            c.coords(self.b_stats, marge, bas_logo + int(12 * k))
            c.coords(self.b_etat, marge, bas_logo + int(34 * k))
            yb = h - int(20 * k)
            c.coords(self.b_w_obj, marge, yb)
            c.coords(self.b_w_farm, w - marge, yb)
            # jeu possédé mais pas installé : le bouton Steam propose de l'installer
            self.b_steam.config(text=T("Installer") if self._a_installer(a) else T("Ouvrir dans Steam"))
            self.r.update_idletasks()
            x_steam = w - marge - self.b_farm.winfo_reqwidth() - int(10 * k)
            c.coords(self.b_w_steam, x_steam, yb)
            x_succes = x_steam - self.b_steam.winfo_reqwidth() - int(10 * k)
            c.coords(self.b_w_succes, x_succes, yb)
            self._geo_jauge = (marge, bas_logo + int(64 * k))
            vis = "normal" if a in self.jeux else "hidden"
            for item in (self.b_w_farm, self.b_w_steam, self.b_w_obj):
                c.itemconfig(item, state=vis)
            # fenêtre étroite : le bouton Succès rétrécit (sans le compte, puis trophée seul), et s'il ne tient
            # toujours pas, il laisse la place à l'objectif (les succès restent dans le clic droit)
            libre = x_succes - marge - self.b_obj.winfo_reqwidth() - int(12 * k)
            place = False
            for variante in ("compte", "texte", "icone"):
                self._maj_bouton_succes(variante)
                self.r.update_idletasks()
                if self.b_succes.winfo_reqwidth() < libre:
                    place = True
                    break
            c.itemconfig(self.b_w_succes, state=vis if place else "hidden")
            self._charger_objectif()
            self._maj_banniere_textes()

        def _maj_banniere_textes(self):
            a, c, k = self.sel, self.ban, self.k
            if a not in self.jeux:
                c.itemconfig(self.b_stats, text=T("Coche des jeux dans la liste, puis Démarrer le farm."))
                c.itemconfig(self.b_etat, text="")
                for item in (self.b_piste, self.b_jauge, self.b_pct):
                    c.itemconfig(item, state="hidden")
                return
            farme = hms(self._total(a)) if a in self.procs else heures(self._total(a) / 60)
            c.itemconfig(self.b_stats, text=T("Temps Steam  {t}      Farmé ici  {f}      AppID  {a}",
                                              t=heures(self._temps_steam(a) / 60), f=farme, a=a))
            remb = self._remboursable(a)
            if a in self.procs:
                txt, coul = T("▶  En cours depuis {d}", d=hms(time.time() - self.procs[a][1])), VERT
                if remb:
                    txt, coul = txt + T("  ·  ⚠ remboursable : {r}", r=self._texte_remb(a)), ORANGE
            elif a in self.coches and self.pause:
                txt, coul = self._texte_pause(), ORANGE
            elif a in self.echecs:
                txt, coul = T("✗  {e} (voir journal.txt)", e=self._texte_echec(a)), ROUGE
            elif a in self.essais:
                txt, coul = T("↻  Steam a refusé, nouvel essai dans quelques secondes"), ORANGE
            elif a in self.atteints and a not in self.coches:
                txt, coul = T("✓  Objectif atteint ({o})", o=objectif_txt(self.objectifs.get(a, 0))), VERT
            elif remb:
                txt, coul = T("⚠  Encore remboursable : {r}", r=self._texte_remb(a)), ORANGE
            elif a in self.coches:
                txt = T("✓  Dans le farm — en attente d'une place (32 jeux max)") if self.actif \
                    else T("✓  Dans le farm — démarre avec le bouton en bas")
                coul = ACCENT
            else:
                txt, coul = T("Pas dans le farm"), GRIS
            c.itemconfig(self.b_etat, text=txt, fill=coul)
            self.b_farm.config(text=T("Retirer du farm") if a in self.coches else T("＋  Ajouter au farm"),
                               style="Verre.TButton" if a in self.coches else "Accent.TButton")
            # jauge de l'objectif
            obj = self.objectifs.get(a, 0)
            geo = getattr(self, "_geo_jauge", None)
            if obj > 0 and geo:
                x, y = geo
                L, ep = int(170 * k), max(4, int(6 * k))
                p = min(1.0, self._temps_steam(a) / (obj * 3600))
                c.coords(self.b_piste, x, y - ep / 2, x + L, y + ep / 2)
                c.coords(self.b_jauge, x, y - ep / 2, x + max(1, L * p), y + ep / 2)
                c.itemconfig(self.b_jauge, fill=VERT if p >= 1 else ACCENT)
                c.coords(self.b_pct, x + L + int(10 * k), y)
                txt = pct(int(p * 100)) + "  ·  " + T("{t} sur {o}", t=heures(self._temps_steam(a) / 60),
                                                      o=objectif_txt(obj))
                fin = self._fin_estimee(a)
                if fin:
                    reste, quand = fin
                    txt += "  ·  " + (T("fin prévue {m}", m=moment(quand)) if quand
                                      else T("reste {r}", r=heures(reste / 60, None)))
                c.itemconfig(self.b_pct, text=txt)
                for item in (self.b_piste, self.b_jauge, self.b_pct):
                    c.itemconfig(item, state="normal")
            else:
                for item in (self.b_piste, self.b_jauge, self.b_pct):
                    c.itemconfig(item, state="hidden")

        # ── objectifs ──
        def _charger_objectif(self):
            self._maj_objectif = True
            self.obj_var.set(court(self.objectifs.get(self.sel, 0)) if self.sel else "0")
            self._maj_objectif = False

        def _regler_objectif(self):
            if self._maj_objectif or self.sel not in self.jeux:
                return
            texte, actuel = self.obj_var.get().strip(), self.objectifs.get(self.sel, 0)
            if texte == court(actuel) or nombre(texte) == actuel:
                return       # le champ montre 2 décimales : 1.83 pour 1 h 50 (1.8333) ne doit pas changer l'objectif
            self._definir_objectif([self.sel], nombre(texte))

        def _definir_objectif(self, ids, h: float):
            for a in ids:
                if h > 0:
                    self.objectifs[a] = h
                else:
                    self.objectifs.pop(a, None)
                if not self._objectif_atteint(a):
                    self.atteints.discard(a)
                self._maj_ligne(a)
            self._sauver()
            self._maj_banniere_textes()

        # ── liste ──
        def _valeurs(self, a: int):
            if a in self.procs:
                f = "▶  " + hms(self._total(a))
            elif a in self.coches and self.pause:
                f = T("⏸  pause")
            elif a in self.echecs:
                f = T("✗  refusé")
            elif a in self.essais:
                f = T("↻  nouvel essai")
            elif a in self.coches and self._a_confirmer(a):
                f = T("⚠  à confirmer")
            elif a in self.coches and self.actif:
                f = T("en attente")
            else:
                f = heures(self._total(a) / 60)
            obj = self.objectifs.get(a, 0)
            if obj > 0:
                p = self._temps_steam(a) / (obj * 3600)
                o = f"✓  {objectif_txt(obj)}" if p >= 1 else T("{p}  de {o}", p=pct(int(p * 100)), o=objectif_txt(obj))
            else:
                o = ""
            n = self._succes_compte(a)
            return (heures(self._temps_steam(a) / 60), o, f, f"{n[0]}/{n[1]}" if n else "")

        def _succes_compte(self, a: int | None) -> tuple[int, int] | None:
            """(débloqués, total) : cache de Steam, sinon le dernier compte lu dans la fenêtre Succès."""
            n = self.succes_cache.get(a) or self.cfg.get("succes_vus", {}).get(str(a))
            return (int(n[0]), int(n[1])) if n and n[1] else None

        def _presque_fini(self, a: int) -> bool:
            n = self._succes_compte(a)
            return bool(n) and PRESQUE <= n[0] / n[1] < 1

        def _tags(self, a: int):
            if a in self.procs:
                return ("encours",)
            if a in self.echecs:
                return ("echec",)
            if self._remboursable(a):
                return ("remb",)
            if a in self.coches and (self.actif or self.pause):
                return ("attente",)
            return () if a in self.coches else ("off",)

        def _filtre_cle(self) -> str:
            """Filtre choisi : une clé de FILTRES, ou « col:<n> » pour la n-ième collection Steam."""
            v = self.filtre.get()
            cle = next((c for c, l in FILTRES if T(l) == v), None)
            if cle is None:
                cle = next((f"col:{i}" for i, c in enumerate(self.collections) if self._nom_collection(c) == v), "tous")
            return cle

        def _visible(self, a: int, q: str) -> bool:
            if q and q not in self.jeux[a]["nom"].lower() and q != str(a):
                return False
            f = self._filtre_cle()
            return (f == "tous" or (f == "coches" and a in self.coches) or (f == "encours" and a in self.procs)
                    or (f == "objectif" and a in self.objectifs) or (f == "remb" and self._remboursable(a))
                    or (f == "jamais" and not self.jeux[a]["min"]) or (f == "installes" and a in self.installes)
                    or (f == "presque" and self._presque_fini(a))
                    or (f.startswith("col:") and a in self.collections[int(f[4:])][2]))

        def _remplir(self):
            q = self.recherche.get().strip().lower()
            sel = self.arbre.selection()
            self.arbre.delete(*self.arbre.get_children())
            cle, inverse = self.tri
            cles = {"nom": lambda a: self.jeux[a]["nom"].lower(), "temps": self._temps_steam,
                    "objectif": lambda a: (self._temps_steam(a) / (self.objectifs[a] * 3600)
                                           if a in self.objectifs else -1),
                    "farme": self._total,
                    "succes": lambda a: (n[0] / n[1], n[1]) if (n := self._succes_compte(a)) else (-1, 0)}
            ordre = sorted(self.jeux, key=cles[cle], reverse=inverse)
            if cle == "succes":                  # les jeux sans succès restent en bas, dans les deux sens
                ordre.sort(key=lambda a: self._succes_compte(a) is None)
            for a in ordre:
                if self._visible(a, q):
                    self.arbre.insert("", "end", iid=str(a), text="  " + self.jeux[a]["nom"],
                                      image=self._image_ligne(a), values=self._valeurs(a), tags=self._tags(a))
            garde = [i for i in sel if self.arbre.exists(i)]
            if garde:
                self.arbre.selection_set(garde)
            for col, titre in (("#0", "Jeu"), ("temps", "Temps Steam"), ("objectif", "Objectif"),
                               ("farme", "Farmé ici"), ("succes", "Succès")):
                fleche = (" ▼" if inverse else " ▲") if (cle == "nom" and col == "#0") or cle == col else ""
                self.arbre.heading(col, text=T(titre) + fleche)

        def _maj_ligne(self, a: int):
            if a in self.jeux and self.arbre.exists(str(a)):
                self.arbre.item(str(a), image=self._image_ligne(a), values=self._valeurs(a), tags=self._tags(a))

        def _maj_lignes_cochees(self):
            for a in self.coches:
                self._maj_ligne(a)

        def _trier(self, col: str):
            self.tri = (col, not self.tri[1] if self.tri[0] == col else col in ("temps", "objectif", "farme", "succes"))
            self._remplir()

        def _selection(self, _=None):
            s = self.arbre.selection()
            a = int(s[0]) if s else None
            if a != self.sel and a is not None:
                self._regler_objectif()          # garde une saisie en cours pour l'ancien jeu
                self.sel = a
                self._charger_objectif()
                self._planifier_banniere()

        def _clic(self, e):
            ligne = self.arbre.identify_row(e.y)
            if ligne and self.arbre.identify_column(e.x) == "#0" and "image" in self.arbre.identify_element(e.x, e.y):
                self._basculer_jeux([int(ligne)])
                self.arbre.selection_set(ligne)
                self.arbre.focus(ligne)
                return "break"

        def _double(self, e):
            ligne = self.arbre.identify_row(e.y)
            if ligne and "image" not in self.arbre.identify_element(e.x, e.y):
                self._basculer_jeux([int(ligne)])

        def _menu_contextuel(self, e):
            ligne = self.arbre.identify_row(e.y)
            if not ligne:
                return
            if ligne not in self.arbre.selection():
                self.arbre.selection_set(ligne)
            ids = [int(i) for i in self.arbre.selection()]
            a = int(ligne)
            m = self.menu
            m.delete(0, "end")
            if len(ids) > 1:
                m.add_command(label=T("Ajouter les {n} au farm", n=len(ids)),
                              command=lambda: self._basculer_jeux([i for i in ids if i not in self.coches]))
                m.add_command(label=T("Retirer les {n} du farm", n=len(ids)),
                              command=lambda: self._basculer_jeux([i for i in ids if i in self.coches]))
            else:
                m.add_command(label=T("Retirer du farm") if a in self.coches else T("Ajouter au farm"),
                              command=lambda: self._basculer_jeux([a]))
                m.add_command(label=T("Farmer uniquement celui-ci"), command=lambda: self._uniquement(a))
            m.add_command(label=T("Définir un objectif d'heures…"), command=lambda: self._objectif_dialogue(ids))
            m.add_command(label=T("Farmer encore quelques heures…"), command=lambda: self._encore_dialogue(ids))
            m.add_separator()
            m.add_command(label=T("Succès…"), command=lambda: self._ouvrir_succes(a))
            if self.captures.get(a):
                m.add_command(label=T("Captures d'écran ({n})…", n=len(self.captures[a])),
                              command=lambda: self._ouvrir_captures(a))
            if self._a_installer(a):
                m.add_command(label=T("Installer"), command=lambda: self._installer(a))
            elif a in self.installations:
                m.add_command(label=T("Désinstaller…"), command=lambda: self._desinstaller(a))
            m.add_command(label=T("Ouvrir dans la bibliothèque Steam"),
                          command=lambda: self._ouvrir("steam://nav/games/details/{}", a))
            m.add_command(label=T("Page du magasin"),
                          command=lambda: self._ouvrir("https://store.steampowered.com/app/{}", a))
            m.add_command(label=T("Copier l'AppID"), command=lambda: (self.r.clipboard_clear(),
                                                                      self.r.clipboard_append(str(a))))
            if a in self.cfg.get("manuels", []):
                m.add_separator()
                m.add_command(label=T("Retirer de la liste"), command=lambda: self._retirer_manuel(a))
            m.tk_popup(e.x_root, e.y_root)

        def _qui(self, ids) -> str:
            return self.jeux[ids[0]]["nom"] if len(ids) == 1 else T("ces {n} jeux", n=len(ids))

        def _objectif_dialogue(self, ids):
            actuel = self.objectifs.get(ids[0], 0)
            choix, txt = self._dialogue(T("Objectif d'heures"),
                                        T("Temps de jeu Steam total à atteindre pour {qui}, en heures.\n"
                                          "Quand il est atteint, le jeu sort du farm tout seul. 0 = pas d'objectif.",
                                          qui=self._qui(ids)),
                                        [(T("Annuler"), "TButton"), (T("Valider"), "Accent.TButton")],
                                        saisie=court(actuel) if actuel else "")
            if choix == 1:
                self._definir_objectif(ids, nombre(txt))
                self._charger_objectif()

        def _encore_dialogue(self, ids):
            """Objectif relatif : « encore X heures à partir de maintenant »."""
            ids = [a for a in ids if a in self.jeux]
            if not ids:
                return
            choix, txt = self._dialogue(T("Farmer encore"),
                                        T("Combien d'heures de plus pour {qui} ?\n"
                                          "L'objectif devient le temps de jeu actuel plus ce nombre d'heures.",
                                          qui=self._qui(ids)),
                                        [(T("Annuler"), "TButton"), (T("Valider"), "Accent.TButton")], saisie="10")
            plus = nombre(txt)
            if choix != 1 or plus <= 0:
                return
            for a in ids:
                self._definir_objectif([a], round((self._temps_steam(a) / 3600 + plus) * 60) / 60)
            self._charger_objectif()
            a_cocher = [a for a in ids if a not in self.coches]
            if a_cocher:
                self._basculer_jeux(a_cocher)

        def _a_installer(self, a: int | None) -> bool:
            """Jeu de la liste qui n'est installé dans aucune bibliothèque Steam (le farm marche quand même)."""
            return a in self.jeux and bool(self.installes) and a not in self.installes

        def _bouton_steam(self):
            if self._a_installer(self.sel):
                self._installer(self.sel)
            else:
                self._ouvrir("steam://nav/games/details/{}")

        def _installer(self, a: int):
            """Ouvre la fenêtre d'installation de Steam (dossier, taille…) : c'est Steam qui demande confirmation."""
            self._ouvrir("steam://install/{}", a)

        def _ouvrir(self, modele: str, a: int | None = None):
            a = a or self.sel
            if a:
                try:
                    os.startfile(modele.format(a))
                except OSError as e:
                    self.etat.config(text=T("Impossible d'ouvrir : {e}", e=e))

        # ── cocher / décocher ──
        def _basculer_selection(self):
            if self.sel in self.jeux:
                self._basculer_jeux([self.sel])

        def _confirmer_remboursables(self, ids: list[int]) -> list[int]:
            """Demande quoi faire des jeux encore remboursables. Renvoie ceux à garder dans le farm."""
            remb = [a for a in ids if self._a_confirmer(a)]
            if not remb:
                return ids
            lignes = "\n".join(T("•  {nom} : {r}", nom=self.jeux[a]["nom"], r=self._texte_remb(a)) for a in remb[:8])
            if len(remb) > 8:
                lignes += "\n" + T("•  … et {n} autres", n=len(remb) - 8)
            modele = ("Ce jeu est encore remboursable sur Steam (moins de {j} jours après l'achat et moins de 2 h "
                      "de jeu) :\n\n{lignes}\n\nAu-delà de 2 h de jeu, Steam ne rembourse plus.") if len(remb) == 1 \
                else ("Ces jeux sont encore remboursables sur Steam (moins de {j} jours après l'achat et moins de "
                      "2 h de jeu) :\n\n{lignes}\n\nAu-delà de 2 h de jeu, Steam ne rembourse plus.")
            choix, _ = self._dialogue(
                T("Jeux encore remboursables"), T(modele, j=REMB_JOURS, lignes=lignes),
                [(T("Ne pas farmer"), "TButton"), (T("Farmer jusqu'à 1 h 50"), "TButton"),
                 (T("Farmer quand même"), "Accent.TButton")])
            if choix == 2:
                self.remb_ok |= set(remb)
                return ids
            if choix == 1:
                for a in remb:
                    self.objectifs[a] = OBJECTIF_REMB
                    self.remb_ok.add(a)
                return ids
            return [a for a in ids if a not in remb]

        def _basculer_jeux(self, ids, demander: bool = True):
            ids = list(ids)
            retrait = [a for a in ids if a in self.coches]
            ajout = [a for a in ids if a not in self.coches and a in self.jeux]
            fini = [a for a in ajout if self._objectif_atteint(a)]
            if fini:
                ajout = [a for a in ajout if a not in fini]
                nom = self.jeux[fini[0]]["nom"]
                self.etat.config(text=T("Objectif déjà atteint pour {nom} : augmente l'objectif ou mets-le à 0 "
                                        "pour le farmer.", nom=nom) if len(fini) == 1 else
                                 T("Objectif déjà atteint pour {nom} et {n} autre(s) : augmente l'objectif ou "
                                   "mets-le à 0 pour les farmer.", nom=nom, n=len(fini) - 1))
            if demander and ajout:
                ajout = self._confirmer_remboursables(ajout)
            for a in retrait:
                self.coches.pop(a, None)
                self._arreter(a)
                self.echecs.pop(a, None)
                self.essais.pop(a, None)
            for a in ajout:
                self.coches[a] = None
            self._remplir_creneaux()
            for a in retrait + ajout:
                self._maj_ligne(a)
            self._sauver()
            self._maj_sous_titre()
            self._maj_banniere_textes()
            if self._filtre_cle() in ("coches", "encours"):
                self._remplir()

        def _uniquement(self, a: int):
            self._basculer_jeux([i for i in self.coches if i != a] + ([a] if a not in self.coches else []))

        def _tout_decocher(self):
            if self.coches:
                self._basculer_jeux(list(self.coches))

        # ── groupes ──
        def _menu_groupes(self):
            m, sup = self.menu, self.menu_sup
            m.delete(0, "end")
            sup.delete(0, "end")
            m.add_command(label=T("Enregistrer les jeux cochés comme groupe…"), command=self._enregistrer_groupe,
                          state="normal" if self.coches else "disabled")
            if self.groupes:
                m.add_separator()
                for nom in sorted(self.groupes, key=str.lower):
                    m.add_command(label=pluriel(len(self.groupes[nom]), "{nom}   ({n} jeu)", "{nom}   ({n} jeux)",
                                                nom=nom),
                                  command=lambda nom=nom: self._appliquer_groupe(nom))
                    sup.add_command(label=nom, command=lambda nom=nom: self._supprimer_groupe(nom))
                m.add_separator()
                m.add_cascade(label=T("Supprimer un groupe"), menu=sup)
            if self.collections:
                col = self.menu_col
                col.delete(0, "end")
                for i, c in enumerate(self.collections):
                    col.add_command(label=pluriel(sum(1 for a in c[2] if a in self.jeux), "{nom}   ({n} jeu)",
                                                  "{nom}   ({n} jeux)", nom=self._nom_collection(c)[2:]),
                                    command=lambda i=i: self._appliquer_collection(i))
                m.add_separator()
                m.add_cascade(label=T("Cocher une collection Steam"), menu=col)
            b = self.b_groupes
            m.tk_popup(b.winfo_rootx(), b.winfo_rooty() + b.winfo_height())

        def _enregistrer_groupe(self):
            n = len(self.coches)
            choix, nom = self._dialogue(T("Nouveau groupe"),
                                        pluriel(n, "Nom du groupe ({n} jeu coché) :", "Nom du groupe ({n} jeux cochés) :"),
                                        [(T("Annuler"), "TButton"), (T("Enregistrer"), "Accent.TButton")], saisie="")
            if choix == 1 and nom:
                self.groupes[nom] = list(self.coches)
                self._sauver()
                self.etat.config(text=T("Groupe « {nom} » enregistré. Menu Groupes pour le recocher en un clic.",
                                        nom=nom))

        def _appliquer_groupe(self, nom: str):
            cible = [a for a in self.groupes.get(nom, []) if a in self.jeux]
            self._basculer_jeux([a for a in self.coches if a not in cible] + [a for a in cible if a not in self.coches])
            self.etat.config(text=pluriel(len(cible), "Groupe « {nom} » : {n} jeu coché.",
                                          "Groupe « {nom} » : {n} jeux cochés.", nom=nom))

        def _supprimer_groupe(self, nom: str):
            self.groupes.pop(nom, None)
            self._sauver()
            self.etat.config(text=T("Groupe « {nom} » supprimé.", nom=nom))

        def _ajouter(self):
            t = self.ajout.get().strip()
            if not t.isdigit():
                self.etat.config(text=T("Un AppID est un nombre : il est dans l'adresse de la page Steam du jeu."))
                return
            a = int(t)
            self.cfg.setdefault("manuels", [])
            if a not in self.cfg["manuels"]:
                self.cfg["manuels"].append(a)
            if a not in self.jeux:
                self.jeux[a] = {"nom": self.infos.get(a, (f"App {a}",))[0], "min": 0}
                self.base_farme[a] = self._total(a)
            self.ajout.set("")
            if a not in self.coches:
                self._basculer_jeux([a])
            self._remplir()
            if self.arbre.exists(str(a)):
                self.arbre.see(str(a))
                self.arbre.selection_set(str(a))
            self.etat.config(text=T("{nom} ajouté au farm.", nom=self.jeux[a]["nom"]))

        def _retirer_manuel(self, a: int):
            if a in self.coches:
                self._basculer_jeux([a])
            self.cfg["manuels"].remove(a)
            if a not in self.cfg.get("possedes", []):
                self.jeux.pop(a, None)
            self._sauver()
            self._remplir()

        # ── farm ──
        def _candidats(self) -> list[int]:
            now = time.time()
            return [a for a in self.coches if a in self.jeux and a not in self.procs and a not in self.echecs
                    and self.essais.get(a, (0, 0.0))[1] <= now and not self._objectif_atteint(a)
                    and not self._a_confirmer(a)]

        def _remplir_creneaux(self):
            if not self.actif or self.pause:
                return
            for a in self._candidats():
                if len(self.procs) >= MAX_SIMULTANES:
                    break
                self._lancer(a)

        def _lancer(self, a: int):
            if a in self.procs:
                return
            env = dict(os.environ, SteamAppId=str(a), SteamGameId=str(a))
            p = subprocess.Popen(commande("--farm", str(a), str(os.getpid())),
                                 env=env, cwd=ICI, creationflags=SANS_FENETRE, stdin=subprocess.DEVNULL,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self.procs[a] = (p, time.time())
            self.echecs.pop(a, None)
            self._maj_ligne(a)

        def _arreter(self, a: int, compter: bool = True):
            self.essais.pop(a, None)
            p = self.procs.pop(a, None)
            if not p:
                return
            if compter:
                self.farme[a] = self.farme.get(a, 0) + time.time() - p[1]
            self.arretes[a] = time.time()
            p[0].terminate()
            try:
                p[0].wait(timeout=3)
            except subprocess.TimeoutExpired:
                pass
            self._maj_ligne(a)

        def _marche_arret(self, interactif: bool = True):
            if self.actif:
                self._tout_arreter(T("Farm arrêté."))
                return
            a_lancer = [a for a in self.coches if a in self.jeux]
            if not a_lancer:
                self.etat.config(text=T("Coche d'abord au moins un jeu (clic sur la case à gauche de son logo)."))
                return
            if not DLL.exists():
                if interactif:
                    messagebox.showerror(NOM,
                                         T("steam_api64.dll introuvable.\n"
                                           "Installe n'importe quel jeu Steam puis clique Recharger."))
                return
            if interactif:
                garder = set(self._confirmer_remboursables(a_lancer))
                for a in a_lancer:
                    if a not in garder:
                        self.coches.pop(a, None)
            now = time.time()
            self.actif, self.debut, self.derniere_rotation = True, now, now
            self.total_depart = sum(self._total(a) for a in self.jeux)
            self.echecs.clear()
            self.essais.clear()
            self.reprise_depuis, self.jeu_reel, self.derniere_verif_jeu = None, None, 0.0
            self.pause = self._raison_pause(now, False)
            if self.eveille.get():
                garder_eveille(True)
            if self.invisible.get():
                self._passer_invisible()
            self._remplir_creneaux()
            self.bouton.config(text=T("■  Arrêter le farm"), style="Arret.TButton")
            self._maj_menu_tray()
            self._remplir()
            self._maj_sous_titre()
            self._maj_banniere_textes()

        def _tout_arreter(self, message: str):
            for a in list(self.procs):
                self._arreter(a)
            self.actif = False
            self.pause = self.reprise_depuis = self.jeu_reel = None
            self.essais.clear()
            garder_eveille(False)
            self._restaurer_statut()
            self.bouton.config(text=T("▶  Démarrer le farm"), style="Accent.TButton")
            self._maj_menu_tray()
            if message:
                self.etat.config(text=message)
            self._sauver()
            self._remplir()
            self._maj_banniere_textes()

        def _tic(self):
            try:
                self._tic_corps()
            except Exception as e:  # noqa: BLE001 — la boucle ne doit jamais s'arrêter
                journal(f"tic : {e!r}")
            self.r.after(1000, self._tic)

        def _tic_corps(self):
            now = time.time()
            ecart = min(max(now - self.dernier_tic, 0.0), 5.0)
            self.dernier_tic = now
            if self.chargement:
                self._appliquer_chargement()
            if SIGNAL.exists():
                SIGNAL.unlink(missing_ok=True)
                self._montrer()
            # processus terminés
            steam_ferme = False
            for a, (p, t0) in list(self.procs.items()):
                code = p.poll()
                if code is None:
                    continue
                del self.procs[a]
                self.arretes[a] = now
                if code != 4:
                    self.farme[a] = self.farme.get(a, 0) + now - t0
                else:
                    jour = self.historique.get(dt.date.today().isoformat(), {})
                    if str(a) in jour and jour[str(a)] <= now - t0 + 2:
                        del jour[str(a)]
                if code == 5:
                    steam_ferme = True
                elif code == 4:
                    n = self.essais.get(a, (0, 0.0))[0] + 1
                    if n <= ESSAIS_MAX:
                        self.essais[a] = (n, now + DELAI_ESSAI)
                    else:
                        self.essais.pop(a, None)
                        self.echecs[a] = "refus"
                else:
                    self.essais.pop(a, None)
                    self.echecs[a] = code
                self._maj_ligne(a)
            if self.actif:
                self._surveiller(now, steam_ferme)
                if not self.pause:
                    self._verifier_objectifs()
                    self._rotation(now)
                    self._remplir_creneaux()
                # historique par jour
                if self.procs and ecart:
                    jour = self.historique.setdefault(dt.date.today().isoformat(), {})
                    for a in self.procs:
                        jour[str(a)] = jour.get(str(a), 0) + ecart
                if self.actif:
                    self._etat_farm(now)
                if now - self._derniere_sauvegarde > 60:
                    self._sauver()
            else:
                if not self.etat.cget("text"):
                    total = sum(self._total(a) for a in self.farme)
                    self.etat.config(text=T("Total farmé avec l'appli : {h}", h=heures(total / 60, None)))
                self._titre_tray(NOM)
                # statut resté invisible (Steam fermé à l'arrêt, PC éteint pendant le farm) : remis dès que possible
                if self.statut_avant is not None and not self.auto:
                    self._restaurer_statut()
            for a in self.procs:
                self._maj_ligne(a)
            self._maj_banniere_textes()
            self._demarrage_auto()
            if now - self._derniere_veille > 1800:      # toutes les 30 min ; chaque vérification se limite à une par jour
                self._derniere_veille = now
                self._verifier_maj()
                self._verifier_promos()

        def _raison_pause(self, now: float, steam_ferme: bool) -> str | None:
            """Pourquoi le farm doit rester en pause en ce moment (ou None)."""
            if steam_ferme or not steam_pret():
                return "steam"
            if self.plage.get():
                debut, fin = self._plage()
                t = time.localtime(now)
                if not dans_plage(t.tm_hour + t.tm_min / 60, debut, fin):
                    return "horaire"
            if self.pause_jeu.get():
                if now - self.derniere_verif_jeu >= VERIF_JEU:
                    self.derniere_verif_jeu = now
                    self.arretes = {a: t for a, t in self.arretes.items() if now - t < GRACE_ARRET}
                    self.jeu_reel = jeu_lance(set(self.procs) | set(self.arretes) | {APP_TEST})
                if self.jeu_reel:
                    return "jeu"
            else:
                self.jeu_reel = None
            if self.si_inactif.get() and inactivite() < nombre(self.inactif_min.get()) * 60:
                return "pc"
            return None

        def _surveiller(self, now: float, steam_ferme: bool):
            """Met le farm en pause (Steam fermé, vrai jeu lancé, PC utilisé) et le relance tout seul ensuite."""
            raison = self._raison_pause(now, steam_ferme)
            if raison:
                self.reprise_depuis = None
                if raison != self.pause:
                    self.pause = raison
                    for a in list(self.procs):
                        self._arreter(a)
                    if raison == "steam":
                        self._notifier(T("Steam s'est fermé : le farm est en pause et reprendra tout seul."))
                    elif raison == "jeu":
                        self._notifier(T("{nom} est lancé : le farm se met en pause jusqu'à ce que tu le quittes.",
                                         nom=self.jeu_reel[1]))
                    elif raison == "horaire":
                        self._notifier(T("Hors de ta plage horaire : le farm se met en pause jusqu'à {h}.",
                                         h=T("{h} h", h=self._plage()[0])))
                    self._remplir()
            elif self.pause:
                self.reprise_depuis = self.reprise_depuis or now
                if now - self.reprise_depuis >= (0 if self.pause in ("pc", "horaire") else PAUSE_STEAM):
                    ancienne, self.pause, self.reprise_depuis = self.pause, None, None
                    if ancienne == "steam":
                        self.essais.clear()
                        self.echecs.clear()
                        self._appliquer_statut()     # pas possible tant que Steam était fermé
                    self._remplir_creneaux()
                    if ancienne == "steam":
                        self._notifier(T("Steam est revenu : le farm a repris."))
                    elif ancienne == "jeu":
                        self._notifier(T("Jeu quitté : le farm a repris."))
                    elif ancienne == "horaire":
                        self._notifier(T("Début de ta plage horaire : le farm a repris."))
                    self._remplir()

        def _verifier_objectifs(self):
            for a in list(self.coches):
                if a in self.jeux and self._objectif_atteint(a):
                    tournait = a in self.procs
                    self._arreter(a)
                    self.coches.pop(a, None)
                    self.atteints.add(a)
                    self._maj_ligne(a)
                    self._maj_sous_titre()
                    if tournait:
                        self._notifier(T("Objectif atteint : {nom} ({o}). Il sort du farm.",
                                         nom=self.jeux[a]["nom"], o=objectif_txt(self.objectifs[a])))
                    self._sauver()
                    if not any(b in self.jeux for b in self.coches):
                        self._tout_arreter(T("Tous les jeux cochés ont atteint leur objectif : farm terminé."))
                        return

        def _rotation(self, now: float):
            h = nombre(self.rotation.get())
            if h <= 0 or len(self.procs) < MAX_SIMULTANES or now - self.derniere_rotation < h * 3600:
                return
            if not self._candidats():
                return                       # tous les jeux cochés tournent déjà : rien à faire tourner
            self.derniere_rotation = now
            for a in list(self.procs):       # les jeux qui tournaient repassent en fin de file
                self._arreter(a)
                self.coches.pop(a, None)
                self.coches[a] = None
            journal(T("rotation : groupe de jeux suivant"))
            self._remplir_creneaux()
            self._maj_lignes_cochees()

        def _etat_farm(self, now: float):
            limite = nombre(self.duree.get()) * 3600
            ecoule = now - self.debut
            gagne = sum(self._total(a) for a in self.jeux) - self.total_depart
            if limite > 0 and ecoule >= limite:
                self._tout_arreter(T("Terminé : {d} de farm, {h} gagnées au total.",
                                     d=hms(ecoule), h=heures(gagne / 60, None)))
                self._notifier(T("Arrêt automatique : la durée de farm choisie est écoulée."))
                return
            n = len(self.procs)
            if self.pause:
                txt = self._texte_pause()
                if self.reprise_depuis and self.pause not in ("pc", "horaire"):
                    txt = T("↻  Reprise du farm dans {s} s",
                            s=max(0, int(PAUSE_STEAM - (now - self.reprise_depuis))))
            else:
                attente = len([a for a in self.coches if a in self.jeux and a not in self.procs])
                txt = pluriel(n, "▶  {n} jeu en cours", "▶  {n} jeux en cours")
                if attente:
                    txt += T(" · {n} en attente", n=attente)
                txt += T("  ·  {d}  ·  +{h} gagnées", d=hms(ecoule), h=heures(gagne / 60, None))
                if limite > 0:
                    txt += T("  ·  arrêt dans {d}", d=hms(limite - ecoule))
                if self.echecs:
                    txt += T("  ·  {n} refusé(s)", n=len(self.echecs))
            self.etat.config(text=txt)
            self._titre_tray(pluriel(n, "Automata — {n} jeu en cours",
                                     "Automata — {n} jeux en cours")
                             if not self.pause else T("Automata — en pause"))

        def _titre_tray(self, titre: str):
            if self.tray and self.tray.title != titre:
                self.tray.title = titre

        def _maj_menu_tray(self):
            """pystray ne recalcule le menu (Démarrer / Arrêter) que sur demande."""
            if self.tray:
                try:
                    self.tray.update_menu()
                except Exception:
                    pass

        def _demarrage_auto(self):
            """Mode --auto (démarrage de Windows) : attend que Steam soit connecté puis lance le farm."""
            if not self.auto or self.actif or not self.jeux:
                return
            if not steam_pret():
                self.steam_pret_depuis = None
                return
            self.steam_pret_depuis = self.steam_pret_depuis or time.time()
            if time.time() - self.steam_pret_depuis > PAUSE_STEAM:
                self.auto = False
                if any(a in self.jeux for a in self.coches):
                    self._marche_arret(interactif=False)
                else:
                    self._restaurer_statut()

        def _sauver(self):
            self._derniere_sauvegarde = time.time()
            limite = (dt.date.today() - dt.timedelta(days=400)).isoformat()
            self.historique = {j: v for j, v in self.historique.items() if j >= limite}
            self.cfg.update(
                coches=list(self.coches), heures=self.duree.get(), rotation=self.rotation.get(),
                eveille=self.eveille.get(), invisible=self.invisible.get(), pause_jeu=self.pause_jeu.get(),
                si_inactif=self.si_inactif.get(), inactif_min=self.inactif_min.get(), maj_auto=self.maj_auto.get(),
                plage=self.plage.get(), plage_debut=self.plage_debut.get(), plage_fin=self.plage_fin.get(),
                promos_auto=self.promos_auto.get(),
                tri=list(self.tri),
                farme={str(a): round(self._total(a)) for a in set(self.farme) | set(self.procs)},
                objectifs={str(a): h for a, h in self.objectifs.items()}, atteints=sorted(self.atteints),
                achats={str(a): t for a, t in self.achats.items()}, famille=sorted(self.famille),
                remb_ok=sorted(self.remb_ok), groupes=self.groupes,
                historique={j: {a: round(s) for a, s in v.items()} for j, v in self.historique.items()})
            if self.statut_avant is None:
                self.cfg.pop("statut_avant", None)
            else:
                self.cfg["statut_avant"] = self.statut_avant
            self._noter_fenetre()
            try:
                ecrire_atomique(CONFIG, json.dumps(self.cfg))
            except OSError as e:
                journal(T("sauvegarde de config.json impossible : {e}", e=repr(e)))

    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass
    def erreur(typ, val, tb):
        journal(T("erreur : {e}", e="".join(traceback.format_exception(typ, val, tb)).rstrip()))

    # pythonw et le .exe n'ont pas de console : sans ça, une erreur de l'interface ne laisserait aucune trace
    threading.excepthook = lambda a: erreur(a.exc_type, a.exc_value, a.exc_traceback)
    r = tk.Tk()

    # Garde-fou : sous Windows, Tk voit parfois un message qui disparaît avant d'être lu (par exemple quand un
    # logiciel de capture photographie la fenêtre) et attend alors le suivant sans limite. Le suivi du farm resterait
    # figé tant que la fenêtre ne reçoit rien. Un message vide par seconde garantit que la boucle repart.
    fil_tk = ctypes.windll.kernel32.GetCurrentThreadId()

    def reveil():
        while True:
            time.sleep(1)
            ctypes.windll.user32.PostThreadMessageW(fil_tk, 0, 0, 0)

    threading.Thread(target=reveil, daemon=True).start()
    r.report_callback_exception = erreur
    appli = Appli(r)
    if apres:
        r.after(200, lambda: apres(appli, r))
    r.mainloop()


# ───────────────────────── Traduction anglaise ─────────────────────────
# Clé = texte français tel qu'il est écrit dans le code ; les {champs} doivent être les mêmes.

EN = {
    # titres, boutons, colonnes
    "↻  Recharger": "↻  Reload",
    "Statistiques": "Statistics",
    "Options": "Options",
    "Objectif": "Goal",
    "h de jeu au total": "h of total playtime",
    "＋ X h…": "＋ X h…",
    "＋  Ajouter au farm": "＋  Add to farm",
    "Ouvrir dans Steam": "Open in Steam",
    "Rechercher": "Search",
    "Groupes  ▾": "Groups  ▾",
    "Tout décocher": "Uncheck all",
    "▶  Démarrer le farm": "▶  Start farming",
    "■  Arrêter le farm": "■  Stop farming",
    "Jeu": "Game",
    "Temps Steam": "Steam time",
    "Farmé ici": "Farmed here",
    "Tous les jeux": "All games",
    "Cochés": "Checked",
    "En cours": "Running",
    "Avec objectif": "With a goal",
    "Remboursables": "Refundable",
    "Jamais lancés": "Never played",
    "Installés": "Installed",
    "Succès presque finis": "Achievements almost done",
    "Installer": "Install",
    "Mises à jour": "Updates",
    # menu Outils
    "Outils  ▾": "Tools  ▾",
    "Valeur de la bibliothèque…": "Library value…",
    "Stockage des jeux installés…": "Storage of installed games…",
    "Promos de ma liste de souhaits…": "Wishlist deals…",
    "Captures d'écran…": "Screenshots…",
    "À quoi je joue ?…": "What should I play?…",
    "↻  Actualiser": "↻  Refresh",
    "jamais": "never",
    "aujourd'hui": "today",
    "hier": "yesterday",
    "il y a {n} jours": "{n} days ago",
    "il y a {n} mois": "{n} months ago",
    "il y a {n} ans": "{n} years ago",
    # valeur de la bibliothèque
    "Valeur de la bibliothèque": "Library value",
    "Valeur au prix normal": "Value at regular price",
    "Payants, jamais lancés": "Paid, never played",
    "Coût moyen par heure jouée": "Average cost per hour played",
    "Tous les jeux payants": "All paid games",
    "En promo en ce moment": "On sale right now",
    "Prix normal": "Regular price",
    "En ce moment": "Right now",
    "Temps de jeu": "Playtime",
    "Prix par heure": "Price per hour",
    "Ta liste de jeux est vide.": "Your game list is empty.",
    "Lecture des prix dans la boutique Steam…": "Reading prices from the Steam store…",
    "La boutique Steam ne répond pas. Vérifie ta connexion, puis clique sur Actualiser.":
        "The Steam store isn't answering. Check your connection, then click Refresh.",
    "{p} avec les promos du moment": "{p} with current sales",
    "{n} jeu payant": "{n} paid game",
    "{n} jeux payants": "{n} paid games",
    "{p} / h": "{p}/h",
    "{h} jouées sur ces jeux": "{h} played on these games",
    "jamais lancé": "never played",
    "Prix du moment dans la boutique Steam, pas ce que tu as payé. Double-clic sur un jeu : page du magasin.":
        "Current Steam store prices, not what you paid. Double-click a game for its store page.",
    "Non comptés : {g} gratuits, {r} retirés de la boutique, {f} partagés par ta famille.":
        "Not counted: {g} free, {r} removed from the store, {f} shared by your family.",
    # stockage
    "Stockage des jeux installés": "Storage of installed games",
    "Clique sur « Dernier lancement » pour voir en premier les jeux que tu ne lances plus.":
        "Click \"Last played\" to see the games you no longer launch first.",
    "Désinstaller…": "Uninstall…",
    "Taille": "Size",
    "Dernier lancement": "Last played",
    "Disque": "Drive",
    "Espace pris par tes jeux": "Space used by your games",
    "Disque {d}": "Drive {d}",
    "{n} jeu installé": "{n} game installed",
    "{n} jeux installés": "{n} games installed",
    "{t} libres": "{t} free",
    "sur {t}": "of {t}",
    "{nom} est en cours de farm : retire-le du farm avant de le désinstaller.":
        "{nom} is being farmed: remove it from the farm before uninstalling it.",
    "Steam te demande de confirmer. Clique ensuite sur Actualiser.": "Steam asks you to confirm. Then click Refresh.",
    # promos de la liste de souhaits
    "Promos de ma liste de souhaits": "Wishlist deals",
    "Liste de souhaits": "Wishlist",
    "Ouvrir ma liste dans Steam": "Open my wishlist in Steam",
    "Vérifier chaque jour et me prévenir des nouvelles promos": "Check every day and tell me about new deals",
    "Prix": "Price",
    "Promo": "Discount",
    "Aucun compte Steam trouvé sur ce PC.": "No Steam account found on this PC.",
    "Lecture de ta liste de souhaits et des prix…": "Reading your wishlist and prices…",
    "Promo sur ta liste de souhaits : {nom} à −{p} ({prix}).": "Wishlist deal: {nom} is {p} off ({prix}).",
    "{n} promos sur ta liste de souhaits : {noms}": "{n} wishlist deals: {noms}",
    "Steam ne répond pas. Vérifie ta connexion, puis clique sur Actualiser.":
        "Steam isn't answering. Check your connection, then click Refresh.",
    "Ta liste de souhaits est vide, ou elle n'est pas publique.": "Your wishlist is empty, or it isn't public.",
    "Pour que l'appli la voie : dans Steam, ton profil → Modifier le profil → Confidentialité → "
    "« Détails du jeu » : Public.":
        "For the app to see it: in Steam, your profile → Edit Profile → Privacy Settings → \"Game details\": Public.",
    "{n} jeu dans ta liste de souhaits": "{n} game on your wishlist",
    "{n} jeux dans ta liste de souhaits": "{n} games on your wishlist",
    "{n} en promo": "{n} on sale",
    "L'appli demande ta liste à Steam avec ton identifiant Steam public, puis les prix à la boutique. "
    "Double-clic sur un jeu : page du magasin.":
        "The app asks Steam for your wishlist with your public Steam ID, then asks the store for prices. "
        "Double-click a game for its store page.",
    # captures d'écran
    "Captures d'écran": "Screenshots",
    "Captures d'écran ({n})…": "Screenshots ({n})…",
    "Ouvrir le dossier": "Open folder",
    "Aucune capture d'écran Steam sur ce PC.\nDans un jeu, appuie sur F12 pour en prendre une.":
        "No Steam screenshots on this PC.\nIn a game, press F12 to take one.",
    "{n} capture": "{n} screenshot",
    "{n} captures": "{n} screenshots",
    "les {n} plus récentes affichées": "showing the {n} most recent",
    # à quoi je joue ?
    "À quoi je joue ?": "What should I play?",
    "Tirer au sort parmi": "Pick at random from",
    "Joués moins de 2 h": "Played less than 2 h",
    "Tous mes jeux": "All my games",
    "↻  Un autre": "↻  Another one",
    "Aucun jeu dans cette catégorie.": "No game in this category.",
    "▶  Lancer": "▶  Play",
    "Temps de jeu : {t}": "Playtime: {t}",
    "Jamais lancé": "Never played",
    "Installé": "Installed",
    "Pas installé": "Not installed",
    "Dernier lancement : {d}": "Last played: {d}",
    "{nom} est retiré du farm pour que tu puisses y jouer.": "{nom} was removed from the farm so you can play it.",
    # collections Steam
    "Favoris": "Favorites",
    "Cocher une collection Steam": "Check a Steam collection",
    "Collection « {nom} » : {n} jeu coché.": "Collection \"{nom}\": {n} game checked.",
    "Collection « {nom} » : {n} jeux cochés.": "Collection \"{nom}\": {n} games checked.",
    "Farmer seulement entre": "Only farm between",
    "h et": ":00 and",
    "h": ":00",
    "{h} h": "{h}:00",
    "Par exemple de 23 h à 8 h pour farmer la nuit. En dehors de ces heures, le farm se met en pause "
    "et reprend tout seul.":
        "For example from 23:00 to 8:00 to farm at night. Outside these hours the farm pauses "
        "and starts again by itself.",
    "⏸  En dehors de ta plage horaire : le farm reprend à {h}": "⏸  Outside your farming hours: back at {h}",
    "Hors de ta plage horaire : le farm se met en pause jusqu'à {h}.":
        "Outside your farming hours: the farm pauses until {h}.",
    "Début de ta plage horaire : le farm a repris.": "Your farming hours have started: the farm is running again.",
    "config.json illisible ({e}) : copie de secours reprise, fichier abîmé gardé dans {f}":
        "config.json unreadable ({e}): backup copy used, damaged file kept as {f}",
    "config.json illisible ({e}) et pas de copie de secours : réglages remis à zéro, fichier abîmé gardé dans {f}":
        "config.json unreadable ({e}) and no backup copy: settings reset, damaged file kept as {f}",
    "sauvegarde de config.json impossible : {e}": "could not save config.json: {e}",
    "erreur : {e}": "error: {e}",
    "Me prévenir quand une nouvelle version sort": "Tell me when a new version is out",
    "Une fois par jour au plus, l'appli demande à GitHub le numéro de la dernière version. "
    "Rien d'autre n'est envoyé : ni ton compte Steam, ni tes jeux.":
        "At most once a day, the app asks GitHub for the latest version number. "
        "Nothing else is sent: not your Steam account, not your games.",
    "Nouvelle version {v}  ↗": "New version {v}  ↗",
    "La version {v} est disponible sur GitHub.": "Version {v} is available on GitHub.",
    # sous-titre
    "{n} jeu": "{n} game",
    "{n} jeux": "{n} games",
    "{n} coché": "{n} checked",
    "{n} cochés": "{n} checked",
    "lecture de ta bibliothèque…": "reading your library…",
    "Steam fermé ? liste du dernier lancement": "Steam closed? list from the last launch",
    "Steam fermé ? liste non vérifiée": "Steam closed? list not verified",
    "steam_api64.dll introuvable : le farm ne pourra pas démarrer": "steam_api64.dll not found: farming can't start",
    "impossible de lire Steam : {e}": "can't read Steam: {e}",
    # options
    "Farm": "Farming",
    "Arrêter le farm automatiquement après": "Stop farming automatically after",
    "h  (0 = jamais)": "h  (0 = never)",
    "Rotation : changer de groupe de jeux toutes les": "Rotation: switch to the next group of games every",
    "Steam accepte {n} jeux à la fois. Si tu en coches plus, l'appli les fait tourner par groupes et passe au "
    "groupe suivant à chaque rotation.":
        "Steam accepts {n} games at once. If you check more, the app runs them in groups and moves to the next "
        "group at each rotation.",
    "Garder le PC éveillé pendant le farm": "Keep the PC awake while farming",
    "Quand farmer": "When to farm",
    "Farmer seulement quand je n'utilise pas le PC, après": "Only farm when I'm not using the PC, after",
    "min sans activité": "min of inactivity",
    "Mettre le farm en pause quand je lance un vrai jeu": "Pause farming when I launch a real game",
    "Passer en invisible sur Steam pendant le farm": "Go invisible on Steam while farming",
    "En invisible, tes amis ne te voient plus « En jeu ». Ton statut d'avant revient quand le farm s'arrête.":
        "While invisible, your friends no longer see you \"In game\". Your previous status comes back when "
        "farming stops.",
    "Lancer avec Windows": "Start with Windows",
    "Au démarrage du PC, l'appli s'ouvre cachée dans la zone de notification, attend que Steam soit connecté, "
    "puis farme tes jeux cochés.":
        "When the PC starts, the app opens hidden in the system tray, waits for Steam to log in, then farms "
        "your checked games.",
    "Langue": "Language",
    "Ajouter un jeu par son AppID": "Add a game by its AppID",
    "Ajouter": "Add",
    "Pour un jeu absent de la liste. L'AppID est le nombre dans l'adresse de sa page Steam "
    "(store.steampowered.com/app/730 → 730).":
        "For a game missing from the list. The AppID is the number in its Steam store page address "
        "(store.steampowered.com/app/730 → 730).",
    "Version {v}": "Version {v}",
    "Fermer": "Close",
    "Page du projet": "Project page",
    # statistiques
    "Aujourd'hui": "Today",
    "7 derniers jours": "Last 7 days",
    "Total farmé avec l'appli": "Total farmed with the app",
    "Heures farmées par jour · 30 derniers jours": "Hours farmed per day · last 30 days",
    "Jeux les plus farmés": "Most farmed games",
    "Pas encore d'heures farmées. Lance le farm pour voir tes stats ici.":
        "No hours farmed yet. Start farming to see your stats here.",
    "auj.": "today",
    "{j} {d} : {h}": "{j} {d}: {h}",
    " · {n} jeu": " · {n} game",
    " · {n} jeux": " · {n} games",
    "Aucun jeu farmé pour l'instant.": "No game farmed yet.",
    # zone de notification
    "Ouvrir": "Open",
    "Arrêter le farm": "Stop farming",
    "Démarrer le farm": "Start farming",
    "Quitter": "Quit",
    "Le farm continue. Clic sur l'icône pour rouvrir, clic droit pour quitter.":
        "Still farming. Click the icon to reopen, right-click to quit.",
    "Au démarrage de Windows, l'appli s'ouvre cachée et farme les jeux cochés.":
        "At Windows startup, the app opens hidden and farms your checked games.",
    "Ne se lance plus avec Windows.": "No longer starts with Windows.",
    "Impossible de régler le démarrage : {e}": "Couldn't change the startup setting: {e}",
    "Automata — {n} jeu en cours": "Automata — {n} game running",
    "Automata — {n} jeux en cours": "Automata — {n} games running",
    "Automata — en pause": "Automata — paused",
    # bannière
    "Coche des jeux dans la liste, puis Démarrer le farm.": "Check games in the list, then click Start farming.",
    "Temps Steam  {t}      Farmé ici  {f}      AppID  {a}": "Steam time  {t}      Farmed here  {f}      AppID  {a}",
    "▶  En cours depuis {d}": "▶  Running for {d}",
    "  ·  ⚠ remboursable : {r}": "  ·  ⚠ refundable: {r}",
    "✗  {e} (voir journal.txt)": "✗  {e} (see journal.txt)",
    "↻  Steam a refusé, nouvel essai dans quelques secondes": "↻  Steam refused, retrying in a few seconds",
    "✓  Objectif atteint ({o})": "✓  Goal reached ({o})",
    "⚠  Encore remboursable : {r}": "⚠  Still refundable: {r}",
    "✓  Dans le farm — en attente d'une place (32 jeux max)": "✓  In the farm — waiting for a free slot (32 games max)",
    "✓  Dans le farm — démarre avec le bouton en bas": "✓  In the farm — starts with the button below",
    "Pas dans le farm": "Not in the farm",
    "Retirer du farm": "Remove from farm",
    "{t} sur {o}": "{t} of {o}",
    "fin prévue {m}": "ETA {m}",
    "aujourd'hui à {h}": "today at {h}",
    "demain à {h}": "tomorrow at {h}",
    "reste {r}": "{r} left",
    "acheté il y a {n} jour, encore {r} avant la limite de 2 h": "bought {n} day ago, {r} left before the 2-hour limit",
    "acheté il y a {n} jours, encore {r} avant la limite de 2 h":
        "bought {n} days ago, {r} left before the 2-hour limit",
    "refusé par Steam": "refused by Steam",
    "arrêté (code {c})": "stopped (code {c})",
    "⏸  Steam est fermé : le farm reprendra tout seul dès que Steam sera de retour":
        "⏸  Steam is closed: farming will resume by itself once Steam is back",
    "⏸  En pause pendant que tu joues à {nom}": "⏸  Paused while you play {nom}",
    "⏸  En pause : tu utilises le PC. Le farm reprend après {n} min sans activité":
        "⏸  Paused: you're using the PC. Farming resumes after {n} min of inactivity",
    # colonnes
    "⏸  pause": "⏸  paused",
    "✗  refusé": "✗  refused",
    "↻  nouvel essai": "↻  retrying",
    "⚠  à confirmer": "⚠  to confirm",
    "en attente": "waiting",
    "{p}  de {o}": "{p}  of {o}",
    # menu clic droit et dialogues
    "Ajouter les {n} au farm": "Add these {n} to the farm",
    "Retirer les {n} du farm": "Remove these {n} from the farm",
    "Ajouter au farm": "Add to farm",
    "Farmer uniquement celui-ci": "Farm only this one",
    "Définir un objectif d'heures…": "Set an hours goal…",
    "Farmer encore quelques heures…": "Farm a few more hours…",
    "Ouvrir dans la bibliothèque Steam": "Open in the Steam library",
    "Page du magasin": "Store page",
    "Copier l'AppID": "Copy AppID",
    "Retirer de la liste": "Remove from list",
    "ces {n} jeux": "these {n} games",
    "Objectif d'heures": "Hours goal",
    "Temps de jeu Steam total à atteindre pour {qui}, en heures.\nQuand il est atteint, le jeu sort du farm tout "
    "seul. 0 = pas d'objectif.":
        "Total Steam playtime to reach for {qui}, in hours.\nOnce it's reached, the game leaves the farm by "
        "itself. 0 = no goal.",
    "Annuler": "Cancel",
    "Valider": "OK",
    "Farmer encore": "Farm more hours",
    "Combien d'heures de plus pour {qui} ?\nL'objectif devient le temps de jeu actuel plus ce nombre d'heures.":
        "How many more hours for {qui}?\nThe goal becomes the current playtime plus this number of hours.",
    "Impossible d'ouvrir : {e}": "Couldn't open: {e}",
    "•  {nom} : {r}": "•  {nom}: {r}",
    "•  … et {n} autres": "•  … and {n} more",
    "Ce jeu est encore remboursable sur Steam (moins de {j} jours après l'achat et moins de 2 h de jeu) :\n\n"
    "{lignes}\n\nAu-delà de 2 h de jeu, Steam ne rembourse plus.":
        "This game can still be refunded on Steam (less than {j} days since purchase and less than 2 hours "
        "played):\n\n{lignes}\n\nOnce it has more than 2 hours of playtime, Steam won't refund it.",
    "Ces jeux sont encore remboursables sur Steam (moins de {j} jours après l'achat et moins de 2 h de jeu) :\n\n"
    "{lignes}\n\nAu-delà de 2 h de jeu, Steam ne rembourse plus.":
        "These games can still be refunded on Steam (less than {j} days since purchase and less than 2 hours "
        "played):\n\n{lignes}\n\nOnce a game has more than 2 hours of playtime, Steam won't refund it.",
    "Jeux encore remboursables": "Games still refundable",
    "Ne pas farmer": "Don't farm",
    "Farmer jusqu'à 1 h 50": "Farm up to 1h 50m",
    "Farmer quand même": "Farm anyway",
    "Objectif déjà atteint pour {nom} : augmente l'objectif ou mets-le à 0 pour le farmer.":
        "Goal already reached for {nom}: raise the goal or set it to 0 to farm it.",
    "Objectif déjà atteint pour {nom} et {n} autre(s) : augmente l'objectif ou mets-le à 0 pour les farmer.":
        "Goal already reached for {nom} and {n} other(s): raise the goal or set it to 0 to farm them.",
    # groupes
    "Enregistrer les jeux cochés comme groupe…": "Save checked games as a group…",
    "{nom}   ({n} jeu)": "{nom}   ({n} game)",
    "{nom}   ({n} jeux)": "{nom}   ({n} games)",
    "Supprimer un groupe": "Delete a group",
    "Nouveau groupe": "New group",
    "Nom du groupe ({n} jeu coché) :": "Group name ({n} checked game):",
    "Nom du groupe ({n} jeux cochés) :": "Group name ({n} checked games):",
    "Enregistrer": "Save",
    "Groupe « {nom} » enregistré. Menu Groupes pour le recocher en un clic.":
        "Group \"{nom}\" saved. Use the Groups menu to check it again in one click.",
    "Groupe « {nom} » : {n} jeu coché.": "Group \"{nom}\": {n} game checked.",
    "Groupe « {nom} » : {n} jeux cochés.": "Group \"{nom}\": {n} games checked.",
    "Groupe « {nom} » supprimé.": "Group \"{nom}\" deleted.",
    "Un AppID est un nombre : il est dans l'adresse de la page Steam du jeu.":
        "An AppID is a number: you'll find it in the address of the game's Steam store page.",
    "{nom} ajouté au farm.": "{nom} added to the farm.",
    # farm
    "Farm arrêté.": "Farming stopped.",
    "Coche d'abord au moins un jeu (clic sur la case à gauche de son logo).":
        "First check at least one game (click the box left of its logo).",
    "steam_api64.dll introuvable.\nInstalle n'importe quel jeu Steam puis clique Recharger.":
        "steam_api64.dll not found.\nInstall any Steam game, then click Reload.",
    "Total farmé avec l'appli : {h}": "Total farmed with the app: {h}",
    "Steam s'est fermé : le farm est en pause et reprendra tout seul.":
        "Steam closed: farming is paused and will resume by itself.",
    "{nom} est lancé : le farm se met en pause jusqu'à ce que tu le quittes.":
        "{nom} is running: farming is paused until you quit it.",
    "Steam est revenu : le farm a repris.": "Steam is back: farming resumed.",
    "Jeu quitté : le farm a repris.": "Game closed: farming resumed.",
    "Objectif atteint : {nom} ({o}). Il sort du farm.": "Goal reached: {nom} ({o}). It leaves the farm.",
    "Tous les jeux cochés ont atteint leur objectif : farm terminé.":
        "All checked games reached their goal: farming finished.",
    "Terminé : {d} de farm, {h} gagnées au total.": "Done: {d} of farming, {h} gained in total.",
    "Arrêt automatique : la durée de farm choisie est écoulée.": "Automatic stop: the farming time you set is over.",
    "↻  Reprise du farm dans {s} s": "↻  Farming resumes in {s} s",
    "▶  {n} jeu en cours": "▶  {n} game running",
    "▶  {n} jeux en cours": "▶  {n} games running",
    " · {n} en attente": " · {n} waiting",
    "  ·  {d}  ·  +{h} gagnées": "  ·  {d}  ·  +{h} gained",
    "  ·  arrêt dans {d}": "  ·  stops in {d}",
    "  ·  {n} refusé(s)": "  ·  {n} refused",
    # succès
    "Succès": "Achievements",
    "Succès…": "Achievements…",
    "Succès · {j}": "Achievements · {j}",
    "Succès  {f}/{n}": "Achievements  {f}/{n}",
    "Tous les succès": "All achievements",
    "Verrouillés": "Locked",
    "Débloqués": "Unlocked",
    "Tout sélectionner": "Select all",
    "Débloqué le": "Unlocked on",
    "Joueurs": "Players",
    "Débloqué": "Unlocked",
    "Verrouillé": "Locked",
    "Caché": "Hidden",
    "Chargement…": "Loading…",
    "Aucun succès": "No achievements",
    "{f} sur {n} débloqués": "{f} of {n} unlocked",
    "Débloquer": "Unlock",
    "Débloquer ({n})": "Unlock ({n})",
    "Re-verrouiller": "Relock",
    "Re-verrouiller ({n})": "Relock ({n})",
    "Ouvre Steam et connecte-toi pour voir les succès.": "Open Steam and sign in to see achievements.",
    "Lecture des succès dans Steam…": "Reading achievements from Steam…",
    "Déblocage en cours…": "Unlocking…",
    "Re-verrouillage en cours…": "Relocking…",
    "Steam a refusé d'ouvrir les succès de ce jeu ({d}).": "Steam refused to open this game's achievements ({d}).",
    "Steam n'a pas envoyé tes succès. Réessaie dans un instant.":
        "Steam didn't send your achievements. Try again in a moment.",
    "La DLL de Steam trouvée est trop ancienne pour lire les succès.":
        "The Steam DLL found is too old to read achievements.",
    "Lecture impossible ({e}). Détails dans journal.txt.": "Couldn't read achievements ({e}). Details in journal.txt.",
    "Ce jeu n'a pas de succès Steam.": "This game has no Steam achievements.",
    "Sélectionne des succès (Ctrl+clic ou Maj+clic pour en prendre plusieurs).":
        "Select achievements (Ctrl+click or Shift+click to pick several).",
    "{n} succès débloqué.": "{n} achievement unlocked.",
    "{n} succès débloqués.": "{n} achievements unlocked.",
    "{n} succès re-verrouillé.": "{n} achievement relocked.",
    "{n} succès re-verrouillés.": "{n} achievements relocked.",
    "Steam en a refusé {n} : le jeu le protège.": "Steam refused {n}: the game protects it.",
    "Steam en a refusé {n} : le jeu les protège.": "Steam refused {n}: the game protects them.",
    "Débloquer {n} succès de {j} ?": "Unlock {n} achievement in {j}?",
    "Débloquer les {n} succès de {j} ?": "Unlock {n} achievements in {j}?",
    "Ils apparaîtront sur ton profil Steam avec la date d'aujourd'hui.":
        "They will show on your Steam profile with today's date.",
    "À savoir : ces succès comptent comme les autres sur ton profil. Des sites de classement peuvent repérer ceux "
    "débloqués tous d'un coup, et les jeux en ligne avec anti-triche ont parfois leurs propres règles. À tes risques.":
        "Good to know: these achievements count like any other on your profile. Ranking sites can spot ones "
        "unlocked all at once, and online games with anti-cheat sometimes have their own rules. Use at your own risk.",
    "Re-verrouiller {n} succès de {j} ?": "Relock {n} achievement in {j}?",
    "Re-verrouiller les {n} succès de {j} ?": "Relock {n} achievements in {j}?",
    "Leur date de déblocage sera perdue. Si tu les débloques à nouveau, ils auront la date de ce jour-là.":
        "Their unlock date will be lost. If you unlock them again, they'll get that day's date.",
    # journal.txt
    "[{a}] succès : {e} {d}": "[{a}] achievements: {e} {d}",
    "DLL copiée depuis {f}": "DLL copied from {f}",
    "SteamAPI_Init a échoué": "SteamAPI_Init failed",
    "[{a}] Steam a refusé : {m}": "[{a}] Steam refused: {m}",
    "[{a}] Steam s'est fermé": "[{a}] Steam closed",
    "vérification des jeux possédés impossible : {e}": "couldn't check owned games: {e}",
    "vérification des jeux possédés : Steam a refusé ({e})": "checking owned games: Steam refused ({e})",
    "appinfo.vdf : format inconnu {m}": "appinfo.vdf: unknown format {m}",
    "changement de statut Steam impossible : {e}": "couldn't change Steam status: {e}",
    "icône de notification impossible : {e}": "couldn't create the tray icon: {e}",
    "chargement : {e}": "loading: {e}",
    "rotation : groupe de jeux suivant": "rotation: next group of games",
}


def main():
    charger_langue()
    args = sys.argv[1:]
    if args[:1] == ["--farm"]:
        sys.exit(mode_farm(int(args[1]), int(args[2])))
    if args[:1] == ["--verif"]:
        sys.exit(mode_verif(Path(args[1]), Path(args[2])))
    if args[:1] == ["--succes"]:
        sys.exit(mode_succes(int(args[1]), Path(args[2]), Path(args[3])))
    if deja_ouvert():
        SIGNAL.touch()
        return
    interface(auto="--auto" in args)


if __name__ == "__main__":
    main()
