"""
Farm d'heures Steam
-------------------
Fait tourner tes jeux Steam « à vide » : Steam croit que le jeu est lancé et
compte le temps de jeu (et les drops de cartes), sans rien exécuter du jeu.

Chaque jeu farmé est un petit processus qui s'annonce à Steam via
steam_api64.dll (la DLL officielle de Valve, copiée depuis un de tes jeux).

Lancement : double-clic sur Lancer.bat (installe Pillow et pystray au premier lancement)
            ou : .venv\\Scripts\\pythonw.exe farm.py   — option --auto : caché + farm auto
Steam doit être ouvert et connecté.
"""
from __future__ import annotations

import ctypes
import json
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

ICI = Path(__file__).resolve().parent
SCRIPT = str(Path(__file__).resolve())
DLL = ICI / "steam_api64.dll"
CONFIG = ICI / "config.json"
JOURNAL = ICI / "journal.txt"
SIGNAL = ICI / ".afficher"        # un 2e lancement demande à la fenêtre déjà ouverte de se montrer
CLE_RUN = r"Software\Microsoft\Windows\CurrentVersion\Run"
NOM_RUN = "FarmHeuresSteam"
MAX_SIMULTANES = 32               # Steam refuse au-delà de 32 jeux lancés en même temps
APP_TEST = 480                    # Spacewar, gratuit pour tous : sert à interroger Steam
SANS_FENETRE = 0x08000000         # CREATE_NO_WINDOW

FOND, PANNEAU, BORD = "#121419", "#1b1f27", "#2a303b"
TEXTE, DOUX, GRIS = "#eef0f4", "#c3c8d2", "#8b93a3"
ACCENT, ACCENT_CLAIR, ENCRE = "#66c0f4", "#93d5fb", "#0b1220"
VERT, ROUGE, SELECTION = "#7bd88f", "#f08080", "#263850"
FILTRES = ("Tous les jeux", "Cochés", "En cours", "Jamais lancés", "Installés")


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


# ───────────────────────── Lecture des fichiers de Steam ─────────────────────────

def dossier_steam() -> Path:
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam") as k:
        return Path(winreg.QueryValueEx(k, "SteamPath")[0])


def utilisateur_actif() -> int:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam\ActiveProcess") as k:
            return int(winreg.QueryValueEx(k, "ActiveUser")[0])
    except OSError:
        return 0


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


def infos_applis(steam: Path) -> dict[int, tuple[str, str]]:
    """appid -> (nom, type) lus dans appcache/appinfo.vdf (format binaire de Steam)."""
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
            elif t in (2, 3, 4, 6):
                v, p = None, p + 4
            elif t in (7, 10):
                v, p = None, p + 8
            else:
                raise ValueError(f"type {t}")
            d[k.lower()] = v

    # après appid+taille : état, maj, jeton(8), sha1(20), n° de changement, [sha1 binaire(20)]
    entete = 40 + (0 if magique == 0x07564427 else 20)
    out: dict[int, tuple[str, str]] = {}
    while True:
        appid, taille = struct.unpack_from("<II", data, pos)
        if appid == 0:
            break
        try:
            d, _ = kv(pos + 8 + entete)
            c = (d.get("appinfo") or {}).get("common") or {}
            if c.get("name"):
                out[appid] = (c["name"], (c.get("type") or "").lower())
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
    """Copie à côté du script une steam_api64.dll prise dans un jeu installé, si besoin."""
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
    """Demande à Steam lesquels des appids donnés sont possédés par le compte."""
    os.environ["SteamAppId"] = os.environ["SteamGameId"] = str(APP_TEST)
    api, ok, msg = _steam_api()
    if not ok:
        sortie.write_text(json.dumps({"erreur": msg}), encoding="utf-8")
        return 4
    nom = next(n for n in ("SteamAPI_SteamApps_v008", "SteamAPI_SteamApps_v009") if hasattr(api, n))
    fabrique = getattr(api, nom)
    fabrique.restype = ctypes.c_void_p
    apps = fabrique()
    possede = api.SteamAPI_ISteamApps_BIsSubscribedApp
    possede.restype = ctypes.c_bool
    possede.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    ids = json.loads(entree.read_text(encoding="utf-8"))
    sortie.write_text(json.dumps({"possedes": [i for i in ids if possede(apps, i)]}), encoding="utf-8")
    api.SteamAPI_Shutdown()
    return 0


def verifier_possession(ids: list[int]) -> set[int] | None:
    entree, sortie = ICI / "_verif_in.json", ICI / "_verif_out.json"
    entree.write_text(json.dumps(ids), encoding="utf-8")
    sortie.unlink(missing_ok=True)
    env = dict(os.environ, SteamAppId=str(APP_TEST), SteamGameId=str(APP_TEST))
    try:
        subprocess.run([interpreteur_sans_console(base=True), SCRIPT, "--verif", str(entree), str(sortie)],
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
    return set(res["possedes"])


# ───────────────────────── Petits outils ─────────────────────────

def hms(s: float) -> str:
    s = int(s)
    return f"{s // 3600}:{s // 60 % 60:02d}:{s % 60:02d}"


def heures(minutes: float, vide: str = "—") -> str:
    minutes = int(minutes)
    return f"{minutes // 60} h {minutes % 60:02d}" if minutes else vide


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
                              f'"{interpreteur_sans_console(base=False)}" "{SCRIPT}" --auto')
        else:
            try:
                winreg.DeleteValue(k, NOM_RUN)
            except FileNotFoundError:
                pass


_MUTEX = None


def deja_ouvert() -> bool:
    global _MUTEX
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateMutexW.restype = wintypes.HANDLE
    k32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
    _MUTEX = k32.CreateMutexW(None, False, "Local\\FarmHeuresSteam")
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
            self.k = r.winfo_fpixels("1i") / 96          # facteur d'échelle (écrans à 125 %, 150 %…)
            self.steam: Path | None = None
            self.jeux: dict[int, dict] = {}               # appid -> {"nom", "min"}
            self.infos: dict[int, tuple[str, str]] = {}
            self.installes: set[int] = set()
            self.chemins: dict[int, dict] = {}
            self.icones: dict[int, Image.Image] = {}
            self.images: dict[tuple, ImageTk.PhotoImage] = {}
            self.coches: set[int] = set(self.cfg.get("coches", []))
            self.farme: dict[int, float] = {int(a): s for a, s in self.cfg.get("farme", {}).items()}
            self.procs: dict[int, tuple[subprocess.Popen, float]] = {}
            self.echecs: dict[int, str] = {}
            self.actif, self.debut, self.total_depart = False, 0.0, 0.0
            self.tri = ("nom", False)
            self.sel: int | None = None
            self.chargement = None
            self.q: queue.Queue = queue.Queue()
            self.auto, self.steam_pret = auto, None
            self.tray, self.notifie = None, False
            self._rendu_prevu = None
            self._masque = None
            self._derniere_sauvegarde = time.time()
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
            r.geometry(f"{int(980 * k)}x{int(720 * k)}")
            r.minsize(int(820 * k), int(560 * k))
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
            s.configure("Treeitem.padding", padding=(int(8 * k), 0))
            s.configure("Treeview.Heading", background=FOND, foreground=GRIS, relief="flat",
                        font=("Segoe UI", 9, "bold"), padding=(int(10 * k), 6))
            s.map("Treeview.Heading", background=[("active", PANNEAU)])
            s.configure("TEntry", fieldbackground=PANNEAU, foreground=TEXTE, insertcolor=TEXTE, padding=6)
            s.configure("TSpinbox", fieldbackground=PANNEAU, foreground=TEXTE, arrowcolor=TEXTE,
                        insertcolor=TEXTE, padding=4)
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
            s.map("TButton", background=[("active", BORD)])
            s.configure("Accent.TButton", background=ACCENT, foreground=ENCRE,
                        font=("Segoe UI", 10, "bold"), padding=(18, 8))
            s.map("Accent.TButton", background=[("active", ACCENT_CLAIR)])
            s.configure("Arret.TButton", background=ROUGE, foreground=ENCRE,
                        font=("Segoe UI", 10, "bold"), padding=(18, 8))
            s.map("Arret.TButton", background=[("active", "#f5a3a3")])
            s.configure("Verre.TButton", background="#262c36", foreground=TEXTE, padding=(14, 7))
            s.map("Verre.TButton", background=[("active", "#323a47")])
            s.configure("TCheckbutton", background=FOND, foreground=DOUX, indicatorcolor=PANNEAU)
            s.map("TCheckbutton", background=[("active", FOND)], indicatorcolor=[("selected", ACCENT)])
            s.configure("Vertical.TScrollbar", background=PANNEAU, troughcolor=FOND, arrowcolor=GRIS, borderwidth=0)
            s.configure("Titre.TLabel", font=("Segoe UI Semibold", 13))
            s.configure("Gris.TLabel", foreground=GRIS)
            s.configure("Doux.TLabel", foreground=DOUX)
            s.configure("Ligne.TFrame", background=BORD)
            try:  # barre de titre sombre sous Windows 11
                r.update_idletasks()
                v = ctypes.c_int(1)
                ctypes.windll.dwmapi.DwmSetWindowAttribute(ctypes.windll.user32.GetParent(r.winfo_id()),
                                                           20, ctypes.byref(v), 4)
            except Exception:
                pass

        def _construire(self):
            r, k = self.r, self.k
            # barre du haut
            haut = ttk.Frame(r, padding=(int(18 * k), int(10 * k), int(18 * k), int(8 * k)))
            haut.pack(fill="x")
            ttk.Label(haut, text="Farm d'heures Steam", style="Titre.TLabel").pack(side="left")
            self.sous_titre = ttk.Label(haut, style="Gris.TLabel")
            self.sous_titre.pack(side="left", padx=(14, 0), pady=(3, 0))
            ttk.Button(haut, text="↻  Recharger", command=self._recharger).pack(side="right")

            # bannière du jeu sélectionné
            self.ban = tk.Canvas(r, height=int(220 * k), bg=FOND, highlightthickness=0, bd=0)
            self.ban.pack(fill="x")
            self.ban.bind("<Configure>", lambda e: self._planifier_banniere())
            self.b_img = self.ban.create_image(0, 0, anchor="nw")
            self.b_nom = self.ban.create_text(0, 0, anchor="sw", fill=TEXTE, font=("Segoe UI Semibold", 22))
            self.b_stats = self.ban.create_text(0, 0, anchor="nw", fill=DOUX, font=("Segoe UI", 10))
            self.b_etat = self.ban.create_text(0, 0, anchor="nw", fill=GRIS, font=("Segoe UI Semibold", 10))
            self.b_farm = ttk.Button(self.ban, style="Accent.TButton", width=17, text="＋  Ajouter au farm",
                                     command=self._basculer_selection)
            self.b_steam = ttk.Button(self.ban, text="Ouvrir dans Steam", style="Verre.TButton",
                                      command=lambda: self._ouvrir("steam://nav/games/details/{}"))
            self.b_w_farm = self.ban.create_window(0, 0, anchor="se", window=self.b_farm)
            self.b_w_steam = self.ban.create_window(0, 0, anchor="se", window=self.b_steam)

            # recherche + filtre
            barre = ttk.Frame(r, padding=(int(18 * k), int(4 * k), int(18 * k), int(8 * k)))
            barre.pack(fill="x")
            self.recherche = tk.StringVar()
            self.recherche.trace_add("write", lambda *_: self._remplir())
            ttk.Label(barre, text="Rechercher", style="Doux.TLabel").pack(side="left", padx=(0, 8))
            self.champ = ttk.Entry(barre, textvariable=self.recherche)
            self.champ.pack(side="left", fill="x", expand=True)
            self.filtre = tk.StringVar(value=FILTRES[0])
            cb = ttk.Combobox(barre, textvariable=self.filtre, values=FILTRES, state="readonly", width=14)
            cb.pack(side="left", padx=(10, 0))
            cb.bind("<<ComboboxSelected>>", lambda e: (self._remplir(), self.arbre.focus_set()))
            ttk.Button(barre, text="Tout décocher", command=self._tout_decocher).pack(side="left", padx=(10, 0))

            # pied (placé avant la liste pour qu'il reste visible quand on rétrécit la fenêtre)
            pied = ttk.Frame(r, padding=(int(18 * k), int(8 * k), int(18 * k), int(14 * k)))
            pied.pack(side="bottom", fill="x")
            self.etat = ttk.Label(pied, style="Doux.TLabel")
            self.etat.pack(side="left", fill="x", expand=True)
            self.bouton = ttk.Button(pied, text="▶  Démarrer le farm", style="Accent.TButton",
                                     command=self._marche_arret)
            self.bouton.pack(side="right")

            opts = ttk.Frame(r, padding=(int(18 * k), int(8 * k), int(18 * k), 0))
            opts.pack(side="bottom", fill="x")
            ttk.Label(opts, text="Ajouter un AppID", style="Doux.TLabel").pack(side="left")
            self.ajout = tk.StringVar()
            e = ttk.Entry(opts, textvariable=self.ajout, width=10)
            e.pack(side="left", padx=6)
            e.bind("<Return>", lambda _: self._ajouter())
            ttk.Button(opts, text="Ajouter", command=self._ajouter).pack(side="left")
            self.au_demarrage = tk.BooleanVar(value=demarrage_windows())
            ttk.Checkbutton(opts, text="Lancer avec Windows", variable=self.au_demarrage,
                            command=self._regler_demarrage).pack(side="right")
            self.eveille = tk.BooleanVar(value=self.cfg.get("eveille", True))
            ttk.Checkbutton(opts, text="PC éveillé", variable=self.eveille,
                            command=self._sauver).pack(side="right", padx=(0, 16))
            self.duree = tk.StringVar(value=str(self.cfg.get("heures", "0")))
            ttk.Label(opts, text="h", style="Doux.TLabel").pack(side="right", padx=(4, 16))
            sp = ttk.Spinbox(opts, from_=0, to=9999, width=4, textvariable=self.duree, command=self._sauver)
            sp.pack(side="right")
            sp.bind("<FocusOut>", lambda _: self._sauver())
            ttk.Label(opts, text="Arrêt auto après", style="Doux.TLabel").pack(side="right", padx=(0, 6))
            ttk.Frame(r, style="Ligne.TFrame", height=1).pack(side="bottom", fill="x", padx=int(18 * k))

            # liste
            milieu = ttk.Frame(r, padding=(int(18 * k), 0, int(18 * k), 0))
            milieu.pack(fill="both", expand=True)
            self.arbre = ttk.Treeview(milieu, columns=("id", "temps", "farme"), show="tree headings",
                                      selectmode="extended")
            self.arbre.heading("#0", text="Jeu", anchor="w", command=lambda: self._trier("nom"))
            self.arbre.column("#0", width=int(420 * k), stretch=True)
            for col, titre, larg in (("id", "AppID", 90), ("temps", "Temps Steam", 120),
                                     ("farme", "Farmé ici", 130)):
                self.arbre.heading(col, text=titre, anchor="e", command=lambda c=col: self._trier(c))
                self.arbre.column(col, width=int(larg * k), anchor="e", stretch=False)
            sb = ttk.Scrollbar(milieu, orient="vertical", command=self.arbre.yview)
            self.arbre.configure(yscrollcommand=sb.set)
            self.arbre.pack(side="left", fill="both", expand=True)
            sb.pack(side="right", fill="y")
            self.arbre.tag_configure("encours", foreground=VERT)
            self.arbre.tag_configure("echec", foreground=ROUGE)
            self.arbre.tag_configure("off", foreground=DOUX)
            self.arbre.bind("<Button-1>", self._clic)
            self.arbre.bind("<Double-1>", self._double)
            self.arbre.bind("<Button-3>", self._menu_contextuel)
            self.arbre.bind("<space>", lambda e: (self._basculer_jeux(map(int, self.arbre.selection())), "break")[1])
            self.arbre.bind("<<TreeviewSelect>>", self._selection)
            r.bind("<Control-f>", lambda e: (self.champ.focus_set(), self.champ.select_range(0, "end")))
            self.champ.bind("<Escape>", lambda e: self.recherche.set(""))

            self.menu = tk.Menu(r, tearoff=0, bg=PANNEAU, fg=TEXTE, activebackground=SELECTION,
                                activeforeground=TEXTE, bd=0, relief="flat", font=("Segoe UI", 10))

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
                    pystray.Menu.SEPARATOR,
                    M("Quitter", lambda: self.q.put(self._quitter)))
                self.tray = pystray.Icon(NOM_RUN, dessiner_logo(64), "Farm d'heures Steam", menu)
                self.tray.run_detached()
            except Exception as e:  # noqa: BLE001
                journal(f"icône de notification impossible : {e!r}")
                self.tray = None

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
                    try:
                        self.tray.notify("Le farm continue. Clic sur l'icône pour rouvrir, clic droit pour quitter.",
                                         "Farm d'heures Steam")
                    except Exception:
                        pass
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
                note, possedes = "", None
                if preparer_dll(steam):
                    possedes = verifier_possession(sorted(jeux))
                    if possedes is None:
                        note = "Steam fermé ? liste " + ("du dernier lancement" if self.cfg.get("possedes")
                                                         else "non vérifiée")
                else:
                    note = "steam_api64.dll introuvable : le farm ne pourra pas démarrer"
                filtre = possedes if possedes is not None else set(self.cfg.get("possedes", [])) or None
                if filtre is not None:
                    jeux &= filtre
                jeux |= set(self.cfg.get("manuels", []))
                s = int(24 * self.k)
                chemins = {a: chemins_images(steam, a) for a in jeux}
                icones = {a: icone_jeu(chemins[a].get("icone"), infos.get(a, ("?",))[0], s) for a in jeux}
                self.chargement = (steam, jeux, infos, temps, installes, possedes, chemins, icones, note, None)
            except Exception as e:  # noqa: BLE001 — affiché dans la fenêtre
                journal(f"chargement : {e!r}")
                self.chargement = (None, set(), {}, {}, set(), None, {}, {}, "", repr(e))

        def _appliquer_chargement(self):
            steam, jeux, infos, temps, installes, possedes, chemins, icones, note, err = self.chargement
            self.chargement = None
            if err:
                self.sous_titre.config(text=f"impossible de lire Steam : {err}")
                return
            self.steam, self.infos, self.installes = steam, infos, installes
            self.chemins.update(chemins)
            self.icones.update(icones)
            self.images.clear()
            self.jeux = {a: {"nom": infos.get(a, (f"App {a}", ""))[0], "min": temps.get(a, 0)} for a in jeux}
            if possedes is not None:
                self.cfg["possedes"] = sorted(possedes)
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
            vert = Image.linear_gradient("L").resize((w, h)).point(lambda v: max(0, v - 120) * 255 // 135)
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
            bas_logo = int(h * .60)
            a_logo = False
            if ch.get("logo"):
                try:
                    lg = Image.open(ch["logo"]).convert("RGBA")
                    lg.thumbnail((int(w * .34), bas_logo - int(18 * k)), Image.LANCZOS)
                    base.paste(lg, (marge, bas_logo - lg.height), lg)
                    a_logo = True
                except OSError:
                    pass
            self._ban_tk = ImageTk.PhotoImage(base)
            c.itemconfig(self.b_img, image=self._ban_tk)
            nom = self.jeux[a]["nom"] if a in self.jeux else ("Farm d'heures Steam" if not a else f"App {a}")
            c.itemconfig(self.b_nom, text="" if a_logo else nom, width=int(w * .6))
            c.coords(self.b_nom, marge, bas_logo)
            c.coords(self.b_stats, marge, bas_logo + int(14 * k))
            c.coords(self.b_etat, marge, bas_logo + int(38 * k))
            c.coords(self.b_w_farm, w - marge, h - int(22 * k))
            self.r.update_idletasks()
            c.coords(self.b_w_steam, w - marge - self.b_farm.winfo_reqwidth() - int(10 * k), h - int(22 * k))
            vis = "normal" if a in self.jeux else "hidden"
            c.itemconfig(self.b_w_farm, state=vis)
            c.itemconfig(self.b_w_steam, state=vis)
            self._maj_banniere_textes()

        def _maj_banniere_textes(self):
            a, c = self.sel, self.ban
            if a not in self.jeux:
                c.itemconfig(self.b_stats, text="Coche des jeux dans la liste, puis Démarrer le farm.")
                c.itemconfig(self.b_etat, text="")
                return
            j = self.jeux[a]
            farme = hms(self._total(a)) if a in self.procs else heures(self._total(a) / 60)
            c.itemconfig(self.b_stats, text=f"Temps Steam  {heures(j['min'])}      Farmé ici  {farme}      AppID  {a}")
            if a in self.procs:
                txt, coul = f"▶  En cours depuis {hms(time.time() - self.procs[a][1])}", VERT
            elif a in self.echecs:
                txt, coul = f"✗  {self.echecs[a]} (voir journal.txt)", ROUGE
            elif a in self.coches:
                txt, coul = "✓  Dans le farm" + ("" if self.actif else " — démarre avec le bouton en bas"), ACCENT
            else:
                txt, coul = "Pas dans le farm", GRIS
            c.itemconfig(self.b_etat, text=txt, fill=coul)
            self.b_farm.config(text="Retirer du farm" if a in self.coches else "＋  Ajouter au farm",
                               style="Verre.TButton" if a in self.coches else "Accent.TButton")

        # ── liste ──
        def _total(self, a: int) -> float:
            p = self.procs.get(a)
            return self.farme.get(a, 0) + (time.time() - p[1] if p else 0)

        def _valeurs(self, a: int):
            j = self.jeux[a]
            if a in self.procs:
                f = "▶  " + hms(self._total(a))
            elif a in self.echecs:
                f = "✗  refusé"
            else:
                f = heures(self._total(a) / 60)
            return (a, heures(j["min"]), f)

        def _tags(self, a: int):
            if a in self.procs:
                return ("encours",)
            if a in self.echecs:
                return ("echec",)
            return () if a in self.coches else ("off",)

        def _visible(self, a: int, q: str) -> bool:
            if q and q not in self.jeux[a]["nom"].lower() and q != str(a):
                return False
            f = self.filtre.get()
            return (f == FILTRES[0] or (f == "Cochés" and a in self.coches) or (f == "En cours" and a in self.procs)
                    or (f == "Jamais lancés" and not self.jeux[a]["min"]) or (f == "Installés" and a in self.installes))

        def _remplir(self):
            q = self.recherche.get().strip().lower()
            sel = self.arbre.selection()
            self.arbre.delete(*self.arbre.get_children())
            cle, inverse = self.tri
            cles = {"nom": lambda a: self.jeux[a]["nom"].lower(), "id": lambda a: a,
                    "temps": lambda a: self.jeux[a]["min"], "farme": self._total}
            for a in sorted(self.jeux, key=cles[cle], reverse=inverse):
                if self._visible(a, q):
                    self.arbre.insert("", "end", iid=str(a), text="  " + self.jeux[a]["nom"],
                                      image=self._image_ligne(a), values=self._valeurs(a), tags=self._tags(a))
            garde = [i for i in sel if self.arbre.exists(i)]
            if garde:
                self.arbre.selection_set(garde)
            for col, titre in (("#0", "Jeu"), ("id", "AppID"), ("temps", "Temps Steam"), ("farme", "Farmé ici")):
                fleche = (" ▼" if inverse else " ▲") if (cle == "nom" and col == "#0") or cle == col else ""
                self.arbre.heading(col, text=titre + fleche)

        def _maj_ligne(self, a: int):
            if a in self.jeux and self.arbre.exists(str(a)):
                self.arbre.item(str(a), image=self._image_ligne(a), values=self._valeurs(a), tags=self._tags(a))

        def _trier(self, col: str):
            self.tri = (col, not self.tri[1] if self.tri[0] == col else col in ("temps", "farme"))
            self._remplir()

        def _selection(self, _=None):
            s = self.arbre.selection()
            a = int(s[0]) if s else None
            if a != self.sel and a is not None:
                self.sel = a
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

        def _ouvrir(self, modele: str, a: int | None = None):
            a = a or self.sel
            if a:
                try:
                    os.startfile(modele.format(a))
                except OSError as e:
                    self.etat.config(text=f"Impossible d'ouvrir : {e}")

        def _basculer_selection(self):
            if self.sel in self.jeux:
                self._basculer_jeux([self.sel])

        def _basculer_jeux(self, ids):
            for a in list(ids):
                if a in self.coches:
                    self.coches.discard(a)
                    self._arreter(a)
                else:
                    self.coches.add(a)
                    if self.actif:
                        self._lancer(a)
                if a not in self.coches:
                    self.echecs.pop(a, None)
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

        def _ajouter(self):
            t = self.ajout.get().strip()
            if not t.isdigit():
                self.etat.config(text="Un AppID est un nombre : il est dans l'adresse de la page Steam du jeu.")
                return
            a = int(t)
            self.cfg.setdefault("manuels", [])
            if a not in self.cfg["manuels"]:
                self.cfg["manuels"].append(a)
            self.jeux.setdefault(a, {"nom": self.infos.get(a, (f"App {a}", ""))[0], "min": 0})
            self.ajout.set("")
            if a not in self.coches:
                self._basculer_jeux([a])
            self._remplir()
            if self.arbre.exists(str(a)):
                self.arbre.see(str(a))
                self.arbre.selection_set(str(a))

        def _retirer_manuel(self, a: int):
            if a in self.coches:
                self._basculer_jeux([a])
            self.cfg["manuels"].remove(a)
            if a not in self.cfg.get("possedes", []):
                self.jeux.pop(a, None)
            self._sauver()
            self._remplir()

        # ── farm ──
        def _lancer(self, a: int):
            if a in self.procs:
                return
            if len(self.procs) >= MAX_SIMULTANES:
                self.etat.config(text=f"Steam limite à {MAX_SIMULTANES} jeux en même temps.")
                return
            env = dict(os.environ, SteamAppId=str(a), SteamGameId=str(a))
            p = subprocess.Popen([interpreteur_sans_console(base=True), SCRIPT, "--farm", str(a), str(os.getpid())],
                                 env=env, cwd=ICI, creationflags=SANS_FENETRE, stdin=subprocess.DEVNULL,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self.procs[a] = (p, time.time())
            self.echecs.pop(a, None)
            self._maj_ligne(a)

        def _arreter(self, a: int):
            p = self.procs.pop(a, None)
            if not p:
                return
            self.farme[a] = self.farme.get(a, 0) + time.time() - p[1]
            p[0].terminate()
            try:
                p[0].wait(timeout=3)
            except subprocess.TimeoutExpired:
                pass
            self._maj_ligne(a)

        def _marche_arret(self):
            if self.actif:
                self._tout_arreter("Farm arrêté.")
                return
            a_lancer = sorted(a for a in self.coches if a in self.jeux)
            if not a_lancer:
                self.etat.config(text="Coche d'abord au moins un jeu (clic sur la case à gauche de son logo).")
                return
            if not DLL.exists():
                messagebox.showerror("Farm d'heures Steam",
                                     "steam_api64.dll introuvable.\nInstalle n'importe quel jeu Steam puis clique Recharger.")
                return
            self.actif, self.debut = True, time.time()
            self.total_depart = sum(self._total(a) for a in self.jeux)
            if self.eveille.get():
                garder_eveille(True)
            for a in a_lancer:
                self._lancer(a)
            self.bouton.config(text="■  Arrêter le farm", style="Arret.TButton")
            self._maj_banniere_textes()

        def _tout_arreter(self, message: str):
            for a in list(self.procs):
                self._arreter(a)
            self.actif = False
            garder_eveille(False)
            self.bouton.config(text="▶  Démarrer le farm", style="Accent.TButton")
            if message:
                self.etat.config(text=message)
            self._sauver()
            self._maj_banniere_textes()

        def _tic(self):
            if self.chargement:
                self._appliquer_chargement()
            if SIGNAL.exists():
                SIGNAL.unlink(missing_ok=True)
                self._montrer()
            for a, (p, t0) in list(self.procs.items()):
                code = p.poll()
                if code is not None:
                    del self.procs[a]
                    self.farme[a] = self.farme.get(a, 0) + time.time() - t0
                    self.echecs[a] = {4: "refusé par Steam", 5: "Steam fermé"}.get(code, f"arrêté ({code})")
                    self._maj_ligne(a)
            for a in self.procs:
                self._maj_ligne(a)
            self._maj_banniere_textes()
            self._demarrage_auto()
            if self.actif:
                try:
                    limite = float(self.duree.get().replace(",", ".")) * 3600
                except ValueError:
                    limite = 0
                ecoule = time.time() - self.debut
                gagne = sum(self._total(a) for a in self.jeux) - self.total_depart
                if limite > 0 and ecoule >= limite:
                    self._tout_arreter(f"Terminé : {hms(ecoule)} de farm, {heures(gagne / 60, '0 h 00')} gagnées au total.")
                else:
                    n = len(self.procs)
                    txt = f"▶  {n} jeu{'x' if n > 1 else ''} en cours  ·  {hms(ecoule)}  ·  +{heures(gagne / 60, '0 h 00')} gagnées"
                    if limite > 0:
                        txt += f"  ·  arrêt dans {hms(limite - ecoule)}"
                    if self.echecs:
                        txt += f"  ·  {len(self.echecs)} refusé(s)"
                    self.etat.config(text=txt)
                    self._titre_tray(f"Farm d'heures Steam — {n} jeu{'x' if n > 1 else ''} en cours")
                if time.time() - self._derniere_sauvegarde > 60:
                    self._sauver()
            else:
                if not self.etat.cget("text"):
                    total = sum(self._total(a) for a in self.farme)
                    self.etat.config(text=f"Total farmé avec l'appli : {heures(total / 60, '0 h 00')}")
                self._titre_tray("Farm d'heures Steam")
            self.r.after(1000, self._tic)

        def _titre_tray(self, titre: str):
            if self.tray and self.tray.title != titre:
                self.tray.title = titre

        def _demarrage_auto(self):
            """Mode --auto (démarrage de Windows) : attend que Steam soit connecté puis lance le farm."""
            if not self.auto or self.actif or not self.jeux:
                return
            if not utilisateur_actif():
                self.steam_pret = None
                return
            self.steam_pret = self.steam_pret or time.time()
            if time.time() - self.steam_pret > 20:
                self.auto = False
                if any(a in self.jeux for a in self.coches):
                    self._marche_arret()

        def _sauver(self):
            self._derniere_sauvegarde = time.time()
            self.cfg.update(coches=sorted(self.coches), heures=self.duree.get(), eveille=self.eveille.get(),
                            farme={str(a): round(self._total(a)) for a in set(self.farme) | set(self.procs)})
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
