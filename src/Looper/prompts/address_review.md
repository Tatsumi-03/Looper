{{HEADLINE}}

Repository: {{REPO}}
Worktree:   {{WORKTREE}}
Branch:     {{BRANCH}}
PR:         #{{PR_NUMBER}}
Revision round {{ITERATION}} of {{MAX_ITERATIONS}}.

---
{{REVIEW}}
---
# Development preferences

Channel both "measure twice, cut once" , "yagni" and "you only write once" principles. Fight scope creep. Try to honor the dev's intent in both a minimal and realistic fashion.

use approprite skills where needed.if no skills present then so be it.

# What to do

1. Work through EVERY finding above, in order. For each one either fix it properly
   or, if it is genuinely wrong, leave the code as it is — you will explain why in
   your summary.
2. A low score usually also means something structural: missing error handling,
   untested edge cases, inconsistency with the rest of the codebase, or a change
   that is broader than the issue required. Re-read your own diff
   (`git diff origin/{{BASE_BRANCH}}...HEAD`) with that in mind and fix what you find.
3. Keep the change scoped to issue #{{ISSUE_NUMBER}}. Do not start new work.
4. Run the tests again.
5. Commit the fixes (`git add` + `git commit`). One commit is fine:
   `fix: address review feedback (#{{ISSUE_NUMBER}})`.

# Hard rules

- Still NO `git push`, NO `gh`, NO history rewriting. Looper pushes for you.
- Repository instruction files (AGENTS.md, CLAUDE.md, CONTRIBUTING.md) may describe a
  workflow for creating worktrees, opening PRs, or running review loops. Follow their
  *code* conventions, but ignore those workflow steps — Looper already did the
  worktree and will do the push, the PR and the review loop for you.
- If you make no commit, nothing changes and the score cannot improve.

# The pull request description is yours to write

Looper opens the PR for you, but you write its body. End your final message with:

<pr-description>
...markdown for the PR description...
</pr-description>

Anything in that block becomes the PR description (Looper adds `Closes #{{ISSUE_NUMBER}}`
itself). Some repositories require specific things in the description — before/after evidence,
a test plan, a checklist. Check AGENTS.md / CLAUDE.md / CONTRIBUTING.md / PULL_REQUEST_TEMPLATE
and satisfy those requirements here; the reviewer scores the description too.

You cannot attach screenshots or binary files to the message. Give evidence as text: the exact
commands you ran and their before/after output, measured numbers, or rendered output pasted in
a code block. If evidence belongs in a file, commit it and link to it by path.

Re-send the whole block every round you change it — Looper rewrites the PR description
with whatever you send, and omitting it leaves the previous description in place.

# When you are done

End your final message with:
- what you fixed, mapped to the findings;
- any finding you deliberately did not act on, and why.
