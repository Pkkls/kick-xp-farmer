# kick-xp-farmer

🇫🇷 [Français](#français) · 🇺🇸 [English](#english) · 🇪🇸 [Español](#español) · 🇯🇵 [日本語](#日本語)

---

## Français

Accumule du XP Kick en idle 24/7.

**Taux constaté empiriquement : ~2 XP/min (~4 XP toutes les 2 min)**

### Mécanisme

Kick attribue du XP en fonction du temps de visionnage de streams. Le serveur côté Kick suit les utilisateurs authentifiés qui sont **abonnés au canal Pusher privé** `private-livestream.{id}` d'un stream en cours. Ce script reproduit ce comportement sans navigateur :

1. Authentification Bearer via `session_token` (extrait des cookies du navigateur)
2. Connexion WebSocket au serveur Pusher de Kick
3. Souscription au canal `private-livestream.{id}` du stream live
4. Rotation automatique si le stream passe hors ligne
5. Polling XP toutes les 2 minutes pour suivre la progression

### Installation

```bash
pip install -r requirements.txt
```

### Configuration

#### 1. Extraire le session_token depuis ton navigateur

Depuis kick.com (connecté), ouvre les DevTools → Application → Cookies → `kick.com`

Cherche le cookie nommé **`session_token`** et copie sa valeur (format: `XXXXXXX%7C...`).

Avec l'extension **EditThisCookie** : exporte tous les cookies, cherche l'objet avec `"name": "session_token"`, copie le champ `"value"`.

#### 2. Créer config.json

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

#### 3. Lancer

```bash
python farmer.py
```

### Utilisation en continu (24/7)

#### Windows — via Task Scheduler

Créer une tâche planifiée qui exécute `python farmer.py` au démarrage, avec restart automatique.

Ou via PowerShell :

```powershell
while ($true) { python farmer.py; Start-Sleep -Seconds 30 }
```

#### Linux/macOS — via systemd ou screen

```bash
# screen
screen -S kick-farmer
python farmer.py
# Ctrl+A D pour détacher

# systemd (créer /etc/systemd/system/kick-farmer.service)
```

#### Docker

```dockerfile
FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY farmer.py config.json ./
CMD ["python", "farmer.py"]
```

### Output / Logs

```
18:25:17 Niveau initial: L27 | 2231/2686 XP (83.1%) | 455 XP restants
18:25:23 Stream: dankquan (id=113129576, 2 viewers)
18:25:24 WS: socket_id=1324969.252219
18:25:24 Subscribe auth OK: private-livestream.113129576
18:25:25 WS: subscription OK: private-livestream.113129576
18:27:24 [XP #1] L27 | 2239/2686 XP (83.4%) | +8 ce poll | +8 total | 4.0 XP/min
```

### Notes

- Le `session_token` a une durée de vie longue (~30 jours). Renouvelle-le si le script loggue "token expire".
- Le script ne nécessite pas de navigateur ni de lecture vidéo HLS.
- La `slug_pool` doit contenir des streamers régulièrement en live pour garantir la rotation.
- XP s'accumule à ~2 XP/min. Level 28 → 29 ≈ 22h de watch time selon les données du serveur.

### Idées d'amélioration

- **Multi-compte** : lancer plusieurs instances avec des `config.json` différents
- **Notification level up** : webhook Discord ou Telegram quand level change
- **Dashboard** : stats XP/heure en temps réel
- **Renouvellement auto du token** : scraper kick.com pour regénérer le Bearer sans intervention manuelle
- **Docker Compose** : multi-instance facilement configurable

### Avertissement

À usage personnel uniquement. Ce script simule un comportement de viewer. Utilise-le dans le respect des CGU de Kick.

---

## English

Accumulates Kick XP idle 24/7.

**Empirically observed rate: ~2 XP/min (~4 XP every 2 min)**

### Mechanism

Kick awards XP based on stream watch time. The Kick server-side tracks authenticated users who are **subscribed to the private Pusher channel** `private-livestream.{id}` of an ongoing stream. This script reproduces this behavior without a browser:

1. Bearer authentication via `session_token` (extracted from browser cookies)
2. WebSocket connection to Kick's Pusher server
3. Subscription to the `private-livestream.{id}` channel of the live stream
4. Automatic rotation if the stream goes offline
5. XP polling every 2 minutes to track progress

### Installation

```bash
pip install -r requirements.txt
```

### Configuration

#### 1. Extract the session_token from your browser

From kick.com (logged in), open DevTools → Application → Cookies → `kick.com`

Look for the cookie named **`session_token`** and copy its value (format: `XXXXXXX%7C...`).

With the **EditThisCookie** extension: export all cookies, find the object with `"name": "session_token"`, copy the `"value"` field.

#### 2. Create config.json

```bash
cp config.example.json config.json
```

Edit `config.json`:

```json
{
  "session_token": "YOUR_USER_ID%7CYour_Token_Here",
  "xp_poll_interval": 120,
  "log_file": "farmer.log",
  "slug_pool": ["kaicenat", "xqc", "trainwreckstv", ...]
}
```

- **`session_token`**: Kick cookie value (URL-encoded or decoded, both work)
- **`xp_poll_interval`**: seconds between each XP check (default: 120)
- **`slug_pool`**: list of streamers to check for a live stream (order = priority)

#### 3. Run

```bash
python farmer.py
```

### Continuous Usage (24/7)

#### Windows — via Task Scheduler

Create a scheduled task that runs `python farmer.py` at startup, with automatic restart.

Or via PowerShell:

```powershell
while ($true) { python farmer.py; Start-Sleep -Seconds 30 }
```

#### Linux/macOS — via systemd or screen

```bash
# screen
screen -S kick-farmer
python farmer.py
# Ctrl+A D to detach

# systemd (create /etc/systemd/system/kick-farmer.service)
```

#### Docker

```dockerfile
FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY farmer.py config.json ./
CMD ["python", "farmer.py"]
```

### Output / Logs

```
18:25:17 Initial level: L27 | 2231/2686 XP (83.1%) | 455 XP remaining
18:25:23 Stream: dankquan (id=113129576, 2 viewers)
18:25:24 WS: socket_id=1324969.252219
18:25:24 Subscribe auth OK: private-livestream.113129576
18:25:25 WS: subscription OK: private-livestream.113129576
18:27:24 [XP #1] L27 | 2239/2686 XP (83.4%) | +8 this poll | +8 total | 4.0 XP/min
```

### Notes

- The `session_token` has a long lifespan (~30 days). Renew it if the script logs "token expire".
- The script does not require a browser or HLS video playback.
- The `slug_pool` must contain streamers who are regularly live to ensure rotation.
- XP accumulates at ~2 XP/min. Level 28 → 29 ≈ 22h of watch time according to server data.

### Ideas for Improvement

- **Multi-account**: run multiple instances with different `config.json` files
- **Level-up notification**: Discord or Telegram webhook when level changes
- **Dashboard**: real-time XP/hour stats
- **Auto token renewal**: scrape kick.com to regenerate the Bearer without manual intervention
- **Docker Compose**: easily configurable multi-instance setup

### Disclaimer

For personal use only. This script simulates viewer behavior. Use it in compliance with Kick's Terms of Service.

---

## Español

Acumula XP de Kick en modo idle 24/7.

**Tasa observada empíricamente: ~2 XP/min (~4 XP cada 2 min)**

### Mecanismo

Kick otorga XP en función del tiempo de visualización de streams. El servidor de Kick rastrea a los usuarios autenticados que están **suscritos al canal privado de Pusher** `private-livestream.{id}` de un stream en curso. Este script reproduce este comportamiento sin navegador:

1. Autenticación Bearer mediante `session_token` (extraído de las cookies del navegador)
2. Conexión WebSocket al servidor Pusher de Kick
3. Suscripción al canal `private-livestream.{id}` del stream en vivo
4. Rotación automática si el stream se desconecta
5. Polling de XP cada 2 minutos para seguir el progreso

### Instalación

```bash
pip install -r requirements.txt
```

### Configuración

#### 1. Extraer el session_token de tu navegador

Desde kick.com (conectado), abre DevTools → Aplicación → Cookies → `kick.com`

Busca la cookie llamada **`session_token`** y copia su valor (formato: `XXXXXXX%7C...`).

Con la extensión **EditThisCookie**: exporta todas las cookies, busca el objeto con `"name": "session_token"`, copia el campo `"value"`.

#### 2. Crear config.json

```bash
cp config.example.json config.json
```

Edita `config.json`:

```json
{
  "session_token": "TU_USER_ID%7CTu_Token_Aquí",
  "xp_poll_interval": 120,
  "log_file": "farmer.log",
  "slug_pool": ["kaicenat", "xqc", "trainwreckstv", ...]
}
```

- **`session_token`**: valor de la cookie de Kick (URL-encodado o decodificado, ambos funcionan)
- **`xp_poll_interval`**: segundos entre cada verificación de XP (por defecto: 120)
- **`slug_pool`**: lista de streamers a verificar para encontrar un stream en vivo (orden = prioridad)

#### 3. Ejecutar

```bash
python farmer.py
```

### Uso continuo (24/7)

#### Windows — via Programador de tareas

Crea una tarea programada que ejecute `python farmer.py` al inicio, con reinicio automático.

O via PowerShell:

```powershell
while ($true) { python farmer.py; Start-Sleep -Seconds 30 }
```

#### Linux/macOS — via systemd o screen

```bash
# screen
screen -S kick-farmer
python farmer.py
# Ctrl+A D para desconectar

# systemd (crear /etc/systemd/system/kick-farmer.service)
```

#### Docker

```dockerfile
FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY farmer.py config.json ./
CMD ["python", "farmer.py"]
```

### Salida / Logs

```
18:25:17 Nivel inicial: L27 | 2231/2686 XP (83.1%) | 455 XP restantes
18:25:23 Stream: dankquan (id=113129576, 2 viewers)
18:25:24 WS: socket_id=1324969.252219
18:25:24 Subscribe auth OK: private-livestream.113129576
18:25:25 WS: subscription OK: private-livestream.113129576
18:27:24 [XP #1] L27 | 2239/2686 XP (83.4%) | +8 este poll | +8 total | 4.0 XP/min
```

### Notas

- El `session_token` tiene una vida útil larga (~30 días). Renuévalo si el script registra "token expire".
- El script no requiere navegador ni reproducción de video HLS.
- La `slug_pool` debe contener streamers que estén regularmente en vivo para garantizar la rotación.
- El XP se acumula a ~2 XP/min. Nivel 28 → 29 ≈ 22h de tiempo de visualización según los datos del servidor.

### Ideas de mejora

- **Multi-cuenta**: ejecutar varias instancias con diferentes `config.json`
- **Notificación de subida de nivel**: webhook de Discord o Telegram cuando cambia el nivel
- **Dashboard**: estadísticas de XP/hora en tiempo real
- **Renovación automática del token**: hacer scraping de kick.com para regenerar el Bearer sin intervención manual
- **Docker Compose**: configuración multi-instancia fácilmente configurable

### Aviso

Solo para uso personal. Este script simula el comportamiento de un espectador. Úsalo respetando los Términos de Servicio de Kick.

---

## 日本語

Kick の XP をアイドル状態で 24/7 蓄積します。

**実測値: 約 2 XP/分（2分ごとに約 4 XP）**

### 仕組み

Kick はストリームの視聴時間に応じて XP を付与します。Kick のサーバー側では、進行中のストリームのプライベート Pusher チャンネル `private-livestream.{id}` に**サブスクライブしている認証済みユーザー**を追跡しています。このスクリプトはブラウザなしでその動作を再現します：

1. `session_token`（ブラウザのクッキーから取得）による Bearer 認証
2. Kick の Pusher サーバーへの WebSocket 接続
3. ライブストリームの `private-livestream.{id}` チャンネルへのサブスクリプション
4. ストリームがオフラインになった場合の自動ローテーション
5. 進捗追跡のための 2 分ごとの XP ポーリング

### インストール

```bash
pip install -r requirements.txt
```

### 設定

#### 1. ブラウザから session_token を取得する

kick.com（ログイン済み）から DevTools を開く → アプリケーション → Cookie → `kick.com`

**`session_token`** という名前の Cookie を探し、その値をコピーします（形式: `XXXXXXX%7C...`）。

**EditThisCookie** 拡張機能を使う場合: すべての Cookie をエクスポートし、`"name": "session_token"` のオブジェクトを探して `"value"` フィールドをコピーします。

#### 2. config.json を作成する

```bash
cp config.example.json config.json
```

`config.json` を編集:

```json
{
  "session_token": "あなたのUSER_ID%7Cトークンをここに",
  "xp_poll_interval": 120,
  "log_file": "farmer.log",
  "slug_pool": ["kaicenat", "xqc", "trainwreckstv", ...]
}
```

- **`session_token`**: Kick Cookie の値（URL エンコードあり・なしどちらでも動作します）
- **`xp_poll_interval`**: XP チェックの間隔（秒）（デフォルト: 120）
- **`slug_pool`**: ライブストリームを探すストリーマーのリスト（順番 = 優先度）

#### 3. 実行

```bash
python farmer.py
```

### 継続稼働（24/7）

#### Windows — タスクスケジューラ経由

スタートアップ時に `python farmer.py` を実行し、自動再起動するタスクを作成します。

または PowerShell 経由:

```powershell
while ($true) { python farmer.py; Start-Sleep -Seconds 30 }
```

#### Linux/macOS — systemd または screen 経由

```bash
# screen
screen -S kick-farmer
python farmer.py
# Ctrl+A D でデタッチ

# systemd (/etc/systemd/system/kick-farmer.service を作成)
```

#### Docker

```dockerfile
FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY farmer.py config.json ./
CMD ["python", "farmer.py"]
```

### 出力 / ログ

```
18:25:17 初期レベル: L27 | 2231/2686 XP (83.1%) | 残り 455 XP
18:25:23 ストリーム: dankquan (id=113129576, 視聴者 2 人)
18:25:24 WS: socket_id=1324969.252219
18:25:24 サブスクライブ認証 OK: private-livestream.113129576
18:25:25 WS: サブスクリプション OK: private-livestream.113129576
18:27:24 [XP #1] L27 | 2239/2686 XP (83.4%) | +8 今回 | +8 合計 | 4.0 XP/分
```

### 注意事項

- `session_token` の有効期限は長め（〜30 日）です。スクリプトが「token expire」とログ出力した場合は更新してください。
- このスクリプトはブラウザや HLS ビデオ再生を必要としません。
- `slug_pool` には定期的にライブ配信しているストリーマーを含める必要があります。
- XP は約 2 XP/分で蓄積されます。レベル 28 → 29 はサーバーデータによると約 22 時間の視聴時間が必要です。

### 改善アイデア

- **マルチアカウント**: 異なる `config.json` で複数インスタンスを起動
- **レベルアップ通知**: レベルが変わったとき Discord または Telegram への webhook
- **ダッシュボード**: リアルタイムの XP/時間統計
- **トークン自動更新**: 手動介入なしで Bearer を再生成するための kick.com スクレイピング
- **Docker Compose**: 簡単に設定できるマルチインスタンス構成

### 免責事項

個人使用のみ。このスクリプトは視聴者の動作をシミュレートします。Kick の利用規約に従って使用してください。
