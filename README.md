# 🎟️ Ticket Price Monitor

Surveille les prix de billets sur billet.ca (et autres sites) toutes les 30 minutes et t'envoie un **SMS** quand un prix passe sous ton seuil. Roule gratuitement sur **GitHub Actions** — pas besoin de serveur ni de laisser ton ordi allumé.

---

## Comment ça marche

1. Tu listes tes événements dans `config.json` (URL + prix max par billet)
2. GitHub Actions roule `monitor.py` toutes les 30 minutes
3. Le script scrape chaque page, trouve le prix le plus bas
4. Si prix ≤ ton seuil → SMS via Twilio avec le lien direct
5. Anti-spam intégré : max 1 alerte par 6h par événement, sauf si le prix baisse encore de 5 %+

---

## Installation (15 minutes)

### Étape 1 — Créer le repo GitHub

1. Va sur [github.com/new](https://github.com/new)
2. Nom : `ticket-monitor` (ou ce que tu veux)
3. **Privé** recommandé
4. Crée le repo, puis upload tous les fichiers de ce dossier (bouton *Add file → Upload files*). Assure-toi que `.github/workflows/monitor.yml` garde bien sa structure de dossiers — le plus simple est de glisser le dossier au complet.

> ⚠️ Limite des repos privés : 2 000 minutes GitHub Actions gratuites/mois. À 48 runs/jour d'environ 1 minute, tu es à ~1 440 min/mois — ça passe. Si tu ajoutes beaucoup d'événements et que ça dépasse, passe le cron à `*/45` ou `0 * * * *` (chaque heure) dans `monitor.yml`.

### Étape 2 — Compte Twilio (pour les SMS)

1. Crée un compte sur [twilio.com/try-twilio](https://www.twilio.com/try-twilio) (gratuit, crédit d'essai inclus)
2. Dans la console, note ton **Account SID** et ton **Auth Token**
3. Obtiens un numéro de téléphone Twilio (inclus dans l'essai) : *Phone Numbers → Buy a number* — prends un numéro canadien
4. **Important en mode essai** : vérifie ton propre numéro de cellulaire dans *Verified Caller IDs*, sinon Twilio refusera de t'envoyer des SMS
5. Coût après l'essai : ~0,01–0,02 $ US par SMS. Vu l'anti-spam, ça restera à quelques cents par mois.

### Étape 3 — Configurer les secrets GitHub

Dans ton repo : *Settings → Secrets and variables → Actions → New repository secret*. Crée ces 4 secrets :

| Nom | Valeur |
|---|---|
| `TWILIO_ACCOUNT_SID` | Ton Account SID (commence par `AC...`) |
| `TWILIO_AUTH_TOKEN` | Ton Auth Token |
| `TWILIO_FROM_NUMBER` | Ton numéro Twilio, format `+1514XXXXXXX` |
| `ALERT_TO_NUMBER` | Ton cell, format `+1XXXXXXXXXX` |

### Étape 4 — Ajouter tes événements

Édite `config.json` directement sur GitHub (icône crayon). Pour chaque événement :

```json
{
  "name": "Canadiens vs Bruins - 15 nov",
  "url": "https://www.billet.ca/tickets/...",
  "site": "billet.ca",
  "max_price": 150,
  "enabled": true,
  "css_selector": null
}
```

- `max_price` : prix **par billet** en dollars
- `enabled` : mets `false` pour mettre un événement en pause sans le supprimer
- `css_selector` : laisse `null` au début (voir section Précision plus bas)

### Étape 5 — Tester

1. Onglet *Actions* de ton repo → *Ticket Price Monitor* → *Run workflow* (lancement manuel)
2. Clique sur le run, ouvre les logs de l'étape *Run monitor*
3. Tu devrais voir chaque événement vérifié avec le prix min détecté
4. Pour tester le SMS : mets temporairement un `max_price` très haut (genre 9999) sur un événement, relance, tu devrais recevoir le texto. Remets ensuite ton vrai seuil.

C'est tout. Le cron part automatiquement toutes les 30 minutes.

---

## Précision du scraping (css_selector)

Par défaut, le script ramasse **tous** les montants en $ sur la page (entre 5 $ et 10 000 $). Ça marche, mais ça peut attraper des faux positifs (frais, autres sections, etc.).

Pour être chirurgical :
1. Ouvre la page de l'événement dans Chrome
2. Clic droit sur un prix → *Inspecter*
3. Trouve la classe CSS de l'élément prix (ex. `<span class="ticket-price">`)
4. Mets `"css_selector": ".ticket-price"` dans la config de l'événement

Le script ne regardera que ces éléments-là.

---

## Limitations à connaître

**Ticketmaster** — Leur protection anti-bot (403/CAPTCHA) bloque souvent les requêtes venant de serveurs. Si tu vois `HTTP 403` dans les logs pour un événement Ticketmaster, c'est ça. Options :
- Beaucoup d'événements Ticketmaster ont aussi des revendeurs sur billet.ca → surveille là
- Un proxy résidentiel peut contourner, mais c'est payant et fragile

**Cron GitHub Actions** — Le `*/30` n'est pas garanti à la minute près. En période de charge, GitHub peut retarder un run de 5 à 20 minutes. Pour du monitoring de prix, c'est correct. Si tu veux du vraiment fiable à la minute, un VPS à ~5 $/mois avec un vrai cron est l'upgrade logique.

**Détection de « paire »** — Le script détecte le prix le plus bas affiché, mais pas de façon fiable si c'est 1, 2 ou 4 billets ensemble (chaque site affiche ça différemment). Quand tu reçois l'alerte, clique le lien pour vérifier la quantité dispo. Si billet.ca affiche la quantité dans le même élément que le prix, un `css_selector` bien choisi peut aider.

**Pages JavaScript** — Si un site charge ses prix en JavaScript après coup (le HTML brut ne contient rien), le script verra « aucun prix détecté ». Dans ce cas, dis-le-moi et on regardera l'API interne du site ou une version avec navigateur headless.

---

## Note sur la revente au Québec

Petit heads-up : au Québec, la Loi sur la protection du consommateur encadre la revente de billets au-dessus du prix annoncé par le vendeur autorisé — c'est interdit sans l'autorisation du producteur. Surveiller les prix et acheter pour toi, aucun problème. Pour la revente à profit, informe-toi avant.

---

## Structure des fichiers

```
ticket-monitor/
├── .github/workflows/monitor.yml   ← cron GitHub Actions (30 min)
├── monitor.py                      ← le script de monitoring
├── config.json                     ← tes événements + seuils
├── state.json                      ← généré automatiquement (anti-spam)
└── README.md                       ← ce fichier
```
