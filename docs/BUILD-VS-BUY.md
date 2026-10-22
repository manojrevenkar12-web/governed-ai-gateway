# Build versus buy

The question is never "gateway: build or buy" as a whole. Some parts are commodity; some encode how a regulated
company must treat its data. The decision is made per capability, written down, and revisited every two quarters,
because this market moves fast.

## Criteria

| Criterion | Question |
|---|---|
| Data handling | Does customer data leave our network before our own controls have seen it? Where is it processed, under which contract? |
| Control | Can our policies be code we review, test and audit, or only settings in a vendor console? |
| Evidence | Can we prove to an auditor what happened for one request, a year later? |
| Fit | Does it support the providers and clients we use, including self-hosted models and AI coding assistants? |
| Cost | Licence plus the engineering time to integrate and run it, against the engineering time to build and run ours |
| Exit | What does leaving cost? Are our policies, keys and history portable? |
| Pace | Will the vendor ship new provider features faster than we could? |

## Options considered

- **Open-source LLM proxies** (for example LiteLLM's proxy server): broad provider coverage, keys, budgets and
  rate limits out of the box. Strong for commodity routing; we would still add our data protection, our audit trail
  and policy-as-code around it, and own its operations.
- **Managed offerings from cloud and model providers**: little to run, guardrail features maturing fast; policies
  live in the vendor's configuration, and coverage of other providers and self-hosted models varies.
- **API management products with AI plugins**: fit organisations already standardised on them; AI-specific
  controls are often a plugin layer.
- **Build a thin gateway** (this repository): full control of data handling, policy and evidence; we carry the
  provider adapters and the operations.

## Current recommendation

Build the parts that carry our obligations: data protection, policy as code, budgets, the audit trail and the usage
ledger. Do not build what is commodity: if an open-source proxy covers provider adapters well, run it behind our
gateway rather than maintaining adapters ourselves. Buy AI developer tools (coding assistants), and route them
through the gateway. Revisit when a managed option can enforce our policies as code with exportable evidence.
