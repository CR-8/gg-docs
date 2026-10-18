// The generated architecture views (build.py writes the files into /site/<side>/).
export const SIDES = {
  backend: { name: "Backend", stack: "FastAPI", src: "app/" },
  frontend: { name: "Frontend", stack: "Next.js", src: "src/" },
} as const
export type Side = keyof typeof SIDES

export const VIEWS = {
  graph: { name: "Module graph", desc: "Code clustered into modules, interactive", file: "graph.html" },
  tree: { name: "Module tree", desc: "Every folder, file and symbol", file: "tree.html" },
  callflow: { name: "Call flow", desc: "Call diagrams per area", file: "callflow.html" },
  deps: { name: "Import map", desc: "Which folder imports which", file: "deps.mmd" },
  api: { name: "API reference", desc: "Every route, with its schemas", file: "api.html", only: "backend" },
  rules: { name: "Dependency rules", desc: "Circular, orphan and unresolvable imports", file: "deps-violations.html", only: "frontend" },
  report: { name: "Graph report", desc: "Hubs and surprising links", file: "GRAPH_REPORT.md" },
} as const
export type View = keyof typeof VIEWS

export const viewsFor = (side: Side) =>
  (Object.keys(VIEWS) as View[]).filter((v) => !("only" in VIEWS[v]) || VIEWS[v].only === side)
