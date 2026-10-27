# Autonomous Python Codebase Full-Refactor Protocol

## Purpose

This document is an operational instruction set for an AI coding agent tasked with performing a **complete cleanup and refactor of an existing Python codebase**.

The agent must not treat the task as a superficial formatting exercise. It must first understand the repository, its runtime architecture, dependency relationships, major files, entry points, configuration, and validation systems. It must then inspect **every relevant source file one by one**, trace related files and call/import relationships, identify redundant or dead logic, and refactor the codebase systematically.

The objective is:

> **Make the entire Python codebase materially cleaner, simpler, more maintainable, and better structured while preserving required functionality.**

The cleanup may be aggressive. The agent is allowed to:

- Delete unused functions, classes, modules, variables, imports, files, and configuration.
- Merge duplicate or overlapping implementations.
- Rename poorly named symbols.
- Move code into more appropriate modules/packages.
- Split oversized modules.
- Collapse unnecessary abstractions.
- Remove obsolete compatibility layers.
- Simplify complex implementations.
- Replace duplicated logic with shared utilities where justified.
- Remove stale dependencies.
- Remove dead configuration.
- Reorganize package structure.
- Change internal APIs.
- Update imports and call sites throughout the repository.
- Introduce or improve tests.
- Improve typing and static-analysis coverage.
- Replace brittle implementations with simpler equivalents.

However:

> **Aggressive does not mean careless. No code should be deleted merely because the agent cannot immediately find a reference to it. The agent must establish sufficient evidence that functionality is obsolete, unused, unreachable, duplicated, replaced, or otherwise safe to remove.**

The agent must prioritize **actual repository evidence over assumptions**.

---

# 1. Core Operating Principles

## 1.1 Understand before changing

Do not begin broad refactoring immediately.

First establish:

- What the repository contains.
- What runs.
- How it is launched.
- Which modules are entry points.
- Which packages are internal.
- Which packages are external dependencies.
- Which files are generated.
- Which files are configuration.
- Which code is production code.
- Which code is test code.
- Which code is tooling.
- Which code is scripts/CLI utilities.
- Which code is experimental or optional.
- Which functionality crosses module/package boundaries.
- Which external systems are contacted.
- Which environment variables, files, databases, APIs, queues, or services are involved.
- Which validation tools are available.

Never infer repository architecture from filenames alone.

---

## 1.2 Build evidence before deleting

Deletion decisions must be based on multiple signals where possible.

For a candidate dead function/class/module/file, inspect:

- Direct imports.
- Direct references.
- Function calls.
- Attribute access.
- Dynamic imports.
- String-based references.
- CLI entry points.
- Plugin registration.
- Framework discovery mechanisms.
- Configuration references.
- Serialization/deserialization references.
- Tests.
- Fixtures.
- Scripts.
- Documentation that represents a real supported interface.
- Packaging metadata.
- Build configuration.
- CI configuration.
- Runtime configuration.
- Reflection/introspection.
- `__all__`.
- Entry-point definitions.
- Decorator registration.
- Dependency injection.
- Monkey-patching.
- Import side effects.
- External callers if evidence exists in the repository.
- Generated code relationships.

A simple "no grep result" is **not sufficient evidence** for deletion when dynamic behavior could exist.

---

## 1.3 Preserve required behavior, not accidental structure

The goal is not to preserve every existing implementation detail.

Preserve:

- Supported functionality.
- Important side effects.
- External interfaces that are actually used.
- Required configuration behavior.
- Data formats.
- Database/schema contracts unless intentionally migrated.
- CLI behavior where it is part of the application.
- API contracts where they are externally consumed.
- Security properties.
- Important performance characteristics when they are intentional.
- Error-handling behavior that callers rely upon.
- Correct business rules.

Do not preserve:

- Redundant wrappers with no semantic purpose.
- Dead compatibility code.
- Duplicate implementations.
- Unused abstractions.
- Accidental complexity.
- Obsolete comments.
- Stale configuration.
- Unreachable branches.
- Workarounds for dependencies no longer present.
- Legacy APIs that are demonstrably not required.
- Code whose only justification is "it was already there."

---

# 2. Phase 0 — Establish Repository Boundaries

Before analysis, determine the repository root and identify what should and should not be modified.

## 2.1 Identify repository structure

Inspect:

- Root directory.
- Package directories.
- Application directories.
- `src/` layouts.
- Test directories.
- Script directories.
- Tooling directories.
- Generated directories.
- Build directories.
- Documentation.
- Configuration.
- Deployment files.
- Docker/container files.
- Infrastructure files.
- CI/CD configuration.
- IDE/editor configuration.

Common files to check include:

- `pyproject.toml`
- `requirements.txt`
- `requirements-dev.txt`
- `requirements-*.txt`
- `Pipfile`
- `Pipfile.lock`
- `poetry.lock`
- `uv.lock`
- `setup.py`
- `setup.cfg`
- `tox.ini`
- `noxfile.py`
- `pytest.ini`
- `.python-version`
- `Dockerfile*`
- `docker-compose*`
- `.github/workflows/*`
- `Makefile`
- shell scripts
- task-runner configuration
- project-specific configuration files

Do not assume any particular packaging system exists.

---

## 2.2 Identify files that must not be modified casually

Explicitly recognize:

- Generated files.
- Vendor/third-party code.
- Git metadata.
- Binary assets.
- Secrets.
- Credentials.
- Deployment-managed files.
- Database migration history.
- Lock files that are intentionally managed by a package manager.
- Auto-generated client code.
- Files consumed by external systems.

Generated source should normally be modified at the generator/source level rather than directly.

If the repository contains generated code, determine how it is generated before attempting to refactor it.

---

## 2.3 Detect project boundaries

A repository may contain multiple Python projects.

Do not assume one root package equals one application.

Identify:

- Separate packages.
- Separate services.
- Separate CLIs.
- Separate libraries.
- Monorepo components.
- Examples.
- Prototypes.
- Archived projects.
- Independent test suites.

For each project, maintain a separate mental/modelled dependency graph where appropriate.

---

# 3. Phase 1 — Tool and Validation Discovery

The repository may have different development tooling from project to project.

The agent must **discover what exists rather than assuming a standard toolchain**.

## 3.1 Detect available validation systems

Look for:

### Tests

Possible systems:

- `pytest`
- `unittest`
- `nose`
- custom test runners
- integration-test scripts
- end-to-end test commands
- project-specific scripts

### Type checking

Possible systems:

- `mypy`
- `pyright`
- `pyre`
- other configured type checkers

### Linting

Possible systems:

- `ruff`
- `flake8`
- `pylint`
- `pycodestyle`
- project-specific lint commands

### Formatting

Possible systems:

- `black`
- `ruff format`
- `autopep8`
- project-specific formatting

### Static analysis

Possible systems:

- `bandit`
- `semgrep`
- `vulture`
- `pyflakes`
- custom analyzers
- IDE/static-analysis configuration

### Dependency auditing

Possible systems:

- `pip-audit`
- `safety`
- package-manager-specific audit commands
- lockfile tooling

### CI/CD

Inspect workflows and reproduce relevant validation locally where possible.

---

## 3.2 Build a project-specific validation matrix

For every independent project, record:

| Capability | Present? | Command/configuration found | Must run? |
|---|---|---|---|
| Unit tests | Yes/No | Detected command | Yes if available |
| Integration tests | Yes/No | Detected command | Yes if relevant |
| E2E tests | Yes/No | Detected command | Yes if relevant |
| Type checking | Yes/No | Detected command | Yes if available |
| Linting | Yes/No | Detected command | Yes if available |
| Formatting | Yes/No | Detected command | Yes if configured |
| Static analysis | Yes/No | Detected command | Yes if available |
| Security scan | Yes/No | Detected command | Yes if configured/relevant |
| Build/package validation | Yes/No | Detected command | Yes if available |
| CI checks | Yes/No | Detected workflow | Reproduce relevant checks |

### Rule

If a validation mechanism exists, use it.

If it does not exist:

1. Do not pretend that it ran.
2. Use alternative verification methods.
3. Add lightweight tests or checks where practical.
4. Perform static inspection.
5. Run the application or relevant entry points.
6. Exercise representative workflows.
7. Use import/compile checks.
8. Compare behavior before and after changes when possible.

---

# 4. Phase 2 — Establish a Baseline

Before changing code, capture the current state.

## 4.1 Record baseline information

For each project, determine:

- Python versions supported.
- Package manager.
- Installation method.
- Runtime entry points.
- CLI commands.
- Test commands.
- Build commands.
- Lint commands.
- Type-check commands.
- Formatting commands.
- Important environment variables.
- Database dependencies.
- External API dependencies.
- Service dependencies.
- Known warnings/failures.
- Existing failing tests.
- Existing type errors.
- Existing lint errors.

The goal is to distinguish:

> **Pre-existing failures from regressions introduced by the refactor.**

---

## 4.2 Run available checks before editing

Run whatever the repository actually supports.

Examples:

- Test suite.
- Targeted unit tests.
- Integration tests.
- Type checker.
- Linter.
- Static analysis.
- Build/package checks.
- Import checks.
- CLI smoke tests.

Do not silently "fix" unrelated pre-existing failures during baseline collection.

Record them.

---

## 4.3 Establish a functional baseline

For applications that can run, identify representative behavior.

Examples:

- CLI commands.
- HTTP endpoints.
- Worker tasks.
- Data-processing pipelines.
- Scheduled jobs.
- Core library functions.
- Database operations.
- Authentication flows.
- File processing.
- Agent/tool workflows.

Where practical, establish expected outputs for representative cases.

This becomes a comparison point after refactoring.

---

# 5. Phase 3 — Dependency and Architecture Reconnaissance

This phase is mandatory.

Do not proceed to file-by-file cleanup without first understanding the broad architecture.

## 5.1 Inventory all Python code

Enumerate:

- `.py`
- `.pyi`
- package `__init__.py`
- test files
- scripts
- migration Python files
- CLI modules
- generated Python
- notebooks containing Python if they are operationally relevant

Categorize files as:

- application
- library
- test
- tooling
- migration
- script
- generated
- experimental
- unknown

Resolve "unknown" categories before continuing.

---

## 5.2 Build an import/dependency graph

Determine:

- Internal imports.
- External imports.
- Package-level dependencies.
- Cross-project dependencies.
- Circular dependencies.
- High-centrality modules.
- Leaf modules.
- Utility modules.
- Shared infrastructure.
- Configuration modules.
- Entry points.

Look for patterns such as:

```text
entry point
  -> application/service layer
      -> domain/business logic
          -> data/access layer
              -> external systems
```

The repository does not have to follow this architecture; this is only a mapping aid.

---

## 5.3 Understand external dependencies

Inspect package metadata and source usage to determine:

- Which external libraries are actually imported.
- Which declared dependencies appear unused.
- Which dependencies are development-only.
- Which dependencies exist only for legacy code.
- Which dependencies are transitively available but incorrectly relied upon.
- Which dependencies are imported conditionally.
- Which dependencies may be optional extras.
- Which packages are loaded dynamically.

Do not remove a dependency solely because static import search does not find it.

Check:

- Optional imports.
- Entry points.
- Plugin systems.
- Framework configuration.
- Dynamic import strings.
- CLI integrations.
- Build tooling.
- Deployment scripts.

---

## 5.4 Identify high-risk architectural areas

Mark modules that are:

- Imported everywhere.
- Responsible for global state.
- Configuration-heavy.
- Side-effect-heavy.
- Framework integration points.
- Database integration points.
- API integration points.
- Authentication/authorization boundaries.
- Serialization boundaries.
- CLI entry points.
- Plugin registries.
- Async/concurrency boundaries.
- Multiprocessing boundaries.
- Dynamic dispatch systems.
- Decorator-driven registration systems.

These require deeper dependency tracing before modification.

---

# 6. Phase 4 — Identify Major Files and Major Systems

Before examining every file, identify the repository's structural "center of gravity."

## 6.1 Major file criteria

A major file may be:

- Large.
- Imported by many modules.
- An entry point.
- A service boundary.
- A central utility module.
- A configuration module.
- A data-access layer.
- A public API surface.
- A registry.
- A core business-logic module.
- A shared abstraction.
- A module containing many classes/functions.
- A module with unusually high coupling.

Rank these files by architectural importance.

---

## 6.2 Read major files in context

For each major file, determine:

- What responsibilities it currently contains.
- What imports it.
- What it imports.
- Which symbols are public.
- Which symbols are internal.
- Which symbols are actually used.
- Which functions call which functions.
- Which classes are instantiated.
- Which classes are inherited.
- Which side effects occur during import.
- Which configuration is consumed.
- Which external systems it touches.
- Which tests cover it.
- Which assumptions other files make about it.

Do not refactor a major file in isolation.

---

# 7. Phase 5 — Mandatory Exhaustive File-by-File Audit

This is the core of the cleanup.

The agent must eventually inspect **every relevant file individually**.

Do not stop after finding obvious cleanup opportunities.

Use a deterministic tracking mechanism so that every file receives an audit status.

Recommended states:

```text
UNSEEN
DISCOVERED
ANALYZED
RELATED_DEPENDENCIES_CHECKED
REFACTORED
VALIDATED
RECHECKED
CLOSED
```

No source file is considered complete until it reaches `CLOSED` or is explicitly marked `EXCLUDED` with a reason.

---

# 8. File Audit Procedure

For each file, perform the following sequence.

## Step 1 — Identify its role

Determine:

- Why the file exists.
- Whether it is production code.
- Whether it is a test.
- Whether it is infrastructure/tooling.
- Whether it is generated.
- Whether it is obsolete.
- Whether it is an entry point.
- Whether it exposes a public interface.

---

## Step 2 — Read the entire file

Do not make conclusions from snippets.

Understand:

- Imports.
- Constants.
- Globals.
- Functions.
- Classes.
- Methods.
- Decorators.
- Type hints.
- Exception handling.
- Control flow.
- Side effects.
- Comments.
- TODOs.
- Configuration.
- Logging.
- Resource management.

---

## Step 3 — Trace every symbol

For every meaningful function, class, constant, exported object, and module-level construct, investigate:

- Who imports it?
- Who calls it?
- Who subclasses it?
- Who instantiates it?
- Who accesses its attributes?
- Is it referenced by tests?
- Is it referenced by configuration?
- Is it dynamically discovered?
- Is it duplicated elsewhere?
- Can it be simplified?
- Is it necessary?
- Does it perform more than one responsibility?
- Does its name accurately describe its behavior?
- Does its implementation still match the surrounding architecture?

---

## Step 4 — Inspect related files

This step is mandatory.

For each file, inspect related:

- Importers.
- Imported modules.
- Callers.
- Callees where relevant.
- Tests.
- Fixtures.
- Configuration.
- CLI registration.
- Serialization definitions.
- Models/schemas.
- Adapters.
- Interfaces/base classes.
- Implementations.
- Related utilities.
- Documentation that describes an actual supported interface.
- Neighboring modules providing similar functionality.

Do not decide whether a function is unnecessary until related code has been checked.

---

## Step 5 — Look for unnecessary code

Check specifically for:

### Unused imports

Remove them unless they have intentional side effects.

### Dead functions

Look for functions that have no legitimate call path or supported external use.

### Dead classes

Check inheritance, instantiation, framework registration, and dynamic loading.

### Dead constants

Check configuration and external consumers.

### Duplicate functions

Look for different implementations of essentially the same operation.

### Duplicate classes

Look for competing abstractions representing the same concept.

### Wrapper functions

Ask whether the wrapper adds:

- validation,
- abstraction,
- compatibility,
- logging,
- error handling,
- dependency isolation,
- semantics.

If not, remove or inline it when safe.

### Pass-through abstractions

Remove classes/interfaces whose only purpose is to forward calls without adding meaningful behavior.

### Over-generalization

Simplify generic frameworks that are used for only one simple case.

### Legacy compatibility

Remove compatibility layers when there is evidence the old interface is no longer required.

### Repeated logic

Consolidate repeated business logic only when doing so improves correctness and maintainability.

Do not extract tiny pieces into utilities merely to reduce line count.

### Unreachable branches

Remove branches that cannot occur under current supported behavior.

### Redundant checks

Remove checks that cannot meaningfully change behavior.

### Stale comments

Delete comments describing code that no longer exists.

### Misleading comments

Update comments that contradict actual behavior.

### Obsolete TODOs

Resolve, remove, or explicitly retain only actionable TODOs.

---

# 9. Dependency-Aware Deletion Protocol

Before deleting any function/class/module/file, perform a deletion audit.

## Required checks

Search for:

1. Symbol references.
2. Imports.
3. Attribute access.
4. Dynamic references.
5. String-based registrations.
6. CLI entry points.
7. Framework registration.
8. Configuration references.
9. Test references.
10. Documentation representing a supported API.
11. Serialization references.
12. Plugin references.
13. Build/deployment references.
14. Reflection/introspection.
15. External-facing package exports.

Then classify the candidate:

```text
SAFE_TO_DELETE
DELETE_WITH_REPLACEMENT
UNCERTAIN
REQUIRED
```

Only delete `SAFE_TO_DELETE` and appropriately handled `DELETE_WITH_REPLACEMENT` candidates.

For `UNCERTAIN`, gather additional evidence rather than guessing.

---

# 10. Dynamic Python Behavior

Python code can hide dependencies.

Before removing or renaming code, consider:

- `importlib`
- `getattr`
- `setattr`
- `globals()`
- `locals()`
- reflection
- decorators
- metaclasses
- plugin discovery
- entry points
- module scanning
- filesystem-based module loading
- environment-controlled imports
- string-based class names
- serialization frameworks
- dependency injection
- framework auto-registration
- monkey-patching
- signals/hooks
- task queues
- CLI frameworks
- web framework routing
- ORM model discovery

If dynamic behavior exists, trace the mechanism before declaring code dead.

---

# 11. Refactoring Strategy

Refactor in dependency-aware increments.

Prefer changes that reduce complexity and improve clarity.

## 11.1 Good candidates

- Removing duplication.
- Removing dead code.
- Simplifying conditionals.
- Simplifying nested logic.
- Separating unrelated responsibilities.
- Consolidating configuration.
- Improving naming.
- Removing unnecessary abstraction.
- Centralizing repeated business rules.
- Reducing import coupling.
- Breaking circular dependencies.
- Removing obsolete dependencies.
- Improving type clarity.
- Simplifying exception handling.
- Improving resource management.
- Making side effects explicit.

---

## 11.2 Avoid cosmetic refactors with no value

Do not spend disproportionate effort changing:

- Variable names that are already clear.
- Formatting that is automatically managed.
- File layouts merely for aesthetics.
- Small stylistic details that create huge diffs.
- Function signatures without a real design benefit.

The objective is meaningful improvement.

---

# 12. Large Module Refactoring

When a file is doing too much:

1. Identify responsibilities.
2. Identify which responsibilities are independent.
3. Identify shared state.
4. Identify dependencies between responsibilities.
5. Identify the smallest coherent module boundaries.
6. Move code carefully.
7. Update all importers.
8. Preserve behavior.
9. Run validation.
10. Search for stale imports and references.
11. Remove the old implementation only after replacement is validated.

Do not split a module into many tiny files simply because it is large.

---

# 13. Duplicate and Similar Logic Analysis

When two functions appear similar, compare:

- Inputs.
- Outputs.
- Side effects.
- Error behavior.
- Logging.
- Performance.
- Mutability.
- Transaction behavior.
- Configuration.
- Callers.
- Business semantics.

Do not merge functions solely because their code looks similar.

A shared abstraction is justified when it reduces duplication **without obscuring semantics**.

---

# 14. API and Interface Cleanup

For every public-looking function/class/module:

Determine whether it is actually public.

Consider:

- Package exports.
- `__all__`.
- Documentation.
- Installed-package structure.
- External callers known to the repository.
- CLI interfaces.
- APIs.
- Plugin interfaces.
- Configuration references.

Internal APIs can be aggressively redesigned.

Actual external APIs require stronger evidence before breaking changes.

When an API must change, update all known internal callers and relevant tests.

---

# 15. Tests Must Be Treated as Part of the Codebase

Tests are not exempt from cleanup.

Audit tests for:

- Dead tests.
- Duplicate tests.
- Redundant fixtures.
- Obsolete mocks.
- Tests covering removed behavior.
- Fragile tests.
- Tests that assert implementation details unnecessarily.
- Missing coverage around refactored behavior.
- Unused helper functions.
- Duplicate test utilities.

Do not delete a test merely because it currently fails after a refactor.

First determine whether:

- The implementation is wrong.
- The test describes obsolete behavior.
- The test has an incorrect assumption.
- The API intentionally changed.
- The test itself is broken.

---

# 16. Missing-Test Projects

Some projects will have weak or nonexistent tests.

When tests are absent:

## Minimum validation strategy

Use available alternatives:

- `python -m compileall`
- Import checks.
- Application startup.
- CLI smoke tests.
- Representative function calls.
- Type checking if available.
- Linting/static analysis if available.
- Manual execution of important workflows.
- Before/after output comparison where practical.
- Existing integration tooling.
- Dependency/build checks.

## Add tests when practical

For important logic with no coverage:

- Create focused unit tests.
- Prefer behavior-level tests over implementation-detail tests.
- Cover important branches.
- Cover error cases.
- Cover public interfaces.
- Cover the exact behavior being refactored.

Do not attempt to achieve arbitrary test-coverage numbers merely for appearance.

---

# 17. Validation After Every Meaningful Refactor

Do not accumulate hundreds of unvalidated changes.

After each meaningful change or coherent group of changes:

1. Run targeted tests.
2. Run relevant static checks.
3. Run type checking when configured.
4. Run linting when configured.
5. Run formatting checks where relevant.
6. Run import/build validation.
7. Exercise the affected workflow.
8. Inspect the diff.
9. Search for stale references.

The validation strategy must adapt to the project.

---

# 18. If a Tool Is Missing

Never invent a tool result.

For example:

If `pytest` is absent:

- Do not claim the test suite passed.
- Determine whether `unittest` or another runner exists.
- Use static and runtime verification.
- Add targeted tests when practical.

If no type checker exists:

- Perform careful annotation/usage inspection.
- Use runtime tests.
- Use lint/static analysis available through other mechanisms.

If no linting exists:

- Run available syntax/import checks.
- Inspect changed code carefully.
- Consider introducing a lightweight linter only when useful and compatible with project constraints.

If no CI exists:

- Perform local validation appropriate to the repository.

---

# 19. Handling Existing Failures

Separate failures into:

```text
PRE_EXISTING
INTRODUCED_BY_REFACTOR
UNCERTAIN
```

Never silently classify a new failure as pre-existing.

When possible:

- Reproduce the baseline failure.
- Apply refactor.
- Re-run the same check.
- Compare results.

A refactor is not complete while introducing unexplained new failures.

---

# 20. Dependency Cleanup

After code cleanup, revisit dependencies.

For each declared dependency:

- Search source usage.
- Check optional/dynamic use.
- Check development tooling.
- Check CLI/build usage.
- Check configuration.
- Check CI.
- Check package extras.
- Check generated code.
- Check runtime loading.

Classify:

```text
RUNTIME_REQUIRED
DEV_ONLY
BUILD_ONLY
OPTIONAL
UNUSED
UNCERTAIN
```

Remove truly unused dependencies.

Then regenerate/update the relevant lockfile using the repository's package manager.

Validate installation/importability afterwards.

---

# 21. Import and Package Structure Cleanup

Inspect for:

- Circular imports.
- Wildcard imports.
- Unnecessary re-exports.
- Confusing package-level exports.
- Side-effect-only imports.
- Imports from implementation details.
- Deep imports that violate intended boundaries.
- Modules with excessive fan-in/fan-out.

Prefer clear dependency direction.

Do not create artificial layering merely to make a graph look clean.

---

# 22. Configuration Cleanup

Audit:

- Environment variables.
- Configuration classes.
- `.env` handling.
- YAML/JSON/TOML config.
- Feature flags.
- Constants.
- Default values.
- CLI arguments.
- Deployment configuration.

Remove:

- Unused settings.
- Obsolete environment variables.
- Duplicate configuration.
- Dead feature flags.
- Configuration for deleted functionality.

Be especially cautious with environment variables because repository search may not reveal externally supplied values.

---

# 23. Error Handling Cleanup

Check for:

- Broad `except Exception`.
- Bare `except`.
- Exceptions swallowed silently.
- Duplicate error handling.
- Errors logged and re-raised unnecessarily.
- Exceptions converted into less useful exceptions.
- Dead fallback behavior.
- Inconsistent error types.

Simplify error handling while preserving meaningful failure behavior.

Do not remove defensive error handling merely because the happy path is obvious.

---

# 24. Logging and Observability Cleanup

Audit:

- Duplicate logs.
- Misleading log messages.
- Debug prints.
- Dead logging paths.
- Inconsistent severity.
- Logging sensitive values.
- Excessive logging inside loops.
- Redundant exception logging.

Do not remove observability that is needed operationally.

---

# 25. Async, Concurrency, and Resource Management

Pay particular attention to:

- `asyncio`
- threads
- multiprocessing
- task queues
- locks
- semaphores
- async context managers
- connection pools
- database sessions
- file handles
- sockets
- subprocesses

Do not perform "simplifications" that change:

- scheduling.
- cancellation behavior.
- locking.
- resource lifetime.
- transaction scope.
- retry behavior.
- concurrency semantics.

Validate these areas with targeted runtime checks.

---

# 26. Security-Sensitive Code

Be conservative around:

- Authentication.
- Authorization.
- Password handling.
- Tokens.
- Secrets.
- Cryptography.
- Session management.
- Input validation.
- SQL/database query construction.
- Command execution.
- File path handling.
- Deserialization.
- Webhooks.
- Access-control logic.

The overall refactor may be aggressive, but security-sensitive behavior requires stronger evidence and tests before alteration.

Do not expose secrets in logs, reports, diffs, or generated documentation.

---

# 27. Performance-Sensitive Code

When changing code that appears performance-critical:

Determine:

- Why it may be performance-sensitive.
- Whether callers depend on its characteristics.
- Whether benchmarks exist.
- Whether a hot path is involved.
- Whether database/query behavior changes.
- Whether memory use changes.
- Whether concurrency changes.

Prefer simplification unless there is evidence the refactor materially harms performance.

Where benchmarks exist, run them.

Where no benchmarks exist but performance is clearly important, create focused measurements where practical.

---

# 28. File-by-File Completion Checklist

Every file must ultimately answer:

### Purpose
- What does this file do?
- Is that responsibility still necessary?

### Dependencies
- What does it import?
- Who imports it?
- Are any dependencies unnecessary?

### Symbols
- Which functions/classes/constants exist?
- Which are actually used?
- Which can be removed?
- Which can be simplified?

### Relationships
- Which related files were checked?
- Which callers were checked?
- Which tests were checked?
- Which configuration was checked?

### Architecture
- Is the file in the right package?
- Does it contain multiple responsibilities?
- Is it duplicating another module?

### Quality
- Are naming and boundaries clear?
- Is error handling appropriate?
- Is typing adequate for the project?
- Is there dead code?
- Is there unnecessary complexity?

### Validation
- What checks were run?
- Did they pass?
- Were any failures pre-existing?
- Were new tests needed?

No file is considered closed until these questions have been addressed.

---

# 29. Exhaustive Repository Sweep

After completing the individual file audits, perform a second repository-wide sweep.

Look for:

- Dead files.
- Dead directories.
- Orphaned modules.
- Unused imports.
- Unused dependencies.
- Stale package exports.
- Broken imports.
- Duplicate utilities.
- Duplicate models.
- Duplicate configuration.
- Obsolete comments.
- Old compatibility code.
- Stale documentation.
- Dead scripts.
- Unused test helpers.
- Unused fixtures.
- Orphaned migrations where applicable.
- Unreferenced assets/configuration.
- Circular dependencies.
- Newly introduced cycles.
- Naming inconsistencies.
- Temporary refactor leftovers.

This second sweep is mandatory because deleting or moving code can create new opportunities for cleanup.

---

# 30. Third-Pass "Can Anything Else Go?" Review

After the repository is functional again, perform one deliberate aggressive cleanup pass.

For every surviving abstraction ask:

> "Is this still justified by real behavior, a real boundary, or a real reusable need?"

For every surviving utility ask:

> "Is this genuinely shared, or is it just a place where code was hidden?"

For every surviving module ask:

> "Would the system be clearer if this were merged, split, moved, or removed?"

For every surviving dependency ask:

> "Is this actually needed?"

For every surviving compatibility layer ask:

> "What current caller still requires this?"

For every surviving test helper ask:

> "Does it reduce meaningful duplication?"

Do not force changes when the answer is clearly "yes, this structure is justified."

---

# 31. Final Validation

Before declaring completion, perform the strongest available validation suite.

## Always attempt, when available

- Full unit tests.
- Integration tests.
- End-to-end tests.
- Type checking.
- Linting.
- Formatting validation.
- Static analysis.
- Security checks.
- Build/package validation.
- Import checks.
- Application startup.
- Relevant CLI smoke tests.
- Representative runtime workflows.

For projects lacking some checks, substitute appropriate alternatives.

---

# 32. Final Dependency Reconciliation

After the last refactor:

1. Rebuild the conceptual dependency graph.
2. Check for stale imports.
3. Check for broken references.
4. Check for newly orphaned modules.
5. Check for newly unused dependencies.
6. Check for stale exports.
7. Check for circular dependencies.
8. Check for files that became unnecessary.
9. Check package installation/importability.
10. Re-run the strongest available validation.

The final architecture should be cleaner than the initial architecture not merely because files were deleted, but because relationships are easier to understand.

---

# 33. Git/Diff Discipline

Inspect the final diff carefully.

Look for:

- Accidental behavior changes.
- Unrelated edits.
- Debug code.
- Secrets.
- Large accidental formatting changes.
- Deleted code that should not have been deleted.
- Incorrect imports.
- Renamed symbols with missed references.
- Configuration changes.
- Lockfile changes unrelated to dependency cleanup.
- Generated-file churn.

Keep changes focused on the refactor.

If the repository contains multiple independent projects, ensure the final diff is coherent for each project.

---

# 34. Completion Report

At the end, produce a concise but complete report containing:

## Repository overview

- Projects discovered.
- Main entry points.
- Major architectural components.
- Important dependency relationships.

## Cleanup performed

- Dead code removed.
- Duplicate logic consolidated.
- Modules moved/merged/split.
- Dependencies removed.
- Configuration removed.
- APIs simplified.
- Major architectural improvements.

## Validation

For each project:

| Check | Result |
|---|---|
| Tests | Passed / Failed / Not available |
| Type checking | Passed / Failed / Not available |
| Linting | Passed / Failed / Not available |
| Formatting | Passed / Failed / Not available |
| Static analysis | Passed / Failed / Not available |
| Security checks | Passed / Failed / Not available |
| Build/package check | Passed / Failed / Not available |
| Runtime smoke tests | Passed / Failed / Not available |

Do not label something "passed" unless it actually ran successfully.

## Remaining concerns

Explicitly list:

- Known pre-existing failures.
- Areas with insufficient test coverage.
- Dynamic behavior that prevented high-confidence deletion.
- External integrations that could not be fully exercised.
- Potential breaking API changes.
- Performance-sensitive areas that were not benchmarked.
- Security-sensitive areas requiring human review.

---

# 35. Human Review Boundaries

Even with an autonomous full cleanup, explicitly flag changes that may require human confirmation.

Examples:

- External API contract changes.
- Database schema changes.
- Destructive data migrations.
- Removal of potentially externally consumed library APIs.
- Authentication/authorization behavior changes.
- Security-sensitive changes.
- Deployment changes.
- Environment-variable contract changes.
- Major concurrency changes.
- Performance regressions.
- Changes whose behavior cannot be validated locally.

Do not block the entire cleanup unnecessarily because one area requires human review.

Isolate the uncertain area, complete safe work around it, and report it clearly.

---

# 36. Important Anti-Patterns

The agent must not:

### Do not delete based only on "unused-looking"

A function with no obvious local caller may still be externally invoked.

### Do not refactor only the biggest files

The task is exhaustive.

### Do not stop after tests pass

Passing tests do not prove dead code is absent.

### Do not rely exclusively on static analysis

Python permits dynamic behavior.

### Do not blindly trust static-analysis dead-code reports

Verify each candidate.

### Do not add abstractions merely because code is duplicated twice

Consider whether duplication is actually harmful.

### Do not over-split files

Small files are not automatically better.

### Do not preserve obsolete architecture for sentimental reasons

If it is demonstrably unnecessary, remove it.

### Do not modify generated code as though it were source code

Find the generator.

### Do not claim validation that was not performed

Always distinguish:

- passed,
- failed,
- skipped,
- unavailable,
- not applicable,
- uncertain.

### Do not hide failures

Surface them clearly.

---

# 37. Recommended Working State

Maintain an internal tracking table throughout the task.

| File | Role | Related files checked | Refactored | Validation | Status |
|---|---|---|---|---|---|
| `...` | ... | Yes/No | Yes/No | ... | ... |

Also maintain candidate cleanup records:

| Candidate | Evidence | Risk | Action | Validation |
|---|---|---|---|---|
| `...` | ... | Low/Med/High | Delete/Keep/Rewrite | ... |

This prevents the agent from losing track of files and prematurely declaring completion.

---

# 38. Preferred Order of Operations

Use this general sequence:

```text
1. Discover repository boundaries
2. Detect all projects
3. Detect available tooling
4. Establish baseline
5. Inventory all files
6. Map dependencies
7. Identify entry points
8. Identify major architectural files
9. Analyze major systems
10. Audit every file individually
11. Trace related files for every candidate
12. Remove dead/unnecessary code
13. Simplify architecture
14. Consolidate justified duplication
15. Clean imports
16. Clean dependencies
17. Clean configuration
18. Clean tests
19. Validate incrementally
20. Perform repository-wide second sweep
21. Perform aggressive final cleanup pass
22. Rebuild dependency understanding
23. Run full available validation
24. Inspect final diff
25. Produce completion report
```

Do not skip the exhaustive file audit simply because earlier phases found major problems.

---

# 39. Definition of Done

The refactor is complete only when all of the following are true:

- The repository has been structurally understood.
- All project boundaries have been identified.
- Available validation tooling has been discovered per project.
- A baseline was established.
- Major files and systems were analyzed.
- Every relevant Python file was individually audited.
- Related files were checked before major deletions.
- Dead or unnecessary code was removed where justified.
- Duplicate logic was consolidated where beneficial.
- Unnecessary abstractions were removed.
- Dependencies were reconciled.
- Imports were cleaned.
- Configuration was cleaned.
- Tests were updated or added where necessary.
- The available validation systems were run.
- Missing validation systems were replaced with appropriate alternatives.
- New regressions were investigated.
- A repository-wide second sweep was completed.
- A final aggressive cleanup pass was completed.
- The resulting code remains functionally usable.
- Remaining uncertainties are explicitly documented.
- The final diff has been reviewed.

---

# 40. Final Instruction to the Agent

Treat this as a **full repository engineering task, not a single-file refactor**.

Do not optimize for:

- minimum number of changed lines,
- minimum number of deleted files,
- fastest completion,
- preserving the existing architecture at all costs.

Optimize for:

- correctness,
- maintainability,
- simplicity,
- clear dependency direction,
- removal of dead weight,
- reduced duplication,
- understandable module boundaries,
- reliable behavior,
- strong evidence for deletion,
- project-appropriate validation.

When deciding between:

```text
"leave it because it might be used"
```

and

```text
"investigate until there is enough evidence to make a defensible decision"
```

prefer investigation.

When deciding between:

```text
"preserve unnecessary complexity just in case"
```

and

```text
"simplify after tracing all dependencies and validating behavior"
```

prefer simplification.

When the repository contains multiple projects with different tooling, **adapt the workflow to each project rather than imposing one universal toolchain**.

The final outcome should be a codebase where:

> **Every surviving file has a reason to exist, every major abstraction has a purpose, dependencies are intentional, duplication is controlled, and the overall architecture is substantially easier to understand than when the task began.**
