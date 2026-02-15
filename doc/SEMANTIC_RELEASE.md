# Semantic Release Guide

This project uses [semantic-release](https://semantic-release.gitbook.io/) to automatically manage versions and releases.

## Branch Strategy

- **`main`**: Production releases (stable versions like `1.0.0`, `1.1.0`, etc.)
- **`beta`**: Pre-releases (beta versions like `1.0.0-beta.1`, `1.1.0-beta.2`, etc.)

## Commit Message Format

This project follows the [Conventional Commits](https://www.conventionalcommits.org/) specification. Each commit message should be structured as follows:

```
<type>[optional scope]: <description>

[optional body]

[optional footer(s)]
```

### Types

- **feat**: A new feature (triggers a minor version bump)
- **fix**: A bug fix (triggers a patch version bump)
- **docs**: Documentation changes
- **style**: Code style changes (formatting, missing semicolons, etc.)
- **refactor**: Code refactoring
- **perf**: Performance improvements
- **test**: Adding or updating tests
- **build**: Changes to build system or dependencies
- **ci**: Changes to CI configuration
- **chore**: Other changes that don't modify src or test files
- **revert**: Reverting a previous commit

### Breaking Changes

To trigger a major version bump, include `BREAKING CHANGE:` in the commit footer or use `!` after the type:

```
feat!: remove support for Python 3.8

BREAKING CHANGE: Python 3.8 is no longer supported
```

### Examples

```bash
# Patch release (1.0.0 -> 1.0.1)
git commit -m "fix: resolve authentication timeout issue"

# Minor release (1.0.0 -> 1.1.0)
git commit -m "feat: add support for charging schedules"

# Major release (1.0.0 -> 2.0.0)
git commit -m "feat!: redesign API interface

BREAKING CHANGE: The connector API has been completely redesigned"
```

## Release Process

### Production Release (main branch)

1. Create commits following the conventional commit format
2. Push or merge to the `main` branch
3. Semantic-release will automatically:
   - Analyze commits to determine version bump
   - Generate/update CHANGELOG.md
   - Create a git tag
   - Create a GitHub release
   - Publish to PyPI

### Pre-release (beta branch)

1. Create commits following the conventional commit format
2. Push or merge to the `beta` branch
3. Semantic-release will automatically:
   - Create a pre-release version (e.g., `1.1.0-beta.1`)
   - Generate/update CHANGELOG.md
   - Create a git tag with the pre-release version
   - Create a GitHub pre-release
   - Publish to PyPI with the beta tag

## Workflow

### For new features in beta:

```bash
git checkout beta
git checkout -b feature/my-new-feature
# Make changes
git commit -m "feat: add new feature"
git push origin feature/my-new-feature
# Create PR to beta branch
```

### Promoting beta to production:

```bash
# After testing beta releases
git checkout main
git merge beta
git push origin main
# Semantic-release will create a production release
```

## Configuration

The semantic-release configuration is in `.releaserc.json`. The GitHub workflow is defined in `.github/workflows/release.yml`.

## Troubleshooting

### Release not triggered

- Ensure commits follow the conventional commit format
- Check that commits are pushed to `main` or `beta` branch
- Verify GitHub Actions is enabled for the repository
- Check workflow logs in the Actions tab

### PyPI publishing failed

- Ensure PyPI environment is configured in repository settings
- Verify trusted publishing is set up for the repository
- Check that the package name is available on PyPI
