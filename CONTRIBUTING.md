# Contributing to Auto Tuckshop

## Branch protection

Direct pushes to `main` are blocked. All changes require a pull request with at least one approval.

## Making changes

**1. Branch off main**

```bash
git checkout main
git pull origin main
git checkout -b your-branch-name
```

Use descriptive branch names: `fix-voice-parsing`, `add-shona-examples`, `update-deploy-docs`.

**2. Commit**

```bash
git add .
git commit -m "Brief description of what and why"
```

Good commit messages:
- `Fix WhatsApp webhook signature verification`
- `Add Shona number words to LLM prompt`
- `Update deployment docs with new Fly.io app name`

Bad:
- `Fixed stuff`
- `Update`

**3. Push and open a PR**

```bash
git push origin your-branch-name
```

PR descriptions should cover: what changed, why, and how to test it. See existing merged PRs for examples.

**4. Address review feedback, then merge**

```bash
git add .
git commit -m "Address review feedback"
git push origin your-branch-name
```

Once approved, merge on GitHub and delete the branch.

## Before creating a PR

- Test your changes locally
- For code changes: `python manage.py check` and `python manage.py test unit_tests`
- For deployment changes: deploy to staging first, run `python -m pytest tests/ -x`
- Update relevant docs if behaviour changed

## What not to do

- Commit secrets — use environment variables
- Edit migration files that are already committed — create new ones
- Push directly to main
- Open a PR that changes 20+ unrelated files — break it up
