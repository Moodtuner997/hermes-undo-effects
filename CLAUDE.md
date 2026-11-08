# CLAUDE.md (undo-effects)

Plugin Hermes Agent (candidature au hackathon plugins, date limite 11/11/2026 13 h PST = 22 h Paris). Reprise du
chantier : `.work/HANDOFF-hackathon-hermes.md` à la racine du parc.

## Règles
- Le plugin ne bloque, ne réécrit et n'approuve jamais un appel d'outil (règle 12 du catalogue) : il sauvegarde
  puis laisse faire. Aucun remplacement de fonction du cœur (règle 9).
- Aucun réseau, aucun processus enfant, aucun outil, aucun cron : le test `test_no_network_no_child_process_and_the_command_is_never_run`
  interdit `socket`, `Popen` et `os.system` pendant un passage du middleware. Ne rien ajouter de tel sans remettre la ligne de divulgation du catalogue
  d'accord avec le code.
- Tout calcul que dicte le texte d'une commande du modèle est borné (accolades `{a,b}` : 256 mots au plus, résumé et notes
  tronqués avant expurgation) : un plugin qui gèle `terminal` gèle tous les outils.
- Chaque modification passe `python -m pytest tests -q`, puis `hermes plugins validate` à l'identique de la CI du
  catalogue, puis `tests/e2e_hermes.py` (vrais appels d'outils, sans modèle). La CI (`.github/workflows/ci.yml`) le fait
  sur ubuntu, windows et macos-latest avec Hermes `v0.22.7` ; en local : `HERMES_HOME` jetable avec `config.yaml`
  `plugins: {enabled: [undo-effects]}` et `approvals: {mode: manual}`, plugin copié dans `plugins/undo-effects`,
  `HERMES_INTERACTIVE=1`, interpréteur de Hermes (jamais un profil réel).
- README, PR et textes publics en anglais ; commits en français, format conventionnel.
- `platforms:` du catalogue = ce qui est réellement testé : Linux, Windows et macOS (arm64) par la CI GitHub sur Hermes
  0.22.7 ; une version ne s'annonce macOS que si ce job est vert. Pas de Mac Intel, pas d'autre Hermes que 0.22.7.

## Pièges
- `plugin.yaml` doit déclarer `provides_middleware`, sinon `hermes plugins validate` échoue ; le chargeur
  affiche pourtant « unknown manifest field » (avertissement inoffensif, à signaler en amont).
- Ordre mesuré le 4/11 sur Hermes 0.22.7 (terminal, sonde sur l'A1) : tool_request, pre_tool_call, tool_execution
  (entrée), porte d'approbation du terminal, tool_execution (sortie). La doc place l'approbation avant
  `tool_execution` : elle se trompe pour le terminal. Sans effet ici (le plugin ne modifie rien) ; un appel refusé
  ne laisse pas d'entrée (vérifié par l'e2e, refus puis accord).
- Hermes refuse `write_file` sur un fichier que l'agent n'a pas lu. Sous Windows (0.22.7, Python 3.14), sa garde compare
  `os.fstat` et `os.stat`, dont le `ctime` diffère pour un fichier créé puis écrit plus tard : le fichier ne compte jamais
  comme lu en entier et l'écrasement est refusé « (partial view) », une fois sur deux sur le laptop. Une relecture identique
  est servie par le cache de Hermes et n'y change rien. L'e2e pose `HERMES_DISABLE_FILE_STATE_GUARD=1` (réglage de Hermes).
- `approvals.mode` vaut `smart` par défaut chez Hermes (lu avec `_get_approval_mode`, 8/11) : `rm -rf` appelle alors un modèle
  auxiliaire et, sur une racine de données neuve, relance le processus (`hermes_bootstrap`). L'e2e exige `manual` et répond à
  la porte par `tools.terminal_tool.set_approval_callback`.
- Le scanner de `hermes plugins validate` (`skills_guard`, règle `destructive_root_rm`) lit aussi `tests/` : le motif
  `rm -rf` suivi d'opérandes puis d'un `/`, même lignes plus loin (un `(work / "proj")` suffit), donne « caution » au lieu de
  « safe ». La commande dangereuse de l'e2e vit donc dans une constante suivie d'un commentaire (`DANGEROUS`).
- Le dossier du dépôt n'est pas importable (tiret) : les tests chargent le paquet sous `undo_effects` ; `pytest.ini`
  vit dans `tests/` car un `__init__.py` à la racine trompe pytest.
- Le magasin (`HERMES_HOME/plugin-data/undo-effects`) est un dossier ordinaire : une commande du modèle qui le supprime
  emporte l'historique (mesuré le 8/11, seule reste l'entrée de la suppression). Dit dans le README, rien à y faire.
- Les fichiers visés directement dans `.ssh`, `.kube`, `.docker`… étaient copiés avant 0.1.1 (le dossier n'était testé que
  lors d'un parcours de dossier) ; le résumé de commande gardait les secrets en clair. Corrigés en 0.1.1, tickets 2 à 5.
