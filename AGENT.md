# Looper

Looper is  a minimal (bare-bones for now ) Tui which runs on a server or your personal device

you can think of Looper as a basic loop-engineering tui which helps the user makes loops easily

## What makes Looper special

Looper  is performance oriented and the user does not need any new api keys 
Looper uses the current Claude or Codex subscription of the user
currently Looper only available on the cli and has no GUI interface

## A note from Kaeser
I like ambitious ideas, simple systems, and software that feels obvious. Do not preserve complexity just because it already exists. Do not introduce machinery because it looks architecturally impressive. Understand the real constraint, then fight for the smallest model that makes the correct behavior unsurprising.

Channel both "measure twice, cut once" and "yagni". Fight scope creep. Try to honor the dev's intent in both a minimal and realistic fashion.

The rest of this document is meant to help you navigate the codebase and make changes effectively. Think of these instructions less as "hard rules", more as "good defaults". The developer's preferences should be able to override anything here.

## A small Glossary

- you means the agent reading this file and changing T3 Code.
- we, us, and maintainers mean Theo, Julius and the people building T3 Code. These are who you are talking to now.
- user means the person using Looper to direct coding agents.
- useragent means the coding agent chosen by the user.
- provider means the agent runtime or harness Looper talks to, such as Codex and Claude and Codex.
- client means the server running Looper.
- environment means one running T3 server and the machine, filesystem, provider credentials, and state it owns.
- project means an environment-local workspace record rooted at a directory.
- thread means the durable conversation and work history for a project.
- turn means one user-to-agent cycle, including follow-up work such as checkpointing.
