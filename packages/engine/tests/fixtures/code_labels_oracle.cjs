// The TypeScript compiler's answers for build_code_labels.py:
// node code_labels_oracle.cjs <typescript> <root> <files.json> <answers.json>
// For every call, `new`, JSX tag and prop given a name, in every listed file: the line, the name
// used, and the declaration it refers to (file and name, through imports and barrels), when that
// declaration is in the repository.
const ts = require(process.argv[2]);
const fs = require("fs"),
  path = require("path");
const [root, filesPath, outPath] = process.argv.slice(3);
const files = JSON.parse(fs.readFileSync(filesPath, "utf8"));
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
        jsx: ts.JsxEmit.ReactJSX,
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
const groups = new Map();
for (const file of files) {
  const abs = path.join(root, file),
    config = configFor(abs);
  if (!groups.has(config.key)) groups.set(config.key, { options: config.options, files: [] });
  groups.get(config.key).files.push(abs);
}
const rows = [];
for (const group of groups.values()) {
  const program = ts.createProgram(group.files, {
    ...group.options,
    noEmit: true,
    allowJs: true,
    skipLibCheck: true,
    types: [],
  });
  const checker = program.getTypeChecker();
  for (const abs of group.files) {
    const sf = program.getSourceFile(abs);
    if (!sf) continue;
    const file = path.relative(root, abs);
    const record = (name, at) => {
      let symbol = checker.getSymbolAtLocation(name);
      if (symbol && symbol.flags & ts.SymbolFlags.Alias) symbol = checker.getAliasedSymbol(symbol);
      const declaration = symbol && symbol.declarations && symbol.declarations[0];
      if (!declaration || !inRoot(declaration.getSourceFile().fileName)) return;
      const declared = declaration.name && declaration.name.text;
      if (!declared) return;
      const line = sf.getLineAndCharacterOfPosition(at.getStart()).line + 1;
      const target = path.relative(root, declaration.getSourceFile().fileName);
      rows.push([file, line, name.text, target, declared]);
    };
    const nameOf = (e) =>
      ts.isIdentifier(e) ? e : ts.isPropertyAccessExpression(e) ? e.name : null;
    const visit = (node) => {
      if (ts.isCallExpression(node) || ts.isNewExpression(node)) {
        const name = nameOf(node.expression);
        if (name) record(name, node);
      } else if (ts.isJsxOpeningElement(node) || ts.isJsxSelfClosingElement(node)) {
        const name = nameOf(node.tagName);
        if (name) record(name, node.tagName);
      } else if (
        ts.isJsxAttribute(node) &&
        node.initializer &&
        ts.isJsxExpression(node.initializer) &&
        node.initializer.expression
      ) {
        const name = nameOf(node.initializer.expression);
        if (name) record(name, node.initializer.expression);
      }
      ts.forEachChild(node, visit);
    };
    visit(sf);
  }
}
fs.writeFileSync(outPath, JSON.stringify(rows));
