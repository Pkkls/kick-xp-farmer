# kick-xp-farmer

Accumule du XP Kick en idle 24/7.

**Taux constaté empiriquement : ~2 XP/min (~4 XP toutes les 2 min)**

## Mécanisme

Kick attribue du XP en fonction du temps de visionnage de streams. Le serveur côté Kick suit les utilisateurs authentifiés qui sont **abonnés au canal Pusher privé** `private-livestream.{id}` d'un stream en cours. Ce script reproduit ce comportement sans navigateur :

1. Authentification Bearer via `session_token` (extrait des cookies du navigateur)
2. Connexion WebSocket au serveur Pusher de Kick
3. Souscription au canal `private-livestream.{id}` du stream live
4. Rotation automatique si le stream passe hors ligne
5. Polling XP toutes les 2 minutes pour suivre la progression

## Installation

```bash
pip install -r requirements.txt
```

## Configuration

### 1. Extraire le session_token depuis ton navigateur

Depuis kick.com (connecté), ouvre les DevTools → Application → Cookies → `kick.com`

Cherche le cookie nommé **`session_token`** et copie sa valeur (format: `XXXXXXX%7C...`).

Avec l'extension **EditThisCookie** : exporte tous les cookies, cherche l'objet avec `"name": "session_token"`, copie le champ `"value"`.

### 2. Créer config.json

```bash
cp config.example.json config.json
```

Édite `config.json` :

```json
{
  "session_token": "TON_USER_ID%7CTon_Token_Ici",
  "xp_poll_interval": 120,
  "log_file": "farmer.log",
  "slug_pool": ["kaicenat", "xqc", "trainwreckstv", ...]
}
```

- **`session_token`** : valeur du cookie Kick (URL-encodé ou décodé, les deux marchent)
- **`xp_poll_interval`** : secondes entre chaque check XP (défaut: 120)
- **`slug_pool`** : liste des streamers à checker pour trouver un stream live (ordre = priorité)

### 3. Lancer

```bash
python farmer.py
```

## Utilisation en continu (24/7)

### Windows — via Task Scheduler

Créer une tâche planifiée qui exécute `python farmer.py` au démarrage, avec restart automatique.

Ou via PowerShell :

```powershell
while ($true) { python farmer.py; Start-Sleep -Seconds 30 }
```

### Linux/macOS — via systemd ou screen

```bash
# screen
screen -S kick-farmer
python farmer.py
# Ctrl+A D pour détacher

# systemd (créer /etc/systemd/system/kick-farmer.service)
```

### Docker

```dockerfile
FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY farmer.py config.json ./
CMD ["python", "farmer.py"]
```

## Output / Logs

```
18:25:17 Niveau initial: L27 | 2231/2686 XP (83.1%) | 455 XP restants
18:25:23 Stream: dankquan (id=113129576, 2 viewers)
18:25:24 WS: socket_id=1324969.252219
18:25:24 Subscribe auth OK: private-livestream.113129576
18:25:25 WS: subscription OK: private-livestream.113129576
18:27:24 [XP #1] L27 | 2239/2686 XP (83.4%) | +8 ce poll | +8 total | 4.0 XP/min
```

## Notes

- Le `session_token` a une durée de vie longue (~30 jours). Renouvelle-le si le script loggue "token expire".
- Le script ne nécessite pas de navigateur ni de lecture vidéo HLS.
- La `slug_pool` doit contenir des streamers régulièrement en live pour garantir la rotation.
- XP s'accumule à ~2 XP/min. Level 28 → 29 ≈ 22h de watch time selon les données du serveur.

## Idées d'amélioration

- **Multi-compte** : lancer plusieurs instances avec des `config.json` différents
- **Notification level up** : webhook Discord ou Telegram quand level change
- **Dashboard** : stats XP/heure en temps réel
- **Renouvellement auto du token** : scraper kick.com pour regénérer le Bearer sans intervention manuelle
- **Docker Compose** : multi-instance facilement configurable

## Avertissement

À usage personnel uniquement. Ce script simule un comportement de viewer. Utilise-le dans le respect des CGU de Kick.
