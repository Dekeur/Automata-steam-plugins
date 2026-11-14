"""
Farm d'heures Steam
-------------------
Fait tourner tes jeux Steam « à vide » : Steam croit que le jeu est lancé et
compte le temps de jeu (et les drops de cartes), sans rien exécuter du jeu.

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

VERSION = "1.1.0"
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
MAX_SIMULTANES = 32               # Steam refuse au-delà de 32 jeux lancés en même temps
APP_TEST = 480                    # Spacewar, gratuit pour tous : sert à interroger Steam
SANS_FENETRE = 0x08000000         # CREATE_NO_WINDOW
REMB_JOURS, REMB_SECONDES = 14, 2 * 3600   # règle de remboursement de Steam
OBJECTIF_REMB = 110 / 60          # « farmer jusqu'à 1 h 50 » pour garder le remboursement
ESSAIS_MAX, DELAI_ESSAI = 3, 30   # nouvel essai quand Steam refuse un jeu (souvent : Steam pas prêt)
PAUSE_STEAM = 20                  # secondes d'attente après le retour de Steam avant de relancer

FOND, PANNEAU, BORD = "#121419", "#1b1f27", "#2a303b"
TEXTE, DOUX, GRIS = "#eef0f4", "#c3c8d2", "#8b93a3"
ACCENT, ACCENT_CLAIR, ENCRE = "#66c0f4", "#93d5fb", "#0b1220"
VERT, ROUGE, ORANGE, SELECTION = "#7bd88f", "#f08080", "#f0b45a", "#263850"
BARRE = "#3b8fd0"                 # barres des statistiques (validée pour le fond sombre)
FILTRES = ("Tous les jeux", "Cochés", "En cours", "Avec objectif", "Remboursables", "Jamais lancés", "Installés")
JOURS = ("lun.", "mar.", "mer.", "jeu.", "ven.", "sam.", "dim.")


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
        raise ValueError(f"appinfo.vdf : format inconnu {magique:#x}")
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
                journal(f"DLL copiée depuis {f}")
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
        ok, msg = bool(api.SteamAPI_Init()), "SteamAPI_Init a échoué"
    api.SteamAPI_IsSteamRunning.restype = ctypes.c_bool
    return api, ok, msg


def mode_farm(appid: int, parent: int) -> int:
    """Se fait passer pour le jeu `appid` jusqu'à ce que la fenêtre principale se ferme."""
    os.environ["SteamAppId"] = os.environ["SteamGameId"] = str(appid)
    api, ok, msg = _steam_api()
    if not ok:
        journal(f"[{appid}] Steam a refusé : {msg}")
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
            journal(f"[{appid}] Steam s'est fermé")
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
        journal(f"vérification des jeux possédés impossible : {e}")
        return None
    finally:
        entree.unlink(missing_ok=True)
        sortie.unlink(missing_ok=True)
    if "erreur" in res:
        journal(f"vérification des jeux possédés : Steam a refusé ({res['erreur']})")
        return None
    return {"possedes": set(res["possedes"]), "achats": {int(a): t for a, t in res.get("achats", {}).items() if t},
            "famille": set(res.get("famille", []))}


# ───────────────────────── Petits outils ─────────────────────────

def hms(s: float) -> str:
    s = int(s)
    return f"{s // 3600}:{s // 60 % 60:02d}:{s % 60:02d}"


def heures(minutes: float, vide: str = "—") -> str:
    minutes = int(minutes)
    return f"{minutes // 60} h {minutes % 60:02d}" if minutes else vide


def nombre(texte: str) -> float:
    try:
        return max(0.0, float(str(texte).replace(",", ".").strip() or 0))
    except ValueError:
        return 0.0


def court(v: float) -> str:
    """1.8333 -> '1.83', 500.0 -> '500'."""
    return f"{v:.2f}".rstrip("0").rstrip(".")


def objectif_txt(h: float) -> str:
    """Objectif en heures, lisible : 500 -> '500 h', 1.8333 -> '1 h 50'."""
    return f"{round(h)} h" if abs(h - round(h)) < 1e-6 else heures(round(h * 60), "0 h")


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
    """Logo de l'appli : carré arrondi bleu Steam avec un sablier stylisé."""
    from PIL import Image, ImageDraw
    t = n * 4
    im = Image.new("RGBA", (t, t), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    d.rounded_rectangle((0, 0, t - 1, t - 1), int(t * 0.24), fill="#1b2838")
    d.rounded_rectangle((int(t * .06), int(t * .06), int(t * .94), int(t * .94)), int(t * .2), outline=ACCENT,
                        width=max(4, int(t * .05)))
    a, b, m = t * .30, t * .70, t / 2
    d.polygon([(a, t * .26), (b, t * .26), (m + t * .03, m), (b, t * .74), (a, t * .74), (m - t * .03, m)], fill=ACCENT)
    d.polygon([(a + t * .06, t * .30), (b - t * .06, t * .30), (m, t * .46)], fill="#1b2838")
    d.polygon([(m, t * .58), (b - t * .07, t * .70), (a + t * .07, t * .70)], fill="#1b2838")
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
            self.echecs: dict[int, str] = {}
            self.essais: dict[int, tuple[int, float]] = {}
            self.actif, self.debut, self.total_depart = False, 0.0, 0.0
            self.pause_steam: float | None = None
            self.steam_revenu: float | None = None
            self.derniere_rotation = 0.0
            self.dernier_tic = time.time()
            self.tri = ("nom", False)
            self.sel: int | None = None
            self.chargement = None
            self.q: queue.Queue = queue.Queue()
            self.auto, self.steam_pret_depuis = auto, None
            self.tray, self.notifie = None, False
            self.fen_stats = self.fen_options = None
            self._rendu_prevu = None
            self._masque = None
            self._maj_objectif = False
            self._derniere_sauvegarde = time.time()
            self.duree = tk.StringVar(value=str(c.get("heures", "0")))
            self.rotation = tk.StringVar(value=str(c.get("rotation", "2")))
            self.eveille = tk.BooleanVar(value=c.get("eveille", True))
            self.au_demarrage = tk.BooleanVar(value=demarrage_windows())
            self._style()
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
            r.title("Farm d'heures Steam")
            r.geometry(f"{int(1000 * k)}x{int(740 * k)}")
            r.minsize(int(840 * k), int(580 * k))
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
            s.map("Accent.TButton", background=[("active", ACCENT_CLAIR)])
            s.configure("Arret.TButton", background=ROUGE, foreground=ENCRE,
                        font=("Segoe UI", 10, "bold"), padding=(18, 8))
            s.map("Arret.TButton", background=[("active", "#f5a3a3")])
            s.configure("Verre.TButton", background="#262c36", foreground=TEXTE, padding=(14, 7))
            s.map("Verre.TButton", background=[("active", "#323a47")])
            s.configure("TCheckbutton", background=FOND, foreground=DOUX, indicatorbackground=PANNEAU,
                        indicatorforeground=ENCRE, upperbordercolor=GRIS, lowerbordercolor=GRIS)
            s.map("TCheckbutton", background=[("active", FOND)], foreground=[("active", TEXTE)],
                  indicatorbackground=[("selected", ACCENT)])
            s.configure("Vertical.TScrollbar", background=PANNEAU, troughcolor=FOND, arrowcolor=GRIS, borderwidth=0)
            s.configure("Titre.TLabel", font=("Segoe UI Semibold", 13))
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

        def _construire(self):
            r, k = self.r, self.k
            m18 = int(18 * k)
            # barre du haut
            haut = ttk.Frame(r, padding=(m18, int(10 * k), m18, int(8 * k)))
            haut.pack(fill="x")
            ttk.Label(haut, text="Farm d'heures Steam", style="Titre.TLabel").pack(side="left")
            self.sous_titre = ttk.Label(haut, style="Gris.TLabel")
            self.sous_titre.pack(side="left", padx=(14, 0), pady=(3, 0))
            ttk.Button(haut, text="↻  Recharger", command=self._recharger).pack(side="right")
            ttk.Button(haut, text="Options", command=self._ouvrir_options).pack(side="right", padx=(0, 8))
            ttk.Button(haut, text="Statistiques", command=self._ouvrir_stats).pack(side="right", padx=(0, 8))

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
            ttk.Label(obj, text="Objectif", style="Ban.TLabel").pack(side="left", padx=(0, 6))
            self.obj_var = tk.StringVar(value="0")
            self.obj_spin = ttk.Spinbox(obj, from_=0, to=100000, increment=10, width=6, style="Ban.TSpinbox",
                                        textvariable=self.obj_var, command=self._regler_objectif)
            self.obj_spin.pack(side="left")
            self.obj_spin.bind("<Return>", lambda e: (self._regler_objectif(), self.arbre.focus_set()))
            self.obj_spin.bind("<FocusOut>", lambda e: self._regler_objectif())
            ttk.Label(obj, text="h de jeu au total", style="Ban.TLabel").pack(side="left", padx=(6, 0))
            self.b_w_obj = self.ban.create_window(0, 0, anchor="sw", window=obj)
            self.b_farm = ttk.Button(self.ban, style="Accent.TButton", width=17, text="＋  Ajouter au farm",
                                     command=self._basculer_selection)
            self.b_steam = ttk.Button(self.ban, text="Ouvrir dans Steam", style="Verre.TButton",
                                      command=lambda: self._ouvrir("steam://nav/games/details/{}"))
            self.b_w_farm = self.ban.create_window(0, 0, anchor="se", window=self.b_farm)
            self.b_w_steam = self.ban.create_window(0, 0, anchor="se", window=self.b_steam)

            # recherche + filtre + groupes
            barre = ttk.Frame(r, padding=(m18, int(4 * k), m18, int(8 * k)))
            barre.pack(fill="x")
            self.recherche = tk.StringVar()
            self.recherche.trace_add("write", lambda *_: self._remplir())
            ttk.Label(barre, text="Rechercher", style="Doux.TLabel").pack(side="left", padx=(0, 8))
            self.champ = ttk.Entry(barre, textvariable=self.recherche)
            self.champ.pack(side="left", fill="x", expand=True)
            self.filtre = tk.StringVar(value=FILTRES[0])
            cb = ttk.Combobox(barre, textvariable=self.filtre, values=FILTRES, state="readonly", width=15)
            cb.pack(side="left", padx=(10, 0))
            cb.bind("<<ComboboxSelected>>", lambda e: (self._remplir(), self.arbre.focus_set()))
            self.b_groupes = ttk.Button(barre, text="Groupes  ▾", command=self._menu_groupes)
            self.b_groupes.pack(side="left", padx=(10, 0))
            ttk.Button(barre, text="Tout décocher", command=self._tout_decocher).pack(side="left", padx=(10, 0))

            # pied (placé avant la liste pour qu'il reste visible quand on rétrécit la fenêtre)
            pied = ttk.Frame(r, padding=(m18, int(10 * k), m18, int(14 * k)))
            pied.pack(side="bottom", fill="x")
            self.etat = ttk.Label(pied, style="Doux.TLabel")
            self.etat.pack(side="left", fill="x", expand=True)
            self.bouton = ttk.Button(pied, text="▶  Démarrer le farm", style="Accent.TButton",
                                     command=self._marche_arret)
            self.bouton.pack(side="right")
            ttk.Frame(r, style="Ligne.TFrame", height=1).pack(side="bottom", fill="x", padx=m18)

            # liste
            milieu = ttk.Frame(r, padding=(m18, 0, m18, 0))
            milieu.pack(fill="both", expand=True)
            self.arbre = ttk.Treeview(milieu, columns=("temps", "objectif", "farme"), show="tree headings",
                                      selectmode="extended")
            self.arbre.heading("#0", text="Jeu", anchor="w", command=lambda: self._trier("nom"))
            self.arbre.column("#0", width=int(420 * k), stretch=True)
            for col, titre, larg in (("temps", "Temps Steam", 120), ("objectif", "Objectif", 140),
                                     ("farme", "Farmé ici", 140)):
                self.arbre.heading(col, text=titre, anchor="e", command=lambda c=col: self._trier(c))
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

        # ── petites fenêtres ──
        def _dialogue(self, titre: str, message: str, boutons, saisie: str | None = None):
            """Fenêtre modale sombre. Renvoie (index du bouton cliqué ou None, texte saisi)."""
            k = self.k
            if self.r.state() != "normal":     # fenêtre cachée (zone de notification) : la montrer d'abord
                self._montrer()
            d = tk.Toplevel(self.r)
            d.title(titre)
            d.configure(bg=FOND)
            d.transient(self.r)
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
            x = self.r.winfo_rootx() + (self.r.winfo_width() - d.winfo_width()) // 2
            y = self.r.winfo_rooty() + (self.r.winfo_height() - d.winfo_height()) // 3
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
            w.geometry(f"{int(taille[0] * self.k)}x{int(taille[1] * self.k)}")
            setattr(self, attr, w)
            self._barre_sombre(w)
            return w

        def _ouvrir_options(self):
            w = self._fenetre("fen_options", "Options", (560, 520))
            if w is None:
                return
            k = self.k
            f = ttk.Frame(w, padding=int(20 * k))
            f.pack(fill="both", expand=True)

            def ligne(texte, var, apres, de=0, a=9999, pas=1):
                l = ttk.Frame(f)
                l.pack(fill="x", pady=(0, 6))
                ttk.Label(l, text=texte).pack(side="left")
                sp = ttk.Spinbox(l, from_=de, to=a, increment=pas, width=5, textvariable=var, command=self._sauver)
                sp.pack(side="left", padx=6)
                sp.bind("<FocusOut>", lambda e: self._sauver())
                ttk.Label(l, text=apres, style="Doux.TLabel").pack(side="left")

            def note(texte):
                ttk.Label(f, text=texte, style="Gris.TLabel", wraplength=int(500 * k)).pack(anchor="w", pady=(0, 12))

            ttk.Label(f, text="Farm", style="Section.TLabel").pack(anchor="w", pady=(0, 8))
            ligne("Arrêter le farm automatiquement après", self.duree, "h  (0 = jamais)")
            ligne("Rotation : changer de groupe de jeux toutes les", self.rotation, "h  (0 = jamais)")
            note(f"Steam accepte {MAX_SIMULTANES} jeux à la fois. Si tu en coches plus, l'appli les fait "
                 "tourner par groupes de 32 et passe au groupe suivant à chaque rotation.")
            ttk.Checkbutton(f, text="Garder le PC éveillé pendant le farm", variable=self.eveille,
                            command=self._sauver).pack(anchor="w", pady=(0, 6))
            ttk.Checkbutton(f, text="Lancer avec Windows", variable=self.au_demarrage,
                            command=self._regler_demarrage).pack(anchor="w")
            note("Au démarrage du PC, l'appli s'ouvre cachée dans la zone de notification, attend que Steam "
                 "soit connecté, puis farme tes jeux cochés.")

            ttk.Frame(f, style="Ligne.TFrame", height=1).pack(fill="x", pady=(4, 14))
            ttk.Label(f, text="Ajouter un jeu par son AppID", style="Section.TLabel").pack(anchor="w", pady=(0, 8))
            l = ttk.Frame(f)
            l.pack(fill="x")
            self.ajout = tk.StringVar()
            e = ttk.Entry(l, textvariable=self.ajout, width=12)
            e.pack(side="left")
            e.bind("<Return>", lambda _: self._ajouter())
            ttk.Button(l, text="Ajouter", command=self._ajouter).pack(side="left", padx=(8, 0))
            note("Pour un jeu absent de la liste. L'AppID est le nombre dans l'adresse de sa page Steam "
                 "(store.steampowered.com/app/730 → 730).")

            ttk.Frame(f, style="Ligne.TFrame", height=1).pack(fill="x", pady=(4, 14))
            bas = ttk.Frame(f)
            bas.pack(fill="x", side="bottom")
            ttk.Label(bas, text=f"Version {VERSION}", style="Gris.TLabel").pack(side="left")
            ttk.Button(bas, text="Fermer", command=w.destroy).pack(side="right")
            ttk.Button(bas, text="Page du projet", command=lambda: os.startfile(PROJET)).pack(side="right", padx=8)

        # ── statistiques ──
        def _ouvrir_stats(self):
            w = self._fenetre("fen_stats", "Statistiques", (800, 760))
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
                ttk.Label(t, text=titre, style="TuileTitre.TLabel").pack(anchor="w")
                self.tuiles[cle] = ttk.Label(t, style="TuileValeur.TLabel")
                self.tuiles[cle].pack(anchor="w")
            ttk.Label(w, text="Heures farmées par jour · 30 derniers jours", style="Section.TLabel",
                      padding=(m18, int(18 * k), m18, int(6 * k))).pack(anchor="w")
            self.graphe = tk.Canvas(w, height=int(220 * k), bg=FOND, highlightthickness=0)
            self.graphe.pack(fill="x", padx=m18)
            self.graphe.bind("<Configure>", lambda e: self._dessiner_graphe())
            self.graphe.bind("<Motion>", self._survol_graphe)
            self.graphe.bind("<Leave>", lambda e: self._survol_graphe(None))
            ttk.Label(w, text="Jeux les plus farmés", style="Section.TLabel",
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
            self.tuiles["jour"].config(text=heures(jours[-1][1] / 60, "0 h 00"))
            self.tuiles["semaine"].config(text=heures(sum(j[1] for j in jours[-7:]) / 60, "0 h 00"))
            total = sum(self._total(a) for a in set(self.farme) | set(self.procs))
            self.tuiles["total"].config(text=heures(total / 60, "0 h 00"))
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
                              text="Pas encore d'heures farmées. Lance le farm pour voir tes stats ici.")
                return
            pas = next(p for p in (0.25, 0.5, 1, 2, 5, 10, 20, 25, 50, 100, 200, 250, 500, 1000)
                       if vmax / p <= 4)
            ymax = pas * math.ceil(vmax / pas)
            hauteur = H - b - t
            for i in range(int(round(ymax / pas)) + 1):
                y = H - b - i * pas / ymax * hauteur
                c.create_line(g, y, W, y, fill=BORD if i else "#3a4150")
                c.create_text(g - int(8 * k), y, text=f"{court(i * pas).replace('.', ',')} h", anchor="e", fill=GRIS,
                              font=("Segoe UI", 8))
            fente = (W - g) / len(jours)
            larg = max(3.0, min(fente * 0.62, 26 * k))
            for i, (d, v, n) in enumerate(jours):
                x0 = g + i * fente + (fente - larg) / 2
                hpx = v / 3600 / ymax * hauteur
                barre = barre_arrondie(c, x0, H - b - hpx, x0 + larg, H - b, 4 * k, fill=BARRE) if v > 0 else None
                self._barres.append((g + i * fente, g + (i + 1) * fente, d, v, n, barre, x0 + larg / 2, H - b - hpx))
                if i % 7 == len(jours) % 7 - 1 or i == len(jours) - 1:
                    c.create_text(x0 + larg / 2, H - b + int(12 * k), fill=GRIS, font=("Segoe UI", 8),
                                  text="auj." if i == len(jours) - 1 else d.strftime("%d/%m"))

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
                    texte = f"{JOURS[d.weekday()]} {d.strftime('%d/%m')} : {heures(v / 60, '0 h 00')}"
                    if n:
                        texte += f" · {n} jeu{'x' if n > 1 else ''}"
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
                              text="Aucun jeu farmé pour l'instant.")
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
                nom = self.jeux.get(a, {}).get("nom") or self.infos.get(a, (f"App {a}",))[0]
                c.create_text(int(30 * k), y, text=nom if len(nom) < 30 else nom[:28] + "…", anchor="w",
                              fill=TEXTE, font=("Segoe UI", 10))
                larg = max(4 * k, (W - nom_l - val_l) * v / vmax)
                c.create_rectangle(nom_l, y - 6 * k, nom_l + larg, y + 6 * k, fill=BARRE, width=0)
                c.create_text(nom_l + larg + int(8 * k), y, text=heures(v / 60), anchor="w", fill=DOUX,
                              font=("Segoe UI", 9))

        # ── zone de notification ──
        def _creer_tray(self):
            if pystray is None:
                return
            try:
                M = pystray.MenuItem
                menu = pystray.Menu(
                    M("Ouvrir", lambda: self.q.put(self._montrer), default=True),
                    M(lambda _: "Arrêter le farm" if self.actif else "Démarrer le farm",
                      lambda: self.q.put(self._marche_arret)),
                    M("Statistiques", lambda: self.q.put(lambda: (self._montrer(), self._ouvrir_stats()))),
                    pystray.Menu.SEPARATOR,
                    M("Quitter", lambda: self.q.put(self._quitter)))
                self.tray = pystray.Icon(NOM_RUN, dessiner_logo(64), "Farm d'heures Steam", menu)
                self.tray.run_detached()
            except Exception as e:  # noqa: BLE001
                journal(f"icône de notification impossible : {e!r}")
                self.tray = None

        def _notifier(self, texte: str):
            journal(texte)
            if self.tray:
                try:
                    self.tray.notify(texte, "Farm d'heures Steam")
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
                    self._notifier("Le farm continue. Clic sur l'icône pour rouvrir, clic droit pour quitter.")
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
                self.etat.config(text="Au démarrage de Windows : l'appli s'ouvre cachée et lance le farm des jeux cochés."
                                 if self.au_demarrage.get() else "Ne se lance plus avec Windows.")
            except OSError as e:
                self.au_demarrage.set(demarrage_windows())
                self.etat.config(text=f"Impossible de régler le démarrage : {e}")

        # ── chargement de la bibliothèque (dans un fil à part) ──
        def _recharger(self):
            self.sous_titre.config(text="lecture de ta bibliothèque…")
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
                        note = "Steam fermé ? liste " + ("du dernier lancement" if self.cfg.get("possedes")
                                                         else "non vérifiée")
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
                journal(f"chargement : {e!r}")
                self.chargement = (None, set(), {}, {}, set(), None, {}, {}, "", repr(e))

        def _appliquer_chargement(self):
            steam, jeux, infos, temps, installes, verif, chemins, icones, note, err = self.chargement
            self.chargement = None
            if err:
                self.sous_titre.config(text=f"impossible de lire Steam : {err}")
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

        def _maj_sous_titre(self, note: str = ""):
            n = sum(1 for a in self.coches if a in self.jeux)
            txt = f"{len(self.jeux)} jeux  ·  {n} coché{'s' if n > 1 else ''}"
            if note:
                self._note = note
            if getattr(self, "_note", ""):
                txt += f"  ·  {self._note}"
            self.sous_titre.config(text=txt)

        # ── calculs ──
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

        def _remboursable(self, a: int) -> bool:
            t = self.achats.get(a)
            return (bool(t) and a not in self.gratuits and a not in self.famille
                    and time.time() - t < REMB_JOURS * 86400 and self._temps_steam(a) < REMB_SECONDES)

        def _a_confirmer(self, a: int) -> bool:
            return self._remboursable(a) and a not in self.remb_ok

        def _texte_remb(self, a: int) -> str:
            jours = int((time.time() - self.achats[a]) // 86400)
            reste = max(0, REMB_SECONDES - self._temps_steam(a))
            return (f"acheté il y a {jours} jour{'s' if jours > 1 else ''}, "
                    f"encore {heures(reste / 60, '0 h 00')} avant la limite de 2 h")

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
            nom = self.jeux[a]["nom"] if a in self.jeux else ("Farm d'heures Steam" if not a else f"App {a}")
            c.itemconfig(self.b_nom, text="" if a_logo else nom, width=int(w * .6))
            c.coords(self.b_nom, marge, bas_logo)
            c.coords(self.b_stats, marge, bas_logo + int(12 * k))
            c.coords(self.b_etat, marge, bas_logo + int(34 * k))
            yb = h - int(20 * k)
            c.coords(self.b_w_obj, marge, yb)
            c.coords(self.b_w_farm, w - marge, yb)
            self.r.update_idletasks()
            c.coords(self.b_w_steam, w - marge - self.b_farm.winfo_reqwidth() - int(10 * k), yb)
            self._geo_jauge = (marge, bas_logo + int(64 * k))
            vis = "normal" if a in self.jeux else "hidden"
            for item in (self.b_w_farm, self.b_w_steam, self.b_w_obj):
                c.itemconfig(item, state=vis)
            self._charger_objectif()
            self._maj_banniere_textes()

        def _maj_banniere_textes(self):
            a, c, k = self.sel, self.ban, self.k
            if a not in self.jeux:
                c.itemconfig(self.b_stats, text="Coche des jeux dans la liste, puis Démarrer le farm.")
                c.itemconfig(self.b_etat, text="")
                for item in (self.b_piste, self.b_jauge, self.b_pct):
                    c.itemconfig(item, state="hidden")
                return
            j = self.jeux[a]
            farme = hms(self._total(a)) if a in self.procs else heures(self._total(a) / 60)
            c.itemconfig(self.b_stats, text=f"Temps Steam  {heures(self._temps_steam(a) / 60)}      "
                                            f"Farmé ici  {farme}      AppID  {a}")
            remb = self._remboursable(a)
            if a in self.procs:
                txt, coul = f"▶  En cours depuis {hms(time.time() - self.procs[a][1])}", VERT
                if remb:
                    txt, coul = txt + f"  ·  ⚠ remboursable : {self._texte_remb(a)}", ORANGE
            elif a in self.coches and self.pause_steam:
                txt, coul = "⏸  En pause : Steam est fermé, reprise automatique dès son retour", ORANGE
            elif a in self.echecs:
                txt, coul = f"✗  {self.echecs[a]} (voir journal.txt)", ROUGE
            elif a in self.essais:
                txt, coul = "↻  Steam a refusé, nouvel essai dans quelques secondes", ORANGE
            elif a in self.atteints and a not in self.coches:
                txt, coul = f"✓  Objectif atteint ({objectif_txt(self.objectifs.get(a, 0))})", VERT
            elif remb:
                txt, coul = f"⚠  Encore remboursable : {self._texte_remb(a)}", ORANGE
            elif a in self.coches:
                txt = "✓  Dans le farm" + (" — en attente d'une place (32 jeux max)" if self.actif
                                           else " — démarre avec le bouton en bas")
                coul = ACCENT
            else:
                txt, coul = "Pas dans le farm", GRIS
            c.itemconfig(self.b_etat, text=txt, fill=coul)
            self.b_farm.config(text="Retirer du farm" if a in self.coches else "＋  Ajouter au farm",
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
                c.itemconfig(self.b_pct, text=f"{int(p * 100)} %  ·  {heures(self._temps_steam(a) / 60)} "
                                              f"sur {objectif_txt(obj)}")
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
            h = nombre(self.obj_var.get())
            if h != self.objectifs.get(self.sel, 0):
                self._definir_objectif([self.sel], h)

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
            elif a in self.coches and self.pause_steam:
                f = "⏸  pause"
            elif a in self.echecs:
                f = "✗  refusé"
            elif a in self.essais:
                f = "↻  nouvel essai"
            elif a in self.coches and self._a_confirmer(a):
                f = "⚠  à confirmer"
            elif a in self.coches and self.actif:
                f = "en attente"
            else:
                f = heures(self._total(a) / 60)
            obj = self.objectifs.get(a, 0)
            if obj > 0:
                p = self._temps_steam(a) / (obj * 3600)
                o = f"✓  {objectif_txt(obj)}" if p >= 1 else f"{int(p * 100)} %  de {objectif_txt(obj)}"
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
            if a in self.coches and (self.actif or self.pause_steam):
                return ("attente",)
            return () if a in self.coches else ("off",)

        def _visible(self, a: int, q: str) -> bool:
            if q and q not in self.jeux[a]["nom"].lower() and q != str(a):
                return False
            f = self.filtre.get()
            return (f == FILTRES[0] or (f == "Cochés" and a in self.coches) or (f == "En cours" and a in self.procs)
                    or (f == "Avec objectif" and a in self.objectifs) or (f == "Remboursables" and self._remboursable(a))
                    or (f == "Jamais lancés" and not self.jeux[a]["min"]) or (f == "Installés" and a in self.installes))

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
                self.arbre.heading(col, text=titre + fleche)

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
                m.add_command(label=f"Ajouter les {len(ids)} au farm",
                              command=lambda: self._basculer_jeux([i for i in ids if i not in self.coches]))
                m.add_command(label=f"Retirer les {len(ids)} du farm",
                              command=lambda: self._basculer_jeux([i for i in ids if i in self.coches]))
            else:
                m.add_command(label="Retirer du farm" if a in self.coches else "Ajouter au farm",
                              command=lambda: self._basculer_jeux([a]))
                m.add_command(label="Farmer uniquement celui-ci", command=lambda: self._uniquement(a))
            m.add_command(label="Définir un objectif d'heures…", command=lambda: self._objectif_dialogue(ids))
            m.add_separator()
            m.add_command(label="Ouvrir dans la bibliothèque Steam",
                          command=lambda: self._ouvrir("steam://nav/games/details/{}", a))
            m.add_command(label="Page du magasin",
                          command=lambda: self._ouvrir("https://store.steampowered.com/app/{}", a))
            m.add_command(label="Copier l'AppID", command=lambda: (self.r.clipboard_clear(),
                                                                   self.r.clipboard_append(str(a))))
            if a in self.cfg.get("manuels", []):
                m.add_separator()
                m.add_command(label="Retirer de la liste", command=lambda: self._retirer_manuel(a))
            m.tk_popup(e.x_root, e.y_root)

        def _objectif_dialogue(self, ids):
            actuel = self.objectifs.get(ids[0], 0)
            qui = self.jeux[ids[0]]["nom"] if len(ids) == 1 else f"ces {len(ids)} jeux"
            choix, txt = self._dialogue("Objectif d'heures",
                                        f"Temps de jeu Steam total à atteindre pour {qui}, en heures.\n"
                                        "Quand il est atteint, le jeu sort du farm tout seul. 0 = pas d'objectif.",
                                        [("Annuler", "TButton"), ("Valider", "Accent.TButton")],
                                        saisie=court(actuel) if actuel else "")
            if choix == 1:
                self._definir_objectif(ids, nombre(txt))
                self._charger_objectif()

        def _ouvrir(self, modele: str, a: int | None = None):
            a = a or self.sel
            if a:
                try:
                    os.startfile(modele.format(a))
                except OSError as e:
                    self.etat.config(text=f"Impossible d'ouvrir : {e}")

        # ── cocher / décocher ──
        def _basculer_selection(self):
            if self.sel in self.jeux:
                self._basculer_jeux([self.sel])

        def _confirmer_remboursables(self, ids: list[int]) -> list[int]:
            """Demande quoi faire des jeux encore remboursables. Renvoie ceux à garder dans le farm."""
            remb = [a for a in ids if self._a_confirmer(a)]
            if not remb:
                return ids
            lignes = "\n".join(f"•  {self.jeux[a]['nom']} : {self._texte_remb(a)}" for a in remb[:8])
            if len(remb) > 8:
                lignes += f"\n•  … et {len(remb) - 8} autres"
            un = len(remb) == 1
            choix, _ = self._dialogue(
                "Jeux encore remboursables",
                f"{'Ce jeu est encore remboursable' if un else 'Ces jeux sont encore remboursables'} sur Steam "
                f"(moins de {REMB_JOURS} jours après l'achat et moins de 2 h de jeu) :\n\n{lignes}\n\n"
                "Au-delà de 2 h de jeu, Steam ne rembourse plus.",
                [("Ne pas farmer", "TButton"), ("Farmer jusqu'à 1 h 50", "TButton"),
                 ("Farmer quand même", "Accent.TButton")])
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
                self.etat.config(text=f"Objectif déjà atteint pour {self.jeux[fini[0]]['nom']}"
                                      + (f" (et {len(fini) - 1} autre(s))" if len(fini) > 1 else "")
                                      + " : augmente l'objectif ou mets-le à 0 pour le farmer.")
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
            if self.filtre.get() in ("Cochés", "En cours"):
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
            m.add_command(label="Enregistrer les jeux cochés comme groupe…", command=self._enregistrer_groupe,
                          state="normal" if self.coches else "disabled")
            if self.groupes:
                m.add_separator()
                for nom in sorted(self.groupes, key=str.lower):
                    n = len(self.groupes[nom])
                    m.add_command(label=f"{nom}   ({n} jeu{'x' if n > 1 else ''})",
                                  command=lambda nom=nom: self._appliquer_groupe(nom))
                    sup.add_command(label=nom, command=lambda nom=nom: self._supprimer_groupe(nom))
                m.add_separator()
                m.add_cascade(label="Supprimer un groupe", menu=sup)
            b = self.b_groupes
            m.tk_popup(b.winfo_rootx(), b.winfo_rooty() + b.winfo_height())

        def _enregistrer_groupe(self):
            n = len(self.coches)
            choix, nom = self._dialogue("Nouveau groupe",
                                        f"Nom du groupe ({n} jeu{'x' if n > 1 else ''} coché{'s' if n > 1 else ''}) :",
                                        [("Annuler", "TButton"), ("Enregistrer", "Accent.TButton")], saisie="")
            if choix == 1 and nom:
                self.groupes[nom] = list(self.coches)
                self._sauver()
                self.etat.config(text=f"Groupe « {nom} » enregistré. Menu Groupes pour le recocher en un clic.")

        def _appliquer_groupe(self, nom: str):
            cible = [a for a in self.groupes.get(nom, []) if a in self.jeux]
            self._basculer_jeux([a for a in self.coches if a not in cible] + [a for a in cible if a not in self.coches])
            self.etat.config(text=f"Groupe « {nom} » : {len(cible)} jeu{'x' if len(cible) > 1 else ''} coché"
                                  f"{'s' if len(cible) > 1 else ''}.")

        def _supprimer_groupe(self, nom: str):
            self.groupes.pop(nom, None)
            self._sauver()
            self.etat.config(text=f"Groupe « {nom} » supprimé.")

        def _ajouter(self):
            t = self.ajout.get().strip()
            if not t.isdigit():
                self.etat.config(text="Un AppID est un nombre : il est dans l'adresse de la page Steam du jeu.")
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
            self.etat.config(text=f"{self.jeux[a]['nom']} ajouté au farm.")

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
            if not self.actif or self.pause_steam:
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
            p[0].terminate()
            try:
                p[0].wait(timeout=3)
            except subprocess.TimeoutExpired:
                pass
            self._maj_ligne(a)

        def _marche_arret(self, interactif: bool = True):
            if self.actif:
                self._tout_arreter("Farm arrêté.")
                return
            a_lancer = [a for a in self.coches if a in self.jeux]
            if not a_lancer:
                self.etat.config(text="Coche d'abord au moins un jeu (clic sur la case à gauche de son logo).")
                return
            if not DLL.exists():
                if interactif:
                    messagebox.showerror("Farm d'heures Steam", "steam_api64.dll introuvable.\n"
                                         "Installe n'importe quel jeu Steam puis clique Recharger.")
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
            self.pause_steam = None if steam_pret() else now
            if self.eveille.get():
                garder_eveille(True)
            self._remplir_creneaux()
            self.bouton.config(text="■  Arrêter le farm", style="Arret.TButton")
            self._remplir()
            self._maj_sous_titre()
            self._maj_banniere_textes()

        def _tout_arreter(self, message: str):
            for a in list(self.procs):
                self._arreter(a)
            self.actif = False
            self.pause_steam = self.steam_revenu = None
            self.essais.clear()
            garder_eveille(False)
            self.bouton.config(text="▶  Démarrer le farm", style="Accent.TButton")
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
                        self.echecs[a] = "refusé par Steam"
                else:
                    self.essais.pop(a, None)
                    self.echecs[a] = f"arrêté ({code})"
                self._maj_ligne(a)
            if self.actif:
                self._surveiller_steam(now, steam_ferme)
                if not self.pause_steam:
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
                    self.etat.config(text=f"Total farmé avec l'appli : {heures(total / 60, '0 h 00')}")
                self._titre_tray("Farm d'heures Steam")
            for a in self.procs:
                self._maj_ligne(a)
            self._maj_banniere_textes()
            self._demarrage_auto()

        def _surveiller_steam(self, now: float, steam_ferme: bool):
            """Met le farm en pause si Steam se ferme, et le relance tout seul quand Steam revient."""
            if steam_ferme or not steam_pret():
                self.steam_revenu = None
                if not self.pause_steam:
                    self.pause_steam = now
                    for a in list(self.procs):
                        self._arreter(a)
                    self._notifier("Steam s'est fermé : le farm est en pause et reprendra tout seul.")
                    self._remplir()
            elif self.pause_steam:
                self.steam_revenu = self.steam_revenu or now
                if now - self.steam_revenu >= PAUSE_STEAM:
                    self.pause_steam = self.steam_revenu = None
                    self.essais.clear()
                    self.echecs.clear()
                    self._remplir_creneaux()
                    self._notifier("Steam est revenu : le farm a repris.")
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
                        self._notifier(f"Objectif atteint : {self.jeux[a]['nom']} "
                                       f"({objectif_txt(self.objectifs[a])}). Il sort du farm.")
                    self._sauver()
                    if not any(b in self.jeux for b in self.coches):
                        self._tout_arreter("Tous les jeux cochés ont atteint leur objectif : farm terminé.")
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
            journal("rotation : groupe de jeux suivant")
            self._remplir_creneaux()
            self._maj_lignes_cochees()

        def _etat_farm(self, now: float):
            limite = nombre(self.duree.get()) * 3600
            ecoule = now - self.debut
            gagne = sum(self._total(a) for a in self.jeux) - self.total_depart
            if limite > 0 and ecoule >= limite:
                self._tout_arreter(f"Terminé : {hms(ecoule)} de farm, {heures(gagne / 60, '0 h 00')} gagnées au total.")
                self._notifier("Arrêt automatique : la durée de farm choisie est écoulée.")
                return
            n = len(self.procs)
            if self.pause_steam:
                txt = "⏸  Steam est fermé : le farm reprendra tout seul dès que Steam sera de retour"
                if self.steam_revenu:
                    txt = f"↻  Steam est revenu, reprise dans {max(0, int(PAUSE_STEAM - (now - self.steam_revenu)))} s"
            else:
                attente = len([a for a in self.coches if a in self.jeux and a not in self.procs])
                txt = f"▶  {n} jeu{'x' if n > 1 else ''} en cours"
                if attente:
                    txt += f" · {attente} en attente"
                txt += f"  ·  {hms(ecoule)}  ·  +{heures(gagne / 60, '0 h 00')} gagnées"
                if limite > 0:
                    txt += f"  ·  arrêt dans {hms(limite - ecoule)}"
                if self.echecs:
                    txt += f"  ·  {len(self.echecs)} refusé(s)"
            self.etat.config(text=txt)
            self._titre_tray(f"Farm d'heures Steam — {n} jeu{'x' if n > 1 else ''} en cours"
                             if not self.pause_steam else "Farm d'heures Steam — en pause (Steam fermé)")

        def _titre_tray(self, titre: str):
            if self.tray and self.tray.title != titre:
                self.tray.title = titre

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

        def _sauver(self):
            self._derniere_sauvegarde = time.time()
            limite = (dt.date.today() - dt.timedelta(days=400)).isoformat()
            self.historique = {j: v for j, v in self.historique.items() if j >= limite}
            self.cfg.update(
                coches=list(self.coches), heures=self.duree.get(), rotation=self.rotation.get(),
                eveille=self.eveille.get(),
                farme={str(a): round(self._total(a)) for a in set(self.farme) | set(self.procs)},
                objectifs={str(a): h for a, h in self.objectifs.items()}, atteints=sorted(self.atteints),
                achats={str(a): t for a, t in self.achats.items()}, famille=sorted(self.famille),
                remb_ok=sorted(self.remb_ok), groupes=self.groupes,
                historique={j: {a: round(s) for a, s in v.items()} for j, v in self.historique.items()})
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


def main():
    args = sys.argv[1:]
    if args[:1] == ["--farm"]:
        sys.exit(mode_farm(int(args[1]), int(args[2])))
    if args[:1] == ["--verif"]:
        sys.exit(mode_verif(Path(args[1]), Path(args[2])))
    if deja_ouvert():
        SIGNAL.touch()
        return
    interface(auto="--auto" in args)


if __name__ == "__main__":
    main()
