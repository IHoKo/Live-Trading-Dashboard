# Claude Code prompts for building Ticker

Copy `plan.md` into your empty repo root first. Everything below assumes it's at `./plan.md`.

Do **not** paste one giant "build the whole thing" prompt. The plan is 8–12 days of work; a single request produces a large volume of untested code that all fails together, and you lose the ability to tell which part is wrong. Run one phase per session instead.

---

## Step 1 — Bootstrap (run once)

```
Read plan.md end to end before doing anything.

Then create CLAUDE.md at the repo root capturing the decisions I don't want
re-litigated in later sessions:

- Stack, versions, and the reason for each choice (§3)
- The single-machine constraint and why (§2, §9.4)
- The propose/confirm rule: the model never writes to the DB directly (§7.2)
- Transactions are the source of truth; positions are derived from FIFO lots (§5)
- Never recommend trades — describe data only (§11)
- API keys are server-side only, never in the browser bundle (§11)
- The repo layout from §14
- uv as the Python package manager — pyproject.toml with a committed uv.lock,
  never pip or requirements.txt

Then scaffold only: directory structure, backend/pyproject.toml, package.json,
Dockerfile, fly.toml, .dockerignore, .gitignore, .env.example.

Run `uv sync` in backend/ so uv.lock is generated and committed. Confirm
.dockerignore excludes .venv but NOT uv.lock.

No application logic yet. Show me the tree when you're done.
```

Review `CLAUDE.md` yourself before continuing. It gets loaded into every future session, so an error there propagates everywhere.

## Step 2 — Phase 0, and confirm it deploys

```
Implement Phase 0 from plan.md §10.

FastAPI serving the built frontend plus /api/health. Follow §9.4 exactly on
two points: /api/health must be registered outside any auth dependency, and
the SPA needs a catch-all route so a hard refresh at /portfolio doesn't 404.

Get `docker build` passing locally. Then stop and give me the exact fly
commands from §9.3 to run myself — don't run them.
```

Deploy it. Confirm the URL loads over HTTPS before writing another line. A deploy that works on day 1 and breaks on day 9 is a small problem; one you first attempt on day 9 is not.

## Step 3 — Phase template (repeat for phases 1–6)

```
Implement Phase N from plan.md §10. Read CLAUDE.md and the referenced plan
sections first.

Constraints:
- Only this phase. If you spot work that belongs to a later phase, note it
  and move on.
- Everything the phase's checklist lists, nothing it doesn't.
- Stop and ask if the plan is ambiguous rather than picking for me.

When done: summarize what changed, what you couldn't verify, and what I
should test by hand.
```

Phase-specific additions worth appending:

**Phase 2** — `The 250ms tick coalescing in §6 is the whole point of this phase. A liquid symbol prints hundreds of trades a second and will lock the tab without it. Also implement all three fallback rungs in §4, not just the WebSocket.`

**Phase 3** — `Write tests/test_lots.py before the lot engine. Cover: partial sells, a sell crossing multiple lots, oversell rejection, and delete-then-rebuild producing identical state. Seed a fixed transaction set with hand-computed P/L and assert against it. Tests pass before you move on.`**Phase 5** — `Verify current model IDs and the web_search tool version at docs.claude.com before writing the API call — don't rely on memory. Write tools must return pending actions only; nothing in the tool dispatch path may write to the DB.`

**Phase 6** — `Read §8.2 and build to it precisely. Do not substitute a generic dark-with-green-accent finance dashboard.`

## Step 4 — Between phases

Review before deploying:

```
Review the Phase N diff against plan.md and CLAUDE.md. Flag: deviations from
the plan, anything that writes to the DB outside the confirm flow, secrets
reachable from the client bundle, and unhandled failure paths.
Report only — don't fix yet.
```

Deploy:

```
Walk me through deploying Phase N. Run the §9.5 verification checks and tell
me what each result means. Give me commands to run; don't run fly commands
yourself.
```

When something breaks:

```
<paste the error and `fly logs` output>

Diagnose before changing anything. Tell me the root cause and the options.
Don't start editing files until I pick one.
```

---

## Two things to keep doing

**Add dependencies with `uv add`, never `pip install`.** If a session reaches
for pip or writes a requirements.txt, stop it — `uv.lock` drifts out of sync
with `pyproject.toml` and the next Docker build fails on `--frozen`. This is
worth a line in CLAUDE.md, which the bootstrap prompt above already covers.

**Keep fly commands in your hands.** Let Claude write code, build images, and run tests. You run `fly deploy`, `fly secrets set`, and anything touching the volume. Deploys cost money and can destroy data.

**Update CLAUDE.md when you change direction.** If you swap providers or drop a feature, say so there. Otherwise session 9 confidently rebuilds against a decision you abandoned in session 4.
