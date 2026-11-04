# CLAUDE.md (undo-effects)

Plugin Hermes Agent (candidature au hackathon plugins, date limite 11/11/2026 17 h New York). Reprise du
chantier : `.work/HANDOFF-hackathon-hermes.md` à la racine du parc.

## Règles
- Le plugin ne bloque, ne réécrit et n'approuve jamais un appel d'outil (règle 12 du catalogue) : il sauvegarde
  puis laisse faire. Aucun remplacement de fonction du cœur (règle 9).
- Chaque modification passe `python -m pytest tests -q`, puis `hermes plugins validate` à l'identique de la
  CI du catalogue, sur l'A1 dans `/home/hermes/hk-test` (HERMES_HOME isolé, jamais un profil réel).
- README, PR et textes publics en anglais ; commits en français, format conventionnel.
- `platforms:` du catalogue = ce qui est réellement testé (Linux testé le 4/11 ; Windows non testé).

## Pièges
- `plugin.yaml` doit déclarer `provides_middleware`, sinon `hermes plugins validate` échoue ; le chargeur
  affiche pourtant « unknown manifest field » (avertissement inoffensif, à signaler en amont).
- Ordre mesuré le 4/11 sur Hermes 0.22.7 (terminal, sonde sur l'A1) : tool_request, pre_tool_call, tool_execution
  (entrée), porte d'approbation du terminal, tool_execution (sortie). La doc place l'approbation avant
  `tool_execution` : elle se trompe pour le terminal. Sans effet ici (le plugin ne modifie rien) ; un appel refusé
  ne laisse pas d'entrée.
- Hermes refuse `write_file` sur un fichier que l'agent n'a pas lu : le test de bout en bout lit d'abord.
- Le dossier du dépôt n'est pas importable (tiret) : les tests chargent le paquet sous `undo_effects` ; `pytest.ini`
  vit dans `tests/` car un `__init__.py` à la racine trompe pytest.
