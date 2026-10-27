// The TypeScript compiler's answers for build_import_labels.py:
// node import_labels_oracle.cjs <typescript> <root> <rows.json> <answers.json>
const ts = require(process.argv[2]);
const fs = require("fs"),
  path = require("path");
const [root, rowsPath, outPath] = process.argv.slice(3);
const rows = JSON.parse(fs.readFileSync(rowsPath, "utf8"));
const inRoot = (f) => f && f.startsWith(root + "/") && !f.includes("/node_modules/");
const configs = new Map();
function configFor(abs) {
  const found = ts.findConfigFile(path.dirname(abs), ts.sys.fileExists, "tsconfig.json");
  if (!found || !inRoot(found))
    return {
      key: "",
      options: {
        moduleResolution: ts.ModuleResolutionKind.Bundler,
        module: ts.ModuleKind.ESNext,
        allowJs: true,
      },
    };
  if (!configs.has(found)) {
    const parsed = ts.getParsedCommandLineOfConfigFile(
      found,
      { ignoreDeprecations: "6.0", allowJs: true },
      { ...ts.sys, onUnRecoverableConfigFileDiagnostic: () => {} },
    );
    configs.set(found, { key: found, options: parsed ? parsed.options : {} });
  }
  return configs.get(found);
}
const files = {},
  groups = new Map();
for (const r of rows) {
  const abs = path.join(root, r.importer),
    c = configFor(abs);
  const res = ts.resolveModuleName(r.source, abs, c.options, ts.sys).resolvedModule;
  files[`${r.importer}:${r.line}:${r.source}`] =
    res && inRoot(res.resolvedFileName) ? path.relative(root, res.resolvedFileName) : null;
  if (!groups.has(c.key)) groups.set(c.key, { options: c.options, files: new Set() });
  groups.get(c.key).files.add(abs);
}
const symbols = {};
for (const g of groups.values()) {
  const program = ts.createProgram([...g.files], {
    ...g.options,
    noEmit: true,
    allowJs: true,
    skipLibCheck: true,
    types: [],
  });
  const checker = program.getTypeChecker();
  for (const abs of g.files) {
    const sf = program.getSourceFile(abs);
    if (!sf) continue;
    const importer = path.relative(root, abs);
    const record = (node, statement) => {
      let sym = checker.getSymbolAtLocation(node);
      if (!sym) return;
      if (sym.flags & ts.SymbolFlags.Alias) sym = checker.getAliasedSymbol(sym);
      const d = sym.declarations && sym.declarations[0];
      if (!d) return;
      const file = d.getSourceFile().fileName;
      const line = sf.getLineAndCharacterOfPosition(statement.getStart()).line + 1;
      const named = !ts.isExportAssignment(d) && !ts.isSourceFile(d) && d.name && d.name.text;
      symbols[`${importer}:${line}:${node.text}`] = {
        file: inRoot(file) ? path.relative(root, file) : null,
        name: named || null,
      };
    };
    for (const n of sf.statements) {
      if (ts.isImportDeclaration(n) && n.importClause) {
        const c = n.importClause;
        if (c.name) record(c.name, n);
        if (c.namedBindings && ts.isNamedImports(c.namedBindings))
          for (const e of c.namedBindings.elements) record(e.name, n);
      } else if (
        ts.isExportDeclaration(n) &&
        n.moduleSpecifier &&
        n.exportClause &&
        ts.isNamedExports(n.exportClause)
      ) {
        for (const e of n.exportClause.elements) record(e.name, n);
      }
    }
  }
}
fs.writeFileSync(outPath, JSON.stringify({ files, symbols }));
