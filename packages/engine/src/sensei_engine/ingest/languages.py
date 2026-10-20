"""A file's language: by name, then extension, then shebang, then ``identify``'s tags.

Language ids are short lowercase names (``python``, ``tsx``, ``dockerfile``). ``DISPLAY_NAMES``
maps the programming and markup ones to GitHub's names, so per-language byte counts read the same
as GitHub's language bar; data formats (JSON, YAML) and prose (Markdown) are left out there, as
GitHub leaves them out.
"""

from __future__ import annotations

import re
from pathlib import PurePosixPath

from identify import identify

BY_NAME: dict[str, str] = {
    "dockerfile": "dockerfile",
    "containerfile": "dockerfile",
    "makefile": "makefile",
    "gnumakefile": "makefile",
    "jenkinsfile": "groovy",
    "gemfile": "ruby",
    "rakefile": "ruby",
    "podfile": "ruby",
    "vagrantfile": "ruby",
    "brewfile": "ruby",
    "pipfile": "toml",
    "cmakelists.txt": "cmake",
    "justfile": "just",
    "procfile": "procfile",
    ".env": "dotenv",
    ".gitignore": "ignore",
    ".dockerignore": "ignore",
    ".senseiignore": "ignore",
    ".gitattributes": "gitattributes",
    ".editorconfig": "ini",
    ".python-version": "text",
    ".nvmrc": "text",
    "license": "text",
    "readme": "text",
}

BY_EXTENSION: dict[str, str] = {
    ".py": "python",
    ".pyi": "python",
    ".pyw": "python",
    ".ipynb": "jupyter",
    ".ts": "typescript",
    ".mts": "typescript",
    ".cts": "typescript",
    ".tsx": "tsx",
    ".js": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".jsx": "jsx",
    ".vue": "vue",
    ".svelte": "svelte",
    ".astro": "astro",
    ".json": "json",
    ".jsonc": "json",
    ".json5": "json",
    ".yaml": "yaml",
    ".yml": "yaml",
    ".toml": "toml",
    ".ini": "ini",
    ".cfg": "ini",
    ".conf": "ini",
    ".properties": "properties",
    ".xml": "xml",
    ".plist": "xml",
    ".md": "markdown",
    ".mdx": "mdx",
    ".markdown": "markdown",
    ".rst": "rst",
    ".adoc": "asciidoc",
    ".txt": "text",
    ".csv": "csv",
    ".tsv": "csv",
    ".sql": "sql",
    ".prisma": "prisma",
    ".graphql": "graphql",
    ".gql": "graphql",
    ".proto": "protobuf",
    ".html": "html",
    ".htm": "html",
    ".jinja": "jinja",
    ".jinja2": "jinja",
    ".j2": "jinja",
    ".mako": "mako",
    ".hbs": "handlebars",
    ".ejs": "ejs",
    ".css": "css",
    ".scss": "scss",
    ".sass": "sass",
    ".less": "less",
    ".svg": "svg",
    ".go": "go",
    ".java": "java",
    ".kt": "kotlin",
    ".kts": "kotlin",
    ".scala": "scala",
    ".groovy": "groovy",
    ".gradle": "groovy",
    ".rs": "rust",
    ".rb": "ruby",
    ".php": "php",
    ".c": "c",
    ".h": "c",
    ".cc": "cpp",
    ".cpp": "cpp",
    ".cxx": "cpp",
    ".hh": "cpp",
    ".hpp": "cpp",
    ".hxx": "cpp",
    ".m": "objective-c",
    ".mm": "objective-cpp",
    ".cs": "csharp",
    ".fs": "fsharp",
    ".swift": "swift",
    ".dart": "dart",
    ".lua": "lua",
    ".r": "r",
    ".jl": "julia",
    ".ex": "elixir",
    ".exs": "elixir",
    ".erl": "erlang",
    ".hs": "haskell",
    ".ml": "ocaml",
    ".clj": "clojure",
    ".zig": "zig",
    ".nim": "nim",
    ".pl": "perl",
    ".pm": "perl",
    ".sh": "shell",
    ".bash": "shell",
    ".zsh": "shell",
    ".fish": "fish",
    ".ps1": "powershell",
    ".bat": "batch",
    ".cmd": "batch",
    ".tf": "hcl",
    ".tfvars": "hcl",
    ".hcl": "hcl",
    ".nix": "nix",
    ".cmake": "cmake",
    ".mk": "makefile",
    ".sol": "solidity",
}

# Interpreter (from the shebang) to language.
BY_INTERPRETER: dict[str, str] = {
    "python": "python",
    "node": "javascript",
    "deno": "typescript",
    "bun": "typescript",
    "tsx": "typescript",
    "ts-node": "typescript",
    "sh": "shell",
    "bash": "shell",
    "zsh": "shell",
    "dash": "shell",
    "ksh": "shell",
    "fish": "fish",
    "ruby": "ruby",
    "perl": "perl",
    "php": "php",
    "lua": "lua",
    "Rscript": "r",
}

# identify tags (for names the maps above don't know) to language.
BY_TAG: dict[str, str] = {
    "python": "python",
    "javascript": "javascript",
    "ts": "typescript",
    "shell": "shell",
    "yaml": "yaml",
    "json": "json",
    "toml": "toml",
    "markdown": "markdown",
    "html": "html",
    "xml": "xml",
}

# GitHub's names for the languages its language bar counts (programming and markup).
DISPLAY_NAMES: dict[str, str] = {
    "python": "Python",
    "jupyter": "Jupyter Notebook",
    "typescript": "TypeScript",
    "tsx": "TypeScript",
    "javascript": "JavaScript",
    "jsx": "JavaScript",
    "vue": "Vue",
    "svelte": "Svelte",
    "astro": "Astro",
    "sql": "SQL",
    "html": "HTML",
    "jinja": "Jinja",
    "mako": "Mako",
    "handlebars": "Handlebars",
    "ejs": "EJS",
    "css": "CSS",
    "scss": "SCSS",
    "sass": "Sass",
    "less": "Less",
    "go": "Go",
    "java": "Java",
    "kotlin": "Kotlin",
    "scala": "Scala",
    "groovy": "Groovy",
    "rust": "Rust",
    "ruby": "Ruby",
    "php": "PHP",
    "c": "C",
    "cpp": "C++",
    "objective-c": "Objective-C",
    "objective-cpp": "Objective-C++",
    "csharp": "C#",
    "fsharp": "F#",
    "swift": "Swift",
    "dart": "Dart",
    "lua": "Lua",
    "r": "R",
    "julia": "Julia",
    "elixir": "Elixir",
    "erlang": "Erlang",
    "haskell": "Haskell",
    "ocaml": "OCaml",
    "clojure": "Clojure",
    "zig": "Zig",
    "nim": "Nim",
    "perl": "Perl",
    "shell": "Shell",
    "fish": "fish",
    "powershell": "PowerShell",
    "batch": "Batchfile",
    "hcl": "HCL",
    "nix": "Nix",
    "cmake": "CMake",
    "makefile": "Makefile",
    "dockerfile": "Dockerfile",
    "solidity": "Solidity",
    "prisma": "Prisma",
    "graphql": "GraphQL",
    "protobuf": "Protocol Buffer",
    "just": "Just",
}

# Source code in a programming language (as opposed to markup, data and prose).
CODE_LANGUAGES = frozenset(DISPLAY_NAMES) - {
    "html",
    "jinja",
    "mako",
    "handlebars",
    "ejs",
    "css",
    "scss",
    "sass",
    "less",
    "makefile",
    "dockerfile",
    "cmake",
    "prisma",
    "graphql",
    "protobuf",
    "just",
    "sql",
}

_SHEBANG = re.compile(rb"^#!\s*(\S+)(?:[ \t]+(\S+))?")
_VERSION_SUFFIX = re.compile(r"[\d.]+$")


def _interpreter(head: bytes) -> str | None:
    match = _SHEBANG.match(head)
    if match is None:
        return None
    program = match.group(1).decode("utf-8", "replace").rsplit("/", 1)[-1]
    if program == "env" and match.group(2):
        program = match.group(2).decode("utf-8", "replace")
        if program == "-S":  # env -S "python3 -u": take the next word
            rest = head[match.end() :].split(maxsplit=1)
            program = rest[0].decode("utf-8", "replace") if rest else ""
    return _VERSION_SUFFIX.sub("", program) or None


def _by_name(lowered: str) -> str | None:
    if lowered in BY_NAME:
        return BY_NAME[lowered]
    if lowered.startswith(".env."):
        return "dotenv"
    if lowered.startswith("dockerfile.") or lowered.endswith(".dockerfile"):
        return "dockerfile"
    if lowered.startswith(("requirements", "constraints")) and lowered.endswith(".txt"):
        return "pip-requirements"
    return BY_EXTENSION.get(PurePosixPath(lowered).suffix)


def detect_language(path: str, head: bytes = b"") -> str | None:
    """``path`` is relative and POSIX; ``head`` the file's first bytes (for the shebang)."""
    name = PurePosixPath(path).name
    language = _by_name(name.lower())
    if language is None and head.startswith(b"#!"):
        language = BY_INTERPRETER.get(_interpreter(head) or "")
    if language is None:
        tags = sorted(identify.tags_from_filename(name))
        language = next((BY_TAG[tag] for tag in tags if tag in BY_TAG), None)
    return language
