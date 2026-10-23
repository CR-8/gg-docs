"""Regenerate the architecture views into SITE_DIR (default ./site).

    python build.py                  # graphs routes deps index
    python build.py graphs deps      # only some: graphs routes deps index

Writes the views (graphify, Redoc and dependency-cruiser pages, import maps as Mermaid, the graph reports),
overview.json for the portal's Overview page, and ARCHITECTURE.md (the same content as Markdown). Inside the portal
container the worker runs this after every push to a default branch; tests are left to CI.
Env: BACKEND_DIR / FRONTEND_DIR (repo checkouts), SITE_DIR, WORK_DIR, BACKEND_PYTHON (an interpreter
with the backend's requirements, for the OpenAPI export).
"""
import ast
import json
import os
import shutil
import subprocess
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
# Absolute, because the tools below run with other working directories.
SITE = Path(os.environ.get("SITE_DIR", ROOT / "site")).resolve()
WORK = Path(os.environ.get("WORK_DIR", ROOT / "work")).resolve()
BACKEND = Path(os.environ.get("BACKEND_DIR", ROOT.parent / "backend")).resolve()
FRONTEND = Path(os.environ.get("FRONTEND_DIR", ROOT.parent / "frontend")).resolve()
WIN = os.name == "nt"
_beside = Path(sys.executable).with_name("graphify.exe" if WIN else "graphify")  # same env as this Python, not
GRAPHIFY = os.environ.get("GRAPHIFY_BIN") or (str(_beside) if _beside.exists() else shutil.which("graphify"))  # a stale PATH copy
BACKEND_PY = os.environ.get("BACKEND_PYTHON", sys.executable)
NPM = "npm.cmd" if WIN else "npm"
DEPCRUISE = ROOT / "node_modules" / "dependency-cruiser" / "bin"
CDN = "https://cdn.jsdelivr.net/npm"
os.environ["GRAPHIFY_NO_AUTO_REFRESH"] = "1"  # stop graphify rewriting ~/.claude/skills on every run


def run(cmd, cwd=ROOT):
    cmd = [str(c) for c in cmd]
    print("$", " ".join(cmd), flush=True)
    code = subprocess.run(cmd, cwd=cwd).returncode
    if code:
        print(f"   (exit {code})", flush=True)
    return code


# ---------- graphs: graphify AST graph, module tree, call flow ----------

def write_labels(g):
    """Name each community after the file most of its symbols live in (no LLM needed)."""
    data = json.loads((g / "graph.json").read_text(encoding="utf-8"))
    files = defaultdict(Counter)
    for n in data["nodes"]:
        if n.get("source_file") and "community" in n:
            files[n["community"]][n["source_file"].rsplit(".", 1)[0]] += 1
    labels = {}
    for c, f in files.items():
        top = f.most_common(1)[0][0]
        labels[str(c)] = top + (f" +{len(f) - 1} files" if len(f) > 1 else "")
    (g / ".graphify_labels.json").write_text(json.dumps(labels), encoding="utf-8")


def step_graphs():
    for side, src in (("backend", BACKEND / "app"), ("frontend", FRONTEND / "src")):
        out = WORK / side
        g = out / "graphify-out"
        run([GRAPHIFY, "extract", src, "--code-only", "--out", out])
        write_labels(g)
        run([GRAPHIFY, "cluster-only", out, "--no-label"])  # report + graph.html, picking up the labels
        run([GRAPHIFY, "tree", "--graph", g / "graph.json", "--output", g / "tree.html", "--label", f"{side} {src.name}/"])
        run([GRAPHIFY, "export", "callflow-html", out, "--output", g / "callflow.html", "--lang", "en"])
        for f in ("graph.html", "tree.html", "callflow.html", "GRAPH_REPORT.md"):
            if (g / f).exists():
                shutil.copy2(g / f, SITE / side / f)


# ---------- routes: FastAPI OpenAPI + Next.js app router ----------

def step_routes():
    spec, tmp = SITE / "backend" / "openapi.json", WORK / "openapi.tmp.json"
    tmp.unlink(missing_ok=True)
    # The backend's Settings require these at import; the export only reads routes, so placeholders do
    # (a real .env in the checkout still wins).
    env = {"SECRET_KEY": "openapi-export-only", "MONGODB_URL": "mongodb://127.0.0.1:1/openapi-export", **os.environ}
    cmd = [BACKEND_PY, "-c", "import json,sys; from app.main import app; "
           "open(sys.argv[1], 'w', encoding='utf-8').write(json.dumps(app.openapi()))", str(tmp)]
    print("$", " ".join(cmd), flush=True)
    subprocess.run(cmd, cwd=BACKEND, env=env)
    if tmp.exists():  # a failed import keeps the last good spec instead of blanking the API page
        tmp.replace(spec)
    (SITE / "backend" / "api.html").write_text(
        f'<!doctype html><html><head><meta charset="utf-8"><title>Backend API</title>'
        f'<meta name="viewport" content="width=device-width,initial-scale=1"></head><body style="margin:0">'
        f'<redoc spec-url="openapi.json"></redoc><script src="{CDN}/redoc@2.5.4/bundles/redoc.standalone.js"></script>'
        f'</body></html>', encoding="utf-8")


def next_routes():
    app = FRONTEND / "src" / "app"
    rows = []
    for f in sorted(app.rglob("*")):
        kind = {"page": "page", "route": "api"}.get(f.stem) if f.suffix in (".ts", ".tsx", ".js", ".jsx") else None
        parts = f.relative_to(app).parts[:-1]
        if not kind or any(p.startswith("_") for p in parts):  # _folders are private, not routable
            continue
        url = "/" + "/".join(p for p in parts if not p.startswith(("(", "@")))  # (groups) and @slots add no segment
        rows.append((url, kind, f.relative_to(FRONTEND).as_posix()))
    return rows


def backend_routes():
    spec = SITE / "backend" / "openapi.json"
    if not spec.exists():
        return {}
    by_tag = defaultdict(list)
    for path, item in json.loads(spec.read_text(encoding="utf-8"))["paths"].items():
        for method, op in item.items():
            if method in ("get", "post", "put", "patch", "delete", "head", "options"):
                by_tag[(op.get("tags") or ["untagged"])[0]].append((method.upper(), path, op.get("summary", "")))
    return dict(sorted(by_tag.items(), key=lambda kv: kv[0].lower()))


# ---------- deps: backend package imports (AST) + frontend dependency-cruiser ----------

def backend_imports():
    """Package-level import edges inside app/, read straight from the AST (pydeps without Graphviz)."""
    app = BACKEND / "app"

    def group(parts):  # longest directory prefix of a module path, cut at two levels, e.g. services/vmis
        d, cur = [], app
        for p in parts:
            cur = cur / p
            if not cur.is_dir():
                break
            d.append(p)
        return "/".join(d[:2]) or "app"

    edges = Counter()
    for f in app.rglob("*.py"):
        pkg = list(f.relative_to(app).parts[:-1])
        try:
            tree = ast.parse(f.read_text(encoding="utf-8", errors="replace"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                targets = [a.name.split(".")[1:] for a in node.names if a.name.split(".")[0] == "app"]
            elif isinstance(node, ast.ImportFrom):
                if node.level:
                    mod = pkg[:max(0, len(pkg) - node.level + 1)] + (node.module.split(".") if node.module else [])
                elif node.module and node.module.split(".")[0] == "app":
                    mod = node.module.split(".")[1:]
                else:
                    continue
                targets = [mod + [a.name] for a in node.names]
            else:
                continue
            src = group(pkg)
            for t in targets:
                if (dst := group(t)) != src:
                    edges[src, dst] += 1
    return edges


def fe_folder(rel):  # path under src/ -> app/<route>, otherwise its top-level folder
    p = rel.split("/")
    return "/".join(p[:2] if p[0] == "app" and len(p) > 2 else p[:1])


def be_package(rel):  # path under app/ -> its folder, two levels deep (matches backend_imports)
    return "/".join(rel.split("/")[:-1][:2]) or "app"


def to_mermaid(edges):
    ids = {}
    nid = lambda n: ids.setdefault(n, f"n{len(ids)}")
    lines = ["flowchart LR"]
    for (a, b), k in sorted(edges.items()):
        lines.append(f'  {nid(a)}["{a}"] -->|{k}| {nid(b)}["{b}"]')
    return "\n".join(lines)


def step_deps():
    mm = to_mermaid(backend_imports())
    for f in (WORK / "backend-imports.mmd", SITE / "backend" / "deps.mmd"):  # ARCHITECTURE.md, Import map view
        f.write_text(mm, encoding="utf-8")
    if not DEPCRUISE.exists():
        run([NPM, "ci", "--prefix", ROOT, "--no-audit", "--no-fund"])
    res = WORK / "frontend-deps.json"
    run(["node", DEPCRUISE / "dependency-cruiser.mjs", "--config", ROOT / "depcruise.cjs",
         "--output-type", "json", "--output-to", res, "src"], cwd=FRONTEND)
    run(["node", DEPCRUISE / "depcruise-fmt.mjs", "--output-type", "err-html",
         "--output-to", SITE / "frontend" / "deps-violations.html", res])
    edges = Counter()
    for m in json.loads(res.read_text(encoding="utf-8"))["modules"]:
        if not m["source"].startswith("src/") or ".test." in m["source"]:
            continue
        for d in m["dependencies"]:
            if d["resolved"].startswith("src/") and (a := fe_folder(m["source"][4:])) != (b := fe_folder(d["resolved"][4:])):
                edges[a, b] += 1
    mm = to_mermaid(edges)
    for f in (WORK / "frontend-imports.mmd", SITE / "frontend" / "deps.mmd"):
        f.write_text(mm, encoding="utf-8")


# ---------- overview.json: what the portal's Overview page (web/src/components/Overview.tsx) shows ----------

def git_ref(repo):
    out = lambda *a: subprocess.run(["git", "-C", str(repo), *a], capture_output=True, text=True).stdout.strip()
    return f"{out('rev-parse', '--abbrev-ref', 'HEAD')} @ {out('rev-parse', '--short', 'HEAD')}"


def graph_size(side):
    gj = WORK / side / "graphify-out" / "graph.json"
    if not gj.exists():
        return None, None
    nodes = json.loads(gj.read_text(encoding="utf-8"))["nodes"]
    return len(nodes), len({n.get("community") for n in nodes})


def write_overview():
    b_routes, f_routes = backend_routes(), next_routes()
    dc = WORK / "frontend-deps.json"
    out = {"built_at": datetime.now().astimezone().isoformat(timespec="seconds"), "sides": {}}
    for side, repo, routes in (("backend", BACKEND, b_routes), ("frontend", FRONTEND, f_routes)):
        symbols, modules = graph_size(side)
        out["sides"][side] = {"ref": git_ref(repo), "symbols": symbols, "modules": modules,
                              "files": sorted(p.name for p in (SITE / side).iterdir()), "routes": routes}
    out["sides"]["frontend"]["violations"] = (
        len(json.loads(dc.read_text(encoding="utf-8"))["summary"]["violations"]) if dc.exists() else None)
    (SITE / "overview.json").write_text(json.dumps(out), encoding="utf-8")


# ---------- ARCHITECTURE.md: the same content as one Markdown file ----------

def cell(s):  # safe inside a Markdown table cell
    return str(s).replace("|", "\\|").replace("\n", " ")


def inventory(side, group):
    """{module: {file: symbol count}} from the graphify graph, test files left out."""
    gj = WORK / side / "graphify-out" / "graph.json"
    if not gj.exists():
        return {}
    per_file = Counter(n["source_file"] for n in json.loads(gj.read_text(encoding="utf-8"))["nodes"]
                       if n.get("source_file") and ".test." not in n["source_file"])
    mods = defaultdict(dict)
    for f, k in per_file.items():
        mods[group(f)][f] = k
    return dict(sorted(mods.items()))


def write_markdown():
    b_routes, f_routes = backend_routes(), next_routes()
    b_inv, f_inv = inventory("backend", be_package), inventory("frontend", fe_folder)
    dc = WORK / "frontend-deps.json"
    violations = json.loads(dc.read_text(encoding="utf-8"))["summary"]["violations"] if dc.exists() else None

    md = ["# Architecture", "",
          f"Generated {datetime.now():%Y-%m-%d %H:%M} from backend `{git_ref(BACKEND)}` and frontend `{git_ref(FRONTEND)}` "
          "by `build.py`. The portal has the interactive version, plus activity, people, CI breakages and reviews.", "",
          "| | Backend (FastAPI, `app/`) | Frontend (Next.js, `src/`) |", "|---|---|---|",
          f"| Routes | {sum(map(len, b_routes.values()))} | {len(f_routes)} |",
          f"| Source files | {sum(map(len, b_inv.values()))} | {sum(map(len, f_inv.values()))} |",
          f"| Code symbols | {sum(sum(m.values()) for m in b_inv.values()):,} | {sum(sum(m.values()) for m in f_inv.values()):,} |",
          f"| Import rule violations | not checked | {'not run' if violations is None else len(violations)} |", ""]

    def modules(title, inv, mmd, note):
        out = [f"## {title}", "", "### Modules", "", "| Module | Files | Symbols |", "|---|---|---|"]
        out += [f"| `{m}` | {len(fs)} | {sum(fs.values())} |" for m, fs in inv.items()]
        out += ["", "<details><summary>Files in each module (symbol count)</summary>", ""]
        for m, fs in inv.items():
            out += [f"**{m}**", ""] + [f"- `{f}` ({k})" for f, k in sorted(fs.items())] + [""]
        out += ["</details>", ""]
        if (WORK / mmd).exists():
            out += ["### Import map", "", note, "", "```mermaid", (WORK / mmd).read_text(encoding="utf-8"), "```", ""]
        return out

    md += modules("Backend", b_inv, "backend-imports.mmd",
                  "Folders under `app/`, two levels deep. Arrow labels count imported names.")
    md += ["### API routes", ""]
    for tag, rows in b_routes.items():
        md += [f"#### {tag} ({len(rows)})", "", "| Method | Path | Summary |", "|---|---|---|"]
        md += [f"| {m} | `{cell(p)}` | {cell(s)} |" for m, p, s in sorted(rows, key=lambda r: r[1])] + [""]
    md += modules("Frontend", f_inv, "frontend-imports.mmd",
                  "One box per `app/` route folder, otherwise per top-level `src/` folder. Arrow labels count imports.")
    md += ["### Pages and API routes", "", "| URL | Kind | File |", "|---|---|---|"]
    md += [f"| `{cell(u)}` | {k} | `{p}` |" for u, k, p in f_routes] + [""]
    if violations:
        md += ["### Import rule violations", "", "| Rule | Severity | From | To |", "|---|---|---|---|"]
        md += [f"| {v['rule']['name']} | {v['rule']['severity']} | `{v['from']}` | `{v['to']}` |" for v in violations] + [""]
    (SITE / "ARCHITECTURE.md").write_text("\n".join(md), encoding="utf-8")


STEPS = {"graphs": step_graphs, "routes": step_routes, "deps": step_deps,
         "index": lambda: None}  # overview.json and ARCHITECTURE.md are rebuilt after any step
DEFAULT = ["graphs", "routes", "deps"]

if __name__ == "__main__":
    names = sys.argv[1:] or DEFAULT
    if bad := [n for n in names if n not in STEPS]:
        sys.exit(f"unknown step(s) {bad}; choose from {list(STEPS)}")
    for d in (SITE / "backend", SITE / "frontend", WORK):
        d.mkdir(parents=True, exist_ok=True)
    for n in names:
        print(f"\n=== {n}", flush=True)
        try:
            STEPS[n]()
        except Exception as e:  # keep going: the index links whatever did get built
            print(f"!!! {n} failed: {e!r}", flush=True)
    write_overview()
    write_markdown()
    print(f"\nviews written to {SITE}")
