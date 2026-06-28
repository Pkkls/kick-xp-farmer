"""Garde d'egress : l'application ne contacte QUE Kick.

Garantie "rien ne sort de ton PC", au niveau du code et auditable :
- Toute URL touchee par l'app passe par assert_allowed(). Seuls kick.com et ses
  sous-domaines sont autorises ; tout le reste leve EgressError.
- Aucune autre destination reseau n'est jamais appelee : pas de backend, pas de
  telemetrie. Les cookies de session et la config restent sur le disque local.

Ce n'est pas un pare-feu OS : c'est une barriere applicative. Pour une preuve
externe, lance l'app derriere un proxy (mitmproxy/Fiddler) : seul kick.com
apparait. Voir README.
"""
from urllib.parse import urlparse

# kick.com + tous ses sous-domaines (web.kick.com, files.kick.com, ...).
ALLOWED_SUFFIX = "kick.com"


class EgressError(RuntimeError):
    """Levee quand une URL hors allowlist est tentee."""


def host_allowed(host: str) -> bool:
    host = (host or "").lower().split(":")[0]
    return host == ALLOWED_SUFFIX or host.endswith("." + ALLOWED_SUFFIX)


def assert_allowed(url: str) -> str:
    """Renvoie l'url si elle vise Kick, sinon leve EgressError."""
    host = urlparse(url).hostname or ""
    if not host_allowed(host):
        raise EgressError(
            f"Egress bloque : '{host or url}' n'est pas {ALLOWED_SUFFIX}. "
            f"Cette app ne contacte que Kick."
        )
    return url


def self_test() -> None:
    """Verifie l'allowlist au demarrage et l'affiche (transparence)."""
    assert host_allowed("kick.com")
    assert host_allowed("web.kick.com")
    assert host_allowed("files.kick.com")
    assert not host_allowed("evil.com")
    assert not host_allowed("kick.com.evil.com")
    print(f"[egress] allowlist active : *.{ALLOWED_SUFFIX} uniquement. "
          f"Aucune autre destination ne sera contactee.")
