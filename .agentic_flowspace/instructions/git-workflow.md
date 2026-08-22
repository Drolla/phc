# Git workflow

- Changes shall be performed in dedicated branches — never commit directly
  to `main`.
- Merges into `main` shall be made without fast-forward (`git merge --no-ff`).
  The repo's local `merge.ff` git config is set to `false` so this is the
  default even for a plain `git merge`.
- Different phases of the updates shall be commited separately. For example, one commit to update the scripts, one for the documentation (if any), one for the yaml config examples, one for the tests
- Before committing relevant changes, update `CHANGELOG.md` — see
  `changelog.md` in this same directory. Give the changelog update its own
  commit, separate from the code/docs/tests commits.
