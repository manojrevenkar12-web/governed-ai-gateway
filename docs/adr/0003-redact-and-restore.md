# 3. Redact regulated data and restore it in the answer; block secrets

**Status:** accepted

**Context.** Blocking every prompt that contains customer data makes AI useless for operations teams; sending it to
external providers is not acceptable.

**Decision.** Detected values are replaced by placeholders consistent within the request (`[IBAN_1]`); the model
reasons about the placeholder; the gateway puts the value back into the answer, also when streamed. Secrets are
always blocked. What each data class may do is set per team in the policy.

**Consequences.** Operations teams can use external models on customer cases. Detection quality is now a
reliability property, measured on a held-out corpus in CI. Names and free-text addresses are not detected yet
(roadmap: a self-hosted model, so the detector itself is not an external call).
