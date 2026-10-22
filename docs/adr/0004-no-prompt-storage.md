# 4. The gateway stores hashes of prompts, never prompts

**Status:** accepted

**Context.** Storing prompts and answers would make debugging easier and the gateway a large store of the most
sensitive data in the company.

**Decision.** The audit trail and the usage ledger hold who, when, which model and provider, data classes found,
redaction counts, tokens, cost and a SHA-256 of the prompt. Teams that need traces of their own application keep
them in their own systems under their own retention rules.

**Consequences.** We can prove what happened for a request (and match a prompt someone presents) without holding
the content. Debugging a bad answer needs the team's cooperation, by design.
