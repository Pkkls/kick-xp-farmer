"""Browser automation and cookie management"""
import json
import os
import re
import shutil
import sqlite3
import subprocess
import tempfile
import undetected_chromedriver as uc
from utils.helpers import cookie_file_for_domain, CHROME_DATA_DIR


def _chromium_cookie_paths():
    """Retourne les chemins possibles de la base SQLite Cookies pour Chrome/Brave."""
    local = os.environ.get("LOCALAPPDATA", "")
    appdata = os.environ.get("APPDATA", "")
    candidates = [
        os.path.join(local, "Google", "Chrome", "User Data", "Default", "Cookies"),
        os.path.join(local, "Google", "Chrome", "User Data", "Default", "Network", "Cookies"),
        os.path.join(local, "BraveSoftware", "Brave-Browser", "User Data", "Default", "Cookies"),
        os.path.join(local, "BraveSoftware", "Brave-Browser", "User Data", "Default", "Network", "Cookies"),
        os.path.join(appdata, "Local", "Google", "Chrome", "User Data", "Default", "Cookies"),
    ]
    return [p for p in candidates if os.path.exists(p)]


def _decrypt_chrome_value(encrypted_value):
    """Dechiffre une valeur cookie Chrome sur Windows via DPAPI."""
    try:
        import ctypes
        import ctypes.wintypes

        # Chrome v80+ : prefixe "v10" + AES-GCM avec cle dans Local State
        if encrypted_value[:3] == b"v10":
            return _decrypt_chrome_v10(encrypted_value)

        # Ancien format DPAPI
        class DATA_BLOB(ctypes.Structure):
            _fields_ = [("cbData", ctypes.wintypes.DWORD),
                        ("pbData", ctypes.POINTER(ctypes.c_char))]

        p = ctypes.create_string_buffer(encrypted_value, len(encrypted_value))
        blobin = DATA_BLOB(ctypes.sizeof(p), p)
        blobout = DATA_BLOB()
        ctypes.windll.crypt32.CryptUnprotectData(
            ctypes.byref(blobin), None, None, None, None, 0, ctypes.byref(blobout))
        result = ctypes.string_at(blobout.pbData, blobout.cbData)
        ctypes.windll.kernel32.LocalFree(blobout.pbData)
        return result.decode("utf-8", errors="replace")
    except Exception:
        return ""


def _get_chrome_encryption_key(db_path):
    """Lit la cle AES depuis Local State."""
    try:
        import base64
        local_state_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(db_path))), "Local State")
        if not os.path.exists(local_state_path):
            return None
        with open(local_state_path, "r", encoding="utf-8") as f:
            local_state = json.load(f)
        encrypted_key = base64.b64decode(
            local_state["os_crypt"]["encrypted_key"])[5:]  # strip DPAPI prefix
        import ctypes
        import ctypes.wintypes

        class DATA_BLOB(ctypes.Structure):
            _fields_ = [("cbData", ctypes.wintypes.DWORD),
                        ("pbData", ctypes.POINTER(ctypes.c_char))]

        p = ctypes.create_string_buffer(encrypted_key, len(encrypted_key))
        blobin = DATA_BLOB(ctypes.sizeof(p), p)
        blobout = DATA_BLOB()
        ctypes.windll.crypt32.CryptUnprotectData(
            ctypes.byref(blobin), None, None, None, None, 0, ctypes.byref(blobout))
        key = ctypes.string_at(blobout.pbData, blobout.cbData)
        ctypes.windll.kernel32.LocalFree(blobout.pbData)
        return key
    except Exception:
        return None


def _decrypt_chrome_v10(encrypted_value, key=None):
    """Dechiffre AES-256-GCM (Chrome v80+)."""
    try:
        from Crypto.Cipher import AES
        iv = encrypted_value[3:15]
        payload = encrypted_value[15:-16]
        tag = encrypted_value[-16:]
        cipher = AES.new(key, AES.MODE_GCM, nonce=iv)
        return cipher.decrypt_and_verify(payload, tag).decode("utf-8", errors="replace")
    except Exception:
        return ""


def _extract_chromium_cookies(domain):
    """Extrait les cookies kick.com depuis Chrome/Brave via copie SQLite + dechiffrement DPAPI."""
    cookies = []
    for db_path in _chromium_cookie_paths():
        try:
            key = _get_chrome_encryption_key(db_path)
            # Copie du fichier pour eviter le verrou SQLite
            with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
                tmp_path = tmp.name
            shutil.copy2(db_path, tmp_path)
            conn = sqlite3.connect(tmp_path)
            conn.row_factory = sqlite3.Row
            cur = conn.cursor()
            cur.execute(
                "SELECT name, encrypted_value, host_key, path, is_secure, expires_utc "
                "FROM cookies WHERE host_key LIKE ?",
                (f"%{domain}%",)
            )
            rows = cur.fetchall()
            conn.close()
            os.unlink(tmp_path)

            for row in rows:
                name = row["name"]
                enc = row["encrypted_value"]
                if not name or not enc:
                    continue
                if enc[:3] == b"v10" and key:
                    value = _decrypt_chrome_v10(enc, key)
                else:
                    value = _decrypt_chrome_value(enc)
                if not value:
                    continue
                cookie = {
                    "name": name,
                    "value": value,
                    "domain": row["host_key"],
                    "path": row["path"] or "/",
                    "secure": bool(row["is_secure"]),
                }
                exp = row["expires_utc"]
                if exp:
                    # Chrome epoch = microseconds since 1601-01-01
                    unix_ts = (exp - 11644473600000000) // 1000000
                    if unix_ts > 0:
                        cookie["expiry"] = unix_ts
                cookies.append(cookie)
            if cookies:
                break
        except Exception:
            continue
    return cookies


class CookieManager:
    """Manages browser cookies for authentication"""
    
    @staticmethod
    def save_cookies(driver, domain):
        """Save cookies from driver to file"""
        path = cookie_file_for_domain(domain)
        cookies = driver.get_cookies()
        with open(path, "w", encoding="utf-8") as f:
            json.dump(cookies, f, indent=2)
        return path

    @staticmethod
    def load_cookies(driver, domain):
        """Load cookies from file into driver"""
        path = cookie_file_for_domain(domain)
        if not os.path.exists(path):
            return False
        with open(path, "r", encoding="utf-8") as f:
            cookies = json.load(f)
        _cf = {"__cf_bm", "_cfuvid", "cf_clearance", "__cflb"}
        for c in cookies:
            if c.get("name") in _cf:
                continue
            if "expiry" in c and c["expiry"] is None:
                del c["expiry"]
            try:
                driver.add_cookie(c)
            except Exception:
                pass
        return True

    @staticmethod
    def import_from_browser(domain: str) -> bool:
        """Attempts to import existing cookies from browsers (Chrome/Edge/Firefox)
        using browser_cookie3. Returns True if a file was written.
        """
        try:
            import browser_cookie3 as bc3  # type: ignore
        except Exception:
            return False

        try:
            cj = bc3.load(domain_name=domain)
        except Exception:
            cj = None

        if not cj:
            # Fallback: extraction directe SQLite (fonctionne sans admin, navigateur ouvert)
            direct_cookies = _extract_chromium_cookies(domain)
            if direct_cookies:
                path = cookie_file_for_domain(domain)
                try:
                    with open(path, "w", encoding="utf-8") as f:
                        json.dump(direct_cookies, f, indent=2)
                    return True
                except Exception:
                    return False
            return False

        cookies = []
        try:
            for c in cj:
                if not getattr(c, "name", None):
                    continue
                cookie = {
                    "name": c.name,
                    "value": c.value,
                    "domain": getattr(c, "domain", domain) or domain,
                    "path": getattr(c, "path", "/") or "/",
                    "secure": bool(getattr(c, "secure", False)),
                }
                exp = getattr(c, "expires", None)
                if exp is not None:
                    try:
                        cookie["expiry"] = int(exp)
                    except Exception:
                        pass
                cookies.append(cookie)
        except Exception:
            return False

        if not cookies:
            return False

        path = cookie_file_for_domain(domain)
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(cookies, f, indent=2)
            return True
        except Exception:
            return False


def _chrome_executable_candidates():
    """Yield likely Chrome executables in preference order."""
    seen = set()
    candidates = []

    if os.name == "nt":
        for env_name in ("LOCALAPPDATA", "PROGRAMFILES", "PROGRAMFILES(X86)"):
            base = os.environ.get(env_name)
            if base:
                candidates.append(os.path.join(base, "Google", "Chrome", "Application", "chrome.exe"))

    for command in ("chrome", "google-chrome", "chromium", "chromium-browser"):
        path = shutil.which(command)
        if path:
            candidates.append(path)

    for path in candidates:
        normalized = os.path.normcase(os.path.abspath(path))
        if normalized in seen:
            continue
        seen.add(normalized)
        if os.path.exists(path):
            yield path


def _parse_major_version(version_text):
    match = re.search(r"(\d+)\.", version_text or "")
    if not match:
        return None
    try:
        return int(match.group(1))
    except ValueError:
        return None


def _chrome_version_from_registry():
    if os.name != "nt":
        return None

    try:
        import winreg
    except Exception:
        return None

    keys = (
        (winreg.HKEY_CURRENT_USER, r"Software\Google\Chrome\BLBeacon"),
        (winreg.HKEY_LOCAL_MACHINE, r"Software\Google\Chrome\BLBeacon"),
        (winreg.HKEY_LOCAL_MACHINE, r"Software\WOW6432Node\Google\Chrome\BLBeacon"),
    )
    for root, key_name in keys:
        try:
            with winreg.OpenKey(root, key_name) as key:
                version, _ = winreg.QueryValueEx(key, "version")
                major = _parse_major_version(str(version))
                if major:
                    return major
        except Exception:
            continue
    return None


def _chrome_version_from_executable(path):
    try:
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        proc = subprocess.run(
            [path, "--version"],
            capture_output=True,
            text=True,
            timeout=5,
            creationflags=flags,
        )
    except Exception:
        return None

    return _parse_major_version((proc.stdout or "") + " " + (proc.stderr or ""))


def _detect_chrome():
    """Return (major_version, executable_path) for installed Chrome when possible."""
    executable = next(_chrome_executable_candidates(), None)

    if os.name == "nt":
        major = _chrome_version_from_registry()
        if major:
            return major, executable

    for path in ([executable] if executable else []):
        major = _chrome_version_from_executable(path)
        if major:
            return major, path

    return None, executable


def make_chrome_driver(
    headless=True,
    visible_width=1280,
    visible_height=800,
    driver_path=None,
    extension_path=None,
):
    """Create and configure a Chrome driver instance"""
    opts = uc.ChromeOptions()  # Use undetected-chromedriver options

    # Headless configuration (adapted for uc)
    if headless:
        try:
            opts.add_argument("--headless=new")
        except Exception:
            opts.add_argument("--headless")
        opts.add_argument("--disable-gpu")
    else:
        opts.add_argument(f"--window-size={visible_width},{visible_height}")

    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--disable-blink-features=AutomationControlled")
    # Remove redundant experimental options to avoid parsing error
    # (undetected-chromedriver already handles this natively)
    opts.add_argument("--log-level=3")
    opts.add_argument("--silent")

    user_data_dir = CHROME_DATA_DIR
    os.makedirs(user_data_dir, exist_ok=True)
    opts.add_argument(f"--user-data-dir={user_data_dir}")

    # Extension loading (compatible with uc)
    if extension_path:
        try:
            if extension_path.lower().endswith(".crx"):
                opts.add_extension(extension_path)
            else:
                opts.add_argument(f"--load-extension={extension_path}")
        except Exception:
            pass

    chrome_major, chrome_executable = _detect_chrome()
    driver_kwargs = {
        "options": opts,
        "version_main": chrome_major,
    }
    if chrome_executable:
        driver_kwargs["browser_executable_path"] = chrome_executable
    if driver_path and os.path.isfile(driver_path):
        driver_kwargs["driver_executable_path"] = driver_path

    driver = uc.Chrome(**driver_kwargs)

    return driver

