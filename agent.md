# Agent notes — dual-remote push flow

When the user asks to push code, push to **both** remotes when they say “work” and/or “client”.

## Remotes

| Name | Repo | Auth |
|------|------|------|
| **Work** (`origin`) | `git@github.com:cyngro/gobal-jump-backend.git` | SSH as `hanzala-cyngro` (`~/.ssh/id_ed25519_hanzala-cyngro`) |
| **Client** (`zataria`) | `https://github.com/ZatariaKugi/Global-Jump-backend.git` | HTTPS + client PAT (never commit the token) |

SSH identity for work:

```bash
ssh -T git@github.com
# expect: Hi hanzala-cyngro! ...
```

## 1) Push to work (`origin/dev`)

From `/Users/mac/code/gobal-jump-backend` on branch `dev`:

```bash
git status -sb
# commit if needed (exclude .cursor/, image*.png, secrets)
git push -u origin HEAD:dev
```

Work SSH cannot write to the client fork — do not expect `git push zataria` via SSH to succeed.

## 2) Push to client (`ZatariaKugi` `dev`)

Histories diverge. Do **not** force-push work `dev` onto client `dev`. Transplant onto a worktree based on client `dev`, then push with the PAT.

### Setup (once)

```bash
git remote add zataria git@github.com:ZatariaKugi/Global-Jump-backend.git 2>/dev/null || true
git fetch zataria dev:refs/remotes/zataria/dev
```

### Transplant + push

```bash
WT="/Users/mac/code/gobal-jump-zataria-push"
# create/update worktree on client tip if missing:
#   git worktree add "$WT" zataria/dev

cd "$WT"
git checkout -B zataria-dev-push zataria/dev

# Take the files from the work tip commit to apply (replace COMMIT with origin/dev tip):
COMMIT=$(git -C /Users/mac/code/gobal-jump-backend rev-parse origin/dev)
git checkout "$COMMIT" -- <changed paths...>

git commit -m "…"   # if there are staged changes

# Push with PAT from env — never hardcode in agent.md or git config
# User must export:  export ZATARIA_TOKEN='ghp_…'
git push "https://ZatariaKugi:${ZATARIA_TOKEN}@github.com/ZatariaKugi/Global-Jump-backend.git" HEAD:dev
```

Verify:

```bash
git ls-remote "https://ZatariaKugi:${ZATARIA_TOKEN}@github.com/ZatariaKugi/Global-Jump-backend.git" refs/heads/dev
```

## Migrations on client

Client alembic head (as of last transplant) was `b2c4e6f8a1d3`. New migrations that parent to that revision apply cleanly on client. Prefer keeping new migration `down_revision` compatible with both remotes when possible.

## Secrets

- Never commit PATs, `.env`, or tokens into the repo or this file.
- If a PAT was pasted in chat, tell the user to **rotate** it after the push.
- Prefer `ZATARIA_TOKEN` in the environment over embedding credentials in the command history when practical.

## Checklist when user says “push”

1. Commit local work on `dev` (if uncommitted).
2. Push work: `git push origin HEAD:dev`.
3. If they also want client: transplant onto `zataria-dev-push` worktree → push `HEAD:dev` to `ZatariaKugi/Global-Jump-backend` with `ZATARIA_TOKEN`.
4. Confirm both remote tips match the intended commits.
