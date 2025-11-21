# linear_to_calendar

Synchronise les Issues et Projects de Linear vers Google Calendar (create / upsert).
- Langage : Python 3.11+
- Exécution : GitHub Actions schedule (toutes les heures)
- Déduplication : on stocke l'ID Linear dans `extendedProperties.private.linear_id` pour upsert

Configuration requise
1. Linear API Key (Personal API Key) — stocker dans GitHub Secret `LINEAR_API_KEY`
2. Google Calendar
   - Créer un Google Cloud Project
   - Créer un Service Account, générer la clé JSON
   - Partager le calendrier Google (si non `primary`) avec l'adresse email du Service Account ou utiliser `primary` du compte lié
   - Copier le contenu JSON de la clé et le stocker dans GitHub Secret `GOOGLE_SERVICE_ACCOUNT_JSON`
   - Optionnel: définir `GCAL_CALENDAR_ID` dans les secrets (par défaut `primary`)
3. Créer un repo GitHub, push des fichiers ci-dessous
4. Ajouter les secrets GitHub: `LINEAR_API_KEY`, `GOOGLE_SERVICE_ACCOUNT_JSON`, `GCAL_CALENDAR_ID` (optionnel)

Utilisation
- Le workflow GitHub Actions exécute le script toutes les heures.
- Tu peux lancer manuellement via "Run workflow" (workflow_dispatch).

Limitations et conseils
- L’API Linear utilisée est GraphQL : adapte les queries si ton schéma diffère.
- Le script liste les événements dans une grosse fenêtre temporelle pour retrouver les events déjà liés : si tu as beaucoup d’événements, adapte la stratégie (index externe, stockage, ou recherche plus ciblée).
- Gérer les quotas et retries si nécessaire.
