# Modpack du serveur Minecraft (Deadlock)

Modpack au format [packwiz](https://packwiz.infra.link/) : Minecraft 1.21.1, NeoForge 21.1.265.
Les mods ne sont pas copiés ici : chaque fichier `*.pw.toml` indique où le télécharger (Modrinth) et son empreinte.

## Joueurs : installation (une seule fois)

1. Installer [Prism Launcher](https://prismlauncher.org/) et y connecter son compte Microsoft.
2. Télécharger **`joueurs/Serveur-Dorian-Prism.zip`** (bouton « Download raw file »).
3. Dans Prism : **Ajouter une instance > Importer**, choisir le zip, valider.
4. Lancer l'instance. Une petite fenêtre télécharge les mods, puis le jeu démarre.

À chaque lancement, les mods sont mis à jour automatiquement pour correspondre au serveur.
**Les réglages ne sont jamais réinitialisés** : graphismes, touches, minimap, JEI, packs de ressources
et shaders restent ceux du joueur. Seuls les mods et les réglages de gameplay communs suivent le serveur.

Envie de garder ses réglages actuels ? Copier `options.txt` (et le dossier `config` si besoin) de
l'ancienne instance dans le dossier `.minecraft` de la nouvelle (clic droit sur l'instance > Dossier), avant le
premier lancement.

## Dorian : modifier le modpack

1. Dans Modrinth App, ajouter, retirer ou mettre à jour des mods dans l'instance, puis l'exporter (.mrpack).
2. Dans Gitea, dossier `import/` : **Téléverser un fichier**, déposer le .mrpack.
3. La CI régénère le modpack (et recopie sur GitHub) : les joueurs l'ont au lancement suivant.
4. Serveur : Semaphore **« 🟩 Minecraft synchro serveur »**.

Réglages : les fichiers `*-common.toml` / `*-startup.toml` (gameplay) sont imposés à tous ; les autres
(client) ne sont posés qu'à la première installation. Les `*-server.toml` du serveur ne sont jamais écrasés.

## Outils

- `tools/mcpack.py` : import, MAJ, installation côté serveur (`python3 tools/mcpack.py` pour l'aide).
- `tools/packwiz-installer-bootstrap.jar` : [packwiz-installer-bootstrap](https://github.com/packwiz/packwiz-installer-bootstrap) (licence MIT).
