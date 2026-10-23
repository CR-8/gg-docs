// dependency-cruiser rules for the frontend; build.py runs it from Genegenius-phase-1/.
// Next.js picks these files up by name, so nothing imports them. (No nested quantifiers: depcruise rejects unsafe regexes.)
const NEXT_ENTRY = "(^|/)(page|layout|route|loading|error|not-found|template|default|global-error)\\.[jt]sx?$";

module.exports = {
  forbidden: [
    { name: "no-circular", severity: "warn", from: {}, to: { circular: true } },
    { name: "not-to-unresolvable", severity: "error", from: {}, to: { couldNotResolve: true } },
    { name: "not-to-test", severity: "error", from: { pathNot: "\\.test\\.tsx?$" }, to: { path: "\\.test\\.tsx?$" } },
    {
      name: "no-orphans",
      severity: "info",
      from: { orphan: true, pathNot: [NEXT_ENTRY, "\\.d\\.ts$", "\\.test\\.tsx?$", "^src/(middleware|instrumentation)\\.ts$", "^src/test/"] },
      to: {},
    },
  ],
  options: {
    doNotFollow: { path: "node_modules" },
    tsConfig: { fileName: "tsconfig.json" },
    tsPreCompilationDeps: true,
    enhancedResolveOptions: {
      exportsFields: ["exports"],
      conditionNames: ["import", "require", "node", "default", "types"],
      extensions: [".ts", ".tsx", ".d.ts", ".js", ".jsx", ".mjs", ".cjs", ".json"],
      mainFields: ["module", "main", "types", "typings"],
    },
  },
};
