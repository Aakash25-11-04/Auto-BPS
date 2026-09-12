"""Layer 3 — AI / Analytics Layer.

Two model families, neither of which ever makes a scheduling decision
directly — they only estimate PARAMETERS (a failure-risk probability, a
predicted low-traffic window) that feed into the CP-SAT optimizer (Layer 4)
as additional inputs alongside the existing rule-based score and the real
corridor-availability data. See README.md "Layer 3" for the full honesty
statement on what's real vs. synthetic here.
"""
