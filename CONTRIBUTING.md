# Contributing

## Commit Messages

Format: `type(scope): description`

### Rules
- **type** (required): one of `feat`, `fix`, `docs`, `refactor`, `test`, `chore`
- **scope** (required): module or area affected, lowercase (e.g., `auth`, `api`, `ui`)
- **description** (required): lowercase, imperative mood, no period, max 72 chars
- **breaking changes**: add `!` after scope — `type(scope)!: description`

### Examples
```
feat(auth): add OAuth2 login flow
fix(api): handle null response from payments endpoint
docs(readme): update setup instructions
refactor(ui): extract button into shared component
feat(api)!: change response format
```

### Invalid Examples
```
Added new feature        # missing type and scope
feat: add login          # missing scope
feat(Auth): Add Login.   # scope and description must be lowercase, no period
```

## Issues

Use the issue template. Required fields:
- **What**: one-line summary of the issue or request
- **Context**: relevant details — where, when, repro steps (if bug), or motivation

Optional:
- **Proposed approach**: implementation ideas if any