You write the map of one module of a code repository for the engineers who work on it: what the module is for, where its work starts, the symbols that matter most, and how data moves through it. You are given the module's summary and its files' summaries, with the symbols they define, each numbered (`s1`, `s2`, ...).

- **title**: what the module is, in a few words, in the domain's terms ("Item creation and listing", not "items.py").
- **summary**: two to four sentences: what it does, for whom, and how it fits the rest.
- **entry_points**: where its work starts (routes, handlers, pages, commands, public functions), as the numbers of the symbols given, each with a few words on what calls it or what it starts. At most five.
- **key_symbols**: the symbols the module's behavior turns on (services, models, the functions the rest call), by number, each with a few words on its role. At most eight; none of the entry points again.
- **data_flow**: the steps data takes through the module, in order, one short sentence each (where it comes in, what transforms or checks it, where it is stored or sent). At most six.

Use only the numbers given: a symbol you would name but that isn't listed, leave out. Say only what the summaries support; when they don't say how data moves, give fewer steps rather than guessing.
