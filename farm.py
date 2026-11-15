"""
Automata · Alter Ego (farm d'heures Steam / Steam hours farm)
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
import re
import shutil
import struct
import subprocess
import sys
import threading
import time
import winreg
from ctypes import wintypes
from pathlib import Path

# Pillow, pystray et tkinter ne sont importés que pour la fenêtre (voir interface()) :
# les processus de farm restent ainsi très légers.

NOM, SOUS_TITRE = "Automata", "Alter Ego"   # nom affiché de l'appli
VERSION = "1.3.0"
PROJET = "https://github.com/Dekeur/steam-hours-farm"
GELE = getattr(sys, "frozen", False)          # True dans le .exe construit avec PyInstaller
ICI = Path(sys.executable).resolve().parent if GELE else Path(__file__).resolve().parent
SCRIPT = "" if GELE else str(Path(__file__).resolve())
DLL = ICI / "steam_api64.dll"
CONFIG = ICI / "config.json"
JOURNAL = ICI / "journal.txt"
SIGNAL = ICI / ".afficher"        # un 2e lancement demande à la fenêtre déjà ouverte de se montrer
CLE_RUN = r"Software\Microsoft\Windows\CurrentVersion\Run"
NOM_RUN = "FarmHeuresSteam"
CLE_APPLI = r"Software\FarmHeuresSteam"   # langue retenue pour tout le PC (valeur « Langue »)
MAX_SIMULTANES = 32               # Steam refuse au-delà de 32 jeux lancés en même temps
APP_TEST = 480                    # Spacewar, gratuit pour tous : sert à interroger Steam
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
           ("remb", "Remboursables"), ("jamais", "Jamais lancés"), ("installes", "Installés"))
FILTRES_SUCCES = (("tous", "Tous les succès"), ("verrouilles", "Verrouillés"), ("debloques", "Débloqués"))
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
        with JOURNAL.open("a", encoding="utf-8") as f:
            f.write(time.strftime("%Y-%m-%d %H:%M:%S  ") + msg + "\n")
    except OSError:
        pass


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


def temps_de_jeu(steam: Path) -> dict[int, int]:
    """appid -> minutes jouées, d'après le localconfig.vdf du compte connecté."""
    uid = utilisateur_actif()
    dossiers = [steam / "userdata" / str(uid)] if uid else []
    if (steam / "userdata").exists():
        dossiers += sorted((steam / "userdata").iterdir(), key=lambda p: p.stat().st_mtime, reverse=True)
    for d in dossiers:
        f = d / "config" / "localconfig.vdf"
        if f.exists():
            apps = lire_vdf(f).get("userlocalconfigstore", {}).get("software", {}) \
                .get("valve", {}).get("steam", {}).get("apps", {})
            return {int(k): int(v.get("playtime", "0") or 0)
                    for k, v in apps.items() if k.isdigit() and isinstance(v, dict)}
    return {}


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

    def cstr(p: int) -> tuple[str, int]:
        fin = data.index(b"\0", p)
        return data[p:fin].decode("utf-8", "replace"), fin + 1

    def kv(p: int) -> tuple[dict, int]:
        d: dict = {}
        while True:
            t = data[p]
            p += 1
            if t in (8, 11):
                return d, p
            if chaines is None:
                k, p = cstr(p)
            else:
                k = chaines[struct.unpack_from("<i", data, p)[0]]
                p += 4
            if t == 0:
                v, p = kv(p)
            elif t == 1:
                v, p = cstr(p)
            elif t == 2:
                v, p = struct.unpack_from("<i", data, p)[0], p + 4
            elif t in (3, 4, 6):
                v, p = None, p + 4
            elif t in (7, 10):
                v, p = None, p + 8
            else:
                raise ValueError(f"type {t}")
            d[k.lower()] = v

    # après appid+taille : état, maj, jeton(8), sha1(20), n° de changement, [sha1 binaire(20)]
    entete = 40 + (0 if magique == 0x07564427 else 20)
    out: dict[int, tuple[str, str, bool]] = {}
    while True:
        appid, taille = struct.unpack_from("<II", data, pos)
        if appid == 0:
            break
        try:
            d, _ = kv(pos + 8 + entete)
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
            try:
                self.cfg = json.loads(CONFIG.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                self.cfg = {}
            c = self.cfg
            self.k = r.winfo_fpixels("1i") / 96          # facteur d'échelle (écrans à 125 %, 150 %…)
            self.steam: Path | None = None
            self.jeux: dict[int, dict] = {}               # appid -> {"nom", "min"}
            self.infos: dict[int, tuple] = {}
            self.installes: set[int] = set()
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
            self.pause: str | None = None                   # "steam", "jeu" (un vrai jeu est lancé) ou "pc"
            self.reprise_depuis: float | None = None
            self.jeu_reel: tuple[int, str] | None = None
            self.derniere_verif_jeu = 0.0
            self.statut_avant: int | None = c.get("statut_avant")
            self.derniere_rotation = 0.0
            self.dernier_tic = time.time()
            self.tri = ("nom", False)
            self.sel: int | None = None
            self.chargement = None
            self.q: queue.Queue = queue.Queue()
            self.auto, self.steam_pret_depuis = auto, None
            self.tray, self.notifie = None, False
            self.fen_stats = self.fen_options = self.fen_succes = None
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
            r.title(f"{NOM} · {SOUS_TITRE}")
            self._construire()
            self._creer_tray()
            self._recharger()
            r.protocol("WM_DELETE_WINDOW", self._fermer)
            r.after(150, self._pomper)
            r.after(500, self._tic)
            if auto:
                r.withdraw()

        # ── apparence ──
        def _style(self):
            r, k = self.r, self.k
            self._taille(r, 1000, 740)
            r.minsize(*self._taille_min())
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
            s.configure("Ego.TLabel", foreground=EGO, font=("Segoe UI Semibold", 8))
            s.configure("Section.TLabel", font=("Segoe UI Semibold", 11), foreground=TEXTE)
            s.configure("Gris.TLabel", foreground=GRIS)
            s.configure("Doux.TLabel", foreground=DOUX)
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
            r.title(f"{NOM}  ·  {SOUS_TITRE}")
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
            ttk.Label(haut, text=SOUS_TITRE.upper(), style="Ego.TLabel").pack(side="left", padx=(int(8 * k), 0), pady=(3, 0))
            self.sous_titre = ttk.Label(haut, style="Gris.TLabel")
            self.sous_titre.pack(side="left", padx=(14, 0), pady=(3, 0))
            ttk.Button(haut, text=T("↻  Recharger"), command=self._recharger).pack(side="right")
            ttk.Button(haut, text=T("Options"), command=self._ouvrir_options).pack(side="right", padx=(0, 8))
            ttk.Button(haut, text=T("Statistiques"), command=self._ouvrir_stats).pack(side="right", padx=(0, 8))

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
                                      command=lambda: self._ouvrir("steam://nav/games/details/{}"))
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
            cb = ttk.Combobox(barre, textvariable=self.filtre, values=[T(l) for _, l in FILTRES],
                              state="readonly", width=15)
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
            self.arbre = ttk.Treeview(milieu, columns=("temps", "objectif", "farme"), show="tree headings",
                                      selectmode="extended")
            self.arbre.heading("#0", text=T("Jeu"), anchor="w", command=lambda: self._trier("nom"))
            self.arbre.column("#0", width=int(420 * k), stretch=True)
            for col, titre, larg in (("temps", "Temps Steam", 120), ("objectif", "Objectif", 140),
                                     ("farme", "Farmé ici", 140)):
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
            self.r.title(f"{NOM} · {SOUS_TITRE}")
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
            w = self._fenetre("fen_options", T("Options"), (620, 750))
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
            vu = self.cfg.get("succes_vus", {}).get(str(self.sel))
            texte = T("Succès  {f}/{n}", f=vu[0], n=vu[1]) if variante == "compte" and vu and vu[1] else T("Succès")
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
            self.cfg.setdefault("succes_vus", {})[str(a)] = [sum(x["fait"] for x in self.succes_liste),
                                                             len(self.succes_liste)]
            self._sauver()
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

        def _fermer(self):
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
                temps = temps_de_jeu(steam)
                candidats, installes = set(temps), set()
                cache = steam / "appcache" / "librarycache"
                if cache.exists():
                    candidats |= {int(p.name) for p in cache.iterdir() if p.name.isdigit()}
                for lib in bibliotheques(steam):
                    for f in (lib / "steamapps").glob("appmanifest_*.acf"):
                        if f.stem[12:].isdigit():
                            installes.add(int(f.stem[12:]))
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
                self.chargement = (steam, jeux, infos, temps, installes, verif, chemins, icones, note, None)
            except Exception as e:  # noqa: BLE001 — affiché dans la fenêtre
                journal(T("chargement : {e}", e=repr(e)))
                self.chargement = (None, set(), {}, {}, set(), None, {}, {}, "", repr(e))

        def _appliquer_chargement(self):
            steam, jeux, infos, temps, installes, verif, chemins, icones, note, err = self.chargement
            self.chargement = None
            if err:
                self.sous_titre.config(text=T("impossible de lire Steam : {e}", e=err))
                return
            self.steam, self.infos, self.installes = steam, infos, installes
            self.gratuits = {a for a, i in infos.items() if i[2]}
            self.chemins.update(chemins)
            self.icones.update(icones)
            self.images.clear()
            self.jeux = {a: {"nom": infos.get(a, (f"App {a}",))[0], "min": temps.get(a, 0)} for a in jeux}
            self.base_farme = {a: self._total(a) for a in self.jeux}
            if verif is not None:
                self.cfg["possedes"] = sorted(verif["possedes"])
                self.achats.update(verif["achats"])
                self.famille = verif["famille"]
                self._sauver()
            self._maj_sous_titre(note)
            self._remplir()
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

        def _texte_pause(self) -> str:
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
            return (heures(self._temps_steam(a) / 60), o, f)

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
            v = self.filtre.get()
            return next((c for c, l in FILTRES if T(l) == v), "tous")

        def _visible(self, a: int, q: str) -> bool:
            if q and q not in self.jeux[a]["nom"].lower() and q != str(a):
                return False
            f = self._filtre_cle()
            return (f == "tous" or (f == "coches" and a in self.coches) or (f == "encours" and a in self.procs)
                    or (f == "objectif" and a in self.objectifs) or (f == "remb" and self._remboursable(a))
                    or (f == "jamais" and not self.jeux[a]["min"]) or (f == "installes" and a in self.installes))

        def _remplir(self):
            q = self.recherche.get().strip().lower()
            sel = self.arbre.selection()
            self.arbre.delete(*self.arbre.get_children())
            cle, inverse = self.tri
            cles = {"nom": lambda a: self.jeux[a]["nom"].lower(), "temps": self._temps_steam,
                    "objectif": lambda a: (self._temps_steam(a) / (self.objectifs[a] * 3600)
                                           if a in self.objectifs else -1),
                    "farme": self._total}
            for a in sorted(self.jeux, key=cles[cle], reverse=inverse):
                if self._visible(a, q):
                    self.arbre.insert("", "end", iid=str(a), text="  " + self.jeux[a]["nom"],
                                      image=self._image_ligne(a), values=self._valeurs(a), tags=self._tags(a))
            garde = [i for i in sel if self.arbre.exists(i)]
            if garde:
                self.arbre.selection_set(garde)
            for col, titre in (("#0", "Jeu"), ("temps", "Temps Steam"), ("objectif", "Objectif"),
                               ("farme", "Farmé ici")):
                fleche = (" ▼" if inverse else " ▲") if (cle == "nom" and col == "#0") or cle == col else ""
                self.arbre.heading(col, text=T(titre) + fleche)

        def _maj_ligne(self, a: int):
            if a in self.jeux and self.arbre.exists(str(a)):
                self.arbre.item(str(a), image=self._image_ligne(a), values=self._valeurs(a), tags=self._tags(a))

        def _maj_lignes_cochees(self):
            for a in self.coches:
                self._maj_ligne(a)

        def _trier(self, col: str):
            self.tri = (col, not self.tri[1] if self.tri[0] == col else col in ("temps", "objectif", "farme"))
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

        def _raison_pause(self, now: float, steam_ferme: bool) -> str | None:
            """Pourquoi le farm doit rester en pause en ce moment (ou None)."""
            if steam_ferme or not steam_pret():
                return "steam"
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
                    self._remplir()
            elif self.pause:
                self.reprise_depuis = self.reprise_depuis or now
                if now - self.reprise_depuis >= (0 if self.pause == "pc" else PAUSE_STEAM):
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
                if self.reprise_depuis and self.pause != "pc":
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
                si_inactif=self.si_inactif.get(), inactif_min=self.inactif_min.get(),
                farme={str(a): round(self._total(a)) for a in set(self.farme) | set(self.procs)},
                objectifs={str(a): h for a, h in self.objectifs.items()}, atteints=sorted(self.atteints),
                achats={str(a): t for a, t in self.achats.items()}, famille=sorted(self.famille),
                remb_ok=sorted(self.remb_ok), groupes=self.groupes,
                historique={j: {a: round(s) for a, s in v.items()} for j, v in self.historique.items()})
            if self.statut_avant is None:
                self.cfg.pop("statut_avant", None)
            else:
                self.cfg["statut_avant"] = self.statut_avant
            try:
                CONFIG.write_text(json.dumps(self.cfg), encoding="utf-8")
            except OSError:
                pass

    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass
    r = tk.Tk()
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
