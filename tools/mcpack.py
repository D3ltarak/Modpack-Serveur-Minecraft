#!/usr/bin/env python3
"""Modpack Minecraft au format packwiz : import d'un .mrpack, MAJ via Modrinth, installation côté serveur.

Le dépôt du modpack (Gitea, recopié sur GitHub public) est la référence unique :
  - les joueurs : packwiz-installer au lancement du jeu (mods synchronisés, réglages jamais écrasés) ;
  - le serveur : « install » ci-dessous (côté serveur seulement).

Commandes (sortie JSON sur une ligne, code 0 = succès) :
  import  <pack.mrpack> <dossier>          crée le modpack packwiz depuis un export Modrinth
  resolve <dossier>                         .jar ajoutés à la main → métadonnées Modrinth si trouvés (sha1)
  refresh <dossier>                         recalcule index.toml et pack.toml
  updates <dossier>                         nouvelles versions compatibles (Modrinth, même MC + NeoForge)
  apply   <dossier> <plan.json> [ids…]      applique des MAJ du plan (toutes, ou les mod-id donnés)
  install <dossier> <serveur> [--side server]  installe le modpack dans un dossier de serveur
  list    <dossier>                         mods du modpack (nom, fichier, version Modrinth, côté)
  reimport <pack.mrpack> <dossier>          remplace le contenu du modpack par un nouvel export Modrinth
  prism   <dossier> <url pack.toml> <bootstrap.jar> <sortie.zip>
                                            instance Prism Launcher prête à importer pour les joueurs

Bibliothèque standard uniquement (Python 3.8+).
"""
import hashlib
import json
import os
import re
import shutil
import sys
import urllib.error
import urllib.parse
import urllib.request
import zipfile

API = os.environ.get("MODRINTH_API", "https://api.modrinth.com/v2")
UA = os.environ.get("MODRINTH_UA", "homelab-infra-ansible/1.0 (minecraft modpack sync)")
MANAGED = ".mcpack-managed.json"  # fichiers posés par « install » (pour retirer les mods supprimés du pack)
# Réglages qui doivent être identiques partout (gameplay) : toujours remis à la version du pack.
# Les autres (graphismes, minimap, touches, JEI…) ne sont posés qu'une fois : le joueur garde les siens.
ENFORCED = re.compile(r"(-common|-server|-startup)(-\d+)?\.(toml|json5?)$|^config/(fml|neoforge-common)\.toml$")
SERVER_CFG = re.compile(r"^config/.*-server(-\d+)?\.toml$")
SKIP = re.compile(r"\.bak$|\.disabled$|(^|/)\.DS_Store$")


def out(ok, **data):
    print(json.dumps({"ok": ok, **data}, ensure_ascii=False))
    sys.exit(0 if ok else 1)


# --- TOML (sous-ensemble utilisé par packwiz) -----------------------------------------------
def q(v):
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    return json.dumps(str(v), ensure_ascii=False)


def toml_read(path):
    """Lit les fichiers packwiz : clés simples, [table], [table.sous], [[tableau]]."""
    data, cur = {}, None
    cur = data
    with open(path, encoding="utf-8") as fh:
        for raw in fh:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            m = re.match(r"^\[\[(.+)\]\]$", line)
            if m:
                cur = {}
                data.setdefault(m.group(1).strip(), []).append(cur)
                continue
            m = re.match(r"^\[(.+)\]$", line)
            if m:
                cur = data
                for part in m.group(1).strip().split("."):
                    cur = cur.setdefault(part.strip().strip('"'), {})
                continue
            k, _, v = line.partition("=")
            k, v = k.strip().strip('"'), v.strip()
            if v.startswith('"'):
                val = json.loads(v)
            elif v in ("true", "false"):
                val = v == "true"
            else:
                try:
                    val = int(v)
                except ValueError:
                    val = v
            cur[k] = val
    return data


def write(path, text):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)


def digest(path, algo):
    h = hashlib.new(algo)
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def slug(name):
    s = re.sub(r"\.(jar|zip)(\.disabled)?$", "", name, flags=re.I)
    s = re.sub(r"[-_ +]?(neo)?forge(d)?|[-_ +]?mc?1\.\d+(\.\d+)?|[-_ +]v?\d[\w.+-]*$", "", s, flags=re.I)
    s = re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")
    return s or "mod"


def pretty(filename):
    s = re.sub(r"\.(jar|zip)$", "", filename, flags=re.I)
    s = re.split(r"[-_ ](?=v?\d)", s)[0]
    return re.sub(r"[-_]+", " ", s).strip().title() or filename


def metafile(name, filename, side, url, sha1, sha512, mod_id="", version=""):
    lines = [f"name = {q(name)}", f"filename = {q(filename)}", f"side = {q(side)}", "",
             "[download]", f"url = {q(url)}", 'hash-format = "sha512"' if sha512 else 'hash-format = "sha1"',
             f"hash = {q(sha512 or sha1)}"]
    if mod_id:
        lines += ["", "[update]", "[update.modrinth]", f"mod-id = {q(mod_id)}", f"version = {q(version)}"]
    return "\n".join(lines) + "\n"


def side_of(env):
    env = env or {}
    if env.get("server") == "unsupported":
        return "client"
    if env.get("client") == "unsupported":
        return "server"
    return "both"


def unique(path):
    base, ext = os.path.splitext(path)
    n = 2
    while os.path.exists(path):
        path = f"{base}-{n}{ext}"
        n += 1
    return path


# --- Modrinth --------------------------------------------------------------------------------
def api(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(API + path, data=data, method=method,
                                 headers={"User-Agent": UA, "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:  # noqa: S310 - API Modrinth
        return json.loads(r.read().decode() or "null")


def pack_versions(d):
    p = toml_read(os.path.join(d, "pack.toml"))
    v = p.get("versions", {})
    loader = next((k for k in ("neoforge", "forge", "fabric", "quilt") if k in v), "")
    return v.get("minecraft", ""), loader, v.get(loader, "")


# --- Commandes -------------------------------------------------------------------------------
def cmd_import(mrpack, d):
    if os.path.exists(os.path.join(d, "pack.toml")):
        out(False, reason=f"{d} contient déjà un modpack")
    with zipfile.ZipFile(mrpack) as z:
        idx = json.loads(z.read("modrinth.index.json"))
        deps = idx.get("dependencies", {})
        loader = next((k for k in ("neoforge", "forge", "fabric-loader", "quilt-loader") if k in deps), "")
        stats = {"modrinth": 0, "disabled": [], "manual": [], "enforced": 0, "preserved": 0, "skipped": 0}
        for f in idx.get("files", []):
            path = f["path"]
            if path.endswith(".disabled"):
                stats["disabled"].append(os.path.basename(path))
                continue
            url = f["downloads"][0]
            m = re.search(r"/data/([A-Za-z0-9]{8})/versions/([A-Za-z0-9]{8})/", url)
            folder, filename = os.path.split(path)
            meta = unique(os.path.join(d, folder, slug(filename) + ".pw.toml"))
            write(meta, metafile(pretty(filename), filename, side_of(f.get("env")), url,
                                 f["hashes"].get("sha1", ""), f["hashes"].get("sha512", ""),
                                 m.group(1) if m else "", m.group(2) if m else ""))
            stats["modrinth"] += 1
        # Fichiers inclus dans l'export (configs, mods ajoutés à la main)
        for name in z.namelist():
            for prefix in ("overrides/", "client-overrides/", "server-overrides/"):
                if name.startswith(prefix) and not name.endswith("/"):
                    rel = name[len(prefix):]
                    if SKIP.search(rel):
                        stats["skipped"] += 1
                        continue
                    if rel.startswith("mods/"):
                        stats["manual"].append(os.path.basename(rel))
                    dest = os.path.join(d, rel)
                    os.makedirs(os.path.dirname(dest), exist_ok=True)
                    with z.open(name) as src, open(dest, "wb") as dst:
                        shutil.copyfileobj(src, dst)
    pack = [f"name = {q(idx.get('name', 'Modpack'))}", f"version = {q(idx.get('versionId', '1.0'))}",
            'pack-format = "packwiz:1.1.0"', "", "[index]", 'file = "index.toml"', 'hash-format = "sha256"',
            'hash = ""', "", "[versions]", f"minecraft = {q(deps.get('minecraft', ''))}"]
    if loader:
        pack.append(f"{loader.replace('-loader', '')} = {q(deps[loader])}")
    write(os.path.join(d, "pack.toml"), "\n".join(pack) + "\n")
    write(os.path.join(d, ".packwizignore"), "*.md\n.git*\n.mcpack*\ntools/\njoueurs/\nimport/\n")
    res = refresh(d)
    stats.update(enforced=res["enforced"], preserved=res["preserved"])
    out(True, **stats)


def ignored(d):
    """Motifs de .packwizignore (fichiers du dépôt qui ne font pas partie du modpack)."""
    import fnmatch
    pats = ["tools/", "joueurs/", "import/"]
    p = os.path.join(d, ".packwizignore")
    if os.path.exists(p):
        pats += [x.strip() for x in open(p, encoding="utf-8") if x.strip() and not x.startswith("#")]

    def test(rel):
        for pat in pats:
            if pat.endswith("/") and (rel + "/").startswith(pat.lstrip("/")):
                return True
            if fnmatch.fnmatch(rel, pat.lstrip("/")) or fnmatch.fnmatch(os.path.basename(rel), pat):
                return True
        return False
    return test


def refresh(d):
    """index.toml (empreinte de chaque fichier) puis pack.toml (empreinte de l'index)."""
    files, enforced, preserved = [], 0, 0
    skip = ignored(d)
    for root, dirs, names in os.walk(d):
        dirs[:] = sorted(x for x in dirs if not x.startswith("."))
        for n in sorted(names):
            p = os.path.join(root, n)
            rel = os.path.relpath(p, d).replace(os.sep, "/")
            if rel in ("pack.toml", "index.toml") or n.startswith(".") or rel.lower().endswith(".md") \
                    or SKIP.search(rel) or skip(rel):
                continue
            entry = [f"file = {q(rel)}", f"hash = {q(digest(p, 'sha256'))}"]
            if rel.endswith(".pw.toml"):
                entry.append("metafile = true")
            elif rel.startswith("config/") and not ENFORCED.search(rel) and not ENFORCED.search(os.path.basename(rel)):
                entry.append("preserve = true")  # posé une fois, jamais écrasé ensuite
                preserved += 1
            elif rel.startswith("config/"):
                enforced += 1
            files.append("[[files]]\n" + "\n".join(entry))
    write(os.path.join(d, "index.toml"), 'hash-format = "sha256"\n\n' + "\n\n".join(files) + "\n")
    pack_path = os.path.join(d, "pack.toml")
    text = open(pack_path, encoding="utf-8").read()
    text = re.sub(r'(\[index\][^\[]*?hash = )"[^"]*"', lambda m: m.group(1) + q(digest(os.path.join(d, "index.toml"), "sha256")),
                  text, count=1, flags=re.S)
    write(pack_path, text)
    return {"files": len(files), "enforced": enforced, "preserved": preserved}


def cmd_resolve(d):
    """Mods .jar posés à la main : remplacés par des métadonnées Modrinth si le fichier y est publié."""
    jars = [os.path.join(d, "mods", f) for f in sorted(os.listdir(os.path.join(d, "mods"))) if f.endswith(".jar")]
    if not jars:
        out(True, resolved=[], kept=[])
    hashes = {digest(p, "sha1"): p for p in jars}
    found = api("POST", "/version_files", {"hashes": list(hashes), "algorithm": "sha1"}) or {}
    resolved, kept = [], []
    for h, p in hashes.items():
        v = found.get(h)
        if not v:
            kept.append(os.path.basename(p))
            continue
        f = next((x for x in v["files"] if x["hashes"].get("sha1") == h), v["files"][0])
        meta = unique(os.path.join(d, "mods", slug(f["filename"]) + ".pw.toml"))
        write(meta, metafile(pretty(f["filename"]), f["filename"], "both", f["url"], h,
                             f["hashes"].get("sha512", ""), v["project_id"], v["id"]))
        os.remove(p)
        resolved.append(os.path.basename(p))
    refresh(d)
    out(True, resolved=resolved, kept=kept)


def mods(d):
    res = []
    for folder in ("mods", "resourcepacks", "shaderpacks"):
        base = os.path.join(d, folder)
        for n in sorted(os.listdir(base)) if os.path.isdir(base) else []:
            if n.endswith(".pw.toml"):
                t = toml_read(os.path.join(base, n))
                mr = t.get("update", {}).get("modrinth", {})
                res.append({"meta": f"{folder}/{n}", "name": t.get("name"), "filename": t.get("filename"),
                            "side": t.get("side", "both"), "mod_id": mr.get("mod-id", ""),
                            "version": mr.get("version", ""), "hash": t.get("download", {}).get("hash", ""),
                            "hash_format": t.get("download", {}).get("hash-format", "")})
            elif n.endswith(".jar"):
                res.append({"meta": "", "name": n, "filename": n, "side": "both", "mod_id": "", "version": "",
                            "file": f"{folder}/{n}"})
    return res


def cmd_updates(d):
    mc, loader, _ = pack_versions(d)
    ms = [m for m in mods(d) if m["mod_id"] and m["meta"].startswith("mods/")]
    by_hash = {m["hash"]: m for m in ms if m["hash_format"] == "sha512"}
    by_hash1 = {m["hash"]: m for m in ms if m["hash_format"] == "sha1"}
    plan = []
    for algo, table in (("sha512", by_hash), ("sha1", by_hash1)):
        if not table:
            continue
        latest = api("POST", "/version_files/update",
                     {"hashes": list(table), "algorithm": algo, "loaders": [loader], "game_versions": [mc]}) or {}
        for h, v in latest.items():
            m = table.get(h)
            if not m or v["id"] == m["version"]:
                continue
            f = next((x for x in v["files"] if x.get("primary")), v["files"][0])
            plan.append({"meta": m["meta"], "name": m["name"], "mod_id": m["mod_id"],
                         "from_file": m["filename"], "to_file": f["filename"], "from_version": m["version"],
                         "to_version": v["id"], "to_number": v.get("version_number", ""),
                         "url": f["url"], "sha1": f["hashes"].get("sha1", ""), "sha512": f["hashes"].get("sha512", ""),
                         "deps": [x["project_id"] for x in v.get("dependencies", [])
                                  if x.get("dependency_type") == "required" and x.get("project_id")]})
    known = {m["mod_id"] for m in mods(d)}
    for p in plan:  # dépendance requise absente du pack : la MAJ ne pourra pas démarrer
        p["missing_deps"] = [x for x in p.pop("deps") if x not in known]
    out(True, minecraft=mc, loader=loader, updates=sorted(plan, key=lambda p: p["name"].lower()),
        manual=[m["filename"] for m in mods(d) if not m["mod_id"]])


def cmd_apply(d, plan_path, only):
    plan = json.load(open(plan_path, encoding="utf-8")).get("updates", [])
    done = []
    for p in plan:
        if only and p["mod_id"] not in only:
            continue
        path = os.path.join(d, p["meta"])
        t = toml_read(path)
        write(path, metafile(t.get("name") or p["name"], p["to_file"], t.get("side", "both"), p["url"],
                             p["sha1"], p["sha512"], p["mod_id"], p["to_version"]))
        done.append({"name": p["name"], "from": p["from_file"], "to": p["to_file"]})
    refresh(d)
    out(True, applied=done)


def fetch(url, dest, algo, want):
    tmp = dest + ".part"
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=300) as r, open(tmp, "wb") as fh:  # noqa: S310
        shutil.copyfileobj(r, fh)
    if digest(tmp, algo) != want:
        os.remove(tmp)
        raise ValueError(f"empreinte incorrecte pour {os.path.basename(dest)}")
    os.replace(tmp, dest)


def cmd_install(d, target, side):
    """Pose le modpack dans un dossier de serveur : mods du bon côté, configs (sauf préservées déjà là),
    retire les fichiers posés précédemment qui ne font plus partie du pack. Rien d'autre n'est touché.
    Tout est téléchargé et vérifié avant de remplacer quoi que ce soit."""
    idx = toml_read(os.path.join(d, "index.toml")).get("files", [])
    state_path = os.path.join(target, MANAGED)
    before = set(json.load(open(state_path, encoding="utf-8"))) if os.path.exists(state_path) else set()
    staging = os.path.join(target, ".mcpack-staging")
    shutil.rmtree(staging, ignore_errors=True)
    os.makedirs(staging)
    now, todo, kept = set(), [], 0
    try:
        for e in idx:
            rel = e["file"]
            if side == "server" and rel.startswith(("resourcepacks/", "shaderpacks/")):
                continue
            # Réglages serveur : ceux du serveur font foi (ceux du pack viennent d'une instance de jeu)
            if side == "server" and SERVER_CFG.search(rel):
                continue
            if e.get("metafile"):
                t = toml_read(os.path.join(d, rel))
                if t.get("side", "both") not in ("both", side):
                    continue
                dest_rel = os.path.join(os.path.dirname(rel), t["filename"]).replace(os.sep, "/")
                dl = t["download"]
                now.add(dest_rel)
                dest = os.path.join(target, dest_rel)
                if os.path.exists(dest) and digest(dest, dl["hash-format"]) == dl["hash"]:
                    kept += 1
                    continue
                tmp = os.path.join(staging, str(len(todo)))
                fetch(dl["url"], tmp, dl["hash-format"], dl["hash"])
                todo.append((tmp, dest_rel))
            else:
                dest = os.path.join(target, rel)
                now.add(rel)
                if (e.get("preserve") and os.path.exists(dest)) or (
                        os.path.exists(dest) and digest(dest, "sha256") == e["hash"]):
                    kept += 1
                    continue
                tmp = os.path.join(staging, str(len(todo)))
                shutil.copy2(os.path.join(d, rel), tmp)
                todo.append((tmp, rel))
        for tmp, rel in todo:
            dest = os.path.join(target, rel)
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            os.replace(tmp, dest)
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    removed = []
    for rel in sorted(before - now):
        p = os.path.join(target, rel)
        if os.path.isfile(p) and not rel.startswith("config/"):
            os.remove(p)
            removed.append(rel)
    with open(state_path, "w", encoding="utf-8") as fh:
        json.dump(sorted(now), fh, indent=1)
    mods_dir = os.path.join(target, "mods")
    extra = sorted(f for f in (os.listdir(mods_dir) if os.path.isdir(mods_dir) else [])
                   if f.endswith(".jar") and f"mods/{f}" not in now)
    out(True, added=[r for _, r in todo], removed=removed, unchanged=kept, extra=extra)


def cmd_reimport(mrpack, d):
    """Nouvel export Modrinth App : mods, packs et configs remplacés, le reste du dépôt est gardé."""
    import tempfile
    tmp = tempfile.mkdtemp(prefix="mcpack-")
    try:
        import io
        import contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            try:
                cmd_import(mrpack, os.path.join(tmp, "p"))
            except SystemExit as exc:
                if exc.code:
                    raise
        stats = json.loads(buf.getvalue())
        before = {m["filename"] for m in mods(d)} if os.path.exists(os.path.join(d, "pack.toml")) else set()
        for folder in ("mods", "resourcepacks", "shaderpacks", "config"):
            shutil.rmtree(os.path.join(d, folder), ignore_errors=True)
            src = os.path.join(tmp, "p", folder)
            if os.path.isdir(src):
                shutil.copytree(src, os.path.join(d, folder))
        # pack.toml : nom, version et versions du jeu de l'export
        shutil.copy2(os.path.join(tmp, "p", "pack.toml"), os.path.join(d, "pack.toml"))
        if not os.path.exists(os.path.join(d, ".packwizignore")):
            shutil.copy2(os.path.join(tmp, "p", ".packwizignore"), os.path.join(d, ".packwizignore"))
        refresh(d)
        after = {m["filename"] for m in mods(d)}
        stats.pop("ok", None)
        out(True, added=sorted(after - before), removed=sorted(before - after), **stats)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def cmd_prism(d, url, bootstrap, dest):
    """Instance Prism Launcher : Minecraft + NeoForge du pack, packwiz-installer lancé avant chaque partie."""
    mc, loader, lv = pack_versions(d)
    name = toml_read(os.path.join(d, "pack.toml")).get("name", "Modpack")
    uids = {"neoforge": "net.neoforged", "forge": "net.minecraftforge", "fabric": "net.fabricmc.fabric-loader",
            "quilt": "org.quiltmc.quilt-loader"}
    comps = [{"uid": "net.minecraft", "version": mc, "important": True}]
    if loader:
        comps.append({"uid": uids[loader], "version": lv})
    cfg = "\n".join([
        "[General]", "ConfigVersion=1.2", "InstanceType=OneSix", f"name={name}", "iconKey=default",
        "OverrideCommands=true",
        f'PreLaunchCommand="$INST_JAVA" -jar packwiz-installer-bootstrap.jar {url}', ""])
    import unicodedata
    ascii_name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    folder = re.sub(r"[^A-Za-z0-9.]+", "-", ascii_name).strip("-") or "modpack"
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(f"{folder}/instance.cfg", cfg)
        z.writestr(f"{folder}/mmc-pack.json", json.dumps({"components": comps, "formatVersion": 1}, indent=1))
        z.write(bootstrap, f"{folder}/.minecraft/packwiz-installer-bootstrap.jar")
    out(True, zip=dest, minecraft=mc, loader=f"{loader} {lv}")


def main():
    a = sys.argv[1:]
    try:
        if a[:1] == ["import"] and len(a) == 3:
            cmd_import(a[1], a[2])
        if a[:1] == ["resolve"] and len(a) == 2:
            cmd_resolve(a[1])
        if a[:1] == ["refresh"] and len(a) == 2:
            out(True, **refresh(a[1]))
        if a[:1] == ["updates"] and len(a) == 2:
            cmd_updates(a[1])
        if a[:1] == ["apply"] and len(a) >= 3:
            cmd_apply(a[1], a[2], set(a[3:]))
        if a[:1] == ["install"] and len(a) >= 3:
            cmd_install(a[1], a[2], a[a.index("--side") + 1] if "--side" in a else "server")
        if a[:1] == ["reimport"] and len(a) == 3:
            cmd_reimport(a[1], a[2])
        if a[:1] == ["prism"] and len(a) == 5:
            cmd_prism(a[1], a[2], a[3], a[4])
        if a[:1] == ["list"] and len(a) == 2:
            out(True, mods=mods(a[1]))
    except SystemExit:
        raise
    except urllib.error.HTTPError as exc:
        out(False, reason=f"Modrinth : HTTP {exc.code}")
    except Exception as exc:  # noqa: BLE001 - message lisible dans le rapport
        out(False, reason=f"{type(exc).__name__} : {exc}")
    out(False, reason="usage : voir l'en-tête de mcpack.py")


if __name__ == "__main__":
    main()
